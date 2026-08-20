# 豆瓣楼主发言追踪（douban-tracker）

基于 **GitHub Actions + Pages** 的豆瓣楼主发言自动追踪工具。**每日北京时间 16:30（含周末）** 自动运行（GitHub Pages 手动触发按钮亦可即时运行），抓取楼主发言 → LLM 研判 → 生成结构化每日简报 → 推送至仓库并发布 Pages 看板。

> 借鉴 [`homjanon/xueqiu-tracker`](https://github.com/homjanon/xueqiu-tracker) 的 state 增量游标 / latest.json 双结构 / 三级 LLM 后端架构，并补回其已删除的「持仓入库」+「昵称映射」能力。

## ?? 双模式抓取（重点）

本工具支持**两种抓取源**，通过 `SCRAPE_MODE` 一个变量切换，**代码零改动**：

| 模式 | `SCRAPE_MODE` | 抓取源 | 机制 | 状态 |
|------|---------------|--------|------|------|
| 用户主页广播（新） | `topic` | `DOUBAN_USER_STATUSES_URL`（主页 `…/statuses`） | 主页第一条广播(豆瓣话题) → rexxar API 拉评论 → 按作者 uid 过滤（只看作者） | ? 默认开启 |
| 小组话题（旧） | `group` | `DOUBAN_GROUP_URLS`（小组页） | 小组最新帖 → `?author=1` → 解析 `reply-doc`（静态 HTML） | 保留 / 休眠 |

### 如何切换

- **当前默认 `topic`（新模式开启、旧模式休眠）**——旧模式代码完整保留，只是不被调用。
- **切回旧小组模式（关闭新模式）**：
  1. 设环境变量 / Secret `SCRAPE_MODE=group`；
  2. 填 `DOUBAN_GROUP_URLS`（小组 URL，多组逗号分隔，恢复原值）；
  3. 重新运行即可——`scrape_user()` 按 `SCRAPE_MODE` 自动分派，无需改任何代码。
- **再切回新模式**：`SCRAPE_MODE=topic` + `DOUBAN_USER_STATUSES_URL` 指向主页广播页（`DOUBAN_GROUP_URLS` 留空也无妨）。
- 两模式共用 `DOUBAN_TARGET_USER`（楼主昵称）与 `DOUBAN_COOKIE`。

> 一句话：**切换 = 改 `SCRAPE_MODE` 一个值 + 对应填写 `DOUBAN_GROUP_URLS` / `DOUBAN_USER_STATUSES_URL`**。

### 新模式技术说明（豆瓣话题）

- 主页 `…/statuses` 里的"广播"实为**豆瓣话题(topic)** 动态，每条带 `data-aid`(话题 id) 与 `data-uid`(作者 uid)。
- 话题评论是 **AJAX 动态加载**，静态 HTML 无 `reply-doc`；真实接口为
  `https://m.douban.com/rexxar/api/v2/group/topic/{aid}/comments`（路径含 `group/topic`，非 `topic`）。
- "只看作者"的服务端参数（`user_id=` / `only_author=`）**不生效**，需在客户端按作者 `uid` 过滤。
- 请求需移动端 `User-Agent` + `Cookie` + `Referer` + `Accept: application/json`。
- 新模式**不做当日过滤**：每日只取"最新一条广播"下作者的全部发言（该用户每天发新广播，话题换新后自然切换）。
- **评论分页稳健性**：rexxar 的 `count` 参数服务端常截断（每页仅回 20–50 条而非 100）；翻页以"接口实际返回量"推进（`start += len(comments)`），遇空页即停，并设 `MAX_PAGES=200` 安全上限防死循环，**确保抓全该广播全部作者发言**（实测单广播 192 条、07:53→13:29 连续无漏）。

## 报告结构（对齐 IMA 每日投资简报）

Actions 每日产出的 `reports/YYYY-MM-DD.md` 与 Pages 看板（`docs/index.html`）严格遵循 **6 大板块骨架**，每日只需把抓取到的内容填入对应板块：

| # | 板块 | 数据来源 | 呈现方式 |
|---|------|---------|---------|
| ① | ? 持仓追踪 | `state.json` 的 `positions`（权益持仓） | 5 列 Markdown/HTML 表格（标的/状态/类型/现价/提及） |
| ② | ? 今日总览 | LLM 单次调用产出 5 子板块 | 市场背景/今日操作/今日议题/看好方向/风险提示 |
| ③ | ? 本次结果 | 运行时统计 | 今日发言数 + 累计存档数 |
| ④ | ? 发言聚合 | 当日发言按标签聚类（>50 条做聚合，否则逐条） | 子板块 + 占比 |
| ⑤ | ? 投资风格分析 | `investor_profile.json`（4 维度 + 综合评估） | 表格 + 段落 |
| ⑥ | ?? 昵称映射表 | `nickname_rules.json`（规则）+ `state.json`（映射） | 规则三列表格 + 映射表 |

## 关键设计

### 持仓自动回写（带严格阀门）
`apply_position_updates` 复用「今日操作」板块，按 emoji 自动维护 `state.json` 的持仓：
- ? 买入/加仓 → 新增或更新；?? 持有 → 仅更新动态；? 卖出 → 第一天标"卖出"保留痕迹、次日确认卖出再移出
- **阀门**：仅当发言命中已知持仓/昵称或符合代码格式才允许新增（拒绝"观察策略"等策略词）
- **成本阈值**：仅当发言明确提及价格且持仓原值为"暂无"时才写入，不编造、不覆盖已有值
- **字段提纯**：每次运行对 `cost_price` / `last_note` 自动归一（成本 → `约xx元`/`约xx-x元`/`约x万元`；动态 → 截断/清空分析腔）

> 无法确认的「新持仓/新昵称」才进 `latest.json` 的 `pending_positions` / `pending_nicknames`（建议区），待你人工确认后提交才生效。

### 投资风格画像全自动增量更新
`update_investor_profile` 复用「今日总览」内容，对 `investor_profile.json` 做增量修订：仅当今日发言确有新依据时才修订对应维度，无变化不强行重写；单维度修订建议 ≤150 字。

### LLM 四级后端（智谱 GLM-4.5-Air 主力 + DeepSeek-V4-Flash 二级 + Agnes 三级 + NVIDIA GLM-5.2 兜底）
按顺序尝试，首个有 key 且成功即生效；**每日 3 次 LLM 调用**（摘要 / 持仓昵称研判 / 今日总览+画像修订合并，2026-08-20 优化，原先 4 次）：
1. **智谱 AI GLM-4.5-Air** `glm-4.5-air`（OpenAI 兼容，`ZHIPU_API_KEY`，主力；`thinking` 关闭 + `max_tokens=12000`，参考 qiugecaozuo：关思考 content 472→5306 字）
2. **商汤 DeepSeek-V4-Flash** `deepseek-v4-flash`（复用 `SENSENOVA_API_KEY`，商汤平台，二级；`reasoning_effort=low` 轻思考 + `max_tokens=12000`，实测 12s→2.6s 且 content 稳定非空）
3. **Agnes AI** `agnes-2.0-flash`（免费多模态，三级）
4. NVIDIA `z-ai/glm-5.2`（免费，兜底，参考 portfolio 仓调用方式）

> **调用次数与稳定性（2026-08-20 优化）**：`analyzer.call_multi` 加 90s 总时限（后端全挂快速降级，不再逐后端叠加超时）；画像更新并入「今日总览」一次调用（`profile_updates` 字段），不再单独调 LLM。

### 昵称规则固化（供 LLM 判断昵称）
`nickname_rules.py` / `nickname_rules.json` 将 47 条持仓映射反推出 **5 类命名规则**（拼音首字母 / 小名代指 / 绰号黑话 / 谐音取名 / 机构基金昵称），注入 LLM 提示。LLM 先按规则推断新昵称，再用 `config.USER_HINTS` 的确认映射校验，冲突以映射为准。

## 文件结构

```
douban-tracker/
├── .github/workflows/track.yml   # Actions：cron 16:30 + 手动触发 + commit/push
├── config.py                     # 三级 LLM 后端 + 双模式抓取配置（SCRAPE_MODE / 两套 URL）
├── scraper.py                    # 豆瓣 HTTP+cookie 抓取（无 Playwright/WAF）
│                                #   topic 模式：find_latest_topic + fetch_topic_comments(API)
│                                #   group 模式：find_latest_post + fetch_posts + parse_reply_blocks(HTML)
├── analyzer.py                   # LLM 研判：归类 / 持仓昵称 / 今日总览，5 子板块
├── tracker.py                    # 主流程：抓→去重→研判→写 latest.json/reports
├── query_stock.py                # 股价查询：股票/ETF 腾讯主、基金天天基金主
├── nickname_rules.py/.json       # 昵称命名规则（5 类，判断昵称用）
├── investor_profile.json         # 楼主投资风格画像（4 维度，自动增量更新）
├── state.json                    # nickname_map + positions（权益持仓列表） + _seen_ids（去重游标）
├── data/latest.json              # 每日产物（Pages 读取）
├── docs/index.html               # Pages 看板（6 板块卡片，持仓 5 列 + 涨跌颜色）
└── reports/YYYY-MM-DD.md         # 每日简报
```

## 配置（GitHub Secrets）

| Secret | 说明 |
|--------|------|
| `DOUBAN_COOKIE` | 豆瓣登录态 cookie（抓广播**必填**） |
| `DOUBAN_USER_STATUSES_URL` | 用户主页广播页 URL（topic 模式，如 `https://www.douban.com/people/295613619/statuses`） |
| `DOUBAN_GROUP_URLS` | 追踪的豆瓣小组 URL，逗号分隔（group 模式；topic 模式可留空） |
| `DOUBAN_TARGET_USER` | 楼主昵称（两种模式共用，用于过滤发言） |
| `SCRAPE_MODE` | `topic`（默认/开启）或 `group`（切回旧模式） |
| `AGNES_API_KEY` | 二级后端 key（agnes-2.0-flash） |
| `NVIDIA_API_KEY` | 兜底后端 key（glm-5.2） |
| `SENSENOVA_API_KEY` | 二级后端 key（DeepSeek-V4-Flash，商汤平台） |
| `ZHIPU_API_KEY` | 主力后端 key（智谱 GLM-4.5-Air，OpenAI 兼容） |

## 本地调试

```bash
pip install -r requirements.txt
export DOUBAN_COOKIE=... DOUBAN_USER_STATUSES_URL=... DOUBAN_TARGET_USER=... SCRAPE_MODE=topic
export AGNES_API_KEY=...
python tracker.py
```

## 注意
- **抓取范围**：topic 模式严格只取"最新一条广播"下作者的全部发言（不按当日过滤，因楼主每天发新广播）；group 模式保留原"仅当日"逻辑。
- `state.json` 的 `positions` 由 Actions **全自动维护**（买入/卖出/成本/字段提纯），`nickname_map` 为持仓映射，无法确认的新增项进 `pending` 建议区待你人工确认后提交才生效。
- `investor_profile.json` / `nickname_rules.json` 可直接编辑，无需改代码。
- 时区：所有时间均为北京时间（UTC+8）。
- Pages 看板右上角「? 手动触发更新」按钮跳转 Actions 页面，点 Run workflow 即可即时运行（免密钥、安全）。
- **cookie 安全**：`DOUBAN_COOKIE` 为登录凭证，仅注入私仓 Secrets，勿提交；建议定期「设置 → 退出其他设备」轮换。
- **看板实时性**：`docs/index.html` 拉取 `data/latest.json` 时带 `cache: 'no-store'`，且 `<head>` 设 `no-cache` meta，浏览器不缓存数据，**每次打开即最新、无需手动清缓存**。

### 人工确认 SOP（pending 建议区）
LLM 拿不准、或未触达自动回写阀门的持仓/昵称，会进入 `latest.json` 的 `pending_positions` / `pending_nicknames`（**仅建议、绝不自动写库**），需你在 `state.json` 人工拍板后提交才生效。

**① 去哪看**
- Pages 看板底部黄色提示框「?? 以下为 LLM 建议……」，分「持仓建议」「新昵称映射建议」「新昵称规律建议」三类，每条附「依据」。
- 或直接看 `data/latest.json` 的 `pending_positions`（数组：name/code/action/evidence/price）、`pending_nicknames`（字典）、`pending_rules`（数组：type/rule/examples/evidence，昵称规律建议）。

**② 认可 → 写入 `state.json`**
- 加持仓：在 `positions.positions` 数组追加一项（`name` 必填、`code` 有则填、`action` 填 买入/持有/卖出、`cost_price` 仅发言明确提及价才写 `约xx元` 否则 `"暂无"`、`first_seen` **留空**，由 Actions 下次运行自动写入当天 `MM-DD` 格式日期）。
- 加昵称：在 `nickname_map` 对象加 `"昵称": "真实标的"` 键值。
- 提交后下次运行即纳入已知数据，同名 pending 不再出现；系统写入的 `first_seen` 为 `MM-DD`，提及列渲染为 `M.D`，距今超 5 天自动清空日期。

**③ 不认可 / 误判 → 不用管**
`pending` 永不自动污染 `state.json`，忽略即可；重复出现的误判也不影响持仓表。

**④ 最小日常流程**
1. 看完板，扫一眼底部「待确认」区；2. 认可的本地改 `state.json` → `git commit && git push`；3. 不认可的忽略；4. 不确定的观察几天再定。

> 修正已自动入表的持仓：直接编辑 `state.json` 对应条目的字段即可，非"暂无"的成本价下次提纯会保留你写的值、不覆盖。
