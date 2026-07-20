"""主流程：抓 → 去重 → 研判 → 写 latest.json + reports/YYYY-MM-DD.md → 更新 state。

报告结构严格对齐 IMA 每日投资简报（6 大板块骨架）：
  ① 持仓追踪  ② 今日总览  ③ 本次结果  ④ 今日发言聚合  ⑤ 投资风格分析  ⑥ 昵称映射表
持仓/风格/昵称 从 state.json / investor_profile.json / nickname_rules 自动填充；
今日总览/发言聚合 由 LLM 从当日发言提取。LLM 的持仓/昵称产出仅作【待确认建议】，不污染 state。
"""
import datetime
import json
import os
import re

from config import (DATA_DIR, REPORT_DIR, STATE_FILE, RECENT_N,
                    AGGREGATE_THRESHOLD, USER_HINTS)
from scraper import scrape_user
from analyzer import (daily_summary, analyze_positions_and_nicknames,
                      build_daily_overview, load_investor_profile,
                      update_investor_profile)
from nickname_rules import load_nickname_rules, rules_to_text
from query_stock import query_stock

CST = datetime.timezone(datetime.timedelta(hours=8))


# ============ 状态管理（增量游标 + 昵称 + 持仓 + 累计数）============
def _load_json_tolerant(fh):
    """宽容加载 JSON（去尾随逗号，沿用 douban_speaker_bot 实战验证）。"""
    import re as _re
    text = fh.read()
    text = _re.sub(r',\s*}', '}', text)
    text = _re.sub(r',\s*\]', ']', text)
    return json.loads(text)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = _load_json_tolerant(f)
    except Exception:
        st = {}
    st.setdefault("updated_at", "")
    st.setdefault("last_cursor", "")          # 上次最新发言时间游标（去重用）
    st.setdefault("total_archived", 0)        # 累计存档发言数（本次结果展示）
    st.setdefault("nickname_map", {})
    st.setdefault("positions", {"positions": []})
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


# ============ 实时查价（复用 query_stock：腾讯主/天天基金主）============
def enrich_prices(positions):
    """对每条有 code 的持仓查最新价，写入 p['current_price']（含涨跌幅）。
    查不到静默降级为 '暂无'；失败不影响主流程。
    """
    pos_list = positions.get("positions", [])
    if not pos_list:
        return
    # 批量：股票类一次性拼腾讯，基金类逐只走天天基金（query_stock 已按 code 自动路由）
    results = {}
    for p in pos_list:
        code = p.get("code", "")
        if not code:
            continue
        try:
            q = query_stock(code)
        except Exception as e:
            print(f"[查价] {p.get('name','?')}({code}) 异常: {e}")
            q = None
        if q and q.get("price"):
            chg = q.get("change", "")
            txt = q["price"]
            if chg:
                txt += f"（{chg}%）"
            results[p["name"]] = txt
            print(f"[查价] {p.get('name','?')}({code}) → {txt} 源={q.get('source','')}")
        else:
            results[p["name"]] = "暂无"
    for p in pos_list:
        p["current_price"] = results.get(p["name"], "暂无")


# ============ 持仓字段提纯（防 LLM 写长文本，让提及列格式统一）============
def _clean_cost_price(raw):
    """把任意成本描述压成简洁形态：约xx元 / 约xx-x元 / 约x万元；无明确数则『暂无』。"""
    if not raw or "暂无" in str(raw):
        return "暂无"
    s = str(raw)
    # 约xx万元
    m = re.search(r'约\s*(\d+(?:\.\d+)?)\s*万元', s)
    if m:
        return f"约{m.group(1)}万元"
    # 约xx元 或 xx-x元（可能带『可能/偏高』等前缀，直接抓数字段）
    m = re.search(r'约?\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*元', s)
    if m:
        return f"约{m.group(1)}-{m.group(2)}元"
    m = re.search(r'约?\s*(\d+(?:\.\d+)?)\s*元', s)
    if m:
        return f"约{m.group(1)}元"
    return "暂无"


