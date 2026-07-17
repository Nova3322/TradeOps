# OMS、Freqtrade 与 VenueAdapter 执行规范

> 文档编号：EXEC-SPEC-001
> 版本：Draft 0.1
> 日期：2026-07-18
> 状态：产品与工程设计基线；不代表任何真实交易能力已经认证
> 上位文档：`交易系统总体方案.md`、`策略合同与数值化验收门.md`
> 适用范围：Trading OMS、Binance / Hyperliquid VenueAdapter、受控 Freqtrade worker、Margin Controller，以及未来 VenueAdapter

---

## 1. 目的与效力

本文把上位文档中的执行原则转化为可实现、可测试、可认证的执行合同，回答：

1. Trading、OMS、VenueAdapter、Freqtrade、Margin Controller 与交易所各自拥有什么权力。
2. 初仓、Add、减仓、保护和退出如何从业务意图走到真实成交。
3. 部分成交、超时、结果未知、进程重启和双主如何处理。
4. 新交易场所如何接入，而不复制另一套策略、风控或授权中心。

本文不选择具体进程协议、消息中间件或部署产品；Freqtrade 的受控接口形态由 `DEC-EXEC-001` 冻结。未通过执行认证的能力必须关闭，不能因接口存在、模拟成功或文档写明而视为可用。

---

## 2. 不可变执行边界

### 2.1 唯一订单发送链

受管账户的日常订单只能沿以下链路产生：

**冻结提案 → 人工初仓批准 → Risk Engine 发送前复核 → Reserved Heat → Trading OMS OrderIntent → VenueAdapter → 绑定的 Freqtrade worker → 交易所 → 私有事实对账**

固定规则：

- Trading 是唯一能够决定“是否允许形成 OrderIntent”的系统。
- Risk Engine 对人工、策略、辅助建议源、Web、Telegram、OMS 和执行器均拥有最终否决权。辅助建议源不得持有密钥，不得签名、下单或覆盖风控。
- Freqtrade 是受控订单执行后端，不是策略所有者、审核主体或风险中心。
- VenueAdapter 只映射场所规则、校验能力、标准化订单并归一化回执，不重新判断交易是否值得做。
- Web / PWA、Telegram、FreqUI、Freqtrade REST、脚本和人工交易所界面均不得成为日常增险入口。
- 交易所私有接口是余额、真实仓位、真实挂单、成交与费用的最终事实源；Trading 单一事务写模型是授权、OrderIntent、幂等身份、风险预留和状态迁移的内部事实源，审计账本随迁移原子追加。
- 一个场所故障不得自动换所、补单、对冲或接管未知订单；换所必须形成新提案。

### 2.2 初仓、加仓与减险授权

- SYSTEM 与 MANUAL 初仓都必须人工明确批准。
- 初仓批准同时可以签发有限 Add 授权包；自动加仓默认关闭。
- 启用后，低 / 中 / 高风险最多分别有 1 / 2 / 3 个 Add 单元；每次 Add 的数量按当时目标杠杆差额计算，而非固定数量或风险比例。
- 每次 Add 仍须使用最新组合 MTM 权益、真实仓位和市场事实通过全部硬门，并占用 Reserved Heat。
- 任意非零真实成交完整消费该 Add；终态确认零成交不消费；结果未知时冻结该 Add 与后续增险。
- 动态去杠杆、硬止损、风险减仓和退出不需要再次人工批准，必须为 `reduce-only` 或场所认证的等价减险语义。
- 系统只能自动收紧授权，不能自动提高档位、增加次数、放宽止损或扩大价格容忍。

### 2.3 交易与资金职责分离

- Freqtrade 与交易 OMS 不执行提款、充值、Vault 释放、Bridge 或资金归集。
- 场内逐仓保证金增加 / 移除由逐场所认证的 Margin Controller 执行。
- Vault 与交易所之间的资金划转由 Capital Transfer Orchestrator（CTO）执行。
- 交易凭证、场内保证金凭证与资金划转凭证分离，任何一套凭证不得因方便而扩展到另一职责域。

---

## 3. 组件责任矩阵

