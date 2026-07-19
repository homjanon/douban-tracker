# 豆瓣楼主发言追踪（douban-tracker）

基于 **GitHub Actions + Pages** 的豆瓣小组楼主发言自动追踪工具。每个工作日北京时间 18:00（UTC 10:00）自动运行，抓取楼主发言 → LLM 研判 → 生成结构化每日简报 → 推送至仓库并发布 Pages 看板。

> 借鉴 [`homjanon/xueqiu-tracker`](https://github.com/homjanon/xueqiu-tracker) 的 state 增量游标 / latest.json 双结构 / 四级 LLM 后端链，并补回其已删除的【持仓入表】+【昵称映射】能力。

## 报告结构（对齐 IMA 每日投资简报）

Actions 每日产出的 `reports/YYYY-MM-DD.md` 与 Pages 看板（`docs/index.html`）严格遵循 **6 大板块骨架**，每日只需把提取到的内容填入对应板块：

| # | 板块 | 数据来源 | 渲染方式 |
|---|------|---------|---------|
| ① | 📊 持仓追踪 | `state.json` 的 `positions`（19 项权威持仓） | 7 列 Markdown 表格 |
| ② | 🌅 今日总览 | LLM 单次调用产出 6 子板块 | 市场背景/核心观点/今日操作/持仓动态/看好方向/风险提示 |
| ③ | 📊 本次结果 | 运行时统计 | 今日发言数 + 累计存档数 |
| ④ | 📝 今日发言聚合 | 当日发言按标的聚类（>50 条聚合，否则逐条） | 子板块 + 占比 |
| ⑤ | 🧠 投资风格分析 | `investor_profile.json`（7 维度 + 综合评估） | 表格 + 段落 |
| ⑥ | 🏷️ 昵称映射表 | `nickname_rules.json`（规律）+ `state.json`（映射） | 规律说明 + 映射表 |

## 关键设计

### 待确认机制（防幻觉污染）
LLM 的持仓/昵称研判结果**只写入 `latest.json` 的 `pending_positions` / `pending_nicknames`（建议区）**，绝不自动写回 `state.json`。需你本地编辑 `state.json` 并 push 后才会生效。这避免了早期"小华=万家""华夏兄弟=华夏基金旗下"这类幻觉污染状态文件。

### LLM 四级后端链（agnes 主力 + 雪球三级备用）
按序尝试，首个有 key 且成功即生效：
1. **Agnes AI** `agnes-2.0-flash`（免费多模态，主力）
2. NVIDIA `qwen3.5-122b-a10b`（免费备用 1）
3. NVIDIA `kimi-k2.5`（免费备用 2）
4. 硅基流动 `Qwen3.5-35B-A3B`（付费兜底）

### 昵称规律固化（供 LLM 判新昵称）
`nickname_rules.py` / `nickname_rules.json` 将 47 条权威映射反推出 **5 类命名规律**（拼音首字母 / 小老代指 / 戏称黑话 / 谐音取字 / 机构基金昵称），注入 LLM 提示。LLM 先按规律推断新昵称，再用 `config.USER_HINTS` 的确定映射校验，冲突以映射为准。

## 文件结构

```
douban-tracker/
├── .github/workflows/track.yml   # Actions：cron 每日运行 + commit/push
├── config.py                     # 四端 LLM 后端 + USER_HINTS（确定映射）
├── scraper.py                    # 豆瓣 HTTP+cookie 抓取（无 Playwright/WAF）
├── analyzer.py                   # LLM 研判：归纳 / 持仓昵称 / 今日总览
├── tracker.py                    # 主流程：抓→研判→6 板块渲染→写 latest.json/reports
├── query_stock.py                # 股价多源查询（腾讯/新浪/akshare/天天基金/东方财富）
├── nickname_rules.py/.json       # 昵称命名规律（判新昵称用）
├── investor_profile.json         # 楼主投资风格画像（7 维度）
├── state.json                    # 增量游标 + nickname_map(47) + positions(19)
├── data/latest.json              # 每日产物（Pages 读取）
├── docs/index.html               # Pages 看板（6 板块卡片）
└── reports/YYYY-MM-DD.md         # 每日简报
```

## 配置（GitHub Secrets）

| Secret | 说明 |
|--------|------|
| `DOUBAN_COOKIE` | 豆瓣登录态 cookie（抓发言必需） |
| `DOUBAN_GROUP_URLS` | 追踪的小组 URL，逗号分隔支持多组 |
| `DOUBAN_TARGET_USER` | 楼主昵称 |
| `AGNES_API_KEY` | Agnes 主力后端 key |
| `NVIDIA_API_KEY` | 备用 1/2 key |
| `SILICONFLOW_API_KEY` | 付费兜底 key |

## 本地调试

```bash
pip install -r requirements.txt
export DOUBAN_COOKIE=... DOUBAN_GROUP_URLS=... DOUBAN_TARGET_USER=...
export AGNES_API_KEY=...
python tracker.py
```

## 注意
- `state.json` 的 `nickname_map` / `positions` 为**权威数据源**，修改需你人工确认后提交，Actions 不会自动覆盖。
- `investor_profile.json` / `nickname_rules.json` 可直接编辑，无需改代码。
- 时区：所有时间均为北京时间（UTC+8）。
