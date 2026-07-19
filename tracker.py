"""主流程：抓 → 去重 → 研判 → 写 latest.json（双结构）+ reports/YYYY-MM-DD.md → 更新 state。

借鉴 xueqiu-tracker 的：
  - load_state/save_state 增量游标
  - latest.json 双结构（顶层合并 + 单用户明细 + daily_summary）
  - 无新增时 recent_posts 兜底展示
补回（雪球已删、本仓需求）：
  - 持仓入表 / 昵称映射研判 → 回写 state（workflow commit 持久化）
  - 发言聚合（>阈值按标的聚类，沿用 douban_speaker_bot.aggregate_posts 思路）
"""
import datetime
import json
import os

from config import (DATA_DIR, REPORT_DIR, STATE_FILE, RECENT_N,
                    AGGREGATE_THRESHOLD, USER_HINTS)
from scraper import scrape_user
from analyzer import daily_summary, analyze_positions_and_nicknames
from query_stock import query_stock

CST = datetime.timezone(datetime.timedelta(hours=8))


# ============ 状态管理（增量游标 + 昵称 + 持仓）============
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
    return sorted(groups.items(), key=lambda x: x[1]["count"], reverse=True)


# ============ 报告渲染 =========
def build_report(ts, summary, posts, analysis, today_count):
    L = [f"# 豆瓣楼主发言追踪 · {ts}", "",
         f"- 跟踪楼主：`{os.getenv('DOUBAN_TARGET_USER', '楼主')}` ｜ 当日新增发言：**{today_count}** 条", ""]
    L.append("## 今日讨论归纳")
    L.append(f"> {summary}")
    L.append("")

    # 持仓/昵称研判结果
    if analysis.get("new_positions"):
        L.append("## 持仓研判（LLM 宽松判定，待你确认）")
        for p in analysis["new_positions"]:
            L.append(f"- **{p.get('name','?')}**（{p.get('code','无代码')}）"
                     f"〔{p.get('action','?')}〕— 依据：{p.get('evidence','')}")
        L.append("")
    if analysis.get("new_nicknames"):
        L.append("## 新昵称映射（待你确认）")
        for k, v in analysis["new_nicknames"].items():
            L.append(f"- `{k}` = {v}")
        L.append("")

    # 聚合 or 逐条
    if today_count > AGGREGATE_THRESHOLD:
        L.append(f"## 发言聚合（共 {today_count} 条，按标的聚类）")
        for label, g in aggregate_posts(posts, {}, {"positions": []}):
            tr = (g["times"][0] if g["times"] else "")
            L.append(f"### {label} （{g['count']} 次提及）{tr}")
            for s in g["samples"]:
                L.append(f"- {s}")
            L.append("")
    else:
        L.append("## 原始发言")
        for p in posts[:60]:
            pic = " [图]" if "![图片]" in p.get("content", "") else ""
            q = f"  ⊳ {p['quote']}" if p.get("quote") else ""
            L.append(f"- ({p.get('time','')}){pic} {p.get('content','')[:300]}{q}")
        L.append("")
    return "\n".join(L)


# ============ 主流程 =========
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

    st = load_state()
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
    # 查价：对研判出的标的补实时价格
    for p in analysis["new_positions"]:
        if p.get("code"):
            q = query_stock(p["code"])
            if q:
                p["price"] = q.get("price", "")
                p["change"] = q.get("change", "")
                p["price_source"] = q.get("source", "")

    # 4. 写 latest.json（双结构，对齐 xueqiu-tracker）
    now = datetime.datetime.now(CST)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    latest = {
        "fetched_at": ts,
        "daily_summary": summary,
        "today_count": len(new),
        "showing_fallback": showing_fallback,
        "new_positions": analysis["new_positions"],
        "new_nicknames": analysis["new_nicknames"],
        "mentions": analysis["mentions"],
        "posts": display,
        "user": {"name": name, "count": len(display)},
    }
    with open(f"{DATA_DIR}/latest.json", "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)

    # 5. 写 reports
    md = build_report(ts, summary, display, analysis, len(new))
    with open(f"{REPORT_DIR}/{now.strftime('%Y-%m-%d')}.md", "w", encoding="utf-8") as f:
        f.write(md)

    # 6. 回写昵称/持仓到 state（workflow 负责 commit 持久化）
    if analysis["new_nicknames"]:
        st["nickname_map"].update(analysis["new_nicknames"])
    if analysis["new_positions"]:
        existing = {p.get("name") for p in st["positions"]["positions"]}
        for np in analysis["new_positions"]:
            if np.get("name") and np["name"] not in existing:
                st["positions"]["positions"].append({
                    "name": np.get("name"),
                    "code": np.get("code", ""),
                    "action": np.get("action", ""),
                    "first_seen": ts,
                })
                existing.add(np["name"])
    # 更新游标为最新发言时间
    if display:
        st["last_cursor"] = max((p.get("date", "") + p.get("sortable_time", "") for p in display))
    st["updated_at"] = ts
    save_state(st)

    print(f"[完成] data/latest.json + reports/{now.strftime('%Y-%m-%d')}.md 已生成；"
          f"昵称→{len(st['nickname_map'])} 持仓→{len(st['positions']['positions'])}")


if __name__ == "__main__":
    main()
