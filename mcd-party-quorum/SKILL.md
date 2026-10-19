---
name: mcd-party-quorum
description: 麦当劳派对与主题活动组局助手：查开放城市、门店、可约日期与场次余位，算还差几人发车、人均多少钱、最晚何时必须定，并把官方活动文案与实时接口数据并列对照标出冲突，产出可直接转发拉人的组局卡与下单参数清单。全程只读，不代下单、不查券、不做营养与热量建议。当用户说「麦当劳派对还差几个人」「我们组 8 人想去」「这家店 25 号还有场吗」「最晚什么时候必须定」「官方说的日期和你能约的日期怎么不一样」时使用。
description_zh: 麦麦组局官——把「想去麦当劳派对但凑不够人」变成一张能直接甩进技术群里的组局卡：查清城市、门店、可约日期与场次余位，算清发车人数、人均预算与预订死线，并把官方文案与实时数据的冲突明确标注出来。全程只读，不代下单。
description_en: Rally-card builder for McDonald's party events. Discovers open cities and stores, drills down bookable dates and session slots, then computes how many more people are needed to start a session, the per-person budget and the booking deadline. Puts official campaign copy side by side with live API data and flags conflicts. Outputs a shareable rally card plus a ready-to-run order-parameter list. Read-only by design - it never places orders, never handles coupons, never gives nutrition advice.
version: 1.0.0
author: Pawnnwap
---

# 麦麦组局官

把「麦当劳派对我想去，但凑不够 5 个人」变成一张能直接甩进技术群里的组局卡：查清哪些
城市有、哪家店近、哪天还有场、还差几个人发车、人均多少钱、最晚什么时候必须定，并把
官方文案与实时接口数据的冲突明确标出来。

全程只读：止步于「算清楚 + 出组局卡 + 给下单参数清单」，不代下单。

## 触发条件

### 该用

- 组局凑人：「我想叫几个人去，还差几个？」「我们组 8 个人想去。」
- 查城市与门店：「杭州有没有？哪家店最近？」
- 查日期与场次：「这家店 25 号还有场吗？余位多少？」
- 问死线：「最晚什么时候必须定？」
- 问冲突：「官方说 23 到 25 号，你怎么查出来 26 号也能约？」
- 活动雷达：「麦当劳最近还有什么能参加的活动？」
- 出卡：「帮我出一张能发群里的组局卡。」

### 不该用（逐条硬边界，动作固定）

| 用户输入 | 动作 |
|---|---|
| 帮我下单 / 直接帮我订 / 帮我报名 | 拒绝代执行，改出下单参数清单 |
| 取消订单 / 查我的订单 | 拒答，订单类不在范围 |
| 帮我领券 / 怎么最省钱 | 拒答，不做券运营 |
| 哪个套餐最划算 / 配一顿 30 块 | 拒答，不做通用点餐 |
| 多少卡路里 / 我想减肥 | 拒答，不做营养与热量建议 |
| 帮我凑人数（假报名）/ 批量抢 | 明确拒绝，属违规套利 |
| 城市不在开放集合内 | 列开放城市清单 + 引导，禁止编造门店 |
| 未成年表示要参加 | 提示官方限定 18 岁以上 |
| 索要会员号 / 券码 / 手机号 | 拒答，不回显任何凭据字段 |
| 询问竞品或非麦当劳活动 | 拒答，超出范围 |
| 活动已下架 / 各层返回空 | 出降级卡，不得伪造场次 |
| MCP 未配置 / 不可达 | 明确报错，不编造数据 |
| 纯闲聊 | 不触发 |

每条的固定话术与「必须包含的信息项」见 `references/fallback-phrases.md`。

## 工作流程

### 第 1 步 活动雷达

调 `mall-points-products`（不传 `catRuleIds`）取全量，用
`scripts/party_fields.py --filter-products` 过滤出 `shopId == 5` 且 `status == 2`
且在售窗内的活动，再逐个取详情，列出活动卡请用户选一个。
**禁止硬编码任何 spuId** —— 活动下架后技能必须仍能列出其他活动或优雅降级。

### 第 2 步 官方须知原文 + 实时数据并列

`note` 去 HTML 后**完整原样输出**（一字不改），用 `scripts/note_locate.py` 定位条目，
与接口实时值并列，冲突标 ⚠️，并附固定免责句。
**note 派生的数值不得进入任何计算或校验通道**，详见 `references/note-rules-sample.md`。

### 第 3 步 四层下钻（严禁跳步）

city → store → date → session，每层展示并**等待用户明确选择**。
每次调用前必须先取票据：`scripts/mcp_client.py --gate <工具> --state <状态>`；
任一层返回空立即转降级态，**不再发起下一跳**。

### 第 4 步 组局结算

`scripts/quorum_calc.py` 计算发车进度、人数上限、人均与总计、预订死线（advisory）。
`partyType == -1` 必须反问用户包场还是拼团，**不得替用户选**。

### 第 5 步 出卡与下单参数清单

