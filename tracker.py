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
                      build_daily_overview, load_investor_profile)
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
    L = ["| 标的 | 状态 | 类型 | 成本价 | 市值 | 备注 | 提及日期 |",
         "|------|------|------|--------|------|------|----------|"]
    for p in positions.get("positions", []):
        mv = p.get("market_value") or "暂无"
        L.append(f"| {p.get('name','')} | {p.get('action','')} | {p.get('category','')} | "
                 f"{p.get('cost_price','暂无')} | {mv} | "
                 f"{p.get('note','')[:60]} | {p.get('first_seen','')} |")
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
        L.append("> 📏 字数软约束（供 AI 参考）：各维度 90-110 字；综合评估 200-250 字。")
        L.append("")
        L.append(prof)
        L.append("")
        L.append("> ⚠️ 执行任务时请根据今日新发言补充分析，与历史结论归纳提炼后更新 `investor_profile.json`。")
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

    # 4. 累计存档数更新
    st["total_archived"] = st.get("total_archived", 0) + len(new)

    # 5. 写 latest.json（6 板块结构；LLM 持仓/昵称产出仅作【待确认建议】）
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
        "positions": st["positions"],              # 持仓追踪（读 state）
        "investor_profile": load_investor_profile(),  # 投资风格分析
        "nickname_rules": load_nickname_rules(),      # 昵称规律
        "nickname_map": st["nickname_map"],           # 已收录映射
        "pending_positions": analysis["new_positions"],    # 待确认
        "pending_nicknames": analysis["new_nicknames"],    # 待确认
        "mentions": analysis["mentions"],
        "aggregated": aggregate_posts(display, st["nickname_map"], st["positions"]) if len(new) > AGGREGATE_THRESHOLD else None,
        "posts": display,
    }
    with open(f"{DATA_DIR}/latest.json", "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)

    # 6. 写 reports（6 板块）
    md = build_report(ts, name, summary, display, analysis, overview, len(new), st["total_archived"])
    with open(f"{REPORT_DIR}/{now.strftime('%Y-%m-%d')}.md", "w", encoding="utf-8") as f:
        f.write(md)

    # 7. 仅更新游标 + 累计数（nickname_map/positions 仍只由人工确认后本地改 state.json 生效）
    if display:
        st["last_cursor"] = max((p.get("date", "") + p.get("sortable_time", "") for p in display))
    st["updated_at"] = ts
    save_state(st)

    print(f"[完成] data/latest.json(6板块) + reports/{now.strftime('%Y-%m-%d')}.md 已生成；"
          f"昵称→{len(st['nickname_map'])} 持仓→{len(st['positions']['positions'])}（仅确认项）")


if __name__ == "__main__":
    main()
