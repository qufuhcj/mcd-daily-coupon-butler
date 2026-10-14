# workbuddy.md · 使用 WorkBuddy 开发「麦麦每日领券管家」的对话上下文

> 📌 **用途**：参加「麦当劳程序员节创意开发大赛」WorkBuddy 专项奖励（排行榜前 100 名且提交本文件可获 3,000 WorkBuddy 积分）
> 🤖 **开发工具**：WorkBuddy（Agent 模式）
> 📦 **项目仓库**：https://github.com/qufuhcj/mcd-daily-coupon-butler
> 📅 **开发周期**：2026-10-13 23:37 — 2026-10-14 23:10
> 🔗 **MCP 连接器**：`mcd-mcp`（麦当劳 MCP，Streamable HTTP，官方地址 `https://mcp.mcd.cn`）

---

## 一、开发环境与连接配置

| 项 | 说明 |
|---|---|
| 开发工具 | WorkBuddy 桌面版（Agent 模式，macOS） |
| MCP 连接器 | 自定义连接器 `mcd-mcp`，类型 streamablehttp，`Authorization: Bearer <MCD_MCP_TOKEN>`（Token 仅存于 WorkBuddy 连接器配置，本仓库仅保存占位符模板 `mcp-config.example.json`） |
| 定时任务 | WorkBuddy 自动化「麦麦每日领券管家」，RRULE `FREQ=DAILY;BYHOUR=10;BYMINUTE=30`，工作目录 `/Users/hcj/WorkBuddy/mcd/mcd-daily-coupon-butler` |
| 运行环境 | Python 3.13（纯标准库，零第三方依赖） |

---

## 二、对话时间线（需求 → WorkBuddy 行动 → 产出）

### 1️⃣ 需求提出（2026-10-13 23:37）

**黄工**：「我想参加这个比赛，请制作一个能每天自动领券的作品」+ 比赛仓库地址。

**WorkBuddy 行动**：
- 抓取比赛 README，明确报名硬性要求（README.md / CONTEST_DECLARATION.md / MCP_INTEGRATION.md / 源代码，四件套）
- 阅读工作区已有方案文档与已上线的兄弟项目结构，复用其工程规范
- 加载 `mcd-mcp` 领券相关工具 schema：`available-coupons`、`auto-bind-coupons`、`query-my-coupons`、`query-store-coupons`、`query-survey-coupon`、`query-my-account`、`campaign-calendar`、`query-lottery-info`、`draw-lottery`、`query-my-prizes`

### 2️⃣ 真实链路验证（2026-10-13 23:38）

在 WorkBuddy 对话中**真实调用**麦当劳 MCP，而不是凭空设计：

| 调用 | 真实返回（脱敏摘要） |
|---|---|
| `available-coupons` | 7 张麦麦省券，状态均为「已领取」 |
| `auto-bind-coupons` | 返回「暂无可领取的优惠券」→ 确认该接口**幂等**（已领满即返回此提示），适合每日定时高频执行 |
| `query-my-coupons` | 卡包 12 张券，含名称、用券价、有效期、渠道标签、券图 URL |
| `query-my-account` | 可用积分 88.9，累计 217.5，**已过期 128.6**，本月/下月将过期字段可用于预警 |

**由此提炼出两个真实痛点（成为产品的立论依据）**：
1. 早餐券**按天投放**，每张只在当天 05:00-10:29 有效，不天天盯着就漏
2. 积分**静默过期**（历史已过期 128.6 分，从无提醒）

### 3️⃣ 工程实现（2026-10-13 23:40 — 23:50）

WorkBuddy 生成项目骨架：
- `SKILL.md`：AI 工作流编排（六阶段 + 合规红线）
- `scripts/coupon_ledger.py`：券资产台账引擎（diff 新增券、临期分类、积分预警、连续打卡、幂等入账）
- `README.md` / `MCP_INTEGRATION.md` / `CONTEST_DECLARATION.md`（官方原文）/ `mcp-config.example.json` / `examples/sample_today.json`

**联调验证**：用真实券包数据连续模拟两天，输出日报，期间修复两个 bug：
- `streak_days` 在快照入账前计算 → 打卡天数恒为 0
- `first_run` 语义误用 → 第二天无法识别新增券

### 4️⃣ 配置每日自动运行（2026-10-13 23:50）

在 WorkBuddy 中创建**每日定时自动化**，让"每天自动领券"真正落地（非纸面方案）。黄工随后提出把时间调到 10:30（电脑都开着不会漏领），已同步更新 RRULE。

