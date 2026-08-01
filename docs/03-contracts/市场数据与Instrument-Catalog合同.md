# 市场数据与 Instrument Catalog 合同

> 版本：当前规范基线
> 日期：2026-08-01
> Owner / 批准人：待 `DEC-GOV-004` 确认
> 文档状态：工程合同基线
> 上位文档：《交易系统总体方案》《策略合同与数值化验收门》
> 决策真源：`docs/00-governance/待确认决策清单.md`
> 本文效力：定义发现范围、Instrument 身份、分类、市场数据语义、健康与交易资格；不冻结具体供应商、阈值或策略参数

---

## 一、目标与边界

Trading 必须拥有独立 Instrument Catalog 和市场数据健康判断，回答：

- 目标场所现在有哪些可发现的 U 本位永续合约。
- 每个合约属于哪个 venue、execution domain、底层资产、板块和风险簇。
- 其价格、交易规则、预言机、市场运营和账户能力是否足以支持风险与执行。
- Perptape 候选或人工提案引用的是哪个精确 Instrument 和数据版本。
- Instrument 从“可发现”到“可真实交易”经过了哪些批准和认证。

Perptape 负责发现机会，不是交易数据或 Instrument 资格的唯一事实源。Trading 必须直接从目标场所取得可成交价格、合约规则、私有账户能力和健康事实，再由 Catalog、Risk Engine、简单 Capability Gate 与当前运行验证决定是否可交易。

本文覆盖 Binance、Hyperliquid Core 和所有可发现的 HIP-3 DEX；范围包含加密货币、美股及股指、贵金属、商品及商品期货参考资产的永续衍生品。发现全部不等于全部获准实盘。

---

## 二、不可混淆的身份

### 2.1 Venue 与 execution domain

- Binance 是一个 venue，其账户、保证金模式和 worker 按明确运行配置与 RBAC/执行作用域隔离。
- Hyperliquid 是一个 venue；Core 与每个 HIP-3 DEX 是不同 execution domain。
- execution domain 绑定市场运营、预言机、订单空间、限速、抵押品和故障结论。

共享基础设施或显示名称不能让不同 HIP-3 DEX 自动共享交易认证。

### 2.2 Instrument Identity

一个规范 Instrument 至少由以下字段确定：

- venue。
- execution domain / DEX identity。
- 场所原生 instrument/asset/contract identity。
- contract type 与永续/到期属性。
- quote、settlement 和 collateral asset。
- contract multiplier、数量与价格精度。
- 必要时的 oracle/benchmark identity。

显示符号不是主键。Binance BTC 永续、Hyperliquid Core BTC 永续与某 HIP-3 DEX 的 BTC 参考永续是不同 Instrument。

### 2.3 Underlying、Sector 与 Risk Cluster

Instrument 映射到：

- `underlying_id`：标准化底层资产。
- `sector`：CRYPTO、EQUITY_INDEX、PRECIOUS_METALS、COMMODITY 或 UNCLASSIFIED。
- 一个或多个 `risk_cluster_id`：表达共同宏观、方向、抵押品、预言机或流动性风险。

跨 venue 的同底层同向暴露在底层/风险簇层聚合，但订单、仓位、费用、清算和 PnL 不合并。分类政策由 `DEC-RISK-007` 冻结；UNCLASSIFIED 的新增风险为零。

---

## 三、Catalog 核心记录

每个 Instrument 记录至少包含：

| 类别 | 必需事实 |
| --- | --- |
| 身份 | venue、execution domain、native ID、canonical ID、display symbol |
| 经济属性 | underlying、sector、risk cluster、quote/settle/collateral、contract multiplier |
| 交易规则 | tick、lot、最小数量、最小/最大名义、订单类型、TIF、reduce-only、条件/保护能力 |
| 杠杆保证金 | 场所最大杠杆、档位、margin mode、isolated/cross 能力、collateral pool |
| 市场运营 | market operator、oracle/benchmark、交易时段、暂停/退市状态 |
| 数据 | 支持的数据类型、来源、sequence/time 语义、新鲜度和质量 |
| 正交资格事实 | discoverable、classification completeness、approval scope、listing/operability、data/account health |
| 版本与验证 | metadata version、classification version、运行配置版本、valid from/to |
| 证据 | 原始场所元数据引用、观察时间、审核与变更原因 |

场所实时规则优先于缓存。Catalog 版本不能让过期 tick/lot、杠杆或交易状态继续执行。

---

## 四、正交事实与派生资格

Catalog 不维护把所有维度排列组合成 `DISCOVERED → CLASSIFIED → CERTIFIED → ...` 的线性状态机。它保存独立、可版本化的事实；同一事实变化只影响相关资格，不制造新的组合状态：

