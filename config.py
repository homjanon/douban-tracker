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
    "default": """【楼主昵称/黑话提示，请据此正确解读发言】
- 小惠 = 惠发食品
- 老莫 = 莫海波，即万家品质生活混合A（519195）基金经理
- 招商银行/招行/小招 = 招商银行（SH600036）
- 国际/国际复材 = 国际复材（仅昵称线索，非持仓）
- 赵姨 = 兆易创新（仅昵称线索，非持仓）
其它昵称/谐音请结合上下文合理推断；无法推断时跳过，不要强行映射。""",
}

# ============ 抓取/输出参数 ============
PAGES = int(os.getenv("PAGES", "3"))
RECENT_N = int(os.getenv("RECENT_N", "10"))          # 无新增时每人保留最近 N 条兜底展示
AGGREGATE_THRESHOLD = int(os.getenv("AGGREGATE_THRESHOLD", "50"))  # 发言>此数则聚合展示

DATA_DIR = os.getenv("DATA_DIR", "data")
REPORT_DIR = os.getenv("REPORT_DIR", "reports")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