| 组件 | 可以做 | 不可以做 | 权威输出 |
| --- | --- | --- | --- |
| Proposal / Approval | 冻结提案、记录人工批准或拒绝、签发有限授权 | 直接下单、覆盖 Risk Engine | 提案版本、授权边界、审核身份与时效 |
| Risk Engine | 定仓、复核最坏损失、占用 / 释放 Heat、收紧系统状态 | 发送订单、提款、修改人工批准边界 | 风险决定、预算快照、拒绝原因 |
| Campaign Manager（含 `TargetPositionArbiter` 纯规则） | 合并止损、趋势退出、去杠杆和系统减仓，得出唯一目标仓位 | `TargetPositionArbiter` 不单独持久化、不设服务或密钥，不产生反向仓或扩大授权 | Campaign 中的目标仓位、紧迫度与原因 |
| Trading OMS | 创建一次性 OrderIntent、维护订单状态、协调复合意图与对账 | 自行改变策略、风险、场所或授权 | OrderIntent、幂等身份、预期状态 |
| VenueAdapter | 读取规则、映射订单、验证能力、标准化回执与查询 | 接受旁路增险、静默改价 / 改量 / 换所 | 场所请求、规范化回执、能力快照 |
| Freqtrade worker | 在绑定账户上执行 Trading 签发的订单并回传状态 | 自主开仓、position adjustment、ROI / trailing 竞争退出、提款 | 执行回执与本地协调状态 |
| Margin Controller | 执行已认证的场内保证金复合意图 | 改变净仓位、调用 Vault、放宽止损 | 保证金动作回执与原生事实 |
| CTO | 消费独立资金授权并完成源端到目的端对账 | 发送交易订单、救援活动仓位 | `capital_transfer_id`、在途与结算事实 |
| Reconciler | 读取交易所私有事实、检测差异、恢复内部投影 | 推测未知成交、直接扩大风险 | 对账快照、差异与恢复结论 |
| 交易所 | 接受订单、维护真实仓位与成交 | 证明内部批准或风险政策 | 余额、仓位、订单、成交、费用事实 |

---

## 4. 核心执行对象

### 4.1 OrderIntent

系统只有一种内部订单意图对象 `OrderIntent`，不设第二类意图对象或并行身份。每个 OrderIntent 至少绑定：

- 全局唯一 `order_intent_id`，创建后永不复用。
- `proposal_id / proposal_version_id / authorization_id`；Add 订单还绑定 `add_unit_id`。
- `strategy_owner / campaign_id`。
- `venue / execution_domain / account_id / worker_id`。
- `instrument_id / side / position_side / reduce_only`。
- 当前真实数量、目标数量、最大允许数量与数量来源。
- 订单类型、触发价、限价、最大滑点、TIF、有效期。
- 风险快照、Reserved Heat、保护要求与最大最坏损失。
- `margin_mode / margin_capability / collateral_scope / collateral_pool_id`。
- 策略、风险、数据、成本、VenueAdapter、Freqtrade 与能力证书版本。
- 创建者、审批主体、系统风险状态、决策时间与事实新鲜度。

OrderIntent 是不可变业务快照。执行中产生的场所订单、替换、撤销和部分成交均作为其子记录，不得回写修改原始边界。

### 4.2 场所订单身份

每个场所订单必须关联：

- `order_intent_id`。
- 永不复用的客户端订单身份。
- 交易所订单 ID；未知时保留查询键和发送证据。
- 尝试序号；只表示同一意图内的可证明安全动作，不代表可以盲目重试。
- 请求摘要、响应摘要、发送前后时间与 worker fencing token。

客户端身份在同一 `venue × execution_domain × account` 内唯一。交易所不提供强幂等时，OMS 仍须以持久化身份、单发送者 fencing、发送前后查询和最坏 Unknown Heat 防止重复。

### 4.3 执行回执

回执至少区分：

- 请求是否被本地接受。
- 请求是否送达执行 worker。
- 交易所是否确认接收。
- 订单是否存在、当前状态和剩余数量。
- 已确认真实成交数量、均价、费用和成交时间。
- 真实仓位、保护覆盖和风险迁移是否完成对账。

“HTTP 成功”“worker 已接收”“订单已创建”均不等于成交，更不等于仓位生命周期已完成。

---

## 5. OrderIntent 权威状态机

以下是 OrderIntent 的唯一权威状态语义；交易所原生状态映射为这些状态，不再建立第二套内部订单意图状态机：

