#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IMA 知识库同步：把 douban-tracker 的 reports/*.md 上传到 IMA「投资」知识库「豆瓣」文件夹。

用法:
    python3 ima_sync.py [--date YYYY-MM-DD] [--file /path/to/report.md]
    # 不带参数时自动取 reports/ 下最新的 .md 文件

环境变量（或从 ~/.config/ima/ 读取）:
    IMA_CLIENT_ID    IMA Client ID
    IMA_API_KEY      IMA API Key

流程: preflight → check_repeated_names → create_media → COS上传 → add_knowledge
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

BASE_URL = "https://ima.qq.com"
# 「投资」知识库 + 「豆瓣」文件夹（已核实）
KB_ID = "E9D0u0kqRjG9KG_a71xNFqcutcdXudDT7tN0Hf2VTlU="
FOLDER_ID = "folder_7478432929696520"

# media_type 枚举: 7 = Markdown, 99 = 文件夹
MEDIA_TYPE_MD = 7
CONTENT_TYPE_MD = "text/markdown"


def load_credentials():
    client_id = os.environ.get("IMA_CLIENT_ID") or os.environ.get("IMA_OPENAPI_CLIENTID")
    api_key = os.environ.get("IMA_API_KEY") or os.environ.get("IMA_OPENAPI_APIKEY")
    if not client_id:
        try:
            client_id = Path.home().joinpath(".config/ima/client_id").read_text().strip()
        except Exception:
            pass
    if not api_key:
        try:
            api_key = Path.home().joinpath(".config/ima/api_key").read_text().strip()
        except Exception:
            pass
    if not client_id or not api_key:
        sys.stderr.write("错误: 缺少 IMA 凭证。请设置 IMA_CLIENT_ID / IMA_API_KEY 或 ~/.config/ima/ 下配置文件。\n")
        sys.exit(1)
    return client_id, api_key


def api_call(client_id, api_key, path, body):
    """调用 IMA OpenAPI。返回解析后的 JSON dict。"""
    url = f"{BASE_URL}/{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("ima-openapi-clientid", client_id)
    req.add_header("ima-openapi-apikey", api_key)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        sys.stderr.write(f"HTTP {e.code} 调用 {path} 失败: {body_text}\n")
        sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"调用 {path} 失败: {e}\n")
        sys.exit(1)


def cos_sign(secret_id, secret_key, method, path, headers, start_time, expired_time):
    """COS 请求签名（与官方 cos-upload.cjs 完全一致）：
    1) signKey = hex(HMAC-SHA1(SecretKey_bytes, KeyTime))
    2) signature = hex(HMAC-SHA1(signKey_hex_string, StringToSign))
    """
    import urllib.parse

    def hmac_sha1_hex(key, msg: str) -> str:
        # key 可以是 str 或 bytes：str 时按 utf-8 编码；bytes 直接用
        if isinstance(key, str):
            key = key.encode()
        return hmac.new(key, msg.encode(), hashlib.sha1).hexdigest()

    def sha1_hex(data: str) -> str:
        return hashlib.sha1(data.encode()).hexdigest()

    key_time = f"{start_time};{expired_time}"
    sign_key = hmac_sha1_hex(secret_key, key_time)  # hex 字符串
    header_keys = sorted(headers.keys())
    http_headers = "&".join(
        f"{k.lower()}={urllib.parse.quote(str(headers[k]), safe='')}" for k in header_keys
    )
    http_string = f"{method.lower()}\n{path}\n\n{http_headers}\n"
    string_to_sign = f"sha1\n{key_time}\n{sha1_hex(http_string)}\n"
    signature = hmac_sha1_hex(sign_key, string_to_sign)  # key 是 hex 字符串
    header_list = ";".join(k.lower() for k in header_keys)
    return (f"q-sign-algorithm=sha1&q-ak={secret_id}&q-sign-time={key_time}"
            f"&q-key-time={key_time}&q-header-list={header_list}"
            f"&q-url-param-list=&q-signature={signature}")