组局卡 + 11 字段下单参数清单 + 「本技能不会代你执行」提示。
`scripts/order_params.py` 做 schema 对齐校验，`scripts/sanitize.py` 在出卡前过滤
敏感字段。模板见 `references/party-card-template.md`。

## 只读护栏

- 7 个写工具（`party-order-create`、`create-order`、`mall-create-order`、`draw-lottery`、
  `cancel-order`、`delivery-create-address`、`auto-bind-coupons`）调用次数**恒为 0**；
  写工具请求由 `scripts/mcp_client.py` 在 Gate 层无条件拦截（退出码 3）。
- 实时值不跨会话缓存；会员号、券码、手机号与任何凭据不落盘、不回显（CLI 的调用账本只记录工具名与次数，落在系统临时目录，不含任何用户数据）。
- 脚本不持有凭据、不发起 HTTP 调用；429 由脚本产出确定性退避策略，重试动作由你执行。

## 输出规范

- **组局卡**：Markdown，可直接复制转发，格式见 `references/party-card-template.md`。
- **下单参数清单**：11 字段，每字段标注来源为 `api` 或 `user`。
- **降级卡**：空态 / 不可预约 / 城市未开放 / 人数超上限 / MCP 不可达，各自对应固定文案。
- 卡内所有数值由脚本注入，模型只填描述性文字且不得含数字。

## 脚本与状态机

| 脚本 | 用途 |
|---|---|
| `scripts/mcp_client.py` | 调用层：只读白名单 / 写工具黑名单 / 票据 / 账本 / 429 策略 |
| `scripts/state_machine.py` | 状态转移裁决与每状态工具白名单（状态表单一事实源） |
| `scripts/money.py` | 金额规范化（元与分互转）、唯一渲染出口、裸值守卫 |
| `scripts/party_fields.py` | 字段抽取与过滤：活动雷达 / 在售窗 / 空态 / 城市集合 |
| `scripts/note_locate.py` | 官方须知原文定位与并列展示 |
| `scripts/quorum_calc.py` | 发车进度 / 人数上限 / 预算 / 死线 |
| `scripts/order_params.py` | 下单参数装配与 schema 对齐校验 |
| `scripts/sanitize.py` | 输出前敏感字段过滤 |
| `scripts/run_scenarios.py` | 场景回归执行器（MockTransport 端到端 + 断言） |

状态表以 `scripts/state_machine.py` 为单一事实源，人类视图见
`references/state-machine.md`，二者由 `T-table-sync` 用例防漂移。
路径约定：所有脚本以自身位置解析技能根与 fixtures，**不依赖调用时的工作目录**；
加 `--json` 得结构化结果，每个脚本都有 `--selftest`。
退出码：0 正常 / 1 断言失败 / 2 入参错误 / 3 写操作被拦截 / 4 不可达或退避耗尽。

## 脚本速查

命令以技能目录为基准，路径不依赖调用时的工作目录。

```
python scripts/mcp_client.py --gate query-party-city --state S3
python scripts/party_fields.py --filter-products --file <响应文件> --now "2026-10-18 23:07:42"
python scripts/note_locate.py --crosscheck --note <详情响应> --api <接口快照>
python scripts/quorum_calc.py --party-min 5 --party-max 12 --left-num 12 --confirmed 3 \
  --price-fen 5900 --date 2026-10-23 --now "2026-10-18 23:07:42"
python scripts/order_params.py --from <参数文件> --schema <schema 文件>
python scripts/sanitize.py --scan-text "<待出卡文本>"
python scripts/run_scenarios.py --all
```

每个脚本都支持 `--selftest`（不联网、不读 fixtures 也能跑）与 `--json`（结构化输出）。
示例中的 `--price-fen 5900` 是内部单位「分」，只存在于脚本边界内，不会出现在卡面上。

## 注意事项

### 字段坑

- **金额单位**：活动列表与详情的 `price` 是**元**（字符串），场次的 `price` 是**分**
  （整数），差 100 倍。内部一律用分，出口只有 `format_cny`，**禁止裸显分数值**。
- **时间字段名**：列表接口返回 upTime 与 downTime，详情接口**实际返回 upDate 与
  downDate**（它自己的接口文档还写错了）。抽取函数双读并回退，两组都缺就报错，不猜。
- **只展示字段**：`partyPeople`、`partyRange`、`spuLimit`、`note` 一律只展示。
  人数判定只用场次接口返回的整型 `partyMin`、`partyMax`、`leftNum`。
- 字段的单位、来源与禁止用途逐条见 `references/field-dictionary.md`。

### 常见错误

- 跳步：未选门店就查场次 —— 会被 Gate 拒绝，先补齐上一层。
- 替用户决策：选哪个活动、哪家店、哪天、哪场、包场还是拼团，一律用户拍板。
- 把官方文案当校验依据：文案只能原文展示与并列对照，不能派生校验值。
- 裸显分数值、编造 spuId 或 skuId、猜测金额单位、用本地时间算死线。
- 因「已过提示性死线」而拒绝出卡 —— 禁止。死线只是提示，过期仍正常出卡并加标注句。