| 状态 | 含义 | 允许的下一步 |
| --- | --- | --- |
| `INTENT_CREATED` | 意图与风险预留已持久化，尚未发送 | 发送、取消 |
| `DISPATCHING` | 已取得 fencing，正在交给 worker | 查询、进入 Unknown |
| `VENUE_ACKNOWLEDGED` | 场所确认订单存在 | 等待成交、撤销、查询 |
| `PARTIALLY_FILLED` | 有非零成交且仍有剩余 | 补足同一意图内保护、撤销剩余、继续查询 |
| `FILLED` | 场所订单数量已成交完 | 仓位与保护对账 |
| `CANCEL_PENDING` | 撤销已发出，结果未确认 | 查询；不得释放风险 |
| `CANCELLED_ZERO_FILL` | 终态且确认零成交 | 释放 Reserved Heat；Add 可恢复可用 |
| `CANCELLED_PARTIAL` | 终态且存在真实成交 | 已成交部分转 Open Heat；Add 已消费 |
| `REJECTED_ZERO_FILL` | 场所拒绝且确认零成交 | 释放 Reserved Heat |
| `RESULT_UNKNOWN` | 无法证明订单不存在或已结束 | 转 Unknown Heat、冻结增险、只查询原意图 |
| `POSITION_RECONCILED` | 真实仓位与成交已匹配 | 建立 / 调整保护或完成减仓 |
| `PROTECTION_CONFIRMED` | 保护覆盖真实仓位并获场所确认 | 完成初仓 / Add 生命周期 |
| `COMPLETED` | 意图、仓位、保护与风险迁移全部完成 | 仅审计与财务结算 |
| `FAILED_SAFE` | 终态保持同等或更小风险 | 审计、人工处置或新意图 |

禁止把 `RESULT_UNKNOWN` 直接改为 `REJECTED_ZERO_FILL`。只能由交易所订单、成交和仓位事实证明零成交或真实结果后迁移。

---

## 6. 各类意图的执行合同

### 6.1 初仓

发送前必须同时成立：提案与人工批准仍有效、价格与时效未越界、账户事实新鲜、资金信封已由已结算运营资金承载、风险预算合格、Reserved Heat 已占用、无未知订单、Venue / worker / 账户 / 证书一致。

初仓流程：

1. 持久化意图和风险预留。
2. 由绑定 VenueAdapter 重新读取精度、最小名义、杠杆档位、盘口和账户状态。
3. 任一事实改变使提案越界时，执行失败并生成明确拒绝；不得静默缩量后继续。用户请求本身超限时也必须回到提案层处理。
4. Freqtrade worker 发送单次受控订单。
5. 任意真实成交立即形成真实仓位并触发保护覆盖。
6. 无法在证书窗口内形成足额保护时，停止增险并受控减仓或退出。
7. 只有真实仓位、保护、费用与风险迁移完成对账后，初仓才完成。

### 6.2 自动 Add

Add 只消费开仓时预授权的一次性单元。发送前除初仓公共门外，还必须证明：对应里程碑已解锁、当前冻结收益率仍达到门槛、真实净浮盈为正、`L_effective < L_min`、布林回调合格、趋势有效、原保护已上移、上次意图已完成、系统为 NORMAL。

数量语义固定为：

- 以当时 `E_campaign`、`P_executable` 与冻结 `L_target` 计算目标数量差。
- 最终数量取目标差额、原授权、动态风险、Funding Envelope、保证金、清算缓冲、流动性和场所规则允许值中的最小安全值。
- 不设置 Add-1 / Add-2 / Add-3 固定手数或比例。
- 安全缩量可以导致未达到目标杠杆；只要非零成交，仍完整消费本 Add，不再补足。
- 零成交且终态已确认时才可恢复单元为可用；新行情必须重新形成新候选和新意图。

### 6.3 动态去杠杆、风险减仓与退出

- 目标仓位由 Campaign 内的纯规则模块 `TargetPositionArbiter` 统一产生；多个退出原因同时存在时选择更小目标和更高紧迫度。该模块只返回决策值，不持久化、不调用 OMS 或交易所。
- 必须使用 `reduce-only` 或经过认证、可证明不会反向开仓的等价语义。
- 不需要人工再次批准，不消费或返还 Add。
- 结果未知时不重复发送；真实风险在对账前不释放。
- 平仓请求成功不等于退出完成；真实仓位为零、残余订单处理完毕且风险迁移完成后，才算风险暴露关闭。