| 事实轴 | 规范事实 |
| --- | --- |
| 场所存在性 | `discoverable`、原生 listing status、首次/最后观察时间 |
| 身份与分类 | Instrument identity、underlying、sector、risk cluster、classification version/completeness |
| 产品允许范围 | `approval_scope = NONE / OBSERVE / RESEARCH / LIVE` 及批准版本 |
| 能力门 | 简单 Capability Gate、批准的运行配置与对应发布验证结果；不建设证书实体 |
| 实时可运营性 | trading/reduce-only/halted/delisting/retired、规则新鲜度、预言机/benchmark 和交易时段 |
| 数据与账户健康 | market/private data、账户、margin mode、collateral pool、worker/adapter 当前健康 |
| 系统风险门 | 当前 Risk Engine 状态及作用域容量 |

`eligible_to_trade` 是查询时派生结论，不持久化为可被人工修改的业务状态。仅当下列条件同时成立时为 `true`：

- Instrument 可发现、身份唯一、分类完整且 `approval_scope = LIVE`。
- listing/operability 允许新增风险，规则与所需市场数据均在认证新鲜度内。
- 当前策略、venue、execution domain、账户、margin mode、adapter、worker 和风险作用域配置已经验证，所需简单 Gate 明确启用且版本匹配。
- 私有账户事实、价格、盘口、预言机/benchmark 和交易时段健康。
- Risk Engine 当前允许该作用域新增风险。

任一输入为缺失、陈旧、冲突或 `UNKNOWN` 时，`eligible_to_trade = false`；现有 Campaign 仍按保护、减仓、退出与对账合同管理。事实恢复不能复活旧 Proposal 或 Authorization，新动作必须重新风险求值。其他 Venue 若以后独立立项，也使用同一派生规则，且不能继承 Binance/Hyperliquid 的运行验证结论。

---

## 五、市场数据类型与职责

| 数据 | 用途 | 不能替代 |
| --- | --- | --- |
| Index / oracle / benchmark | 公允参考、预言机健康 | 可成交价格 |
| Mark | 未实现 PnL、账户权益和清算参考 | 实际委托成本 |
| Best bid/ask 与 depth | 下单前滑点、容量和可成交上限 | 长周期趋势信号 |
| Last trade | 观察最新成交 | Mark、盘口或保护触发事实 |
| Trades / volume | 参与度、K线和研究 | 私有成交 |
| Open interest | 趋势参与度辅助 | 方向保证 |
| Funding | 成本、容量和压力 | 已结算资金费 |
| Kline | 突破、趋势、布林回调和退出 | 未完成周期的确定事实 |
| Trading rules | tick/lot、订单、杠杆和状态 | 账户实际能力 |
| Private account data | 余额、仓位、订单、成交 | 公开市场指标 |

每种用途明确 `price_type`，Risk Engine 和 PnL 不得使用一个“price”字段承载全部语义。

稳定币和抵押资产估值不能固定按 1 USD；由 `DEC-RISK-006`、`DEC-FUND-002` 冻结。

---

## 六、时间、顺序与新鲜度

每条市场事实至少保存：

- source event time。
- observed/received time。
- processed time。
- source sequence、trade ID 或可证明顺序的标识。
- source、connection/session 与 Schema version。
- completeness、freshness 和 quality。

系统不能只用本机接收时间排序历史 Kline，也不能把断线后的第一条增量当作完整快照。

数据状态至少包括：

- `HEALTHY`：在认证新鲜度和完整度内。
- `STALE`：超过用途允许时长。
- `GAPPED`：序列、时间窗或 Kline 缺口。
- `DIVERGENT`：多个权威来源分歧超过门槛。
- `UNAVAILABLE`：无法获取。
- `UNKNOWN`：不能确定当前状态。

不同用途使用不同门：候选展示、提案冻结、下单、Add、保护和 PnL 可以有不同新鲜度。正式 SLO 与对账频率由 `DEC-OPS-002`、`DEC-OPS-008` 冻结。

---

## 七、快照、增量与缺口修复

所有实时流必须具备：

1. 有版本和时间的初始快照。
2. 可排序或检测缺口的增量。
3. 断线后重新建立快照/水位的流程。
4. REST 或独立来源校准，但不与实时流重复生成交易事件。
5. 明确的 readiness；预热和修复期间不产生新交易。

快照与增量合并必须保持同一 Instrument identity、Schema、价格类型和时间语义。缺口无法补齐时标记 GAPPED/UNKNOWN，不能用最近值填充后继续 Add。

---

## 八、Kline 合同

