"""研判层：LLM 四级后端 + 鲁棒提取 + 中性归纳 + 持仓/昵称判断。

后端优先级（agnes 主力 + 雪球三级备用）：
  agnes-2.0-flash → nvidia-qwen3.5-122b → nvidia-kimi-k2.5 → siliconflow-qwen3.5-35b
首个有 key 且调用成功即生效；全部失败回退发言摘录。

与 xueqiu-tracker 的差异：
  - 雪球已 refactor 为"纯中性归纳、放弃交易信号"；
  - 本仓需求相反——需补回【持仓入表判定】+【昵称映射】，沿用宽松原则：
      宁可信其有入表；70%+ 把握即可写映射；拿不准交给用户后续指正。
"""
import json
import re

import requests

from config import BACKENDS, TIMEOUT, USER_HINTS, USER_HINTS as _HINTS


# ============ 通用 LLM 调用 ============
def _post(backend, messages):
    key = backend.get("api_key")
    if not key:
        return None
    try:
        r = requests.post(
            f"{backend['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": backend["model"], "messages": messages, "temperature": 0.3},
            timeout=backend.get("timeout", TIMEOUT),
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[analyzer] {backend['name']} 调用失败: {e}")
        return None


def call_multi(messages):
    """按 BACKENDS 顺序尝试，返回首个成功内容；全失败返回 None。"""
    for b in BACKENDS:
        c = _post(b, messages)
        if c:
            print(f"[analyzer] ✅ {b['name']} 调用成功（{b['model']}）")
            return c
    print("[analyzer] ⚠️ 所有后端均未成功，回退摘录")
    return None


def _clean_think(s):
    return re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL | re.IGNORECASE)


def _strip_fence(s):
    return re.sub(r"^```(?:json|markdown)?|```$", "", s.strip(), flags=re.M)


def _extract_text(content):
    """从模型输出提取纯文本（兼容 {"summary":...} / 裸文本 / 围栏 / <think>）。"""
    if not content:
        return None
    s = _clean_think(content)
    try:
        d = json.loads(_strip_fence(s))
        if isinstance(d, dict):
            for k in ("summary", "result", "answer", "text"):
                if isinstance(d.get(k), str) and d[k].strip():
                    return d[k].strip()
    except Exception:
        pass
    t = _strip_fence(s).strip().strip("\"'。 ").strip()
    return t or None


# ============ 中性归纳（daily_summary）============
def _summarize_user(name, posts):
    if not posts:
        return "暂未发言"
    hint = USER_HINTS.get("default", "")
    text_block = "\n".join(f"- {p.get('content', '')}" for p in posts[:15])
    system = ("你是财经编辑。若用户用了黑话/昵称（见下方提示），请据此正确理解其讨论内容；"
              "归纳中只做事实描述，不判断买卖操作。"
              + ("\n\n黑话/昵称提示：\n" + hint if hint else ""))
    user = (f"以下是豆瓣用户「{name}」近期发言原文：\n\n{text_block}\n\n"
            f"请用不超过 50 字的一两句话，中性归纳他讨论了什么（关注的市场/标的/观点/情绪等）。"
            f"只做事实性归纳，禁止出现「买入/卖出/持有/加仓/减仓」等结论性标签；不编造；"
            f"严格≤50字，无标题无列表无解释。")
    out = call_multi([{"role": "system", "content": system},
                      {"role": "user", "content": user}])
    sent = _extract_text(out) if out else None
    if not sent:
        sent = (posts[0].get("content", "")[:50]) or "暂未发言"
    return sent[:50]


def daily_summary(user_info):
    """每用户各一句（≤50字）中性归纳；无人发言则该人「暂未发言」。"""
    name = user_info.get("name") or "楼主"
    posts = user_info.get("posts") or user_info.get("recent_posts") or []
    return f"{name}：{_summarize_user(name, posts)}"


# ============ 持仓 / 昵称研判（补回雪球已删的逻辑）============
def analyze_positions_and_nicknames(posts, nickname_map, positions):
    """扫描今日发言，研判持仓变动 + 新昵称映射。

    遵循宽松原则（源自 douban_speaker_bot.py 提示词规则）：
      - 持仓：有买入/持有迹象（明说买了、加仓、有底仓、不舍得卖、多次提及+关注）即可入表；
        纯分析/看戏不入表。拿不准时宁可信其有。
      - 昵称：推断合理（70%+ 把握）即写入；完全无法推断则跳过。
    返回 {new_positions: [...], new_nicknames: {nick: target}, mentions: {stock: count}}
    """
    if not posts:
        return {"new_positions": [], "new_nicknames": {}, "mentions": {}}
    hint = USER_HINTS.get("default", "")
    text_blob = "\n".join(f"- {p.get('content', '')}" for p in posts[:40])
    nick_lines = "\n".join(f"  {k} = {v}" for k, v in nickname_map.items()) or "（空）"
    pos_lines = "\n".join(f"  {p.get('name','?')}" for p in positions.get("positions", [])) or "（空）"

    system = ("你是A股/港股/美股/基金实战分析师，擅长从口语化发言中识别真实持仓与昵称映射。"
              "你有实时查价能力，判断比死规则更准。遵循宽松原则："
              "① 持仓——有买入/持有迹象（明说买了、加仓、有底仓、不舍得卖、多次提及且表达关注）即可入表；"
              "纯分析/看戏（如『这股不错』『可以关注』）不入表；拿不准宁可信其有。"
              "② 昵称——推断合理（70%+把握）即映射；无法推断跳过。"
              "发现错误用户会后续指正，无需过度谨慎。"
              + ("\n\n黑话/昵称提示：\n" + hint if hint else ""))
    user = (f"现有昵称映射：\n{nick_lines}\n\n现有持仓：\n{pos_lines}\n\n"
            f"今日发言：\n{text_blob}\n\n"
            f"请输出 JSON：\n"
            f"{{"
            f'"new_positions": [{{"name":"标的名","code":"代码(可空)","action":"买入/加仓/持有/观察","evidence":"原话依据"}}],'
            f'"new_nicknames": {{"昵称":"真实标的或基金经理"}},'
            f'"mentions": {{"标的名": 提及次数}}'
            f"}}\n"
            f"只输出 JSON，不要解释。无新增则对应数组/对象为空。")

    out = call_multi([{"role": "system", "content": system},
                      {"role": "user", "content": user}])
    if not out:
        return {"new_positions": [], "new_nicknames": {}, "mentions": {}}
    raw = _extract_text(out)
    try:
        # 宽容：可能包了 ```json 围栏
        m = re.search(r'\{.*\}', raw, flags=re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        return {"new_positions": [], "new_nicknames": {}, "mentions": {}}
    return {
        "new_positions": data.get("new_positions", []) or [],
        "new_nicknames": data.get("new_nicknames", {}) or {},
        "mentions": data.get("mentions", {}) or {},
    }