### 6.4 保护订单

- 初仓与每次 Add 的任意非零成交都必须纳入保护数量。
- 保护以交易所真实仓位为准，不能按计划数量假定。
- 替换保护时不得留下超出证书窗口的无保护缺口。
- 原生保护的触发价、订单类型、限价穿透和 reduce-only 语义须逐场所认证。
- 保护缺失或数量不足时停止所有新增风险；无法恢复则受控退出。

---

## 7. 部分成交、撤销与结果未知

### 7.1 Heat 守恒

- 发送增险前，最坏允许成交量进入 Reserved Heat。
- 已确认成交部分原子地从 Reserved 转为 Open Heat。
- 已知仍在途部分保留 Reserved Heat。
- 撤销 / 拒绝且确认未成交的部分才可释放。
- 无法确定结果的部分转 Unknown Heat；不得同时重复计入 Reserved，也不得漏记。

### 7.2 Add 消费

- 任意非零真实成交永久消费当前 Add。
- 部分成交的继续查询、撤销、保护和收尾属于同一个 OrderIntent，不能新建第二个 Add 补足。
- 结果未知时 Add 保持锁定，后续 Add 与同仓增险全部冻结。
- 只有终态与交易所事实共同证明零成交，Add 才恢复可用。

### 7.3 安全重试

仅在以下条件同时成立时，才可执行同一意图内的技术重发或替代动作：

- 能证明原请求没有产生订单或成交，或场所支持并已验证强幂等。
- 仍在原提案的价格、数量、时效和风险边界内。
- 使用同一 OrderIntent 关联的新子尝试身份。
- Risk Engine 与对账器重新确认 Reserved / Unknown Heat 不会双计或漏记。

否则只能查询、撤销、减仓或转人工异常处理。

---

## 8. Freqtrade 受控执行合同

### 8.1 worker 隔离

- 一个 bot / config 只绑定一个 `venue × execution_domain × account × margin_mode × collateral_pool_id`。
- 一个杠杆账户同一时刻只允许一个活动订单发送者。
- Binance 与 Hyperliquid 不共享 worker、数据库、凭据、订单身份或故障域。
- Hyperliquid Core 与每个获批 HIP-3 DEX 是独立执行认证域。
- cross 与 isolated 使用独立账户 / 子账户、worker、配置与证书。
- worker 重启后默认只对账；Trading 重新下发当前系统状态前不得增险。

具体账户、子账户和 position mode 由 `DEC-EXEC-002` 冻结；外部人工交易与既有场外仓位的处理政策由 `DEC-EXEC-003` 冻结。

### 8.2 必须关闭或隔离的能力

- Freqtrade 自主开仓信号。
- position adjustment / 自动加码。
- 与 Trading 竞争的 ROI、trailing stop 或策略回调退出。
- 面向用户的 FreqUI / REST / Telegram 增险操作。
- 结果未知时自动重放旧意图。
- 自动切换交易所或账户。

### 8.3 控制接口最低要求

无论 `DEC-EXEC-001` 最终选择何种受控接口，都必须满足：

- 双向身份认证、最小网络暴露和动作级授权。
- 一次性 OrderIntent、幂等身份与 fencing token 可传递和验证。
- Trading 可以查询 worker 健康、请求、订单与回执，但 worker 不能签发业务授权。
- 接口超时不被解释为失败；进入查询与 Unknown 流程。
- 所有请求和回执进入不可变审计链。
- 控制面不可用不阻断交易所原生保护和预认证的只减仓故障路径。

---

## 9. Margin Controller 与复合意图

### 9.1 能力分类

| 类别 | 订单行为 | 保证金行为 |
| --- | --- | --- |
| `CROSS_SHARED` | 直接执行风险核准的净增 / 减仓 | 不做仓位级保证金移出；共享池按真实事实管理 |
| `ISOLATED_REMOVABLE` | 先完成净仓位、成交、保护对账 | 之后才可执行已认证的 excess-margin normalization 或减仓后回加 |
| `STRICT_ISOLATED` | 只有真实可用抵押与缓冲充足时才增仓 | 禁止主动移出；减仓后仅在已认证 top-up 能力内回加 |

### 9.2 逐仓复合意图

