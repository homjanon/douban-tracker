"""行情查询：A/港/美股/ETF + 公募基金。

数据源（极简直查，无冗余兜底）：
  A/港/美股/ETF : 腾讯 qt.gtimg.cn（唯一源；查询失败即返回 None，上层显示"暂无"）
  基金(场外/净值型/QDII) : 天天基金 JSONP 直连 fundgz.1234567.com.cn（主）
                         → 东方财富 lsjz 历史净值 api.fund.eastmoney.com（备，含 QDII）

设计要点：
  - 股票/ETF 仅走腾讯；失败返回 None，由 enrich_prices 回退为"暂无"，绝不拉全市场快照。
  - 腾讯 qt.gtimg.cn 的 parts[3]=当前价，parts[32]=涨跌幅；港股加 hk 前缀、美股加 us 前缀。
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


def _tencent_prefix(code):
    """按交易所返回腾讯行情前缀：沪市 sh / 深市 sz（含深市 ETF 15/16/18）。"""
    if code.startswith(('60', '68', '90', '50', '51', '52', '55', '56', '58')):
        return 'sh'
    return 'sz'   # 00/30/20/15/16/18 等深市


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

    # A/港/美股/ETF：仅腾讯直查；失败即返回 None（上层显示"暂无"）
    if qtype == 'hk':
        tencent_prefix = 'hk'
    elif qtype == 'us':
        tencent_prefix = 'us'
    else:
        tencent_prefix = _tencent_prefix(code_clean)

    tencent_raw = f"{tencent_prefix}{code_clean}"
    t = _fetch_tencent([tencent_raw])
    if t and tencent_raw in t:
        return t[tencent_raw]
    return None


if __name__ == "__main__":
    import json
    for c in ["600036", "00700", "AAPL", "159949", "563300", "270023", "006227"]:
        print(c, "→", json.dumps(query_stock(c), ensure_ascii=False))
