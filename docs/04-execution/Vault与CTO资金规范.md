# Vault 与 Capital Transfer Orchestrator 资金规范

> 文档编号：FUND-SPEC-001
> 版本：Draft 0.1
> 日期：2026-07-31
> 状态：产品与工程设计基线；NoTilt 三链只读、统一净值、外部钱包未签名交易交接和链上回执验证已实现，真实划转仍因外部事实缺失而关闭
> 上位文档：`交易系统总体方案.md`、`策略合同与数值化验收门.md`
> 适用范围：RiskControl / NoTilt Vault、Capital Transfer Orchestrator、CapitalTransferAdapter、交易所运营资金与 Trading 风险账本

---

## 1. 目的

本文定义长期储备、交易所运营资金、单笔 Trade Funding Envelope、风险资本计入与真实资金划转之间的边界，防止：

- 把链上余额误当成交易所保证金。
- 把“允许计入风险分母”误当成“允许提款”。
- 同一笔资金在 Vault、在途与交易所重复计入。
- 浮亏、清算压力或活动仓位触发 Vault 救援。
- 交易执行凭证获得提款或 Vault owner 权限。

当前实现用三张独立资金生命周期表表达 `TransferProposal`、`TransferAuthorization` 与 `CapitalTransfer`，并用一张当前 `CapitalAutomationPolicy` 保存运营阈值；Vault/场所资本事实复用 `AccountEquity`。NoTilt 边界使用官方 `@notilt/sdk` 固定支持 chain id `1/56/42161`，只允许 Registry/官方部署/Vault budget 读取、deposit/release 的未签名交易构造，以及固定函数回执的只读验证。未签名计划、协议 request id、执行窗口和已确认 tx hash 保存在同一 `CapitalTransfer`；可信 RPC 回执必须匹配链、发送者、Vault、函数、参数、事件和逐链确认深度，tx hash 不可跨划转复用。运行时没有 NoTilt 私钥字段、签名或广播能力，也没有 owner、白名单管理、Panic、Full Exit 或任意合约调用入口。Vault、Binance、Hyperliquid 以新鲜 USD 估值形成 LIVE 净值；任一必需来源缺失或过期时整体标记为 `INCOMPLETE` 并阻断新增风险。`CAPITAL_TRANSFER`、`AUTO_PROFIT_SWEEP` 和 `AUTO_OPERATING_REFILL` 均默认关闭；自动候选仍需双人复核和独立授权。

当前外部事实是：Arbitrum whitelist assignment 尚未激活，且三条链均未配置 Vault 地址。因此系统可以验证 Registry 状态，但不能把 Vault 余额纳入完整净值，也不能生成真实资金计划。条件齐备后，未签名计划仍必须由独立钱包逐笔确认；服务只验证已广播交易的链上回执，不会接收、保存或使用 Vault 私钥。

本文不冻结具体链、资产、签名托管、比例、金额、费用、确认数或自动化阈值。这些事项只通过 `DEC-FUND-*` 与 `DEC-RISK-*` 决策记录冻结。

---

## 2. 三个正交概念

### 2.1 风险计入

`risk_inclusion_mode` 决定合格 Vault 资金是否以折扣后净值进入组合风险口径：

- `EXCHANGE_ONLY`：固定为安全默认，Vault 风险贡献为零，只展示储备。
- `EXCHANGE_PLUS_VAULT`：经独立风险政策批准后，Eligible Vault Equity 才可进入 Current Portfolio MTM Equity 与 Extended Risk Equity。

风险计入只影响允许承担的组合风险上限，不产生资金转移授权，也不改变交易所实际可用保证金。

### 2.2 资金划转

`capital_transfer_mode` 决定 CTO 可以接受哪些资金提案：

- 只读 / 禁止划转。
- 人工双向划转。
- 已认证的空仓自动利润归集。
- 已认证且独立启用的下一周期自动运营补充。

允许资金划转不代表资金可以进入风险分母；允许风险计入也不代表自动划转已开启。

### 2.3 交易授权

交易提案与 Add 授权只决定仓位风险边界。它们不能授权 Vault 请求、交易所提款、Bridge、充值或中转钱包操作。CTO 必须消费独立 `Capital Transfer Authorization`。

---

## 3. 资金分层与口径

