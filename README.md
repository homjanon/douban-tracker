# 豆瓣楼主发言追踪（douban-tracker）

基于 **GitHub Actions + Pages** 的豆瓣小组楼主发言自动追踪工具。**每日北京时间 16:30（含周末）** 自动运行（GitHub Pages 手动触发按钮亦可即时运行），抓取楼主发言 → LLM 研判 → 生成结构化每日简报 → 推送至仓库并发布 Pages 看板。

> 借鉴 [`homjanon/xueqiu-tracker`](https://github.com/homjanon/xueqiu-tracker) 的 state 增量游标 / latest.json 双结构 / 三级 LLM 后端链，并补回其已删除的【持仓入表】+【昵称映射】能力。

## 报告结构（对齐 IMA 每日投资简报）

Actions 每日产出的 `reports/YYYY-MM-DD.md` 与 Pages 看板（`docs/index.html`）严格遵循 **6 大板块骨架**，每日只需把提取到的内容填入对应板块：

| # | 板块 | 数据来源 | 渲染方式 |
|---|------|---------|---------|
| ① | 📊 持仓追踪 | `state.json` 的 `positions`（19 项权威持仓） | 5 列 Markdown/HTML 表格（标的/状态/类型/现价/提及） |
| ② | 🌅 今日总览 | LLM 单次调用产出 6 子板块 | 市场背景/核心观点/今日操作/持仓动态/看好方向/风险提示 |
| ③ | 📊 本次结果 | 运行时统计 | 今日发言数 + 累计存档数 |
| ④ | 📝 今日发言聚合 | 当日发言按标的聚类（>50 条聚合，否则逐条） | 子板块 + 占比 |
| ⑤ | 🧠 投资风格分析 | `investor_profile.json`（7 维度 + 综合评估） | 表格 + 段落 |
| ⑥ | 🏷️ 昵称映射表 | `nickname_rules.json`（规律）+ `state.json`（映射） | 规律三列表格 + 映射表 |

## 关键设计

### 持仓追踪全自动回写（带严格阀门）
`apply_position_updates` 复用「今日操作」板块，按 emoji 自动维护 `state.json` 的持仓：
- ✅ 买入/加仓 → 新增或更新；⏭️ 持有 → 仅更新动态；❌ 卖出 → 第一天标"卖出"保留痕迹、次日确认后移出
- **阀门**：仅当标的命中已知持仓/昵称或符合代码格式才允许新增（拒绝"观察策略"等策略词）；单次新增 > 5 条触发**熔断**不回写
- **成本价**：仅当发言中明确提及价格且持仓原值为"暂无"时才写入，不编造、不覆盖已有值
- **字段提纯**：每次运行对 `cost_price` / `last_note` 自动归一（成本 → `约xx元`/`约xx-x元`/`约x万元`；分析腔动态清空、超长截断），保证提及列格式统一

> 仅"无法确认的新持仓/新昵称"才进 `latest.json` 的 `pending_positions` / `pending_nicknames`（建议区）待你人工确认；其余已确认维度均全自动增量更新，无需手动维护。

### 投资风格画像全自动增量更新
`update_investor_profile` 复用「今日总览」内容，对 `investor_profile.json` 做增量修订：仅当有今日发言依据时才改对应维度（附 evidence），无变化的维度不返回；单次修订 > 5 维度触发熔断不回写。

### LLM 三级后端链（agnes 主力 + GLM-5.2 二级 + SenseNova 兜底）
按序尝试，首个有 key 且成功即生效：
1. **Agnes AI** `agnes-2.0-flash`（免费，主力）
2. NVIDIA `z-ai/glm-5.2`（免费，二级，参考 portfolio 仓调用方式）
3. 商汤日日新 `sensenova-6.7-flash-lite`（免费，Token Plan 限时免费，兜底）

### 昵称规律固化（供 LLM 判新昵称）
`nickname_rules.py` / `nickname_rules.json` 将 47 条权威映射反推出 **5 类命名规律**（拼音首字母 / 小老代指 / 戏称黑话 / 谐音取字 / 机构基金昵称），注入 LLM 提示。LLM 先按规律推断新昵称，再用 `config.USER_HINTS` 的确定映射校验，冲突以映射为准。

## 文件结构

```
douban-tracker/
├── .github/workflows/track.yml   # Actions：cron 16:30 + 手动触发 + commit/push
├── config.py                     # 三端 LLM 后端 + USER_HINTS（确定映射）
├── scraper.py                    # 豆瓣 HTTP+cookie 抓取（无 Playwright/WAF），当日全量
├── analyzer.py                   # LLM 研判：归纳 / 持仓昵称 / 今日总览（6 子板块）
├── tracker.py                    # 主流程：抓→研判→持仓回写/提纯→6 板块渲染→写 latest.json/reports
├── query_stock.py                # 股价查询：股票/ETF 走腾讯直查，基金走天天基金/东方财富，失败回退「暂无」
├── nickname_rules.py/.json       # 昵称命名规律（5 类，判新昵称用）
├── investor_profile.json         # 楼主投资风格画像（7 维度，自动增量更新）
├── state.json                    # nickname_map + positions(19) + _seen_ids(去重累计)
├── data/latest.json              # 每日产物（Pages 读取）
├── docs/index.html               # Pages 看板（6 板块卡片，持仓 5 列 + 涨跌色）
└── reports/YYYY-MM-DD.md         # 每日简报
```

## 配置（GitHub Secrets）

| Secret | 说明 |
|--------|------|
| `DOUBAN_COOKIE` | 豆瓣登录态 cookie（抓发言必需） |
| `DOUBAN_GROUP_URLS` | 追踪的小组 URL，逗号分隔支持多组 |
| `DOUBAN_TARGET_USER` | 楼主昵称 |
| `AGNES_API_KEY` | Agnes 主力后端 key |
| `NVIDIA_API_KEY` | 二级后端 key（Qwen3.5-122B） |
| `SENSENOVA_API_KEY` | 兜底后端 key（商汤 SenseNova 6.7 Flash-Lite，免费） |

## 本地调试

```bash
pip install -r requirements.txt
export DOUBAN_COOKIE=... DOUBAN_GROUP_URLS=... DOUBAN_TARGET_USER=...
export AGNES_API_KEY=...
python tracker.py
```

## 注意
- **抓取范围**：`scrape_user()` 严格只保留 `date == 今日` 的发言（从 0 点起当日全量），不跨日累积；`total_archived` 累计数基于发言 `id` 跨运行去重，仅计首次见到的。
- `state.json` 的 `positions` 由 Actions **全自动维护**（买入/卖出/成本价/字段提纯），`nickname_map` 为权威映射；无法确认的新增项进 `pending` 建议区待你人工确认后提交才生效。
- `investor_profile.json` / `nickname_rules.json` 可直接编辑，无需改代码。
- 时区：所有时间均为北京时间（UTC+8）。
- Pages 看板右上角「🔄 手动触发更新」按钮跳转 Actions 页，点 Run workflow 即可即时运行（零密钥、安全）。

### 人工确认 SOP（pending 建议区）
LLM 拿不准、或未触发自动回写阀门的持仓/昵称，会进入 `latest.json` 的 `pending_positions` / `pending_nicknames`（**仅建议、绝不自动写回**），需在 `state.json` 人工拍板后才生效。

**① 去哪里看**
- Pages 看板底部黄色提示框「⚠️ 以下为 LLM 建议…」，分「持仓建议」「新昵称映射建议」两类，每条附 `依据`。
- 或直接看 `data/latest.json` 的 `pending_positions`（数组：含 `name/code/action/evidence/price`）、`pending_nicknames`（字典）。

**② 认可 → 写进 `state.json`**
- 加持仓：在 `positions.positions` 数组追加一项（`name` 必填、`code` 有则填、`action` 填 买入/持有/卖出、`cost_price` 仅发言明确提成本才写 `约xx元` 否则 `"暂无"`、`first_seen` **留空**，由 Actions 下次运行自动写入当天 `MM-DD` 格式日期）
- 加昵称：在 `nickname_map` 对象加 `"昵称": "真实标的"` 键值
- 提交后下次运行即纳入已知数据，同名 pending 不再出现；系统写入的 `first_seen` 为 `MM-DD`，提及列渲染为 `M.D`，距今天数超 5 天则自动清空日期。

**③ 不认可 / 误判 → 不用管**
`pending` 永不自动污染 `state.json`，忽略即可；反复出现的误判也不影响持仓表。

**④ 最小日常流程**
1. 看完板，扫一眼底部"待确认"区；2. 认可的本地改 `state.json` → `git commit && git push`；3. 不认可的忽略；4. 不确定的观察几天再定。

> 修正已自动入表的持仓：直接编辑 `state.json` 中对应条目字段即可，非"暂无"的成本价下次提纯会保留你写的值、不覆盖。
