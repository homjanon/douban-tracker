"""行情查询：A/港/美股 + 公募基金（多源容错链）。

数据源（复用 portfolio/cmb-tracker 验证过的 proven 模式）：
  A/港/美股/ETF : 腾讯 qt.gtimg.cn（主）→ 新浪 hq.sinajs.cn（备）→ akshare 现货（兜底）
  基金(场外/净值型/QDII) : 天天基金 JSONP 直连 fundgz.1234567.com.cn（主）
            → 东方财富 lsjz 历史净值 api.fund.eastmoney.com（备，含 QDII 备用）

设计要点：
  - 全部失败返回 None，由调用方决定回退（LLM 用 WebSearch 或标注"无数据"）。
  - 腾讯 qt.gtimg.cn 的 parts[3]=当前价，parts[32]=涨跌幅；港股加 hk 前缀。
  - 美股走腾讯 us 前缀，无需额外密钥。
  - ETF（15/5xxxxx 前缀）按股票走腾讯实时行情；场外基金 00 开头与深 A 股冲突，用 _KNOWN_FUNDS 显式集合优先判定。
"""
import re
import requests

SESSION = requests.Session()
SESSION.trust_env = False
SESSION.verify = False   # 对齐本地 douban_speaker_bot 写法，规避沙盒/个别环境 CA 缺失
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
TENCENT_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
SINA_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}

# 已知基金代码集合（00开头因与深A股冲突，需单独判断）
KNOWN_FUNDS = {
    '001532', '166001', '016372', '005698', '002891', '000906',
    '006555', '100055', '008253', '008254', '539002', '160213',
    '450010', '012920', '007540', '018984', '006227', '006328',
    '005656', '006452',   # 原靠 00 前缀兜底，前缀收窄后显式列入
}

# 场内 ETF 前缀（走腾讯，同 A 股实时行情，勿走基金净值接口）
ETF_PREFIXES = ('15', '50', '51', '52', '55', '56', '58')
# 场外/净值型公募基金前缀（含 QDII：27；LOF：16/18）
FUND_PREFIXES = ('01', '02', '11', '16', '18', '27')
STOCK_PREFIXES = ('60', '68', '30', '20', '90')


def _classify(code_str):
    """返回 (qtype, code_clean) —— a_stock / hk / us / fund"""
    if re.search(r'[A-Z]', code_str) and not code_str.startswith(('SH', 'SZ', 'HK')):
        return 'us', code_str
    if code_str.startswith(('SH', 'SZ')):
        return 'a_stock', code_str[2:]
    if code_str.startswith('HK'):
        return 'hk', code_str[2:]
    if code_str.isdigit():
        if len(code_str) == 6:
            # ① 场内 ETF 优先走腾讯（实时行情，勿走基金净值接口）
            if code_str.startswith(ETF_PREFIXES):
                return 'a_stock', code_str
            # ② 已知场外/净值型基金（权威清单，含 QDII/LOF）
            if code_str in KNOWN_FUNDS:
                return 'fund', code_str
            # ③ 000/001/002 开头多为深市 A 股（场外基金已在前置清单捕获）
            if code_str.startswith('00') and code_str[2:3] in ('0', '1', '2'):
                return 'a_stock', code_str
            # ④ 其余场外基金前缀（01/02/11/16/18/27）
            if code_str.startswith(FUND_PREFIXES):
                return 'fund', code_str
            return 'a_stock', code_str
        if len(code_str) <= 5:
            return 'hk', code_str
        return 'a_stock', code_str
    return 'a_stock', code_str


def _parse_tencent(text):
    """解析腾讯 qt.gtimg.cn 多行返回，返回 {code: parts}"""
    out = {}
    for line in text.strip().split("\n"):
        if "=" not in line:
            continue
        k = line.split("=")[0].replace("v_", "").strip()
        v = line.split("=", 1)[1].strip().strip('"')
        parts = v.split("~")
        if len(parts) > 3:
            out[k] = parts
    return out


def _fetch_tencent(codes):
    """codes: list[str]，已带前缀。返回 {raw_code: {name,price,change}}"""
    if not codes:
        return {}
    try:
        r = SESSION.get(f"https://qt.gtimg.cn/q={','.join(codes)}",
                        headers=TENCENT_H, timeout=25)
        r.encoding = "gbk"
        q = _parse_tencent(r.text)
        res = {}
        for raw, parts in q.items():
            res[raw] = {
                "name": parts[1] if len(parts) > 1 else raw,
                "price": parts[3] if len(parts) > 3 else "",
                "change": parts[32] if len(parts) > 32 else "",
                "source": "tencent",
            }
        return res
    except Exception as e:
        print(f"[query] 腾讯行情失败: {e}")
        return {}