`POST_REDUCTION_MARGIN_NORMALIZATION` 的顺序固定为：

1. 冻结当前仓位、目标仓位、目标保证金缓冲、最坏滑点与复合意图身份。
2. 取得同一账户、仓位和抵押池的 fencing。
3. Freqtrade 先执行 `reduce-only` 减仓。
4. 对账真实成交、剩余仓位、保护和实际释放保证金。
5. 将合同允许使用的本次释放额预留给该复合意图。
6. Margin Controller 在发送前重新核验费用、其他仓位、挂单、可用抵押与证书。
7. 只把已确认、已预留且不突破缓冲的场内保证金回加到同一剩余仓位。
8. 保证金腿失败时保持更小风险终态，或继续减仓 / 退出；不得反向增仓追赶目标。

净增仓后的 excess-margin normalization 同样必须在订单与保护完成对账后进行。`unrealized_pnl`、`max_removable_margin`、`released_margin_after_reduction` 与 `free_collateral_after_reconcile` 是不同事实，不能互相替代。

逐仓 normalization 的目标缓冲、最大回加额、可移除计算、并发和证书参数由 `DEC-EXEC-006` 与 `DEC-RISK-010` 冻结；在此之前能力状态为关闭或仅影子计算。

---

## 10. 对账、重启与双主防护

### 10.1 对账输入

- 交易所真实余额与保证金权益。
- 真实仓位、position mode、margin mode 与杠杆。
- 所有活动、条件、保护和近期终态订单。
- 成交、费用、资金费与可查询历史窗口。
- Trading OrderIntent、Freqtrade 本地状态、Heat 和保护预期。

### 10.2 重启顺序

1. 进入 `NO_NEW_RISK / RECONCILING`。
2. 获取 fencing，确认不存在第二活动发送者。
3. 拉取场所事实并与 Trading 事件账本比较。
4. 解决未知订单、残余仓位、保护缺口与 Heat 差异。
5. 必要时只减仓或退出；不得重放历史订单请求。
6. 记录对账证据与差异关闭方式。
7. 满足恢复政策并由 Trading 明确解锁后，才允许新增风险。

### 10.3 双主

任何两个 worker 同时拥有发送能力都属于 P0 安全事件。fencing token 失效、租约不明、时间漂移或控制面分区时，所有增险必须失败关闭；保护、减仓与退出只能由已认证的单一接管者执行。

---

## 11. Venue Capability Contract

任何 VenueAdapter 必须提供版本化能力合同，至少包括：

- 场所、执行域、账户、司法辖区与凭证权限。
- instrument 身份、合约乘数、tick / lot、最小数量 / 名义、报价 / 抵押 / 结算资产。
- 交易时段、停牌、到期、换月、公司行动、预言机与价格陈旧规则。
- 净仓 / 双向仓、cross / isolated、抵押共享范围与保证金调整能力。
- 市价、限价、条件单、TIF、reduce-only、客户端身份和查询能力。
- 原生保护、触发价格、保护替换与极端穿透行为。
- 部分成交、撤单失败、拒单、限速、断线、结果未知和恢复语义。
- 余额、仓位、订单、成交、费用、融资 / 资金费与强平事实源。
- 允许的订单与风险容量、性能和监控门。

能力状态只能是：

- `CERTIFIED`：证据完整，允许在证书范围内使用。
- `VALIDATING`：正在影子、模拟或小额验证，不能扩展到未通过范围。
- `NOT_SUPPORTED`：场所不提供或系统明确不支持。
- `UNKNOWN`：事实缺失或已漂移，按不支持处理并禁止增险。
- `EXPIRED`：版本或账户事实变化导致证书失效。

技术接口存在只说明“待验证”，不能自动成为 `CERTIFIED`。

---

## 12. 未来交易场所扩展

当前完整产品的交易接入范围同时覆盖 Binance 与 Hyperliquid。CFD、股票或其他衍生品平台只在有明确业务需求时新增 VenueAdapter，不得新增旁路策略、风控、审批或账户事实源。

新场所最低准入门：

- 能证明 Trading 是唯一日常 OrderIntent 来源。
- 能建立稳定 instrument 身份与历史规则版本。
- 能提供可查询的订单、成交、仓位和费用事实。
- 能实现幂等或通过 fencing、查询与 Unknown Heat 达到等价安全。
- 能实现可验证的 reduce-only / 防反向仓语义和保护路径。
- 能隔离交易凭证与提款凭证。
- 能通过部分成交、断线、重启、结果未知和人工接管重放。

