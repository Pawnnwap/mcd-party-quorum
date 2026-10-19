# 字段字典

> 逐字段登记：来源接口 / 类型 / 单位 / 用途 / **禁止用途**。
> 凡是标「仅展示」的字段，一律不得进入任何计算、校验或分支判断 —— 这不是建议，
> 是麦当劳 MCP 工具字段说明里的明文约束，评审会逐字核对。

## 1. 金额字段：同名不同单位（差 100 倍）

| 来源接口 | 字段路径 | 类型 | 单位 | 用途 | 禁止用途 |
|---|---|---|---|---|---|
| `mall-points-products` | `data[].price` | string | **元** | 活动列表价格展示 | 禁止直接当分使用 |
| `mall-product-detail` | `skuList[].price` | string | **元** | 详情价格展示 | 禁止直接当分使用 |
| `query-party-store-session` | `data[].price` | int | **分** | 场次金额的**唯一计算来源** | 禁止裸显给用户 |

**裁决**：内部一律以**分（int）**存储与计算，渲染出口只有
`../scripts/money.py` 的 `format_cny()`。来源单位由 `SOURCE_UNIT` 表按**字段路径**
查表决定，**未知来源直接退出码 2，禁止默认猜测单位**。
出卡前再跑一次裸值守卫（发现裸分数值即拒绝出卡）。

同一条链路上这两个值必然同时出现：活动列表说「59」，场次接口说「5900」。它们相等，
单位差 100 倍。任何绕过 `../scripts/money.py` 的手写换算都是缺陷。

## 2. 时间字段：不同接口字段名不一致

| 来源接口 | 文档声称 | 实际返回 | 处理 |
|---|---|---|---|
| `mall-points-products` | upTime 与 downTime | upTime 与 downTime | 正常读 |
| `mall-product-detail` | upTime 与 downTime | **upDate 与 downDate** | 以实际返回为准 |

这是实调抓出来的坑：详情接口的**接口文档结构里写的是 upTime 与 downTime，实际响应
返回的是 upDate 与 downDate**，文档与响应不一致。

**裁决**：`../scripts/party_fields.py --sale-window` 做**双读** —— 先读 upDate 与
downDate，缺失时回退 upTime 与 downTime；**两组都缺就退出码 2 并报缺哪个字段，
不猜、不默认在售**。附带：下架时间实测为 `23:49:59` 而非 `23:59:59`，因此上下架
时间一律以接口返回为准，示例值不作断言基准。

所有涉及时间的判定必须传 `--now`，且 `--now` 取自 `now-time-info` 的服务器时间，
**禁用本地时间**。

## 3. 派对专属字段：只展示，不校验

| 字段 | 类型 | 含义 | 用途 | 禁止用途（官方原文约束） |
|---|---|---|---|---|
| `partyPeople` | string | 人数区间文案，形如 5-12 | **仅展示** | 「不得从中提取数值用于 AI 前置校验」 |
| `partyRange` | int | 派对时长，半小时为单位 | **仅透传展示** | 不与文案的「约 90 分钟」自行换算后混用 |
| `spuLimit` | object | 限购信息 | **仅展示** | 「不得用于下单前的前置校验」 |
| `note` | string | 购买须知富文本 | **完整原文展示** | 「严禁从中提取任何数值用于下单校验、价格计算、库存判断等业务逻辑」 |
| `partyAge` | string | 年龄门槛文案 | 仅展示 | 不作为资格判定（以接口返回为准） |

**人数判定只用 `query-party-store-session` 返回的整型 `partyMin`、`partyMax`、
`leftNum`。** `partyPeople` 解析后恒带 `display_only` 标记，写不进校验通道。

## 4. 各工具可用字段白名单

只引用下表列出的字段，不得假设任何未列出的字段存在。

| 工具 | 入参 | 允许引用的返回字段 |
|---|---|---|
| `mall-points-products` | 不传 `catRuleIds` | spuId、spuName、catName、shopId、point、price、selling、spuImage、status、upTime、downTime |
| `mall-product-detail` | spuId | spuId、spuName、shopId、partyType、partyAge、partyPeople、partyRange、selling、detail、note、images、upDate、downDate、skuList |
| `query-party-city` | spuId | code、name、initial、latitude、longitude、sortNo |
| `query-party-store` | code、latitude、longitude、spuId | code、name、shortName、address、cityCode、cityName、distance、distanceText、businessStatus、latitude、longitude |
| `query-party-store-date` | spuId、storeCode | date、spuId、storeCode |
| `query-party-store-session` | spuId、storeCode、dateStr | id、timeStart、timeEnd、price、leftNum、partyMin、partyMax |
| `now-time-info` | 无 | formatted、date、year、month、day、dayOfWeek、timezone、timestamp |

`shopId` 路由：仅处理 **shopId == 5**（派对 / 体验类，走现金）。shopId == 2 是纯积分
兑换类，本技能不处理并明确说明。

## 5. 空态判定枚举

`../scripts/party_fields.py --is-empty` 只认三种形态：

1. `data` 为空数组；
2. `data` 为 null 或字段缺失且接口未报错；
3. 接口返回登记过的文本型空提示（暂无 / 无数据 / 没有找到 / 暂未开放 / 无可用 /
   未查询到 / 暂无可预约 / 尚未开放 / 没有可预约）。

**不属于空态**：fixtures 未命中 → 退出码 2；429 未耗尽重试 → 走退避；MCP 不可达 →
退出码 4。空态是业务上的合法终态（降级卡），技术故障必须让用户知道是故障。

## 6. 派生值命名约定（结构性护栏）

| 前缀 | 产地 | 允许去向 | 禁止去向 |
|---|---|---|---|
| `note_display_` | `../scripts/note_locate.py` | 展示层（对照表） | 金额层、结算层、下单参数层、状态机层 |
| `deadline_` / `overdue` | `../scripts/quorum_calc.py` | 展示层（卡面） | 任何 gate、状态转移、分支判断 |

四个入口（`../scripts/money.py`、`../scripts/quorum_calc.py`、
`../scripts/order_params.py`、`../scripts/state_machine.py`）都有**参数名守卫**：
入参出现上述前缀立即退出码 2。这是结构性保证，不是靠自觉遵守。
