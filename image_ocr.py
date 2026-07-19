"""图片识别：每次运行从当日发言取前 3 张真实图片，下载后调 Agnes 多模态识别。

设计要点：
  - 仅识别前 3 张（控制耗时与 token）。
  - 多模态主力 = Agnes (agnes-2.0-flash)，复用 config.BACKENDS 中第一个带 key 的。
  - 兜底：任何环节失败（下载失败 / 无 key / 调用异常 / 非图片），本模块一律返回
    [] 并打日志，绝不影响主流程研判与输出。
  - 返回结构：[{url, desc}]，desc 为空表示识别失败（已尽力，跳过）。
"""
import base64
import os
import re

import requests

from config import BACKENDS, TIMEOUT

_MAX_IMAGES = int(os.getenv("OCR_MAX_IMAGES", "3"))
_OCR_TIMEOUT = int(os.getenv("OCR_TIMEOUT", "60"))


def _extract_image_urls(posts, max_n=_MAX_IMAGES):
    """从发言 content 的 ![图片](url) 中抽取前 max_n 个真实图片 URL。
    过滤掉 data: 占位、icon/avatar 等噪声。
    """
    urls = []
    seen = set()
    for p in posts:
        content = p.get("content", "") or ""
        for m in re.finditer(r'!\[图片\]\(([^)]+)\)', content):
            u = m.group(1).strip()
            if not u or u in seen:
                continue
            if u.startswith("data:"):
                continue
            if "icon" in u or "avatar" in u:
                continue
            seen.add(u)
            urls.append(u)
            if len(urls) >= max_n:
                return urls
    return urls


def _download_b64(url):
    """下载图片并返回 base64（带 mime）。失败返回 None。"""
    try:
        r = requests.get(url, timeout=_OCR_TIMEOUT, headers={
            "User-Agent": "Mozilla/5.0 (compatible; douban-tracker/1.0)"
        })
        if r.status_code != 200 or not r.content:
            print(f"[识图] 下载失败 {url[:60]} → HTTP {r.status_code}")
            return None
        mime = r.headers.get("Content-Type", "").split(";")[0].strip() or "image/jpeg"
        if not mime.startswith("image/"):
            print(f"[识图] 非图片类型跳过 {url[:60]} → {mime}")
            return None
        b64 = base64.b64encode(r.content).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        print(f"[识图] 下载异常 {url[:60]} → {e}")
        return None


def _call_vision(backend, b64_list, text_blob):
    """调用一个多模态后端识别图片。成功返回文字，失败返回 None。"""
    key = backend.get("api_key")
    if not key:
        return None
    try:
        content = [{"type": "text", "text":
            "下面是一组投资相关图片（可能是K线图、持仓截图、交易记录或含文字的图表）。"
            "请客观描述图片中的关键信息：标的名称、价格/涨跌幅、买卖信号、持仓数量、"
            "文字内容等。若无法识别或无关，请说明。用中文，分条列出，不要编造。"}]
        for b in b64_list:
            content.append({"type": "image_url", "image_url": {"url": b}})
        if text_blob:
            content.append({"type": "text",
                "text": f"补充上下文（楼主当日部分文字发言）：\n{text_blob[:500]}"})
        r = requests.post(
            f"{backend['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": backend["model"],
                  "messages": [{"role": "user", "content": content}],
                  "temperature": 0.2},
            timeout=backend.get("timeout", _OCR_TIMEOUT),
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[识图] {backend['name']} 调用失败: {e}")
        return None


def recognize_images(posts, text_blob="", max_n=_MAX_IMAGES):
    """主入口：识别前 max_n 张图片，返回 [{url, desc}]。

    任何失败都兜底跳过，保证返回（空列表或不含该图），不抛异常、不影响主流程。
    """
    results = []
    urls = _extract_image_urls(posts, max_n)
    if not urls:
        print("[识图] 当日发言无图片，跳过")
        return results
    print(f"[识图] 检出 {len(urls)} 张图片，尝试识别（前 {min(max_n, len(urls))} 张）")

    b64_list = []
    for u in urls:
        b = _download_b64(u)
        if b:
            b64_list.append(b)
        else:
            results.append({"url": u, "desc": ""})  # 下载失败占位，desc 空
    if not b64_list:
        print("[识图] 全部图片下载失败，跳过识别")
        return results

    # 按 BACKENDS 顺序，找到第一个有 key 的多模态后端
    desc = None
    for b in BACKENDS:
        if not b.get("api_key"):
            continue
        desc = _call_vision(b, b64_list, text_blob)
        if desc:
            print(f"[识图] ✅ {b['name']} 识别成功（{len(desc)} 字）")
            break
    if not desc:
        print("[识图] ⚠️ 所有后端均无 key / 均失败，兜底跳过，正常输出")
        results = [{"url": u, "desc": ""} for u in urls]
        return results

    # 把识别文字按图片数大致均分；若只有一段描述，全部图共享同一描述
    if len(urls) == 1:
        results = [{"url": urls[0], "desc": desc}]
    else:
        # 按"图N"或换行切片都不可靠，简单按句子均分
        import re as _re
        sents = _re.split(r'(?<=[。！？\n])', desc)
        sents = [s.strip() for s in sents if s.strip()]
        per = max(1, len(sents) // len(urls))
        for i, u in enumerate(urls):
            seg = sents[i * per: (i + 1) * per] or [desc]
            results.append({"url": u, "desc": " ".join(seg)})
    # 仅保留成功下载的那些 url 对齐（b64_list 顺序与 urls 一致，因失败已占位）
    return results


if __name__ == "__main__":
    import json
    # 自测：无图时应返回 []
    print(json.dumps(recognize_images([{"content": "今天没图"}], ""), ensure_ascii=False, indent=2))
