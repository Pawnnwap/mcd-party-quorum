# 状态机（人类视图）

> **单一事实源是 `../scripts/state_machine.py` 内的常量表**，本文件是它的渲染产物。
> `../scripts/run_scenarios.py` 的内建用例 `T-table-sync` 解析本文件的两张表格与脚本表
> 逐条比对，漂移即退出码 1。修改状态机请改脚本，再重新生成本文件的表格部分。
>
> 设计目的：把「严禁跳步」与「空态不产生下一跳」从提示词约束升级为**机器可裁决、
> 可断言**的结构约束。

## 1. 状态定义

| 状态 | 含义 | 允许调用的工具 |
|---|---|---|
| S0 IDLE | 意图未确认 | `now-time-info` |
| S1 DISCOVER | 活动雷达：拉活动列表并逐个取详情 | `mall-points-products`、`mall-product-detail`、`now-time-info` |
| S2 PICK_ACTIVITY | 已列出活动，等待用户选一个 | 无（空集） |
| S3 CITY_LISTED | 已选定活动，等待用户选城市 | `query-party-city` |
| S4 STORE_LISTED | 已选城市，等待用户选门店 | `query-party-store` |
| S5 DATE_LISTED | 已选门店，等待用户选日期 | `query-party-store-date` |
| S6 SESSION_LISTED | 已选日期，等待用户选场次 | `query-party-store-session` |
| S7 ASK_PARTY_TYPE | partyType 为 -1，必须反问包场 / 拼团 | 无（空集） |
| S8 QUORUM | 组局结算计算（纯脚本，无工具调用） | `now-time-info` |
| S9 CARD | 出组局卡 + 下单参数清单（终态） | 无（空集） |
| S10 DEGRADED | 降级终态：空态 / 不可预约 / 城市未开放 / 超上限 / 退避耗尽 / MCP 不可达 | 无（空集） |
| S11 REFUSED | 拒答终态：写操作 / 套利 / 营养 / 域外 / 闲聊 / 索要凭据 | 无（空集） |

**两条硬规则由上表直接成立：**

- **严禁跳步**：每个下钻状态的工具白名单只含本层那一个工具。在 S3 请求场次查询工具
  → 白名单不含 → Gate 拒绝 → 调用根本不发生（C5 的机器保证）。
- **空态不产生下一跳**：S10 DEGRADED 的白名单是空集，进入后任何工具请求都被拒绝，
  结构上不可能再产生任何一跳（C4 的机器保证）。

## 2. 转移表

| 从 | 事件 | 到 | 说明 |
|---|---|---|---|
| `S0` | `intent_matched` | `S1` | 命中组局 / 查场次 / 查规则 / 雷达 / 出卡意图 |
| `S0` | `off_topic` | `S11` | 13 类硬边界，走固定话术 |
| `S0` | `refuse_intent` | `S11` | 13 类硬边界，走固定话术 |
| `S1` | `activities_found` | `S2` | 存在 shopId==5 且 status==2 且在售的活动 |
| `S1` | `no_activity` | `S10` | C13：明确告知当前没有可预约的派对活动 |
| `S1` | `not_onsale` | `S10` | C13：活动已下架或已过下架时间 |
| `S1` | `mcp_unreachable` | `S10` | MCP 不可达 / 未配置 → 退出码 4 文案 + 降级 |
| `S2` | `user_selected_activity` | `S3` | 必须有显式用户选择 |
| `S3` | `cities_nonempty` | `S3` | 展示城市清单，不自动推进 |
| `S3` | `user_selected_city` | `S4` | 携带该城市的 code 与 latitude/longitude |
| `S3` | `cities_empty` | `S10` | C4：出降级卡，门店查询调用次数恒为 0 |
| `S3` | `user_city_not_in_open_list` | `S10` | C12：列出开放城市清单 + 引导，禁止编造门店 |
| `S4` | `stores_nonempty` | `S4` | 展示门店清单，不自动推进 |
| `S4` | `user_selected_store` | `S5` | 携带 storeCode |
| `S4` | `stores_empty` | `S10` | 空态熔断 |
| `S5` | `dates_nonempty` | `S5` | 展示日期清单，不自动推进 |
| `S5` | `user_selected_date` | `S6` | 携带 dateStr |
| `S5` | `dates_empty` | `S10` | 空态熔断 |
| `S6` | `sessions_nonempty` | `S6` | 展示场次卡片，不自动推进 |
| `S6` | `user_selected_session` | `S7` 或 `S8` | C9：partyType 为 -1 时经 S7 反问，1 / 2 直入 S8 |
| `S6` | `sessions_empty` | `S10` | 空态熔断 |
| `S7` | `user_answered_party_type` | `S8` | 用户拍板，脚本不得替选 |
| `S8` | `calc_ok` | `S9` | 出卡。不受死线影响：已过 advisory 死线也走这条，只追加标注句 |
| `S8` | `over_cap` | `S10` | C7：给可执行修正建议，不出组局卡（唯一允许阻断出卡的条件） |
| `S9` | `done` | `S0` | 终态输出到用户后复位 |
| `S10` | `user_restart` | `S0` | 用户重新发起 |
| `S11` | `user_restart` | `S0` | 用户重新发起 |
| `任意` | `rate_limited` | `S10` | C11：明确失败提示，不静默成功 |
| `任意` | `write_op_requested` | `S11` | B3：7 个写工具一律拒，调用次数恒为 0 |

