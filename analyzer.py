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
import os
import re

import requests

from config import BACKENDS, TIMEOUT, USER_HINTS, USER_HINTS as _HINTS
from nickname_rules import load_nickname_rules, rules_to_text

# 投资风格画像（楼主历史发言提炼，作研判上下文，避免误判其操作意图）
_PROFILE_FILE = os.getenv("PROFILE_FILE", "investor_profile.json")


def load_investor_profile():
    """加载投资风格画像；缺失或损坏时返回空字符串（不影响主流程）。"""
    try:
        with open(_PROFILE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        prof = d.get("profile", {})
        if not prof:
            return ""
        parts = []
        for k, v in prof.items():
            parts.append(f"- {k}：{v}")
        return "\n".join(parts)
    except Exception:
        return ""


# ============ 已确认错误项黑名单（历史模板识别错误，禁止再出现）============
INVALID_HINTS = (
    "以下为历史上被错误识别的伪标的，请**不要再**将它们写入持仓/今日操作/持仓动态/昵称映射："
    "①「国际复材」——仅为昵称线索，并非楼主持仓，不要再映射或写入；"
    "②「鼎泰高科」——历史上识别错误，不属于楼主持仓，不要再出现。"
)


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
def analyze_positions_and_nicknames(posts, nickname_map, positions, image_context=""):
    """扫描今日发言，研判持仓变动 + 新昵称映射。

    遵循宽松原则（源自 douban_speaker_bot.py 提示词规则）：
      - 持仓：有买入/持有迹象（明说买了、加仓、有底仓、不舍得卖、多次提及+关注）即可入表；
        纯分析/看戏不入表。拿不准时宁可信其有。
      - 昵称：推断合理（70%+ 把握）即写入；完全无法推断则跳过。
    image_context：图片识别文字（可选），拼接进研判上下文。
    返回 {new_positions: [...], new_nicknames: {nick: target}, mentions: {stock: count}}
    """
    if not posts:
        return {"new_positions": [], "new_nicknames": {}, "mentions": {}}
    hint = USER_HINTS.get("default", "")
    rules = rules_to_text()
    profile = load_investor_profile()
    text_blob = "\n".join(f"- {p.get('content', '')}" for p in posts[:40])
    if image_context:
        text_blob += "\n\n【图片识别内容（来自楼主当日图片）】\n" + image_context
    nick_lines = "\n".join(f"  {k} = {v}" for k, v in nickname_map.items()) or "（空）"
    pos_lines = "\n".join(f"  {p.get('name','?')}" for p in positions.get("positions", [])) or "（空）"

    system = ("你是A股/港股/美股/基金实战分析师，擅长从口语化发言中识别真实持仓与昵称映射。"
              "你有实时查价能力，判断比死规则更准。遵循宽松原则："
              "① 持仓——有买入/持有迹象（明说买了、加仓、有底仓、不舍得卖、多次提及且表达关注）即可入表；"
              "纯分析/看戏（如『这股不错』『可以关注』）不入表；拿不准宁可信其有。"
              "② 昵称——先按下方【命名规律】推断，再用【已确认映射】校验；两侧冲突以映射为准；"
              "合理（70%+把握）即映射，无法推断跳过。"
              "发现错误用户会后续指正，无需过度谨慎。"
              + ("\n\n黑话/昵称提示（已确认映射，权威）：\n" + hint if hint else "")
              + ("\n\n" + rules if rules else "")
              + ("\n\n楼主投资风格画像（判断其操作意图时务必参考，避免误判）：\n" + profile if profile else "")
              + ("\n\n" + INVALID_HINTS if INVALID_HINTS else ""))
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


# ============ 今日总览（单次 LLM 调用产出 6 子板块）============
def build_daily_overview(posts, nickname_map, positions, image_context=""):
    """从当日发言一次性提取「今日总览」6 子板块，避免多次调用浪费 token。

    返回 dict：
      market_background / core_views / today_actions / position_dynamics /
      favored_sectors / risk_warnings
    image_context：图片识别文字（可选），拼接进研判上下文。
    无发言或调用失败则返回各字段空字符串。
    """
    if not posts:
        return {k: "" for k in ("market_background", "core_views", "today_actions",
                                "position_dynamics", "favored_sectors", "risk_warnings")}
    hint = USER_HINTS.get("default", "")
    rules = rules_to_text()
    profile = load_investor_profile()
    text_blob = "\n".join(f"- {p.get('content', '')}" for p in posts[:40])
    if image_context:
        text_blob += "\n\n【图片识别内容（来自楼主当日图片）】\n" + image_context
    nick_lines = "\n".join(f"  {k} = {v}" for k, v in nickname_map.items()) or "（空）"
    pos_lines = "\n".join(f"  {p.get('name','?')}" for p in positions.get("positions", [])) or "（空）"

    system = ("你是财经编辑+实战分析师，依据楼主当日发言，产出结构化的「今日总览」。"
              "务必使用下方昵称映射与规律正确解码黑话；结合楼主投资风格画像理解其操作意图。"
              "各字段独立成文、事实导向、不编造；行情数字无依据则留空。"
              + ("\n\n黑话/昵称提示（已确认映射，权威）：\n" + hint if hint else "")
              + ("\n\n" + rules if rules else "")
              + ("\n\n楼主投资风格画像：\n" + profile if profile else "")
              + ("\n\n" + INVALID_HINTS if INVALID_HINTS else ""))
    user = (f"现有持仓：\n{pos_lines}\n\n现有昵称映射：\n{nick_lines}\n\n"
            f"今日发言：\n{text_blob}\n\n"
            f"请输出 JSON（6 个字段，均为字符串，可含 Markdown 列表/表格）：\n"
            f'{{'
            f'"market_background": "市场背景（宏观/指数/情绪一段概述）",'
            f'"core_views": "楼主核心观点（无序列表 - **关键词**——阐述）",'
            f'"today_actions": "今日操作（Markdown 表格：| 操作 | 标的 | 详情 |，操作列用 ✅/⏭️/❌ 标注）",'
            f'"position_dynamics": "持仓动态（Markdown 表格：| 标的 | 今日表现 | 关键动态 |，表现含涨跌%）",'
            f'"favored_sectors": "看好板块/方向（无序列表 - **板块**：理由）",'
            f'"risk_warnings": "风险提示（无序列表 - 「原文」——解读）"'
            f'}}\n'
            f"只输出 JSON，不要解释。每个字段的值必须是**字符串**（即便是列表也请写成 Markdown 文本，不要输出 JSON 数组）；"
            f"无内容字段给空字符串。")

    out = call_multi([{"role": "system", "content": system},
                      {"role": "user", "content": user}])
    if not out:
        return {k: "" for k in ("market_background", "core_views", "today_actions",
                                "position_dynamics", "favored_sectors", "risk_warnings")}
    raw = _extract_text(out)
    try:
        m = re.search(r'\{.*\}', raw, flags=re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        data = {}
    keys = ("market_background", "core_views", "today_actions",
            "position_dynamics", "favored_sectors", "risk_warnings")
    return {k: _to_text(data.get(k, "")) for k in keys}


def _to_text(v):
    """把 LLM 返回的任意类型安全转成文本（兼容 list/dict/数字/None）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)):
        # 列表元素逐行拼接；元素本身可能也是 dict/list，递归处理
        return "\n".join(_to_text(x) for x in v if x not in (None, "")).strip()
    if isinstance(v, dict):
        return "\n".join(f"{k}：{_to_text(val)}" for k, val in v.items()).strip()
    return str(v).strip()


# ============ 投资风格画像全自动增量更新 ============
def update_investor_profile(overview, posts, today):
    """复用今日总览内容，对 investor_profile.json 做增量更新。

    阀门：LLM 只返回『确有今日发言依据』的维度修订（含 evidence）；
          无变化/无依据的维度不返回。返回空表示本次不改动。
    安全阀：单次修订维度 > 5 个 → 不回写、返回告知熔断。
    成功回写 investor_profile.json（profile 覆盖 + evolution 追加 + last_updated 更新）。
    返回 [(变更描述)] 供审计。
    """
    prof_text = load_investor_profile()
    if not prof_text:
        return []
    ov = overview or {}
    overview_blob = "\n".join(f"- {k}：{_to_text(v)}" for k, v in ov.items() if v)
    if not overview_blob.strip():
        return []

    system = ("你是投资心理分析师。依据楼主【今日总览】，对已有投资风格画像做**增量修订**。"
              "严格规则：① 仅当今日发言确能支撑某维度更新时才返回该维度；"
              "② 每条修订必须附 evidence（今日原话/事实依据）；无依据绝不改动；"
              "③ 无变化的维度不要返回；不要重写整个画像、不要凑字数；单维度 ≤150 字。")
    user = (f"现有画像：\n{prof_text}\n\n"
            f"今日总览（增量依据）：\n{overview_blob}\n\n"
            f"请输出 JSON：{{ \"updates\": [{{ \"dimension\": \"维度名\", "
            f"\"new_text\": \"修订后表述\", \"evidence\": \"今日依据\" }}] }}\n"
            f"无更新则 \"updates\": []。只输出 JSON。")

    out = call_multi([{"role": "system", "content": system},
                      {"role": "user", "content": user}])
    if not out:
        return []
    raw = _extract_text(out)
    try:
        m = re.search(r'\{.*\}', raw, flags=re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        return []
    updates = data.get("updates", []) or []
    # 过滤：必须含 new_text 且 evidence
    valid = [u for u in updates
             if isinstance(u, dict) and u.get("dimension") and u.get("new_text") and u.get("evidence")]

    if len(valid) > 5:
        print(f"[画像更新] ⚠️ 单次修订 {len(valid)} 维度 > 5，触发熔断，不回写")
        return [f"🚫 熔断：拟改 {len(valid)} 维度 > 5，未回写"]

    if not valid:
        return []

    # 回写 investor_profile.json
    try:
        with open(_PROFILE_FILE, encoding="utf-8") as f:
            prof = json.load(f)
    except Exception:
        return []
    prof_dim = prof.setdefault("profile", {})
    evo_list = prof.get("evolution", "")
    changed = []
    for u in valid:
        dim = u["dimension"]
        ev = u.get("evidence", "")
        prof_dim[dim] = _to_text(u["new_text"])
        changed.append(f"🔄 {dim}（依据：{ev[:30]}）")
    # evolution 追加
    new_evo = f"{today}：更新 {len(valid)} 个维度（{', '.join(u['dimension'] for u in valid)}）"
    prof["evolution"] = f"{evo_list}\n{new_evo}" if evo_list else new_evo
    prof["last_updated"] = today
    try:
        with open(_PROFILE_FILE, "w", encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[画像更新] ⚠️ 写回失败: {e}")
        return []
    return changed
