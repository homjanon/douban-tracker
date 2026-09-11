"""研判层：LLM 四级后端 + 鲁棒提取 + 中性归纳 + 持仓/昵称判断。

后端优先级（智谱 GLM-4.5-Air 主力 + 商汤 DeepSeek-V4-Flash 二级 + Agnes 三级 + NVIDIA GLM-5.2 兜底）：
  glm-4.5-air → deepseek-v4-flash → agnes-2.0-flash → nvidia-glm-5.2
首个有 key 且调用成功即生效；全部失败回退发言摘录。

与 xueqiu-tracker 的差异：
  - 雪球已 refactor 为"纯中性归纳、放弃交易信号"；
  - 本仓需求相反——需补回【持仓入表判定】+【昵称映射】，沿用宽松原则：
      宁可信其有入表；70%+ 把握即可写映射；拿不准交给用户后续指正。
"""
import json
import os
import re
import time

import requests

from config import BACKENDS, TIMEOUT, USER_HINTS, USER_HINTS as _HINTS
from nickname_rules import load_nickname_rules, rules_to_text

# 投资风格画像（楼主历史发言提炼，作研判上下文，避免误判其操作意图）
_PROFILE_FILE = os.getenv("PROFILE_FILE", "investor_profile.json")
# 画像历史归档：每次更新后的完整快照按日期追加（纯归档，不参与 LLM 输入；供回溯/恢复）
_PROFILE_HISTORY_FILE = os.getenv("PROFILE_HISTORY_FILE", "investor_history.json")
_HISTORY_KEEP_DAYS = 90  # 归档保留天数，自动裁剪更早的快照


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
        payload = {"model": backend["model"], "messages": messages, "temperature": 0.3}
        # 后端级 max_tokens / extra（如智谱关思考、商汤 reasoning_effort=low，参考 qiugecaozuo 用法）
        if backend.get("max_tokens"):
            payload["max_tokens"] = backend["max_tokens"]
        payload.update(backend.get("extra", {}))
        r = requests.post(
            f"{backend['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=backend.get("timeout", TIMEOUT),
        )
        r.raise_for_status()
        # 兼容两种响应：标准 content / 商汤系 reasoning_content（2026-08-20）
        msg = r.json()["choices"][0].get("message", {})
        c = msg.get("content") or msg.get("reasoning_content") or ""
        return c or None
    except Exception as e:
        print(f"[analyzer] {backend['name']} 调用失败: {e}")
        return None


def call_multi(messages, budget=90):
    """按 BACKENDS 顺序尝试，返回首个成功内容；全失败返回 None。
    budget=总时限(秒)：2026-08-20 加固，防止后端全挂时逐轮超时叠加拖垮 job。"""
    start = time.monotonic()
    for b in BACKENDS:
        c = _post(b, messages)
        if c:
            print(f"[analyzer] ✅ {b['name']} 调用成功（{b['model']}）")
            return c
        if time.monotonic() - start >= budget:
            print(f"[analyzer] ⚠️ 已达总时限 {budget}s，放弃剩余后端")
            break
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
# ============ 发言 → LLM 输入文本（统一拼装）============
# 图片 Markdown：download_post_images 会把 ![图片](url) 原地改写为本地路径
# （如 data/images/2026-09-11/xxx.jpg），这些路径对 LLM 无任何信息量，纯噪音，需剥离。
_IMG_MD = re.compile(r'!\[图片\]\([^)]*\)')

# 全量兜底上限：正常日发言约 7k–12k 字，此处仅防极端爆炸日撑爆上下文
_POSTS_TEXT_BUDGET = 20000


def _posts_to_text(posts):
    """把发言列表拼成 LLM 输入文本。

    2026-09-11 修改（原为 posts[:40] 硬截断）：
      ① 去掉条数截断——实测 9/11 共 195 条、全量仅 7475 字，主流模型上下文
         完全容得下；原截断只喂 40 条（覆盖率 20%）既丢信息，又会促使 LLM
         因"发言侧信息不足"而向画像借内容，是市场背景污染的重要诱因。
      ② 剥离图片 Markdown（本地路径对 LLM 无意义）。
      ③ 跳过纯表情/极短回复（<4 字无信息量，如「🤣🤣🤣」「给传媒」）。
      ④ 保留 _POSTS_TEXT_BUDGET 作为极端情况兜底。
    """
    lines = []
    total = 0
    for p in posts or []:
        c = _IMG_MD.sub('', (p.get("content") or "")).strip()
        if len(c) < 4:
            continue
        line = f"- {c}"
        total += len(line) + 1
        if total > _POSTS_TEXT_BUDGET:
            lines.append("（…发言过长，此处截断…）")
            break
        lines.append(line)
    return "\n".join(lines)


