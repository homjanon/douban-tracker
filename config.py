"""配置：从环境变量读取，缺失时用默认值。

LLM 后端优先级（agnes 主力 + 雪球三级备用）：
  1) Agnes AI agnes-2.0-flash —— 免费多模态，主力
  2) NVIDIA Qwen3.5-122B-A10B —— 免费，备用1
  3) NVIDIA Kimi-K2.5          —— 免费，备用2（走 build.nvidia.com 专属 endpoint）
  4) 硅基流动 Qwen3.5-35B-A3B  —— 便宜付费，最后兜底

抓取：豆瓣话题页 + DOUBAN_COOKIE 登录态（HTTP 直连，无 WAF/Playwright）。
状态：state.json 增量游标 + 昵称映射 + 持仓（每轮 commit 回仓库持久化）。
"""
import os

# ============ 豆瓣抓取配置 ============
DOUBAN_COOKIE = os.getenv("DOUBAN_COOKIE", "")
# 追踪的豆瓣话题组 URL（楼主发言所在组），逗号分隔支持多组
DOUBAN_GROUP_URLS = [x.strip() for x in
                     os.getenv("DOUBAN_GROUP_URLS",
                               "https://www.douban.com/group/your-group/").split(",") if x.strip()]
# 目标楼主昵称（用于过滤发言）
DOUBAN_TARGET_USER = os.getenv("DOUBAN_TARGET_USER", "楼主昵称")

HEADLESS = os.getenv("HEADLESS", "false").lower() != "false"