| 层级 | 定义 | 可否作为交易所保证金 | 可否进入组合风险分母 |
| --- | --- | --- | --- |
| RiskControl / NoTilt Vault | 链上长期储备 | 否 | 仅 Eligible Vault Equity 且模式开启时 |
| 交易所运营资本 | 已到账、已对账、可用于保证金与费用的结算资本 | 是 | 按交易所风险 / MTM 口径 |
| Trade Funding Envelope | 一笔完整交易生命周期的逻辑资金占用上限 | 不是独立余额池 | 不新增风险资本，只约束占用 |
| 源端转出预留 | 已从源端可用金额扣除、尚未广播或完成 | 否 | 否，防止重复使用 |
| 在途资金 | 链上、Bridge、中转或充值确认中的金额 | 否 | 否 |
| 目的端确认中 | 目的端可观察但尚未满足冻结确认与对账门 | 否 | 否 |
| 已结算目的端资金 | 目的端私有事实确认可用并完成账本迁移 | 视目的端而定 | 按目的端口径计入 |

### 3.1 Managed Settled Capital

资金配置使用独立口径：

`Managed Settled Capital = 已确认交易所资本 + 已确认且纳入资金政策的 Vault 资本`

在途、未知、待确认、源端转出预留和无法可靠估值的资金不进入该口径。该口径服务资金分配，不替代风险权益、Current Portfolio MTM Equity 或交易所可用保证金。

### 3.2 Eligible Vault Equity

Eligible Vault Equity 是唯一 Vault 风险贡献账本。每个 Vault 只有在以下事实均可验证时才可能贡献：

- 链、合约身份、资产、余额、控制权与确认高度。
- 所有 active whitelist 属于批准控制集合，且不存在未批准的待激活权限扩张。
- owner、Panic Lock、Full Exit、pending withdrawal 和请求状态明确。
- 扣除 pending、源端预留转出、在途、费用和其他负担。
- 使用版本化、保守且新鲜的价格、资产折扣、流动性折扣和控制权 / 时效折扣。
- 未超过冻结的 Vault 贡献上限。

任何关键事实未知时，对应贡献归零并触发风险重算，而不是沿用旧余额。具体链、资产、价格源与确认由 `DEC-FUND-002`，折扣与贡献上限由 `DEC-RISK-009` 冻结。

### 3.3 Trade Funding Envelope

每笔待审核提案冻结：

`FundingEnvelope_0 = Total Capital Snapshot_0 × trade_funding_pct`

固定语义：

- 它是初仓到全部 Add 共用的资金占用上限，不是每次 Add 各获得一份。
- 它不等于 1R、最大亏损、保证金占用或名义仓位。
- 场所已有运营资金充足时，只做逻辑预留，不需要为每笔交易从 Vault 提款。
- 资金不足时，Vault → 交易所流程必须在初仓发送前完成并对账；到账后仍要重新校验提案价格、有效期、风险与资金信封。
- 仓位生命周期开始后禁止从 Vault 补充该资金信封或救仓。

正式 `trade_funding_pct = N%` 由 `DEC-RISK-001` 的预登记研究冻结；取得证据前不得把 1.5% 示例硬编码为默认值。

---

## 4. 场所运营资金政策

每个 `venue × account / subaccount × collateral_pool_id × asset` 保存独立资金配置周期：

- `venue_operating_target_pct = n%`。
- 目标金额 `T_v`。
- 补充下沿 `T_low`。
- 归集上沿 `T_high`。
- 最低退出 / 费用 / 异常储备。
- 单次、日、周、月限额与冷却期。

必须满足 `T_low < T_v < T_high`。目标在一个配置周期内冻结，不能因刚产生盈利或亏损而循环改变分母。具体 `n` 与上下沿由 `DEC-FUND-004`，最低储备由 `DEC-FUND-007` 冻结。

所有活动与 Reserved Funding Envelope 合计不得超过该场所已经确认的运营资本。场所低于目标只说明“可能需要下一周期补充”，不允许向活动仓位注资。

---

## 5. CTO 权限与责任

### 5.1 CTO 可以做

- 接收自动或人工资金提案。
- 校验 Treasury Policy、空仓、订单、Unknown、地址、资产、网络、额度、费用与状态。
- 校验职责分离、MFA、授权有效期和签名策略。
- 在授权后冻结源端金额并执行 CapitalTransferAdapter。
- 关联 Vault 请求、交易所 withdrawal / deposit、tx hash、Bridge 或中转腿。
- 维护互斥资金状态、费用、确认与对账。
- 在故障时冻结新划转并继续查询既有转账。

### 5.2 CTO 不可以做

- 发送任何交易订单或改变净仓位。
- 使用交易提案、初仓批准或 Add 授权替代资金授权。
- 把在途或预期到账当成保证金或风险资本。
- 因浮亏、清算压力、保证金不足或活动仓位触发 Vault 提款。
- 持有 Trading / Freqtrade 的交易权限。
- 把软件 whitelist 策略描述为交易所或合约必然提供的硬限制。
- 在源端、在途和目的端之间重复计入金额。