def _clean_last_note(raw, limit=25):
    """当日动态截断/清空：超长或含分析腔（观点/分享/提及作为）直接清空，保持提及列简洁。"""
    if not raw:
        return ""
    s = str(raw).strip()
    # 分析腔（LLM 观点描述，非操作动态）→ 清空
    if re.search(r'(观点|分享|提及作为|属\b|逻辑|潜在标的|未明确|仅观点|仅供参考)', s):
        return ""
    # 超长截断
    if len(s) > limit:
        return s[:limit] + "…"
    return s


# ============ 持仓追踪动态更新（复用今日操作板块 + 严格阀门）============
def parse_today_actions(overview):
    """解析 overview.today_actions 表格，返回 [{action, target, detail}]。
    操作列 emoji：✅=买入/加仓/建仓；❌=卖出/清仓；⏭️=持有/观察无动作。
    无 emoji 或无法识别的行直接丢弃（阀门：只认显式买卖信号）。
    """
    raw = (overview or {}).get("today_actions", "") or ""
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line or line.startswith("| 操作"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        op, target, detail = cells[0], cells[1], cells[2]
        if "✅" in op:
            act = "买入"
        elif "❌" in op:
            act = "卖出"
        elif "⏭️" in op or "➖" in op:
            act = "持有"
        else:
            continue  # 阀门：无明确 emoji 的信号一律排除
        rows.append({"action": act, "target": target, "detail": detail})
    return rows


def _match_position(positions, target, nickname_map):
    """按标的名/昵称匹配持仓条目，返回索引或 None。"""
    pos_list = positions.get("positions", [])
    # 正向：标的名包含 target 或 target 包含标的名
    for i, p in enumerate(pos_list):
        nm = p.get("name", "")
        if not nm:
            continue
        if nm == target or nm in target or target in nm:
            return i
    # 昵称反查：target 可能是昵称（如 小浦），映射到真实标的
    real = nickname_map.get(target)
    if real:
        for i, p in enumerate(pos_list):
            if p.get("name", "") == real:
                return i
    return None


def _is_valid_stock_target(target, nickname_map):
    """阀门加强：新增持仓的标的必须是『已知标的/昵称』或『符合代码格式』，否则拒绝。
    避免 LLM 把策略/板块/模糊词（如『观察策略』『科技基金』）当持仓写入。
    """
    t = (target or "").strip()
    if not t:
        return False
    # 已是已知昵称或持仓名
    if t in nickname_map or t in nickname_map.values():
        return True
    # 股票代码格式：6 位纯数字（A股/港股） / 含 ETF / 含 QDII / 含 (LOF) / 含 基金
    if (len(t) == 6 and t.isdigit()) or "ETF" in t or "QDII" in t or "LOF" in t or "基金" in t:
        return True
    # 已知持仓名子串（如『浦银安盛全球智能科技』匹配『浦银安盛全球智能科技(QDII)A』）
    if any(t in v or v in t for v in nickname_map.values()):
        return True
    return False


# ============ 成本价明确提及自动填充（严格阀门：仅填明确提及）============
_COST_PATTERNS = [
    # 成本/建仓价/本/买入价 + 数字 + 元（或万元）
    re.compile(r'(?:成本|建仓[价价]|买入价|我的本|本钱|我的成本|持仓成本)[约在是]?\s*[:：]?\s*'
               r'(\d+(?:\.\d+)?)\s*(万元|万|元)?', re.IGNORECASE),
    re.compile(r'(?:成本|本|建仓)\s*(\d+(?:\.\d+)?)\s*(万元|万|元)?', re.IGNORECASE),
    re.compile(r'(\d+(?:\.\d+)?)\s*(万元|万|元)\s*(?:的成本|的本|建仓|买入)'),
]


def parse_cost_mentions(overview):
    """从今日操作/持仓动态表格的『详情/关键动态』列抽取「明确成本价表述」。
    返回 [(target_text, cost_str)]，仅围栏明确提及价格的行（阀门：无数字价格不要）。
    """
    found = []
    raw = (overview or {})
    for key in ("today_actions", "position_dynamics"):
        blob = raw.get(key, "") or ""
        if not blob:
            continue
        for line in blob.splitlines():
            line = line.strip()
            if not line.startswith("|") or "---" in line or line.startswith("| 操作") \
               or line.startswith("| 标的"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 2:
                continue
            # 操作表：列=[操作, 标的, 详情]；持仓动态表：列=[标的, 今日表现, 关键动态]
            target = cells[1] if key == "today_actions" else cells[0]
            detail = cells[-1]  # 详情 / 关键动态 列
            for pat in _COST_PATTERNS:
                m = pat.search(detail)
                if m:
                    num = m.group(1)
                    unit = m.group(2) or "元"
                    found.append((target, f"约{num}{unit}"))
                    break
    return found


def _apply_cost_mentions(st, overview, today):
    """把明确提及的成本价写入对应持仓 cost_price（仅当命中已有持仓标的）。
    返回 [(变更描述)] 供审计。不编造、未命中不填。
    """
    nickname_map = st.get("nickname_map", {})
    changes = []
    for target, cost in parse_cost_mentions(overview):
        idx = _match_position(st["positions"], target, nickname_map)
        if idx is None:
            continue  # 阀门：未命中任何已知持仓/昵称，拒绝填写（避免误填）
        p = st["positions"]["positions"][idx]
        # 仅当原 cost_price 为空/"暂无" 或 本次更明确时覆盖；已有人工/历史值保留
        old = p.get("cost_price", "") or ""
        if old and "暂无" not in old:
            changes.append(f"⏭️ 成本价已存在跳过：{p['name']}（{old}）")
            continue
        p["cost_price"] = f"约{cost[1:]}"  # cost 形如 '约xx元'，统一存 '约xx元'
        if cost.endswith("万元"):
            p["cost_price"] = cost
        changes.append(f"💰 填充成本价：{p['name']} → {p['cost_price']}（依据：{target}）")
    return changes, bool(changes)


def apply_position_updates(st, overview, today):
    """依据今日操作表更新 state.positions。返回 [(变更描述)] 供审计。
    阀门：
      ✅ 买入/加仓 → 无则新增、有则更新 action/last_note
      ⏭️ 持有   → 仅更新 last_note，不增删
      ❌ 卖出   → 第一天标 action=卖出(保留痕迹)；第二天 detect 仍为卖出且无回购 → 移出
    阈值熔断：单次新增 > 5 条 → 不回写，返回 (changes, blocked=True)
    """
    rows = parse_today_actions(overview)
    positions = st.setdefault("positions", {"positions": []})
    pos_list = positions["positions"]
    nickname_map = st.get("nickname_map", {})
    changes = []
    new_count = 0

    for r in rows:
        idx = _match_position(positions, r["target"], nickname_map)
        if r["action"] == "买入":
            if idx is None:
                if not _is_valid_stock_target(r["target"], nickname_map):
                    changes.append(f"🚫 拒增（非标的/策略词）：{r['target']}")
                    continue
                pos_list.append({
                    "name": r["target"], "code": "", "action": "买入",
                    "category": "", "cost_price": "暂无", "market_value": "暂无",
                    "note": r["detail"], "last_note": r["detail"],
                    # 系统自动写入 MM-DD 格式日期（用户仅人工确认名称/代码）
                    "first_seen": today[5:] if today else "",
                })
                new_count += 1
                changes.append(f"➕ 新增持仓：{r['target']}（依据：{r['detail'][:40]}）")
            else:
                pos_list[idx]["action"] = "买入"
                pos_list[idx]["last_note"] = r["detail"]
                changes.append(f"🔄 更新买入：{pos_list[idx]['name']}")
        elif r["action"] == "卖出":
            if idx is not None:
                if pos_list[idx].get("action") == "卖出":
                    # 第二天：正式移出
                    removed = pos_list.pop(idx)
                    changes.append(f"➖ 移出持仓（次日确认卖出）：{removed['name']}")
                else:
                    pos_list[idx]["action"] = "卖出"
                    pos_list[idx]["last_note"] = r["detail"]
                    changes.append(f"⚠️ 标记卖出（待次日移出）：{pos_list[idx]['name']}")
            # 若 state 无此标的，卖出信号无对应持仓，忽略
        elif r["action"] == "持有":
            if idx is not None:
                pos_list[idx]["last_note"] = r["detail"]
                # 若此前标记卖出但今日又现持有信号，恢复为持有
                if pos_list[idx].get("action") == "卖出":
                    pos_list[idx]["action"] = "持有"
                    changes.append(f"♻️ 恢复持有（卖出信号撤销）：{pos_list[idx]['name']}")

    # 成本价明确提及自动填充（仅绑定已命中持仓标的、仅当原值缺失）
    cost_changes, _ = _apply_cost_mentions(st, overview, today)
    for c in cost_changes:
        changes.append(c)

    # 阈值熔断
    if new_count > 5:
        print(f"[持仓更新] ⚠️ 单次新增 {new_count} 条 > 5，触发熔断，回滚本次持仓变更")
        return changes, True
    return changes, False


# ============ 发言聚合（>阈值按标的聚类）============
def aggregate_posts(posts, nickname_map, positions):
    """将大量发言按标的/话题自动归类聚合（沿用 douban_speaker_bot 思路）。"""
    keywords = {}
    for nick, target in nickname_map.items():
        keywords[nick] = target
    for p in positions.get("positions", []):
        nm = p.get("name", "")
        if nm:
            keywords[nm] = nm
    # 最长优先匹配，避免部分命中
    kws = sorted(keywords.keys(), key=len, reverse=True)
    groups = {}
    total = len(posts) or 1
    for post in posts:
        text = post.get("content", "")
        hit = None
        for kw in kws:
            if kw in text:
                hit = keywords[kw]
                break
        label = hit or "其他讨论"
        g = groups.setdefault(label, {"count": 0, "times": [], "samples": []})
        g["count"] += 1
        g["times"].append(post.get("time", ""))
        if len(g["samples"]) < 3:
            g["samples"].append(post.get("content", "")[:120])
    ranked = sorted(groups.items(), key=lambda x: x[1]["count"], reverse=True)
    # 附占比
    out = []
    for label, g in ranked:
        pct = round(g["count"] / total * 100)
        out.append((label, g, pct))
    return out


# ============ 报告渲染（IMA 同构 6 板块）============
def _is_stale(first_seen):
    """提及日期距今天数是否超过 5 天。first_seen 支持 MM-DD 或 YYYY-MM-DD。
    无法解析（或格式异常）时返回 False（不强制清空）。"""
    if not first_seen:
        return False
    s = str(first_seen).strip()
    m = re.match(r'(?:\d{4}-)?(\d{1,2})-(\d{1,2})', s)
    if not m:
        return False
    try:
        mm, dd = int(m.group(1)), int(m.group(2))
        today = datetime.datetime.now()
        # 以当前年补足年份，转成年内序数比较（忽略原始年份、假定同年）
        def _ord(mm_, dd_):
            return (datetime.date(today.year, mm_, dd_).toordinal()
                    if 1 <= mm_ <= 12 and 1 <= dd_ <= 31 else None)
        a = _ord(today.month, today.day)
        b = _ord(mm, dd)
        if a is None or b is None:
            return False
        return (a - b) > 5
    except Exception:
        return False


def _fmt_mention_date(d):
    """提及日期 MM-DD 或 YYYY-MM-DD → M.D（如 07-03 → 7.3，07-19 → 7.19）。
    超 5 天自动清空返回空串。"""
    if not d:
        return ""
    d = str(d).strip()
    # 兼容 YYYY-MM-DD 与 MM-DD
    m = re.match(r'(?:\d{4}-)?(\d{1,2})-(\d{1,2})', d)
    if m:
        if _is_stale(d):
            return ""
        return f"{int(m.group(1))}.{int(m.group(2))}"
    return d


def _cost_hint(cost_price):
    """成本价已事先由 _clean_cost_price 提纯为『约xx元/约xx-x元/约x万元』，
    此处无需再正则抽取，非『暂无』则直接返回。无有效成本返回空。"""
    if not cost_price or "暂无" in str(cost_price):
        return ""
    return str(cost_price).strip()


def _positions_table(positions):
    """持仓表 5 列：标的/状态/类型/现价/提及。
    现价=实时查价(current_price)；提及=日期(M.D)+当日动态+约xx成本价。
    """
    L = ["| 标的 | 状态 | 类型 | 现价 | 提及 |",
         "|------|------|------|------|------|"]
    for p in positions.get("positions", []):
        price = p.get("current_price") or "暂无"
        date = _fmt_mention_date(p.get("first_seen", ""))
        last = p.get("last_note") or ""
        cost = _cost_hint(p.get("cost_price"))
        # 提及栏：日期 + 当日动态 + 成本价提示
        parts = []
        if date:
            parts.append(date)
        if last:
            parts.append(last)
        if cost:
            parts.append(f"成本{cost}")
        mention = "；".join(parts) if parts else "暂无"
        L.append(f"| {p.get('name','')} | {p.get('action','')} | {p.get('category','')} | "
                 f"{price} | {mention[:60]} |")
    return "\n".join(L)


def build_report(ts, name, summary, posts, analysis, overview, today_count, total_archived):
    """渲染 IMA 同构 6 板块日报 Markdown。"""
    L = [f"# 📋 楼主每日发言推送",
         "",
         f"> **推送日期**：{ts[:10]} ｜ **执行时间**：{ts} ｜ **监控用户**：`{name}`",
         f"> **所在小组**：{os.getenv('DOUBAN_GROUP_URLS', '豆瓣小组').split(',')[0].split('/')[-2] if os.getenv('DOUBAN_GROUP_URLS') else '豆瓣小组'}",
         ""]

    # ① 持仓追踪
    L.append("## 📊 持仓追踪")
    L.append(f"> 上次更新：{ts[:10]} ｜ 基于楼主发言持续追踪（共 {len(analysis) and ''}{_pos_count()} 项）")
    L.append("")
    L.append(_positions_table(load_state_positions()))
    L.append("")
    L.append("> ⚠️ 执行任务时如发现新操作，请更新 `state.json` 的 `positions`。成本价和市值有数据才填，没有则保留「暂无」。")
    L.append("")

    # ② 今日总览（6 子板块）
    L.append("## 🌅 今日总览")
    if overview.get("market_background"):
        L.append("### 📌 市场背景")
        L.append(overview["market_background"])
        L.append("")
    if overview.get("core_views"):
        L.append("### 📌 楼主核心观点")
        L.append(overview["core_views"])
        L.append("")
    if overview.get("today_actions"):
        L.append("### 📌 今日操作")
        L.append(overview["today_actions"])
        L.append("")
    if overview.get("position_dynamics"):
        L.append("### 📌 持仓动态")
        L.append(overview["position_dynamics"])
        L.append("")
    if overview.get("favored_sectors"):
        L.append("### 📌 看好板块/方向")
        L.append(overview["favored_sectors"])
        L.append("")
    if overview.get("risk_warnings"):
        L.append("### 📌 风险提示")
        L.append(overview["risk_warnings"])
        L.append("")

    # ③ 本次结果
    L.append("## 📊 本次结果")
    L.append(f"- **今日发言**：{today_count} 条")
    L.append(f"- **累计存档**：{total_archived} 条")
    L.append("")

    # ④ 今日发言聚合
    L.append(f"## 📝 今日发言聚合（共 {today_count} 条）")
    if today_count > AGGREGATE_THRESHOLD:
        for label, g, pct in aggregate_posts(posts, {}, {"positions": []}):
            tr = (g["times"][0] if g["times"] else "")
            L.append(f"### {label}（提及 {g['count']} 次，占比 {pct}%）{tr}")
            for s in g["samples"]:
                L.append(f"- {s}")
            L.append("")
    else:
        for p in posts[:60]:
            pic = " [图]" if "![图片]" in p.get("content", "") else ""
            q = f"  ⊳ {p['quote']}" if p.get("quote") else ""
            L.append(f"- ({p.get('time','')}){pic} {p.get('content','')[:300]}{q}")
        L.append("")

    # ⑤ 投资风格分析（读 investor_profile.json）
    prof = load_investor_profile()
    if prof:
        L.append("## 🧠 投资风格分析")
        L.append("> 📏 增量更新指引：仅当今日发言确有新依据时才修订对应维度，无变化不强行重写；单维度修订建议 ≤150 字。")
        L.append("")
        L.append(prof)
        L.append("")
        L.append("> ⚠️ 本板块由 Actions 全自动增量更新（复用当日发言与今日总览），每次仅在内容足够支撑时才改动。")
        L.append("")

    # ⑥ 昵称映射表（规律 + 映射）
    L.append("## 🏷️ 昵称映射表")
    rules = rules_to_text()
    if rules:
        L.append("### 📖 昵称规律（供 AI 判断新昵称时参考）")
        L.append(rules)
        L.append("")
    L.append("### 📋 已收录映射")
    L.append("| 昵称 | 对应名称 |")
    L.append("|------|----------|")
    for k, v in load_state_nicknames().items():
        L.append(f"| {k} | {v} |")
    L.append("")

    # 待确认建议区（LLM 产出，不自动写 state）
    L.append("> ⚠️ 以下为 LLM **建议**，未自动写入状态文件。请人工确认后，"
             "本地编辑 `state.json` 的 `nickname_map` / `positions` 并 push 生效。")
    L.append("")
    if analysis.get("new_positions"):
        L.append("## 持仓建议（待你确认）")
        for p in analysis["new_positions"]:
            L.append(f"- **{p.get('name','?')}**（{p.get('code','无代码')}）"
                     f"〔{p.get('action','?')}〕— 依据：{p.get('evidence','')}")
        L.append("")
    if analysis.get("new_nicknames"):
        L.append("## 新昵称映射建议（待你确认）")
        for k, v in analysis["new_nicknames"].items():
            L.append(f"- `{k}` = {v}")
        L.append("")

    L.append(f"\n*🤖 自动生成于 {ts} ｜ 豆瓣楼主发言追踪*")
    return "\n".join(L)


# 缓存在 main 中注入，避免重复 load_state
_STATE_CACHE = {}


def load_state_positions():
    return _STATE_CACHE.get("positions", {"positions": []})


def load_state_nicknames():
    return _STATE_CACHE.get("nickname_map", {})


def _pos_count():
    return len(_STATE_CACHE.get("positions", {}).get("positions", []))


# ============ 主流程 =========
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

    st = load_state()
    _STATE_CACHE["positions"] = st["positions"]
    _STATE_CACHE["nickname_map"] = st["nickname_map"]
    print(f"[状态] 上次游标: {st['last_cursor'][:16] or '无'} ｜ 昵称 {len(st['nickname_map'])} ｜ 持仓 {len(st['positions']['positions'])}")

    # 1. 抓取
    posts = scrape_user()
    print(f"[抓取] 总 {len(posts)} 条（已按当日 date 过滤，方案A：每日0点起当日全量）")

    # 2. 当日全量展示：scrape_user 已按 date==today 过滤，直接采用全量
    #    （不再用 last_cursor 二次裁剪，避免丢弃 0 点至上次运行之间的当日发言）
    display = posts
    showing_fallback = (len(display) == 0) and bool(posts)

    # 3. 研判（纯文本，无图片识别）
    name = os.getenv("DOUBAN_TARGET_USER", "楼主")
    summary = daily_summary({"name": name, "posts": display})
    print(f"[归纳] {summary}")
    analysis = analyze_positions_and_nicknames(display, st["nickname_map"], st["positions"])
    overview = build_daily_overview(display, st["nickname_map"], st["positions"])
    # 查价：对研判出的标的补实时价格
    for p in analysis["new_positions"]:
        if p.get("code"):
            q = query_stock(p["code"])
            if q:
                p["price"] = q.get("price", "")
                p["change"] = q.get("change", "")
                p["price_source"] = q.get("source", "")

    # 4. 持仓追踪动态更新（复用今日操作板块 + 阀门 + 卖出两阶段）
    today = now.strftime("%Y-%m-%d") if False else datetime.datetime.now(CST).strftime("%Y-%m-%d")
    pos_changes, pos_blocked = apply_position_updates(st, overview, today)
    if pos_blocked:
        print(f"[持仓更新] 🚫 熔断触发，本次持仓变更未回写（{pos_changes}）")
    else:
        for c in pos_changes:
            print(f"[持仓更新] {c}")

    # 5. 投资风格画像全自动增量更新（复用今日总览内容，有依据才改）
    prof_changes = update_investor_profile(overview, display, today)
    for c in prof_changes:
        print(f"[画像更新] {c}")

    # 6. 累计存档数更新（基于发言 id 跨运行去重，只累加本次新见到的，避免同日重复计）
    seen_ids = set(st.get("_seen_ids", []))
    fresh = [p for p in display if p.get("id") and p["id"] not in seen_ids]
    st["total_archived"] = st.get("total_archived", 0) + len(fresh)
    # 保留最近 500 个 id 用于去重，防止无限膨胀
    st["_seen_ids"] = list((seen_ids | {p["id"] for p in display if p.get("id")}))[-500:]

    # 6.5 实时查价注入持仓（腾讯/天天基金，复用 query_stock）
    enrich_prices(st["positions"])

    # 6.6 持仓字段提纯（防 LLM 写长文本，统一提及列格式：成本→约xx；动态→截断/清空）
    for p in st["positions"]["positions"]:
        p["cost_price"] = _clean_cost_price(p.get("cost_price", ""))
        p["last_note"] = _clean_last_note(p.get("last_note", ""))

    # 7. 写 latest.json（6 板块结构；持仓/画像已自动增量更新并透明记录）
    now = datetime.datetime.now(CST)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    latest = {
        "fetched_at": ts,
        "user": {"name": name, "count": len(display)},
        "today_count": len(display),
        "total_archived": st["total_archived"],
        "showing_fallback": showing_fallback,
        "daily_summary": summary,
        "overview": overview,                       # 今日总览 6 子板块
        "positions": st["positions"],              # 持仓追踪（已动态更新）
        "investor_profile": load_investor_profile(),  # 投资风格分析（已增量更新）
        "nickname_rules": load_nickname_rules(),      # 昵称规律（结构化列表）
        "nickname_map": st["nickname_map"],           # 已收录映射
        "pending_positions": analysis["new_positions"],    # 待确认（LLM 研判辅助参考）
        "pending_nicknames": analysis["new_nicknames"],    # 待确认
        "applied_position_changes": [] if pos_blocked else pos_changes,  # 本次自动持仓变更（审计）
        "applied_profile_update": prof_changes,      # 本次自动画像变更（审计）
        "mentions": analysis["mentions"],
        "aggregated": aggregate_posts(display, st["nickname_map"], st["positions"]) if len(display) > AGGREGATE_THRESHOLD else None,
        "posts": display,
    }
    with open(f"{DATA_DIR}/latest.json", "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)

    # 8. 写 reports（6 板块）
    md = build_report(ts, name, summary, display, analysis, overview, len(display), st["total_archived"])
    with open(f"{REPORT_DIR}/{now.strftime('%Y-%m-%d')}.md", "w", encoding="utf-8") as f:
        f.write(md)

    # 9. 更新游标 + 累计数 + 持仓/画像（已在上游自动回写）
    if display:
        st["last_cursor"] = max((p.get("date", "") + p.get("sortable_time", "") for p in display))
    st["updated_at"] = ts
    save_state(st)

    print(f"[完成] data/latest.json(6板块) + reports/{now.strftime('%Y-%m-%d')}.md 已生成；"
          f"昵称→{len(st['nickname_map'])} 持仓→{len(st['positions']['positions'])}"
          f"｜ 持仓变更 {len(pos_changes)} 条（{'熔断' if pos_blocked else '已回写'}）"
          f"｜ 画像变更 {len(prof_changes)} 项")


if __name__ == "__main__":
    main()