### 2.1 守卫语义

- 带 `user_` 前缀的事件必须由**用户的显式选择**产生，不得由脚本或 LLM 代填。缺失该
  事件即不转移，状态原地停留并反问。
- 无事件的状态间跳转一律视为非法转移，`../scripts/state_machine.py` 返回退出码 2。
- 服务器时间工具不参与状态推进，只提供时间，允许于 S0 / S1 / S8。

### 2.2 倒计时恐慌禁令

死线是**官方文案派生值**。若允许它驱动状态转移，就等于换条路让文案派生值进入业务
逻辑通道。因此写死三条：

1. **非事件**：`deadline_passed`、`overdue`、`deadline_kind` 不是合法事件。转移表的事件
   取值是封闭枚举，收到这类值 → 退出码 2 并报「死线相关值禁止作为转移条件」。
2. **不阻断**：S8 不因超时转 S10。已过 advisory 死线仍正常出组局卡，卡面固定追加
   「已过官方文案建议的提前预订期，是否仍可预订请以预约页面为准」。禁止断言
   「你赶不上了」。
3. **唯一阻断条件**：S8 转 S10 只允许由 `over_cap` 触发，且该判定只基于接口返回的
   partyMax / leftNum（不是文案派生值）。

覆盖这条的回归用例：`T-overdue-still-cards`。

## 3. 空态判定（进入 S10 的触发条件）

`../scripts/party_fields.py --is-empty` 只认以下三种可枚举形态，命中任一即判空：

1. `data` 为 `[]`；
2. `data` 为 `null` 或字段缺失且接口未报错；
3. 接口返回文本型空提示（由字段字典登记的提示串集合匹配）。

**不属于空态的情况（必须显式报错，不得伪装成空态）：**

- fixtures 未命中 → 退出码 2；
- HTTP 429 未耗尽重试 → 走退避；
- MCP 未配置 / 不可达 → 退出码 4 + 明确文案。

区分的意义：空态是业务上的合法终态（降级卡），而上述三种是技术故障，必须让用户
知道是故障而不是「没活动」。

## 4. 与验收断言的映射

| 断言 | 状态机侧证据 |
|---|---|
| C1 | S1 只输出 shopId==5 的活动，shopId==2 条目不出现 |
| C4 | 城市层返回空 → 转 S10；账本断言门店查询调用次数 = 0 |
| C5 | 从 S3 直接请求场次查询 → Gate 拒绝；场次与写工具调用次数均为 0 |
| C6 | S8 输出 need；partyMin=5、confirmed=3 → 还差 2 人发车 |
| C7 | S8 经 over_cap 转 S10：出修正建议且不出组局卡 |
| C8 | S8 金额经 format_cny 出口；文本断言不含裸分数值 |
| C9 | partyType 为 -1 必须经 S7；1 / 2 不进 S7 |
| C10 | S8 死线 = 场次日期减 3 个自然日，时刻 23:59:59，标 advisory |
| C10-b | 已过死线仍走 S8 → S9 正常出卡，卡面含标注句，不出现「赶不上」 |
| C11 | 任意状态连续 3 次 429 后转 S10，且不再产出重试指令 |
| C12 | S3 城市不在开放集合 → S10 + 开放城市清单 |
| C13 | S1 活动 status 不为 2 或已过下架时间 → S10，不展示场次 |
| C14 | S9 下单参数 11 字段齐备，每字段来源为 api 或 user |
| B3 | 写操作请求 → S11；7 个写工具调用次数恒为 0 |

## 5. 实现约束

1. 状态表以 `../scripts/state_machine.py` 常量为准；本文件是渲染产物，由
   `T-table-sync` 用例防漂移。
2. Gate 是唯一的调用入口：任何 MCP 调用必须先取票据，`../scripts/mcp_client.py`
   拒绝无票据的调用。
3. 账本是权威事实：C 类用例在 MockTransport 下由 `../scripts/run_scenarios.py`
   在进程内执行，账本覆盖全部调用，断言「调用次数为 0」可信。
4. 状态推进所需的全部字段（spuId / 城市 code / storeCode / dateStr / 场次 id）
   只能来自上一跳接口返回或用户显式选择，不得由任何环节编造。