# ============ LLM 后端（agnes 主力 + 雪球三级备用）============
BACKENDS = [
    {
        # ① 主力：Agnes AI 免费多模态
        "name": "agnes-2.0-flash",
        "base_url": os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1"),
        "api_key": os.getenv("AGNES_API_KEY", ""),
        "model": os.getenv("AGNES_MODEL", "agnes-2.0-flash"),
        "timeout": int(os.getenv("AGNES_TIMEOUT", "120")),
    },
    {
        # ② 备用1：NVIDIA Qwen3.5-122B-A10B（免费，10B激活，比397B更快更稳）
        "name": "nvidia-qwen3.5-122b",
        "base_url": os.getenv("PRIMARY_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "api_key": os.getenv("NVIDIA_API_KEY", ""),
        "model": os.getenv("PRIMARY_MODEL", "qwen/qwen3.5-122b-a10b"),
        "timeout": int(os.getenv("PRIMARY_TIMEOUT", "120")),
    },
    {
        # ③ 备用2：NVIDIA Kimi-K2.5（免费，走 build.nvidia.com 专属 endpoint）
        "name": "nvidia-kimi-k2.5",
        "base_url": os.getenv("FALLBACK1_BASE_URL",
                              "https://ai.api.nvidia.com/v1/nim/moonshotai/kimi-k2.5/v1"),
        "api_key": os.getenv("NVIDIA_API_KEY", ""),
        "model": os.getenv("FALLBACK1_MODEL", "moonshotai/kimi-k2.5"),
        "timeout": int(os.getenv("FALLBACK1_TIMEOUT", "150")),
    },
    {
        # ④ 最后兜底：硅基流动 Qwen3.5-35B-A3B（便宜付费）
        "name": "siliconflow-qwen3.5-35b",
        "base_url": os.getenv("FALLBACK2_BASE_URL", "https://api.siliconflow.cn/v1"),
        "api_key": os.getenv("SILICONFLOW_API_KEY", ""),
        "model": os.getenv("FALLBACK2_MODEL", "Qwen/Qwen3.5-35B-A3B"),
        "timeout": int(os.getenv("FALLBACK2_TIMEOUT", "90")),
    },
]

# 全局默认超时（各后端可用 BACKENDS[].timeout 覆盖）
TIMEOUT = int(os.getenv("TIMEOUT", "150"))

# ============ 昵称/黑话词典（USER_HINTS 范式，借鉴 xueqiu-tracker）============
# 楼主发言中出现的昵称 → 真实标的映射；作为轻量上下文注入 LLM 提升识别准确率。
# 研判时仍遵循"宽松原则"：宁可信其有入表、70%+ 把握即可写入。
USER_HINTS = {
    "default": """【楼主昵称/黑话提示，请严格据此正确解读发言——以下均为已确认的权威映射】
【A股/ETF】
- 招招/招商银行/招行 = 招商银行（SH600036，红利防御仓，7000股）
- 小惠 = 惠发食品（SH603536）
- 老莫 = 万家品质生活混合A（519195）基金经理莫海波
- 赵姨 = 兆易创新（SH603986，仅昵称线索）
- 露露 = 泸州老窖（SZ000568）
- 空调 = 格力电器（SZ000651）
- 万华 = 万华化学（SH600309）
- 保利发展/宝 = 保利发展（SH600048）
- 东百 = 东百集团（SH600693）
- 鼎泰高科 = 鼎泰高科（SZ301377，PCB材料方向高价股）
- 奶茶 = 易点天下（SZ301171，传媒进攻仓）
- YL/yl = 引力传媒（SH603598，传媒）
- ZY/zy = 掌阅科技（SH603533，传媒）
- SJ/sj = 视觉中国（SZ000681，传媒）
- 老师 = 中文在线（SZ300364，传媒）
- 蓝色 = 蓝色光标（SZ300058，传媒，奶茶的弟弟）
- C50/c50 = 创业板50ETF（SZ159949，约30%科技仓位）
- cc = 华安文体健康混合（001532，约30%科技仓位）
- 周经理 = 中欧新趋势混合(LOF)A（166001，约20%仓位，万华化学拖累）
- 张经理 = 信澳匠心回报混合A（015608，约20%仓位，化工方向）
- 小德 = 德明利（SZ001309，PCB/存储）
- 小远 = 华宝致远混合(QDII)C（008253，经常被网友批评）
- 华子 = 华泰保兴安悦债券（纯债基）
【QDII/美股基金】
- 小华 = 华夏全球科技先锋混合(QDII)A（005698，重仓光方向）
- 小华兄弟 = 华夏移动互联混合(QDII)（002891，重仓光方向）
- 小广 = 广发全球精选股票(QDII)A（270023）
- 小浦/浦银安盛 = 浦银安盛全球智能科技(QDII)A（005656）
- 网易云/音乐世家 = 富国全球科技互联网股票(QDII)A（006452，核心仓约10万）
- 讨饭/亚洲讨饭/建信新兴/国富亚洲 = 建信新兴市场混合(QDII)A（539002，主投亚洲）
- 国泰纳指 = 国泰纳斯达克100指数(QDII)
- 易方达全球 = 易方达全球成长精选混合(QDII)A
- 华宝 zy/华宝zy = 华宝致远混合(QDII)A
- 小广 = 广发全球精选股票(QDII)A
【板块/黑话】
- 卖国/PCB = PCB板块（英伟达AI产业链）
- 爱国 = 国产自主（半导体设备/芯片制造）
- 宇航员 = 航空航天板块
- 国际/国际复材 = 国际复材（仅昵称线索）
- 巨无霸 = 长鑫科技等重大IPO企业
- 昆仑万维/kun = 昆仑万维（SZ300418）
- 周经理 = 中欧新趋势混合(LOF)A
【其它】
- C50/k50 = 科创50
- xhxc = 新瀚新材；zxfc = 中欣氟材；yl = 引力传媒；zy = 掌阅科技；sj = 视觉中国
- 无法从上述映射推断的昵称，结合上下文合理推断；仍无法推断时跳过，不要强行映射。""",
}

# ============ 抓取/输出参数 ============
PAGES = int(os.getenv("PAGES", "3"))
RECENT_N = int(os.getenv("RECENT_N", "10"))          # 无新增时每人保留最近 N 条兜底展示
AGGREGATE_THRESHOLD = int(os.getenv("AGGREGATE_THRESHOLD", "50"))  # 发言>此数则聚合展示

DATA_DIR = os.getenv("DATA_DIR", "data")
REPORT_DIR = os.getenv("REPORT_DIR", "reports")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