---

## 6. Capital Transfer Authorization

每份资金授权至少冻结：

- `capital_transfer_authorization_id` 与版本。
- 来源、目的、方向与业务原因：人工调拨、利润归集或运营补充。
- 源端 Vault / 交易所 / 钱包身份与目的端账户 / Vault 身份。
- 链、网络、资产、Token 地址、memo / tag、Bridge / 中转路径。
- gross、预计 net、费用上限、最小到账与价格口径。
- 单次与周期限额、冷却期和最低储备。
- 有效期、确认深度、超时、reorg 与失败策略。
- 空仓、无订单、无 Unknown、损失门与系统状态快照。
- requester、reviewer、executor、MFA、职责分离与签名策略。
- Vault owner / whitelist / Panic Lock / Full Exit 与目标地址白名单快照。
- 风险计入与源端扣减时点。

任何影响地址、资产、网络、方向、金额上限或目的端的修改都必须生成新授权。人工划转审批规则由 `DEC-FUND-009`，签名托管和白名单由 `DEC-FUND-003` 冻结。

---

## 7. 资金状态机与资本互斥

### 7.1 统一状态

完整状态机为：

**`REQUESTED → POLICY_CHECKED → APPROVED → SOURCE_CONFIRMED → SOURCE_RESERVED_OUT → WITHDRAW_SUBMITTED → TRANSFER_IN_FLIGHT → DESTINATION_CONFIRMING → DESTINATION_CONFIRMED → SETTLED`**

异常状态至少包括：

- `REJECTED`：政策或授权拒绝，未产生源端扣减。
- `CANCELLED_PRE_SUBMISSION`：广播前取消，证明未扣款后释放预留。
- `RESULT_UNKNOWN`：源端、链路或目的端事实不明；不得释放或重复发送。
- `FAILED_SOURCE_RESTORED`：证明源端未扣或已经退回。
- `FAILED_DESTINATION_SAFE`：已到达目的端但业务目标未完成，资金仍有唯一归属。
- `REORG_PENDING`：已观察确认受到链重组影响，重新进入确认流程。
- `MANUAL_RECONCILIATION`：自动证据不足，需要独立人工对账。

### 7.2 互斥规则

同一金额在任一时刻只能属于一类：

1. 源端已确认可用。
2. 源端已预留待转出。
3. 在途。
4. 目的端确认中。
5. 目的端已确认 / 已结算。

进入 `SOURCE_RESERVED_OUT` 时，金额立即从源端可用资本扣除；若来自计入风险的 Vault，则只在 Eligible Vault Equity 源账本扣一次，所有风险口径随后引用同一净值。进入 `TRANSFER_IN_FLIGHT` 后不能恢复为源端可用，除非交易所、链上与目的端证据共同证明未扣或已退回。

只有目的端权威事实确认到账且满足确认深度、资产与金额对账后，才进入 `DESTINATION_CONFIRMED`；只有目的端业务系统确认可用后才进入 `SETTLED`。

### 7.3 单一关联身份

所有腿沿用同一个 `capital_transfer_id`，关联：

- 资金提案与授权。
- 源端扣减 / withdrawal ID / Vault request ID。
- 每个 tx hash、Bridge、中转钱包和充值记录。
- gross、net、Gas、协议费、提款费、Bridge 费与差额。
- 确认高度、时间、reorg 与数据来源。
- 目的端 deposit ID、余额增量与结算证据。

---

## 8. 交易所到 Vault：利润归集

`AUTO_PROFIT_SWEEP` 只处理已实现、已对账并超过场所上沿的结算资本。所有条件必须同时满足：

- 该受管场所、账户、子账户与抵押池真实仓位为零。
- 没有活动、条件、保护、撤销中、Unknown 或可能恢复成交的订单。
- 费用、资金费、已实现盈亏、内部划转与前序提款已对账。
- 归集后仍满足 `T_v`、最低储备、费用和异常缓冲。
- 资产、网络、Token、目的 Vault、地址、费用与最小到账处于当前认证范围。
- Vault owner、whitelist、Panic Lock、Full Exit、资产与链状态符合资金政策。
- 资金授权、限额、冷却、职责分离与 MFA 有效。

归集额只能取以下约束中的安全最小值：已确认可提款利润、超过 `T_high` 的部分、场所限制、资金政策限额、费用与目的 Vault 可接受额度。浮盈不触发归集。

具体归集参数由 `DEC-FUND-005` 冻结；决策与能力认证完成前自动归集保持关闭。

