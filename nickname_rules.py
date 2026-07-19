"""昵称规律（供 LLM 判断新昵称时参考，源自 47 条权威映射的反推归纳）。

与 `config.USER_HINTS`（线性映射表）的区别：
  - USER_HINTS = 是什么（昵称→标的 的确定映射，已知项）
  - 本文件      = 为什么（昵称的命名规律，用于推断未知新昵称）
LLM 先按规律推断，再用 USER_HINTS 校验；两侧冲突以 USER_HINTS 为准。
"""
import json
import os

_RULES_FILE = os.getenv("NICKNAME_RULES_FILE", "nickname_rules.json")


# ============ 5 类命名规律（结构化，注入 LLM 提示）============
RULES = [
    {
        "type": "拼音首字母缩写",
        "rule": "取标的/基金名汉字拼音首字母串（含数字代码），多为 ETF/股票代码口语化",
        "examples": "yl=引力传媒｜zy=掌阅科技｜sj=视觉中国｜cc=华安文体健康混合｜"
                    "C50=创业板50ETF｜k50=科创50｜kun=昆仑万维｜xhxc=新瀚新材｜zxfc=中欣氟材",
    },
    {
        "type": "小/老+姓氏代指",
        "rule": "「小X」「老X」通常指姓 X 的标的或该标的的基金经理；「X兄弟」多为同系基金",
        "examples": "小惠=惠发食品｜老莫=万家品质生活(莫海波)｜小华=华夏全球科技先锋(QDII)A｜"
                    "小华兄弟=华夏移动互联(QDII)｜小浦=浦银安盛全球智能科技(QDII)A｜"
                    "小广=广发全球精选(QDII)A｜小远=华宝致远(QDII)C｜招招=招商银行｜"
                    "小德=德明利｜赵姨=兆易创新｜露露=泸州老窖｜空调=格力电器",
    },
    {
        "type": "戏称/黑话→题材板块",
        "rule": "生活化比喻或黑话对应某个题材/板块，而非单一标的；出现时多指板块方向",
        "examples": "卖国=PCB板块(英伟达AI链)｜爱国=国产自主(半导体设备/芯片)｜"
                    "奶茶=易点天下｜老师=中文在线｜蓝色=蓝色光标｜宇航员=航空航天板块｜"
                    "华子=华泰保兴安悦债券｜国际=国际复材｜巨无霸=长鑫科技等重大IPO",
    },
    {
        "type": "谐音/取字",
        "rule": "昵称含标的名关键字（取一字或谐音），多为 A 股个股或基金简称",
        "examples": "万华=万华化学｜东百=东百集团｜宝=保利发展｜"
                    "讨饭/亚洲讨饭/建信新兴=建信新兴市场(QDII)A｜"
                    "华宝zy=华宝致远(QDII)A",
    },
    {
        "type": "机构/基金经理昵称",
        "rule": "以基金公司名、经理姓、或平台代称指代对应基金产品",
        "examples": "周经理=中欧新趋势混合(LOF)A｜张经理=信澳匠心回报混合A｜"
                    "网易云/音乐世家=富国全球科技互联网股票(QDII)A｜"
                    "国富亚洲=国富亚洲机会股票(QDII)A｜国泰纳指=国泰纳斯达克100指数(QDII)｜"
                    "易方达全球=易方达全球成长精选混合(QDII)A｜浦银安盛=浦银安盛全球智能科技(QDII)A",
    },
]


def load_nickname_rules():
    """返回规律文本（优先读 nickname_rules.json，缺失则用内置 RULES）。"""
    try:
        with open(_RULES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        rules = data.get("rules", RULES)
    except Exception:
        rules = RULES
    if not rules:
        return ""
    lines = ["【昵称命名规律（用于推断未知新昵称）】"]
    for r in rules:
        lines.append(f"- {r.get('type','')}：{r.get('rule','')}｜示例：{r.get('examples','')}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(load_nickname_rules())
