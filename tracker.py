"""主流程：抓 → 去重 → 研判 → 写 latest.json + reports/YYYY-MM-DD.md → 更新 state。

报告结构严格对齐 IMA 每日投资简报（6 大板块骨架）：
  ① 持仓追踪  ② 今日总览  ③ 本次结果  ④ 今日发言聚合  ⑤ 投资风格分析  ⑥ 昵称映射表
持仓/风格/昵称 从 state.json / investor_profile.json / nickname_rules 自动填充；
今日总览/发言聚合 由 LLM 从当日发言提取。LLM 的持仓/昵称产出仅作【待确认建议】，不污染 state。
"""
import datetime
import json
import os

from config import (DATA_DIR, REPORT_DIR, STATE_FILE, RECENT_N,
                    AGGREGATE_THRESHOLD, USER_HINTS)
from scraper import scrape_user
from analyzer import (daily_summary, analyze_positions_and_nicknames,
                      build_daily_overview, load_investor_profile,
                      update_investor_profile)
from nickname_rules import load_nickname_rules
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
                    "note": r["detail"], "last_note": r["detail"], "first_seen": today,
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
def _positions_table(positions):
    """精简持仓表：备注改为『当日动态』，仅保留当天内容；不重复昵称（昵称见映射表板块）。"""
    L = ["| 标的 | 状态 | 类型 | 成本价 | 市值 | 当日动态 | 提及日期 |",
         "|------|------|------|--------|------|----------|----------|"]
    for p in positions.get("positions", []):
        mv = p.get("market_value") or "暂无"
        last = p.get("last_note") or p.get("note", "") or "暂无"
        L.append(f"| {p.get('name','')} | {p.get('action','')} | {p.get('category','')} | "
                 f"{p.get('cost_price','暂无')} | {mv} | "
                 f"{last[:50]} | {p.get('first_seen','')} |")
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
    rules = load_nickname_rules()
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
    print(f"[抓取] 总 {len(posts)} 条")

    # 2. 增量：取游标之后（时间更新）的发言
    cursor = st["last_cursor"]
    new = [p for p in posts if (p.get("date", "") + p.get("sortable_time", "")) > cursor] if cursor else posts[:RECENT_N]
    print(f"[增量] 新增 {len(new)} 条（游标 {cursor[:16] or '无'}）")

    # 无新增 → 用最近 RECENT_N 条兜底展示
    showing_fallback = (len(new) == 0) and bool(posts)
    display = new if new else posts[:RECENT_N]

    # 3. 研判
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

    # 6. 累计存档数更新
    st["total_archived"] = st.get("total_archived", 0) + len(new)

    # 7. 写 latest.json（6 板块结构；持仓/画像已自动增量更新并透明记录）
    now = datetime.datetime.now(CST)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    latest = {
        "fetched_at": ts,
        "user": {"name": name, "count": len(display)},
        "today_count": len(new),
        "total_archived": st["total_archived"],
        "showing_fallback": showing_fallback,
        "daily_summary": summary,
        "overview": overview,                       # 今日总览 6 子板块
        "positions": st["positions"],              # 持仓追踪（已动态更新）
        "investor_profile": load_investor_profile(),  # 投资风格分析（已增量更新）
        "nickname_rules": load_nickname_rules(),      # 昵称规律
        "nickname_map": st["nickname_map"],           # 已收录映射
        "pending_positions": analysis["new_positions"],    # 待确认（LLM 研判辅助参考）
        "pending_nicknames": analysis["new_nicknames"],    # 待确认
        "applied_position_changes": [] if pos_blocked else pos_changes,  # 本次自动持仓变更（审计）
        "applied_profile_update": prof_changes,      # 本次自动画像变更（审计）
        "mentions": analysis["mentions"],
        "aggregated": aggregate_posts(display, st["nickname_map"], st["positions"]) if len(new) > AGGREGATE_THRESHOLD else None,
        "posts": display,
    }
    with open(f"{DATA_DIR}/latest.json", "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)

    # 8. 写 reports（6 板块）
    md = build_report(ts, name, summary, display, analysis, overview, len(new), st["total_archived"])
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