def cos_upload(file_path, cred):
    """上传文件到 COS。cred 来自 create_media 返回的 cos_credential。"""
    import urllib.parse
    with open(file_path, "rb") as f:
        content = f.read()
    host = f"{cred['bucket_name']}.cos.{cred['region']}.myqcloud.com"
    cos_key = cred["cos_key"]
    path = "/" + cos_key
    start_time = str(cred["start_time"])
    expired_time = str(cred["expired_time"])
    # 与官方一致：仅签名 content-length 和 host
    sign_headers = {
        "content-length": str(len(content)),
        "host": host,
    }
    auth = cos_sign(
        cred["secret_id"], cred["secret_key"],
        "PUT", path, sign_headers, start_time, expired_time,
    )
    url = f"https://{host}{path}"
    req = urllib.request.Request(url, data=content, method="PUT")
    req.add_header("Content-Type", CONTENT_TYPE_MD)
    req.add_header("Content-Length", str(len(content)))
    req.add_header("Authorization", auth)
    req.add_header("x-cos-security-token", cred["token"])
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            if 200 <= resp.status < 300:
                print(f"COS 上传成功 (HTTP {resp.status})")
                return True
            sys.stderr.write(f"COS 上传失败 (HTTP {resp.status})\n")
            return False
    except Exception as e:
        sys.stderr.write(f"COS 上传失败: {e}\n")
        return False


def main():
    parser = argparse.ArgumentParser(description="同步 douban-tracker 报告到 IMA 知识库")
    parser.add_argument("--date", help="报告日期 YYYY-MM-DD，默认取最新")
    parser.add_argument("--file", help="指定报告文件路径")
    args = parser.parse_args()

    client_id, api_key = load_credentials()

    # 确定要上传的文件
    if args.file:
        file_path = Path(args.file)
    else:
        reports_dir = Path(__file__).resolve().parent.parent / "reports"
        if not reports_dir.is_dir():
            sys.stderr.write(f"找不到 reports 目录: {reports_dir}\n")
            sys.exit(1)
        if args.date:
            file_path = reports_dir / f"{args.date}.md"
        else:
            md_files = sorted(reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not md_files:
                sys.stderr.write("reports 目录没有 .md 文件\n")
                sys.exit(1)
            file_path = md_files[0]
    if not file_path.exists():
        sys.stderr.write(f"文件不存在: {file_path}\n")
        sys.exit(1)

    file_name = file_path.name
    file_size = file_path.stat().st_size
    file_ext = file_path.suffix.lstrip(".").lower()
    print(f"上传: {file_name} ({file_size} bytes)")

    # ① 重名检查
    resp = api_call(client_id, api_key, "openapi/wiki/v1/check_repeated_names", {
        "params": [{"name": file_name, "media_type": MEDIA_TYPE_MD}],
        "knowledge_base_id": KB_ID,
    })
    if resp.get("code") != 0:
        sys.stderr.write(f"重名检查失败: {resp.get('msg')}\n")
        sys.exit(1)
    repeated = resp.get("data", {}).get("is_repeated", False)
    if repeated:
        print(f"⚠️ {file_name} 已在知识库中，跳过（如需覆盖请先删除旧文件）")
        sys.exit(0)

    # ② create_media — 获取 media_id 和 COS 凭证
    resp = api_call(client_id, api_key, "openapi/wiki/v1/create_media", {
        "file_name": file_name,
        "file_size": file_size,
        "content_type": CONTENT_TYPE_MD,
        "knowledge_base_id": KB_ID,
        "file_ext": file_ext,
    })
    if resp.get("code") != 0:
        sys.stderr.write(f"create_media 失败: {resp.get('msg')}\n")
        sys.exit(1)
    data = resp.get("data", {})
    media_id = data.get("media_id")
    cos_cred = data.get("cos_credential") or {}
    if not media_id or not cos_cred:
        sys.stderr.write(f"create_media 返回缺少 media_id 或 cos_credential: {json.dumps(data, ensure_ascii=False)}\n")
        sys.exit(1)
    print(f"create_media OK: media_id={media_id}")

    # ③ 上传文件到 COS
    if not cos_upload(file_path, cos_cred):
        sys.exit(1)

    # ④ add_knowledge — 关联到知识库文件夹
    resp = api_call(client_id, api_key, "openapi/wiki/v1/add_knowledge", {
        "media_type": MEDIA_TYPE_MD,
        "media_id": media_id,
        "title": file_name,
        "knowledge_base_id": KB_ID,
        "folder_id": FOLDER_ID,
        "file_info": {
            "cos_key": cos_cred.get("cos_key", ""),
            "file_size": file_size,
            "file_name": file_name,
        },
    })
    if resp.get("code") != 0:
        sys.stderr.write(f"add_knowledge 失败: {resp.get('msg')}\n")
        sys.exit(1)
    print(f"✅ {file_name} 已上传到 IMA「投资/豆瓣」知识库")


if __name__ == "__main__":
    main()