def _fetch_sina(codes):
    """新浪行情备接口。codes: list[str] 原始6位（港股加 rt_hk 前缀）。"""
    if not codes:
        return {}
    items, hk_set = [], set()
    for c in codes:
        if c.startswith("hk"):
            items.append(f"rt_hk{c[2:]}")
            hk_set.add(c)
        else:
            items.append(c)
    try:
        r = SESSION.get("https://hq.sinajs.cn/list=" + ",".join(items),
                        headers=SINA_H, timeout=25)
        r.encoding = "gbk"
        res = {}
        for line in r.text.strip().split("\n"):
            m = re.match(r'var hq_str_(?:rt_hk)?([^=]+)="([^"]*)"', line)
            if not m:
                continue
            code = m.group(1).replace("sh", "").replace("sz", "")
            p = m.group(2).split(",")
            idx = 6 if "rt_hk" in line else 3
            if len(p) > idx and p[idx]:
                try:
                    res[code] = {"price": p[idx], "change": p[idx + 1] if len(p) > idx + 1 else "", "source": "sina"}
                except (ValueError, IndexError):
                    pass
        return res
    except Exception as e:
        print(f"[query] 新浪行情失败: {e}")
        return {}


def _fetch_akshare_spot(code_str, market):
    """akshare 现货兜底（仅 A/港股）。"""
    try:
        import akshare as ak
        if market == 'hk':
            df = ak.stock_hk_spot_em()
            row = df[df['代码'].astype(str).str.contains(code_str)]
        else:
            df = ak.stock_zh_a_spot()
            row = df[df['代码'].astype(str) == code_str]
        if not row.empty:
            r = row.iloc[0]
            return {"price": r.get("最新价", ""), "change": r.get("涨跌幅", ""), "source": "akshare"}
    except Exception as e:
        print(f"[query] akshare 兜底失败: {e}")
    return None


def _fetch_fund_tiantian(code_str):
    """天天基金 JSONP 直连（主）。返回最新估算净值/涨跌。"""
    try:
        url = f"https://fundgz.1234567.com.cn/js/{code_str}.js"
        r = SESSION.get(url, headers={"Referer": "https://fund.eastmoney.com/"}, timeout=10)
        if r.status_code == 200:
            m = re.search(r'\{[^{}]+\}', r.text)
            if m:
                import json
                raw = m.group(0)
                try:
                    d = json.loads(raw)
                except Exception:
                    try:
                        d = json.loads(raw.replace("'", '"'))  # 个别返回单引号容错
                    except Exception:
                        d = {}
                if d:
                    return {
                        "name": d.get("name", ""),
                        "price": d.get("gsz", d.get("dwjz", "")),  # 估算值/单位净值
                        "change": d.get("gszzl", ""),             # 估算涨跌%
                        "date": d.get("jzrq", ""),
                        "source": "tiantian",
                    }
    except Exception as e:
        print(f"[query] 天天基金失败: {e}")
    return None


def _fetch_fund_eastmoney(code_str):
    """东方财富 lsjz 历史净值（备，含 QDII 备用）。"""
    try:
        url = "https://api.fund.eastmoney.com/f10/lsjz"
        params = {"fundCode": code_str, "pageIndex": 1, "pageSize": 3}
        r = SESSION.get(url, params=params, headers={"Referer": "https://fund.eastmoney.com/"}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            records = data.get("Data", {}).get("LSJZList", [])
            if records:
                latest = records[0]
                return {
                    "name": code_str,
                    "price": latest.get("DWJZ", ""),
                    "change": latest.get("JZZZL", ""),
                    "date": latest.get("FSRQ", ""),
                    "source": "eastmoney",
                }
    except Exception as e:
        print(f"[query] 东方财富基金失败: {e}")
    return None


def query_stock(code):
    """统一入口。返回 dict 或 None。"""
    code_str = str(code).strip().upper()
    qtype, code_clean = _classify(code_str)

    if qtype == 'fund':
        res = _fetch_fund_tiantian(code_str) or _fetch_fund_eastmoney(code_str)
        return res

    # A/港/美股：腾讯 → 新浪 → akshare
    if qtype == 'hk':
        tencent_prefix, sina_code, market = 'hk', f"hk{code_clean}", 'hk'
    elif qtype == 'us':
        tencent_prefix, sina_code, market = 'us', code_clean, 'us'
    else:
        qc = code_str.replace('SH', '').replace('SZ', '')
        tencent_prefix = 'sz' if qc.startswith(('00', '30', '20')) else 'sh'
        sina_code, market = qc, 'a'

    tencent_raw = f"{tencent_prefix}{code_clean}"
    t = _fetch_tencent([tencent_raw])
    if t and tencent_raw in t:
        return t[tencent_raw]
    s = _fetch_sina([sina_code])
    if s and sina_code in s:
        return s[sina_code]
    a = _fetch_akshare_spot(code_clean, market)
    return a


if __name__ == "__main__":
    import json
    for c in ["600036", "00700", "AAPL", "006227", "519195"]:
        print(c, "→", json.dumps(query_stock(c), ensure_ascii=False))