具体认证门由 `DEC-EXEC-007` 冻结。无法满足者只能作为只读数据源或保持不接入。

---

## 13. 故障降级

| 故障 | 默认行为 |
| --- | --- |
| Risk Engine / 审批事实不可用 | 禁止开仓与 Add；保护、减仓、退出继续 |
| VenueAdapter / worker 不可用 | 冻结对应执行单元增险；确认原生保护并对账 |
| 订单、成交或仓位未知 | 相关范围进入 Unknown；禁止扩大风险，只查询 / 减仓 / 退出 |
| 保护缺失 | 立即禁止 Add；优先恢复，失败则退出 |
| Margin Controller 未知 | 冻结对应抵押池增险；保持更小仓位并对账 |
| 账户抽象或抵押池变化 | 执行与风险证书失效，进入 NO_NEW_RISK |
| Freqtrade / CCXT / adapter 版本变化 | 受影响证书失效，完成重放与影子后恢复 |
| Web / Telegram / 辅助建议源不可用 | 新开仓审核受限；已有保护与退出不受影响 |
| 双主或 fencing 不明 | 全部增险失败关闭；启动 P0 处置 |

---

## 14. 认证与证据要求

每个认证单位至少绑定：

`venue × execution_domain × account × position_mode × margin_mode × collateral_pool_id × VenueAdapter version × Freqtrade version/config × capability set`

证据包至少包含：

- 账户与凭证权限证明。
- 场所规则、instrument 与能力快照。
- 请求 / 回执 / 订单 / 成交 / 仓位全链路关联。
- 正常、拒单、零成交、部分成交、撤销失败、超时、结果未知案例。
- 保护创建、替换、数量覆盖与穿透案例。
- 动态去杠杆和完全退出案例。
- 进程重启、网络分区、双主、限速与规则变化重放。
- Heat 守恒、Add 消费和审计完整性证据。
- 若启用逐仓 normalization，需追加 Margin Controller 复合意图证据。

证书必须记录范围、版本、通过环境、证据位置、审批人、有效期、失效条件与当前状态。Binance 的证据不得证明 Hyperliquid；Core 不得证明 HIP-3 DEX；一个账户 / margin mode 不得证明另一个。

---

## 15. 待决策引用

本文不替用户冻结以下事项：

| 决策编号 | 需要冻结的事项 | 未决期间的安全行为 |
| --- | --- | --- |
| `DEC-EXEC-001` | Trading 与 Freqtrade 的受控接口 | 仅设计 / 影子，不开放真实执行 |
| `DEC-EXEC-002` | 账户、子账户、position mode 与 worker 拓扑细节 | 每个候选拓扑独立，不混用账户 |
| `DEC-EXEC-003` | 外部人工交易与既有仓位政策 | 检测到外部变更即冻结并对账 |
| `DEC-EXEC-004` | 逐场所订单与原生保护语义 | 能力保持 `VALIDATING / UNKNOWN` |
| `DEC-EXEC-005` | Hyperliquid 使用原生能力或定制桥接 | 不声称任一路径已认证 |
| `DEC-EXEC-006` | 逐仓 normalization 参数与证书 | 功能关闭或只做影子计算 |
| `DEC-EXEC-007` | 未来 Venue 认证门 | 新场所只读或不接入 |
| `DEC-RISK-010` | 保证金压力与清算缓冲 | 不启用依赖该阈值的增险能力 |

---

## 16. 完成定义

本文对应的执行模块只有在以下条件全部成立时才可称为“可进入受限实盘认证”，而不是“已生产可用”：

- 执行接口和账户拓扑的决策已经冻结。
- 所有增险入口都能证明经过 Trading 与 Risk Engine。
- 订单状态、Heat 与 Add 消费可在重启后确定性恢复。
- 原生保护、reduce-only、部分成交和结果未知已逐场所重放。
- 双主、旁路和提款权限被技术与运营控制共同阻断。
- 对应 Binance 或 Hyperliquid 执行清单有完整证据，且当前状态不是 `UNKNOWN / EXPIRED`。

在此之前，本文只是一份实现与验证合同，不签发现实订单权限。