- 策略和退出默认只使用已完成 Kline；未完成 Kline 仅用于界面实时观察，除非策略合同明确认证。
- 每根 Kline 绑定 venue、execution domain、Instrument、周期、open/close event time、来源和完成状态。
- 迟到成交导致的 Kline 修正必须生成 revision，不静默改写已用于决策的历史快照。
- 空周期、交易暂停、传统资产闭市和数据缺口分别表达，不能全部填成零成交 Kline。
- 多来源 Kline 的聚合、校准和修复必须防止同一成交重复计算。
- 策略周期、突破、趋势退出和布林回调参数由 `DEC-RISK-004` 冻结。

Trading 应保存每次提案和 Add 使用的 Kline/指标版本或证据引用，以便重放。

---

## 九、Perptape → Trading 候选合同

Perptape 候选至少提供：

- `candidate_id`、策略/信号版本和 source service version。
- 规范或可映射的 venue、execution domain、源场所 raw symbol、canonical symbol 和 native Instrument identity。
- 方向、决策周期、触发/突破时间、候选有效期。
- 价格、Kline、成交量、持仓量和其他依据的时间与来源。
- 数据健康、readiness、缺口和不确定性。
- 排名、反证和市场状态；若未提供应明确缺失。

Trading 处理：

- 映射到精确 Catalog version；映射不唯一时拒绝。
- 从目标场所重新读取规则、盘口、交易时段和账户事实。
- 去重迟到/重复候选，不因重连重复创建 Proposal。
- Perptape 不可用或陈旧时冻结 SYSTEM 新候选；已有仓位继续由 Trading 管理。

当前候选 ID 的确定性输入包含 `source_exchange + raw symbol + canonical symbol + timeframe + source_direction + triggered_at`。raw symbol 是源合同身份的一部分，不是纯显示字段：例如同一 canonical symbol 下的 `BTCUSDT` 与 `BTCUSDC` 必须形成不同 candidate ID、深链和 Proposal 关联，不能因 canonical 化被去重。

2026-08-01 之前持久化的 legacy candidate ID 未包含 raw symbol。兼容查询只有在该旧 ID 对当前候选得到唯一匹配时才可沿用既有 Proposal；如果两个或更多当前报价合约命中同一 legacy ID，必须返回歧义拒绝，不能任选其一，也不能让另一合约复用旧 Proposal。新建 Proposal 优先冻结当前精确 candidate ID。

候选不是 Proposal、Approval、Authorization 或 OrderIntent。

---

## 十、人工提案的数据门

MANUAL 提案可以来自用户看图，不要求 Perptape 已经产生信号，但不能跳过：

- 精确 Instrument 映射。
- 场所当前交易状态和规则。
- 可成交盘口、Mark、费用、资金费和流动性。
- sector、risk cluster 和执行证书。
- 非加密标的的交易日历、预言机/benchmark 和市场运营健康。
- 目标账户、margin mode、collateral pool 和真实容量。

用户提供的触发价、委托价、数量和图表假设是请求，不是权威市场事实。

---

## 十一、非加密参考永续的附加合同

美股/股指、贵金属和商品参考永续仍是交易场所提供的衍生品，不等于直接持有传统现货或期货。每个 Instrument 必须额外记录：

- benchmark/oracle 的运营者、更新机制和异常状态。
- 基准市场时区、交易日历、节假日和闭市行为。
- 场所永续是否在基准闭市后继续交易，以及价差/跳空风险。
- 公司行动、指数调整、合约换月或基准方法变化的影响。
- 市场运营者、预言机和 DEX 的独立风险证书。

缺失任一事实时只允许展示。非加密实盘顺序由 `DEC-PROD-014` 冻结。

---

## 十二、规则与能力快照

下单前必须取得并持久化当时适用的：

- tick/lot、最小数量、最小/最大名义价值。
- 允许的订单类型、TIF、trigger price、reduce-only 和保护语义。
- 最大杠杆、名义档位和 margin mode。
- isolated/cross、可移除保证金、strict isolated 等能力。
- 市场/Instrument 暂停、仅减仓、退市和限速状态。
- 账户抽象、collateral scope 和 collateral pool。

Catalog 保存规范事实和证书引用；VenueAdapter 在发送前使用场所实时规则复核。逐所订单/保护、Hyperliquid 能力和 normalization 分别由 `DEC-EXEC-004`、`DEC-EXEC-005`、`DEC-EXEC-006` 冻结。

---

## 十三、流动性与执行资格

策略信号合格不表示执行合格。实时资格至少考虑：

- bid/ask、目标数量多档深度和压力滑点。
- 最小订单、舍入后风险偏差和最大可安全拆分量。
- 资金费、费用和预期持有成本。
- 订单限速、账户容量、Open Interest/市场容量与场所限制。
- 原生保护是否能覆盖实际成交。
- 市场/预言机/基准是否健康。