---

## 9. Vault 到交易所：运营补充

`AUTO_OPERATING_REFILL` 属于完整产品能力，但固定默认关闭。完成独立认证并由用户显式启用后，也只允许为空仓账户的下一交易周期补充运营资金或尚未发送的初仓资金信封。

必须同时满足：

- 目标场所真实空仓、无挂单、无 Unknown、无活动复合意图。
- 不存在清算压力、活动仓位、待发 Add 或保护缺口。
- A-DD、C-DD、日 / 周损失门、KILL_SWITCH 与冷却政策允许重新开始。
- Vault 状态、owner、whitelist、Panic Lock、Full Exit、请求等待期与执行窗口正常。
- 补充后不突破单次、周期、连续次数、Vault 最低储备和目标场所接收上限。
- 资金在初仓发送前完成目的端结算和重新风险复核。

补充额只能取 `T_v - 当前已确认场所资本`、待执行信封缺口、Vault 可释放净额、周期上限和目的端容量中的安全最小值。

具体补充参数由 `DEC-FUND-006` 冻结。人工 Vault → 交易所划转也使用相同空仓与无救援硬门，不存在 `MANUAL_CAPITAL_INJECTION` 例外。

---

## 10. Vault 路径

### 10.1 owner direct release

作为完整人工双向划转能力的一种强认证、受审计路径。实际链路只有在 `DEC-FUND-002` 外部事实齐全、签名人与金额门登记完成且资金证书有效时才可启用；职责分离由 `DEC-FUND-003`、`DEC-FUND-009` 固定。自动服务不得持有 Vault owner 私钥。

### 10.2 whitelist funding wallet

自动路径原则上使用预先批准的专用 funding wallet，但必须真实经历请求、等待、执行、钱包到账、必要 Bridge / 中转和交易所充值。现有合约的 requester、等待期和全局释放限制不自动等于逐地址、资产、网络和单授权额度硬限制；不足部分须由受限签名器、独立钱包、低额度、软件策略和链上监控补强。

### 10.3 直接 Token 转账与事件

交易所直接向 Vault 地址转入 ERC20 时，可能没有 Vault 应用层 Deposit 事件。到账必须以链上 Token Transfer、余额增量、资产身份与确认高度共同证明，不能只依赖应用事件。

---

## 11. 费用、超时、reorg 与差额

每次划转分别记录：

- gross 源端扣减。
- 场所提款费。
- Vault 协议费。
- Gas、Bridge、Swap 或中转费。
- 目的端实际 net 到账。
- 价格变化与稳定币折价造成的价值差异。

费用是实际资本损失，不得作为外部流量从回撤中完全消除。外部本金移动对 A-DD / C-DD 高点做流量调整，实际费用保留为损失。

超时不能自动重发提款。系统先判断源端是否扣减、交易是否广播、链上是否确认、目的端是否观察到，再选择继续等待、重新确认、进入人工对账或在证明安全后取消。确认深度、超时、reorg 和费用容忍由 `DEC-FUND-008` 冻结。

---

## 12. 稳定币与估值风险

USDT、USDC 或后续经证据批准的美元锚定资产不能永久按 1 美元计价。以下任一情况必须按风险政策降级：

- 价格源陈旧、分歧或不可用。
- 脱锚超过冻结门。
- 赎回、链、Bridge、发行人或流动性异常。
- 同一资产在不同链上的可兑换性不再等价。

稳定币政策由 `DEC-RISK-006` 冻结。未决或状态未知时，不扩大 Eligible Vault Equity，不使用在途资产满足交易所资金门，并可停止新划转。

---

## 13. 权限与密钥隔离

| 凭证 / 身份 | 允许 | 禁止 |
| --- | --- | --- |
| Freqtrade 交易凭证 | 绑定账户的交易与查询 | 提款、Vault 释放、Bridge |
| Margin Controller 凭证 | 已认证的场内保证金动作 | 交易订单、外部提款 |
| CTO 交易所资金凭证 | 冻结地址 / 资产 / 网络 / 额度内提款或查询 | 交易订单 |
| Vault funding wallet | 经授权请求 / 接收 / 转发资金 | 持有 owner 全权、交易所交易 |
| Vault owner / 高权限签名 | 仅人工治理与批准范围 | 自动交易服务持有 |
| Treasury Admin | 提议、审核或执行资金授权的指定步骤 | 批准交易、改变交易风险、单人完成全部高风险步骤 |

创建者与最终批准者默认分离；实际双人门、MFA、金额和紧急策略由 `DEC-FUND-009` 与安全决策登记冻结。

---