# ============ 中性归纳（daily_summary）============
def _summarize_user(name, posts):
    if not posts:
        return "暂未发言"
    hint = USER_HINTS.get("default", "")
    text_block = _posts_to_text(posts[:15])
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
    """扫描今日发言，研判持仓变动 + 新昵称映射 + 新昵称规律。

    遵循宽松规则（源自 douban_speaker_bot.py 提示词规则）：
      - 持仓：有买入/加仓/持有对象（明说买了、加仓、有底仓、不舍得卖、多次提及+关注）即可入库；
        纯分析/看戏不入库。拿不准时宁可信其有。
      - 昵称：推断合理（70%+ 把握）即写入；完全无法推断则跳过。
    映射与规律均【仅建议】，不自动写回 state / nickname_rules，由用户人工确认。
    返回 {new_positions: [...], new_nicknames: {nick: target}, new_rules: [...], mentions: {stock: count}}

    2026-09-11：删除 image_context 形参（该参数从未被调用方传入，属死分支；
    LLM 不读图，图片仅供网页展示），并改用 _posts_to_text 全量输入。
    """
    if not posts:
        return {"new_positions": [], "new_nicknames": {}, "new_rules": [], "mentions": {}}
    hint = USER_HINTS.get("default", "")
    rules = rules_to_text()
    profile = load_investor_profile()
    text_blob = _posts_to_text(posts)
    nick_lines = "\n".join(f"  {k} = {v}" for k, v in nickname_map.items()) or "（空）"
    pos_lines = "\n".join(f"  {p.get('name','?')}" for p in positions.get("positions", [])) or "（空）"

    system = ("你是A股/港股/美股/基金实战分析师，擅长从口语化发言中识别真实持仓与昵称映射。"
              "你有实时查价能力，判断比普通人更准。遵循宽松原则："
              "① 持仓——有买入/加仓/持有对象（明说买了、加仓、有底仓、不舍得卖、多次提及且表达关注）即可入库；"
              "纯分析/看戏（如'这股不错''可以关注'）不入库，拿不准宁可信其有。"
              "② 昵称——先按下方【命名规律】推断，再用【已确认映射】校验；两侧冲突以映射为准；"
              "合理（70%+把握）即映射，无法推断跳过。"
              "发现错误用户会后续指正，无需过度自责。"
              + ("\n\n黑话/昵称提示（已确认映射，权威）：\n" + hint if hint else "")
              + ("\n\n" + rules if rules else "")
              + ("\n\n【楼主投资风格画像——仅供理解其表达习惯与偏好】\n"
                 "⚠️ 该画像为历史积累，其中可能含已过期的行情/阶段判断。\n"
                 "严禁将其内容直接引用或复述到 new_positions / new_nicknames / new_rules 的"
                 "依据（evidence）之外；判定持仓与昵称一律只以【今日发言】为事实来源。\n"
                 + profile if profile else "")
              + ("\n\n" + INVALID_HINTS if INVALID_HINTS else ""))
    user = (f"现有昵称映射：\n{nick_lines}\n\n现有持仓：\n{pos_lines}\n\n"
            f"今日发言：\n{text_blob}\n\n"
            f"请输出 JSON：\n"
            f'{{'
            f'"new_positions": [{{"name":"标的名","code":"代码(可空)","action":"买入/加仓/持有/观察","evidence":"原话依据"}}],'
            f'"new_nicknames": {{"昵称":"真实标的或基金经理"}},'
            f'"new_rules": [{{"type":"规律类别","rule":"规律描述","examples":"示例","evidence":"今日发言依据"}}],'
            f'"mentions": {{"标的名": 提及次数}}'
            f'}}\n'
            f"只输出 JSON，不要解释。无新增则对应数组/对象为空。")

    out = call_multi([{"role": "system", "content": system},
                      {"role": "user", "content": user}])
    if not out:
        return {"new_positions": [], "new_nicknames": {}, "new_rules": [], "mentions": {}}
    raw = _extract_text(out)
    try:
        m = re.search(r'\{.*\}', raw, flags=re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        return {"new_positions": [], "new_nicknames": {}, "new_rules": [], "mentions": {}}
    return {
        "new_positions": data.get("new_positions", []) or [],
        "new_nicknames": data.get("new_nicknames", {}) or {},
        "new_rules": data.get("new_rules", []) or [],
        "mentions": data.get("mentions", {}) or {},
    }

# ============ 今日总览（单次 LLM 调用产出 5 子板块 + 画像修订建议）============
def build_daily_overview(posts, nickname_map, positions):
    """从当日发言一次性提取「今日总览」5 子板块 + profile_updates 画像修订建议。

    返回 dict：
      market_background / today_actions / discussion_topics /
      favored_sectors / risk_warnings / profile_updates / overview_warnings
    profile_updates：画像增量修订建议数组 [{dimension,new_text,evidence}]（并入本调用，
    2026-08-20 起 update_investor_profile 不再单独调 LLM，每日调用 4→3 次）。
    overview_warnings：产出于画像的可疑重合预警（见 _detect_profile_leak），
    非空表示 LLM 可能又在复述画像，供看板提示。
    无发言或调用失败则返回各字段空字符串。

    2026-09-11 修复（"市场背景连续 11 天复读同一句"事故）：
      - 画像降级为「仅供理解表达习惯」，并显式禁止引用到各字段（原写法等于把
        历史观点摆在 LLM 面前又不设边界，发言信息不足时必然被照抄）；
      - market_background 增加硬约束：须引 ≥2 条今日原话，不足时如实写"未涉及"；
      - profile_updates 规则修正：禁止把行情/阶段类时效判断写入画像（污染根源）；
      - 输入改为 _posts_to_text 全量（原 posts[:40] 只覆盖 20% 发言）；
      - 删除 image_context 死分支（该参数从未被传入，LLM 不读图）。
    """
    keys = ("market_background", "today_actions",
            "discussion_topics", "favored_sectors", "risk_warnings")
    if not posts:
        d = {k: "" for k in keys}
        d["profile_updates"] = []
        d["overview_warnings"] = []
        return d
    hint = USER_HINTS.get("default", "")
    rules = rules_to_text()
    profile = load_investor_profile()
    text_blob = _posts_to_text(posts)
    nick_lines = "\n".join(f"  {k} = {v}" for k, v in nickname_map.items()) or "（空）"
    pos_lines = "\n".join(f"  {p.get('name','?')}" for p in positions.get("positions", [])) or "（空）"

    system = ("你是财经编辑+实战分析师，依据楼主当日发言，产出结构化的「今日总览」+「画像修订建议」。"
              "务必使用下方昵称映射与规律正确解码黑话。"
              "各字段独立成文、事实导向、不编造；"
              "discussion_topics 必须基于发言原文提炼，不得无中生有。"
              + ("\n\n黑话/昵称提示（已确认映射，权威）：\n" + hint if hint else "")
              + ("\n\n" + rules if rules else "")
              + ("\n\n【楼主投资风格画像——仅供理解其表达习惯与偏好】\n"
                 "⚠️ 该画像为历史积累，其中可能含已过期的行情/阶段判断。"
                 "严禁将其内容直接引用、改写或复述到 market_background / today_actions / "
                 "discussion_topics / favored_sectors / risk_warnings 任何字段；"
                 "上述字段一律只以【今日发言】为唯一事实来源。\n"
                 + profile if profile else "")
              + ("\n\n" + INVALID_HINTS if INVALID_HINTS else ""))
    user = (f"现有持仓：\n{pos_lines}\n\n现有昵称映射：\n{nick_lines}\n\n"
            f"今日发言：\n{text_blob}\n\n"
            f"请输出 JSON（6 个字段）：\n"
            f'{{'
            f'"market_background": "市场背景：**仅依据今日发言**提炼。'
            f'必须至少引用 2 条今日发言原文（每条≤25字，用「」标注）。'
            f'禁止使用任何未在今日发言中出现的信息；禁止复用历史表述与画像内容。'
            f'若今日发言不足以支撑整体市场判断，如实写「今日发言未涉及整体市场判断」，'
            f'不得编造、不得以画像内容填充。",'
            f'"today_actions": "今日操作（Markdown 表格：| 操作 | 标的 | 详情 |，操作列用 ✅/⏭️/❌ 标注）",'
            f'"discussion_topics": "今日议题（Markdown 表格：| 议题 | 态度 | 核心观点 | 关键引用 |；态度用 📈看多/📉看空/➡️中性/💬讨论 标注；核心观点为楼主对该议题的看法一句话；关键引用为1-2条发言原话，每条≤25字）",'
            f'"favored_sectors": "看好板块/方向（无序列表 - **板块**：理由；仅列今日发言中确有依据者，不得沿用历史观点）",'
            f'"risk_warnings": "风险提示（无序列表 - 「原文」——解读）",'
            f'"profile_updates": [{{"dimension":"现有画像维度名","new_text":"【整合历史要点与今日新依据后的完整描述】","evidence":"今日原话/事实依据"}}]'
            f'}}\n'
            f"只输出 JSON，不要解释。前 5 个字段的值必须是**字符串**（即便是列表也请写成 Markdown 文本，不要输出 JSON 数组）；"
            f"无内容字段给空字符串。"
            f"profile_updates 规则："
            f"① new_text 整合历史要点+今日新增（非只写当日）、单维度≤300字，超出时优先保留最近依据；"
            f"② **禁止将行情/阶段/资金流向类时效性判断写入画像**"
            f"（如「市场处于X阶段」「资金转向X」「X板块将轮动」「慢牛」等）——"
            f"这类内容只属于当日报告，写入画像会造成永久污染；"
            f"③ 画像只记录**稳定特质**：投资理念、风格偏好、风险态度、交易习惯、心理特质；"
            f"④ 仅当今日发言确有新依据时才返回该条目；无依据/无变化的维度不要返回；"
            f"完全无更新则 \"profile_updates\": []。")

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    _empty_ov = {k: "" for k in keys}
    _empty_ov["profile_updates"] = []
    _empty_ov["overview_warnings"] = []

    def _parse_overview(out):
        """把 LLM 输出解析为 5 子板块 + profile_updates dict；空响应返回 _empty_ov。"""
        if not out:
            return dict(_empty_ov)
        raw = _extract_text(out)
        try:
            m = re.search(r'\{.*\}', raw, flags=re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
        except Exception:
            data = {}
        res = {k: _to_text(data.get(k, "")) for k in keys}
        res["profile_updates"] = data.get("profile_updates") or []
        return res

    result = _parse_overview(call_multi(messages))
    # 空值校验 + 自动重试：若 5 子板块全空（LLM 偶发返回空 JSON），
    # 再调一次 call_multi（每次从头遍历 BACKENDS，自动尝试二级/兜底后端）。
    if posts and all(not result[k].strip() for k in keys):
        print("[analyzer] ⚠️ 今日总览 5 子板块全空，重试一次（走二级/兜底后端）")
        result = _parse_overview(call_multi(messages))
    # 污染护栏：检测产出是否与画像长片段重合（2026-09-11 新增，防复读回归）
    result["overview_warnings"] = _detect_profile_leak(result, profile)
    return result


# ---- 画像污染护栏（2026-09-11 新增）----
# 背景：市场背景曾连续 11 天复读「拔网线后的情绪恢复阶段」。该短语当日发言命中为 0，
# 系画像残留被 LLM 照抄。此护栏用于在产出阶段及时发现同类回潮。
_LEAK_MIN_LEN = 12   # 连续重合 ≥12 字即视为可疑（短语级复述）
_LEAK_FIELDS = ("market_background", "today_actions",
                "discussion_topics", "favored_sectors", "risk_warnings")


def _lcs_len(a, b):
    """求两个字符串的最长公共子串长度（滚动数组 DP，O(len(a)*len(b))）。

    用「最长公共子串」而非「整句包含」的原因（实测踩坑）：
      画像以第三人称转述——「认为市场处于拔网线后的情绪恢复阶段」，
      而 LLM 产出是直接陈述——「市场处于拔网线后的情绪恢复阶段」，
      两者少了「认为」二字，用 `frag in text` 会漏报。改子串匹配后即可命中。
    """
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _detect_profile_leak(overview, profile_text):
    """检测产出字段中是否与画像存在长片段重合，返回预警字符串列表。

    做法：以句末标点把画像切成整句（保留完整语义单元），逐句与产出字段求
    最长公共子串；命中 ≥_LEAK_MIN_LEN 字即判为疑似复述画像。
    （阈值取 12 字：正常"观点相似"不会连续 12 字逐字一致。）
    """
    warns = []
    if not overview or not profile_text:
        return warns
    frags = []
    for seg in re.split(r'[。；;\n]', profile_text):
        s = seg.strip().lstrip('- ').strip()
        # 去掉「- 维度名：」前缀，只保留内容主体
        s = re.sub(r'^[^：:]{1,20}[：:]', '', s).strip()
        if len(s) >= _LEAK_MIN_LEN:
            frags.append(s)
    if not frags:
        return warns
    for key in _LEAK_FIELDS:
        val = overview.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        best_frag, best_len = "", 0
        for frag in frags:
            n = _lcs_len(frag, val)
            if n > best_len:
                best_len, best_frag = n, frag
        if best_len >= _LEAK_MIN_LEN:
            msg = f"{key} 与画像存在 {best_len} 字连续重合（画像原文：「{best_frag[:40]}」）"
            warns.append(msg)
            print(f"[analyzer] ⚠️ [污染预警] {msg}")
    return warns

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
    """消费 build_daily_overview 输出里的 profile_updates（2026-08-20 起不再单独调 LLM，
    每日 LLM 调用 4→3 次），对 investor_profile.json 做增量更新。

    阀门：只接受『确有今日发言依据』的维度修订（含 evidence）；
          无变化/无依据的维度不返回。返回空表示本次不改动。
    安全阀：接受全部有依据（dimension+new_text+evidence 齐备）的更新；
    仅对现有维度回写，避免 schema 漂移（不新增/不删除维度）。
    成功回写 investor_profile.json（profile 覆盖 + evolution 追加 + last_updated 更新）。
    返回 [(变更描述)] 供审计。
    """
    if not load_investor_profile():
        return []
    updates = (overview or {}).get("profile_updates") or []
    if not updates:
        return []
    # 现有维度集合（锁定 schema：仅回写已有维度，防止 LLM 把维度膨胀回去）
    try:
        with open(_PROFILE_FILE, encoding="utf-8") as _f:
            _prof = json.load(_f)
        _existing = set(_prof.get("profile", {}).keys())
    except Exception:
        _existing = set()
    # 过滤：必须含 dimension(须为现有维度) + new_text + evidence
    valid = [u for u in updates
             if isinstance(u, dict) and u.get("dimension") in _existing
             and u.get("new_text") and u.get("evidence")]

    if not valid:
        return []

    print(f"[画像更新] ✅ 接受 {len(valid)} 条有依据更新（无数量熔断）")

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

    # 归档：把更新后的完整画像快照按日期追加到 investor_history.json（纯归档，不参与 LLM 输入）
    try:
        with open(_PROFILE_HISTORY_FILE, encoding="utf-8") as f:
            hist = json.load(f)
        if not isinstance(hist, dict):
            hist = {}
    except Exception:
        hist = {}
    hist[today] = {
        "profile": {k: _to_text(v) for k, v in prof.get("profile", {}).items()},
        "summary": prof.get("summary", ""),
        "updated_dims": [u["dimension"] for u in valid],
    }
    # 自动裁剪：仅保留最近 _HISTORY_KEEP_DAYS 天
    dates = sorted(hist.keys())
    for old in dates[:-_HISTORY_KEEP_DAYS]:
        hist.pop(old, None)
    try:
        with open(_PROFILE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
        print(f"[画像归档] ✅ {today} 快照已追加到 {_PROFILE_HISTORY_FILE}（现存 {len(hist)} 天）")
    except Exception as e:
        print(f"[画像归档] ⚠️ 写失败: {e}")
    return changed