流动性只能向下裁剪自动 Add 或拒绝初仓，不能自动换所或放宽止损。正式门属于 `DEC-RISK-010` 和逐场所执行证书。

---

## 十四、Catalog 变更与证书失效

以下变化必须生成新版本并评估影响：

- native Instrument identity、quote/settlement/collateral 或 multiplier。
- tick/lot、最小名义、最大杠杆或 margin mode。
- execution domain、DEX、market operator、oracle/benchmark。
- underlying、sector 或 risk cluster 分类。
- 订单、保护、reduce-only、TIF 或保证金能力。
- 交易时段、退市、公司行动、指数方法或基准规则。

若变化影响风险或执行，相关 Proposal/Authorization/执行证书失效；已有仓位进入保护、对账、只减仓或退出评估。分类变化不重写历史交易所使用的 Catalog version。

---

## 十五、市场数据事件与 API

Catalog/Market 事件至少表达：

- InstrumentDiscovered/MetadataChanged/Classified/Suspended/Retired。
- TradingRuleChanged/CapabilityChanged/CertificateInvalidated。
- MarketDataHealthy/Stale/Gapped/Divergent/Recovered。
- OracleOrBenchmarkDegraded/TradingSessionChanged。
- CandidateReceived/Rejected/Deduplicated/Expired。

每个事件遵守《API事件数据与审计契约》的 event identity、aggregate sequence、Schema 和 outbox/inbox 规则。数据恢复事件不能复活旧 Proposal 或 Add 候选。

查询必须支持按 version/as-of 重建某次决策使用的 Instrument、分类、规则和数据健康。

---

## 十六、审计与保留

必须保留：

- 原始场所元数据证据和规范化结果。
- Instrument identity 映射、人工分类、批准、证书和变更原因。
- 每次提案/风险/订单使用的 Catalog、规则和市场数据快照引用。
- 数据缺口、修复、来源切换、健康变化和人工 override；override 不能放宽硬门。
- Perptape candidate 与 Trading Proposal 的一对多/拒绝关系。

保留和访问由 `DEC-GOV-002` 冻结。市场原始高频数据可以分级保留，但用于真实交易决定的证据不可在审计期内丢失。

---

## 十七、失败语义

| 失败 | 行为 |
| --- | --- |
| Instrument 无法唯一映射 | 拒绝候选/提案，不猜测 symbol |
| legacy Perptape ID 匹配多个 raw symbol 合约 | 返回歧义拒绝，不复用旧 Proposal、不猜测报价合约 |
| 规则陈旧或变化 | 停止该 Instrument 新仓/Add，重新认证 |
| 公开行情陈旧/Gapped | 停止依赖该数据的新动作；已有保护继续 |
| Mark/指数/盘口分歧 | 使用场景化保守政策或进入 UNKNOWN，不扩大风险 |
| 预言机/benchmark 异常 | 冻结对应 execution domain/Instrument |
| 私有账户事实未知 | 按 `DEC-OPS-005` 全局关闭新增风险 |
| Perptape 不可用 | 停 SYSTEM 新候选；MANUAL 仍需独立完整数据门 |
| Catalog 主库不可写 | 按 `DEC-OPS-007` 停止新增风险 |

UNKNOWN 不是“无信号”“零风险”或“市场平稳”。

---

## 十八、验收门

影子执行前必须证明：

- 所有可发现 U 本位合约都有无歧义 identity，重复名称不会合并。
- Binance、Hyperliquid Core 与各 HIP-3 DEX 的 Instrument、故障和证书可独立表达。
- discoverable、approved、certified 和 eligible_to_trade 不会混淆。
- snapshot/incremental 断线、乱序、重复和 gap 能检测、修复或失败关闭。
- 每个价格用途使用正确 price type，不能以中间价替代可成交风险。
- SYSTEM 候选和 MANUAL 提案都必须经过 Trading 场所实时复核。
- 未完成 Kline、闭市、停牌和数据缺口不会被误作完成数据。
- Catalog/规则变化会使对应授权和证书失效，但已有仓持续受保护。
- 非加密标的在日历、预言机、基准和板块未认证时只能展示。
- 任一真实决策都能按 Catalog version 和 as-of 数据重放。

正式新鲜度、策略、分类、执行和非加密顺序分别由 `DEC-OPS-002`、`DEC-OPS-008`、`DEC-RISK-004`、`DEC-RISK-006`、`DEC-RISK-007`、`DEC-RISK-010`、`DEC-EXEC-004` 至 `DEC-EXEC-007` 与 `DEC-PROD-014` 冻结；本文不替代决策清单。