## 14. 对账与异常处理

### 14.1 日常对账

- 源端余额与预留。
- 交易所 withdrawal / deposit 与状态。
- 链上交易、Token、金额、确认高度与重组状态。
- 中转 / Bridge 各腿。
- 目的端余额增量与可用状态。
- gross、net、费用与差额。
- Eligible Vault Equity、Managed Settled Capital 和风险资本迁移。

### 14.2 异常原则

- 事实未知时冻结新划转，不释放源端预留。
- 任何差异先确定资金唯一归属，再修复会计展示。
- CTO 故障不阻断交易仓位的保护、减仓和退出。
- Vault / RPC / 价格异常使 Vault 风险贡献降级或归零，但不触发活动仓救援。
- 用户在系统外注入或提出资金时，Trading 冻结新增风险、登记外部资本流并强制对账；不能借此重置回撤或损失计数。

---

## 15. 能力激活证据

只读、人工双向划转、自动利润归集和可选自动运营补充都属于长期愿景，但按外部副作用由低到高的端到端流程逐项开发、验证和授权：

1. Vault 只读身份、余额、控制权与 Eligible Vault Equity 影子计算。
2. 小额人工 Vault → 交易所与交易所 → Vault 双向路径。
3. 资金信封不足时的初仓前人工补充。
4. 空仓自动利润归集。
5. 可选、默认关闭的空仓下一周期自动运营补充。
6. 更高额度、更多链 / 资产或更复杂 Bridge 路径。

每一级必须逐 `source × destination × chain × asset × network × signer policy` 认证；一个方向、链或资产通过不证明另一个通过。

证据至少包含：

- 授权、MFA、职责分离与拒绝案例。
- 正常、取消、源端拒绝、超时、结果未知、reorg 和目的端差额。
- 源端 / 在途 / 目的端互斥守恒。
- Vault 状态变化、whitelist 漂移、Panic Lock 与 Full Exit 拒绝。
- 空仓与订单硬门、活动仓救援阻断。
- 费用、确认、目的端可用和回撤流量调整。
- 进程重启与人工恢复。

---

## 16. 决策、外部事实与研究引用

| 决策编号 | 已定方法 / 待补事实或证据 | 证据未齐时的安全行为 |
| --- | --- | --- |
| `DEC-FUND-001` | 完整实现只读、人工双向、自动归集和可选自动补充；四类能力独立授权 | `EXCHANGE_ONLY`；自动能力关闭；未认证路径零划转 |
| `DEC-FUND-002` | 用户补充实际链、合约、资产、网络和地址 | 未列明资产不计风险、不划转 |
| `DEC-FUND-003` | 多签 owner + 专用受限 funding signer；实际成员与白名单待登记 | 自动服务不持有 owner 全权 |
| `DEC-FUND-004` | 研究场所运营资金 n 与上下沿 | 不执行目标驱动自动划转 |
| `DEC-FUND-005` | 研究利润归集金额、费用与冷却参数 | `AUTO_PROFIT_SWEEP` 关闭 |
| `DEC-FUND-006` | 研究运营补充限额、次数、冷却和损失门 | `AUTO_OPERATING_REFILL` 关闭 |
| `DEC-FUND-007` | 研究 Vault 与场所最低储备 | 不发送依赖未知储备的划转 |
| `DEC-FUND-008` | 统一 CTO 状态机已定；逐路径费用、超时、确认与 reorg 需认证 | 不自动重发 |
| `DEC-FUND-009` | 金额分级、职责分离与多签已定；实际人员待登记 | 不允许单人旁路划转 |
| `DEC-RISK-001` | Trade Funding Envelope 的 N | 不把示例值作为默认 |
| `DEC-RISK-006` | 研究稳定币风险参数 | 未认证资产贡献为零 |
| `DEC-RISK-009` | 研究 Vault 折扣与贡献上限 | 保持 `EXCHANGE_ONLY` |

---

## 17. 完成定义

资金模块只有在以下条件全部成立时，才可称为对应范围“已认证”：

- 风险计入、划转与交易授权被证明为三个独立开关和状态机。
- 每个金额在源端、在途和目的端始终唯一归属。
- 活动仓位、清算压力和损失门无法触发自动或人工 Vault 救援。
- 凭证、签名、地址、资产、网络、额度和职责分离已验证。
- 正常、超时、未知、reorg、差额与重启证据完整。
- 实际到账和交易所可用余额由目的端权威事实确认。
- 自动利润归集与自动运营补充分开认证、分开启停，且后者仍由用户明确启用。

在此之前，文档描述的是安全合同，不代表任何资金可以自动移动。