### 5️⃣ 日报迭代（2026-10-14 00:00 — 00:15）

黄工三轮反馈，WorkBuddy 逐轮改版：

| 反馈 | 改法 |
|---|---|
| 「太简单，也算一下折扣率判断实惠程度」 | 新增 `originalPrice` 字段与 `coupon_value()`：买一送一按「÷单件原价×2」精算、无原价按 5 折估算并标 ≈、四档评级（神券🔥/超值✨/划算👍/常规）+ 预计省额 + "今日神券"建议 |
| 「要一直滑动不友好，如果有图更好」 | 重新调用 `query-my-coupons` 取官方券图 URL → 快照 `image` 字段 → 卡片 62px 缩略图；双栏网格；待生效区折叠为 `<details>`；顶部四宫格数据磁贴；全站 emoji 强化 |
| 「今天过期那块白字看不清 / 建议放上面 / 磁贴可点 / 渠道标签分色」 | 修复 CSS 类名冲突（section 误用 `.alert` 红渐变+白字 → 改 `.sec.hot` 浅红底深红字）；💡今日建议移到磁贴之后；可用券/待生效磁贴改锚点链接（待生效点击自动展开折叠区）；到店=蓝、外送=绿标签 |

### 6️⃣ 命名修正与发布（2026-10-14 00:16 — 09:33）

- 黄工指出 `hero` 命名不对（管家应为 `butler`）→ 全库改名 `mcd-daily-coupon-butler`，目录、README 链接、定时任务路径全部同步
- 手写 SVG 宣传 banner（黄工撤回了 AI 生图以节省额度，改用零成本 SVG，中文文字精确），置于 README 顶部
- `git init` → 3 次提交 → SSH 推送至 `qufuhcj/mcd-daily-coupon-butler`（Public，main）

---

## 三、WorkBuddy 中实际使用的 MCP Tool 清单

| Tool | 是否实际调用 | 用途 |
|---|---|---|
| `available-coupons` | ✅ | 查询可领券列表与领取状态 |
| `auto-bind-coupons` | ✅ | **每日自动领券的核心能力**（幂等，定时任务天天跑无副作用） |
| `query-my-coupons` | ✅ | 卡包盘点（券名/用券价/有效期/渠道/券图 URL） |
| `query-my-account` | ✅ | 积分余额与本月、下月将过期积分预警 |
| `now-time-info` | 编排中使用 | 校时，作为券状态判定基准 |
| `query-nearby-stores` + `query-meals` | 可选增强 | 匹配餐品原价填 `originalPrice`（失败不阻塞） |
| `campaign-calendar` | 编排中使用 | 用户问"最近有什么活动"时查询 |
| `query-lottery-info` | 编排中使用（只读） | 积分抽奖监控，**明确不自动抽奖** |
| `create-order` / `cancel-order` / `draw-lottery` | ❌ 明确禁用 | 合规红线：管家只管资产，花钱与抽奖必须本人点头 |

---

## 四、WorkBuddy 协同产出的工程成果

1. **`scripts/coupon_ledger.py`**（约 600 行，纯标准库）
   券资产台账引擎：与历史台账 diff 识别新增券 → 按时间分类（可用/待生效/已过期）→ "今天就过期"置顶预警 → 折扣率与实惠度评级 → 积分过期预警 → 连续打卡统计 → 幂等入账 → 输出 Markdown/HTML 日报
2. **`SKILL.md`**：可被 MCP Client 直接加载的 Skill 编排指令（含合规红线与容错策略）
3. **`MCP_INTEGRATION.md`**：真实调用的 Tool 清单、调用时序图（mermaid）、两个来自真实数据的产品洞察、五项关键设计决策
4. **WorkBuddy 自动化任务**：每天 10:30 自动执行领券 → 盘点 → 生成 HTML 日报 → 推送给黄工，实现"领券这件事从待办里消失"

---

## 五、合规声明

- 本文档与仓库中**不包含**任何 MCP Token、密钥、账号凭证；配置文件仅使用 `${MCD_MCP_TOKEN}` 环境变量占位符。
- 项目为作者独立开发，非麦当劳官方产品；券与积分数据均为作者本人账号资产，日报与快照不入库。
- 本 Skill 只做资产盘点与提醒，不代客下单、不自动抽奖。
- 本文档为 WorkBuddy 开发过程记录，用于核验联动活动奖励资格。
