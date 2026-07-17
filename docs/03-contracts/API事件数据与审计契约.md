# API、事件、数据与审计契约

> 版本：当前规范基线
> 日期：2026-07-18
> Owner / 批准人：待 `DEC-GOV-004` 确认
> 文档状态：工程合同基线
> 上位文档：《交易系统总体方案》《策略合同与数值化验收门》《领域模型与状态机》
> 决策真源：`docs/00-governance/待确认决策清单.md`
> 本文效力：定义系统间命令、查询、事件、逻辑数据和审计的共同语义；不冻结具体框架、URL、消息产品或数据库品牌

---

## 一、目标

本契约确保 Web、PWA、Telegram、Perptape、Trading、Risk Engine、Freqtrade worker、VenueAdapter、Margin Controller 和 CTO 对同一业务事实使用一致身份、版本、幂等和状态语义。

核心要求：

- Trading 是受管账户唯一日常订单发送者；任何外部 API 都不能绕过 Proposal、Approval、Authorization、Risk 与 OMS。
- Freqtrade 是执行后端，只接受 Trading 签发的一次性 `OrderIntent`，不接受 Web、Telegram、FreqUI 或策略旁路增险。
- API 返回“已受理”不等于交易所成交；通知送达不等于业务完成。
- `DEC-ARCH-002` 已确认关系型事实库、不可变账本与 transactional outbox/inbox；具体产品选型不改变本契约。
- 事件至少一次投递；重复、乱序、超时和重启不能产生第二个订单、第二次 Add 消费或第二次资本移动。

---

## 二、交互类型

| 类型 | 用途 | 结果语义 |
| --- | --- | --- |
| Command | 请求改变业务状态 | 先验证身份、版本、幂等和前置条件；返回拒绝、原子受理或已存在结果 |
| Query | 读取事实或投影 | 必须携带事实截止时间、新鲜度、成熟度和投影版本 |
| Event | 陈述已持久化发生的事实 | 不可变、可重放、至少一次；消费者幂等 |
| Notification | 向人传递事件或待办 | 不成为授权、订单或成交事实 |

命令名称使用业务动词；事件名称使用已发生事实。不能把“请求下单”和“订单已成交”用同一状态表达。

---

## 三、接口边界

| 调用方 | 被调用方 | 允许内容 | 禁止内容 |
| --- | --- | --- | --- |
| Web/PWA | Trading API | 草稿、审核、查询、收紧、风险/资金提案 | 直连 Freqtrade/交易所/Vault |
| Telegram Bot | Trading API | 冻结提案决定意图、关闭 Add、暂停、预定义减仓 | 任意数量/杠杆开仓、恢复增险、直连 Freqtrade |
| Perptape | Trading Ingestion | 候选、指标、数据健康和版本 | 私有仓位、审批、订单 |
| Trading Core | Risk Engine | 不可变 Decision Snapshot | 通过管理员参数覆盖拒绝 |
| OMS | VenueAdapter/Freqtrade | 已授权、已预留、一次性 `OrderIntent` | 策略自由信号、无限重试 |
| Reconciler | Venue/Freqtrade | 私有订单、成交、仓位、余额和执行镜像 | 用镜像覆盖场所事实 |
| Campaign Manager | Margin Controller | 已认证保证金意图 | 交易订单或 Vault 请求 |
| Treasury Policy | CTO | 独立资金授权 | 复用交易授权 |
| CTO | CapitalTransferAdapter/Vault | 地址、资产、网络和额度受限的资金动作 | 发送交易订单 |

Freqtrade 受控接口的传输、认证、状态回传和升级策略由 `DEC-EXEC-001` 冻结；Hyperliquid 能力缺口由 `DEC-EXEC-005` 冻结。

---

## 四、统一命令信封

每个改变状态的命令至少包含：

| 字段 | 要求 |
| --- | --- |
| `command_id` | 全局唯一，永久可审计 |
| `idempotency_key` | 调用方和业务作用域内唯一；重复返回同一业务结果 |
| `command_type` | 版本化业务动作，不使用自由文本 |
| `object_type/id` | 目标权威写模型或其稳定子对象 |
| `expected_version` | 乐观并发版本；不匹配时拒绝并要求刷新 |
| `actor_id / service_principal` | 发起身份，不信任前端显示名 |
| `channel` | WEB、PWA、TELEGRAM、SYSTEM 或受认证内部服务 |
| `scope` | tenant/workspace、venue、account、sector、instrument、campaign 等 |
| `correlation_id / causation_id` | 贯穿旅程和因果链 |
| `issued_at / expires_at` | 认证服务器时间；过期命令不得执行 |
| `auth_context` | 标签、ABAC、MFA、会话和设备证据引用 |
| `payload_schema_version` | 精确 Schema 版本 |
| `reason` | 人工高风险动作和拒绝/收紧必须提供结构化原因 |

命令载荷不得信任客户端提供的风险结果、角色、账户余额或场所能力；服务端从权威事实重新读取。

---

## 五、命令处理语义

1. 鉴别身份、会话、渠道和重放风险。
2. 按统一 RBAC+ABAC、MFA、自审与职责分离服务鉴权。
3. 检查幂等键；已存在则返回原业务结果。
4. 检查 `expected_version`、有效期和对象状态。
5. 对增险动作重新取得 Catalog、市场、账户和风险事实。
6. 原子保存状态迁移、不可变账本和 outbox 事件。
7. 外部副作用异步执行；最终结果由事件和查询返回。

客户端断线或超时后必须用 command/idempotency 查询，不得创建新语义命令盲重试。

命令响应至少区分：

- `REJECTED`：没有产生业务状态变化。
- `ACCEPTED`：内部原子状态已提交，外部副作用尚未完成。
- `COMPLETED`：无需外部等待或已取得最终事实。
- `ALREADY_PROCESSED`：幂等重复，返回原结果。
- `CONFLICT`：版本或终态冲突。
- `UNKNOWN`：不能确定外部结果；禁止自动重新发起。

---

## 六、查询契约

查询结果除业务字段外必须返回：

- `as_of`：事实截止时间。
- `observed_at`：系统观察时间。
- `projection_version`：查询投影版本/水位。
- `source_health`：每个依赖的数据健康。
- `freshness`：新鲜、陈旧、缺失或 Unknown。
- `maturity`：实时 provisional、venue confirmed、reconciled、period final、corrected 或 unknown。
- `policy_versions`：影响展示结论的策略、风险、Catalog 和权限版本。

离线 PWA 查询只能返回明确时间戳的只读快照；不能把缓存结果标为实时，也不能基于缓存批准或恢复。PWA 设备和缓存政策由 `DEC-PROD-010`、`DEC-SEC-005` 冻结。

---

## 七、核心业务命令族

### 7.1 Proposal

- 创建/更新 MANUAL 草稿。
- 提交预检。
- 冻结提案版本。
- 取消未执行提案。
- 创建 SYSTEM 提案由受认证策略 service principal 完成。

MANUAL 草稿、预检与冻结命令至少携带：方向、触发价、委托价、数量、`reduce_only`、有效期、初始失效价和用户请求最大风险。最大风险超过档位上限时命令必须拒绝，不得静默改为上限；`reduce_only` 必须通过已有真实仓位验证，不得反向建仓。

冻结后修改必须产生新版本；SYSTEM 所有权由 `DEC-PROD-006` 冻结。

### 7.2 Approval

- 批准、拒绝、退回修改、放弃。
- 命令绑定精确 `proposal_version_id`、风险摘要 hash、Reviewer、MFA 和有效期。
- 每个审核动作先原子持久化为 `ReviewerVote`；再由审批服务按已确认 quorum 聚合出唯一 `ApprovalDecision`。
- 需要多人复核时，首个赞成 vote 仍是待审，不签发授权；权威终态之后的迟到动作返回已处理结果。

自审、人数和 Telegram step-up 由 `DEC-PROD-004`、`DEC-PROD-005`、`DEC-SEC-001` 冻结。

### 7.3 Position Control

- 关闭单 Campaign 或作用域 Add。
- 暂停新仓。
- 请求预定义紧急减仓或退出。
- 请求人工逐级风险恢复。

恢复或扩大风险不能通过 Telegram 轻操作完成；作用域与粒度由 `DEC-PROD-008` 冻结。

### 7.4 Execution

- OMS 创建和派发一次性 OrderIntent。
- 查询、取消仍可能成交的订单。
- 建立/替换 Protection。
- Margin Controller 执行认证保证金动作。

所有命令关联 Authorization 和 Risk Reservation；场所结果以私有事实确认。

### 7.5 Capital

- 创建、审核、取消资金提案。
- CTO 执行和对账 Capital Transfer。
- 启停独立自动归集/补充政策。

资金命令使用 Treasury 权限与独立 `capital_transfer_id`，不得复用交易 Approval。

---

## 八、统一事件信封

每个领域事件至少包含：

| 字段 | 含义 |
| --- | --- |
| `event_id` | 全局唯一，消费者去重主键 |
| `event_type` | 过去时事实名称 |
| `schema_version` | 事件载荷版本 |
| `object_type/id` | 所属权威对象 |
| `object_version/sequence` | 对象内严格递增 |
| `occurred_at` | 业务事实发生时间 |
| `observed_at` | 本系统观察时间 |
| `recorded_at` | 原子持久化时间 |
| `producer` | 产生服务和版本 |
| `correlation_id / causation_id` | 旅程与因果 |
| `actor/auth_context_ref` | 人或服务身份与鉴权证据 |
| `tenant/workspace/scope` | 数据与权限边界 |
| `payload` | 版本化事实，不含可变查询投影 |
| `source_evidence_ref` | 场所原始回执、链上交易或内部命令证据 |

事件时间不是接收顺序。消费者按 object sequence 维护顺序；发现缺口先补齐或进入 Unknown，不自行猜测。

---

## 九、事件分类

必须至少覆盖：

- ProposalCreated/Prechecked/Frozen/Superseded/Expired。
- ApprovalRequested/Approved/Rejected/Returned/Expired。
- AuthorizationIssued/Activated/Revoked/Expired/Consumed。
- RiskEvaluated/Reserved/Released/Unknown/StateTightened/RecoveryApproved。
- AddUnitClaimed/ReleasedZeroFill/Consumed/Invalidated/Expired；候选评估作为决策证据，不作为 AddUnit 生命周期事件。
- OrderIntentCreated/Dispatched/Acknowledged/PartiallyFilled/Filled/Cancelled/Rejected/Unknown。
- VenueFillObserved/PositionObserved/BalanceObserved/ReconciliationDifferenceFound/Reconciled。
- ProtectionRequired/Confirmed/Degraded/Triggered/Failed。
- CampaignOpened/TargetChanged/ClosingStarted/Closed。
- MarginWorkflowCreated/PositionLegReconciled/MarginLegSettled/SafeReduced/Unknown。
- CapitalTransferRequested/Approved/SourceReserved/InFlight/DestinationConfirmed/Settled/Unknown。
- IdentityTagGranted/Revoked、MFAResult、PermissionDenied、BreakGlassInvoked。
- NotificationQueued/Sent/Delivered/Acknowledged/Failed。

命名可以在详细 Schema 阶段规范化，但不得用事件同时表达请求和结果。

---

## 十、投递、顺序与重放

- 生产者先在业务事务中写入事实、Ledger 和 outbox，再异步发布。
- 消费者先用 `event_id` inbox 去重，再推进自己的投影/流程。
- 交付语义为至少一次，不依赖“消息系统恰好一次”保证资金和订单安全。
- 相同对象的事件保持 sequence；跨对象不假设全局顺序。
- 缺失 sequence、Schema 不支持或证据不完整时停止相应增险流程并告警。
- 重放只能重建投影、统计和验证，不得重新执行外部订单、签名或资金转账。
- 外部副作用使用稳定 business idempotency 和场所查询闭环，不通过发布事件次数推断执行次数。
- 死信不自动丢弃；按业务重要性进入告警、隔离和人工恢复。

消息设施和完整事件溯源是否采用由 `DEC-ARCH-002`、`DEC-ARCH-003` 冻结。

---

## 十一、逻辑数据模型

事实库至少需要以下逻辑域，具体表结构由详细设计给出：

| 域 | 核心事实 |
| --- | --- |
| Identity | 内部 user/service-principal 映射、label、ABAC scope、撤权事实和外部 `auth_context_ref`；会话、MFA challenge 与设备注册优先由托管 IdP 管理 |
| Market Catalog | venue、execution domain、instrument、underlying、sector、risk cluster、`CapabilityCertificate` 引用 |
| Strategy | strategy、parameter version、signal/candidate evidence |
| Proposal/Review | versioned proposal、ReviewerVote、ApprovalDecision、TradingAuthorization |
| Campaign | 唯一经济仓位生命周期、AddUnit、target、protection requirement、exit reason |
| Risk | decision snapshot、capacity、Reserved/Open/Unknown/Stress Heat、system state |
| Execution | `OrderIntent`、venue order、fill、可重建 venue position/balance projection、reconciliation |
| Margin | MarginNormalizationWorkflow、fencing、released/reserved margin、settlement |
| Capital | vault/account balance、funding envelope、capital transfer、fee、confirmation |
| Audit | command、event、policy/version、actor、evidence、correction |
| Notification | template、recipient、delivery、acknowledgement、escalation |

所有外部事实保留原始不可变证据引用与归一化版本；敏感原始载荷按最小化、加密和访问审计处理。场所仓位只是由场所事实与账本重建的投影，不建立第二个经济仓位生命周期。

### 11.1 `CapabilityCertificate` 统一 Schema

系统只使用一个能力证书对象；不同能力以 `certificate_type` 区分，不创建平行证书实体。Schema 至少包含：

| 字段 | 合同 |
| --- | --- |
| `certificate_id / schema_version` | 全局唯一身份与 Schema 版本 |
| `certificate_type` | `STRATEGY_EVIDENCE`、`EXECUTION`、`RISK_COVERAGE` 或 `MARGIN_NORMALIZATION` |
| `subject_ref / scope` | 精确绑定策略/参数、来源、板块、方向、Instrument、venue/execution domain、adapter、worker/config、账户、margin mode、collateral pool、风险档位和 Add 层级；不适用维度显式为 `NOT_APPLICABLE` |
| `evidence_refs / policy_versions` | 不可变证据及其策略、风险、Catalog、Adapter 和执行版本 |
| `status` | `ACTIVE`、`SUSPENDED`、`REVOKED` 或 `EXPIRED`；只有 `ACTIVE` 可满足资格门 |
| `issued_at / valid_from / expires_at` | 签发与有效时间边界 |
| `issuer_principal / approval_ref` | 签发主体和批准证据；不能是被认证的执行 worker 自签 |
| `invalidation_conditions / supersedes` | 规则、版本、账户、作用域或证据变化时的确定性失效条件和替代关系 |

证书不授予人工审核权限，也不授予任何辅助建议源交易/资金密钥、签名、下单、划转或覆盖风控的权限。人工权限继续由 RBAC/ABAC、职责分离、MFA 和 Approval 事实决定。

---

## 十二、数据约束

必须具备可验证的唯一与引用约束：

- proposal version、approval、authorization 和 campaign 的精确绑定。
- `idempotency_key` 在命令作用域唯一。
- venue/execution/account 下 order/trade ID 唯一。
- 一个 AddUnit 不得有两个已消费结果。
- 同一风险数量在 Reserved/Open/Unknown 中互斥。
- 同一 Capital Transfer 的源、在途、目的资本互斥。
- 终态和历史事件不可原地删除或降级。
- 所有金额保存原生币种、精度、换算价格和版本。
- 所有时间以 UTC 事实保存，并保留场所时间和观察时间。

已确认的关系型事实库下，业务事实、Ledger、outbox 和幂等结果必须位于可证明的原子边界内。

---

## 十三、错误与拒绝分类

错误必须结构化，至少包括：

- `AUTHENTICATION_REQUIRED` / `MFA_REQUIRED`。
- `PERMISSION_DENIED` / `SELF_REVIEW_DENIED` / `SEPARATION_OF_DUTIES_REQUIRED`。
- `VERSION_CONFLICT` / `ALREADY_FINAL` / `EXPIRED`。
- `VALIDATION_FAILED` / `INSTRUMENT_NOT_ELIGIBLE`。
- `DATA_STALE` / `FACTS_UNKNOWN` / `RECONCILIATION_REQUIRED`。
- `RISK_DENIED`，并附具体风险作用域和规则 ID。
- `FUNDING_NOT_SETTLED` / `MARGIN_INSUFFICIENT` / `PROTECTION_UNAVAILABLE`。
- `VENUE_UNAVAILABLE` / `RATE_LIMITED` / `EXECUTION_UNKNOWN`。
- `AUDIT_UNAVAILABLE` / `SYSTEM_REDUCE_ONLY`。

错误响应保存 correlation ID、稳定原因码、用户可读解释、是否可重试及安全下一步。不得把所有场所错误映射为“下单失败”。

---

## 十四、审计不可抵赖

必须审计：

- 每个命令的原始业务字段 hash、actor、channel、session、device、MFA 和权限结果。
- 每次允许/拒绝的规则、政策版本和解释。
- Proposal、Approval、Authorization、Risk、Order、Fill、Protection、Margin、Capital 的全部迁移。
- 标签、作用域、自审、密钥、配置、`CapabilityCertificate` 和发布变更。
- 任何 break-glass、外部人工交易、修正、重放和数据导出。

审计数据只追加；修正通过新事件关联原记录。访问审计、加密、保留、防篡改和删除政策由 `DEC-GOV-002`、`DEC-SEC-002` 冻结。

通知和普通应用日志不是不可变审计的替代品；日志中不得包含密钥、完整签名或不必要的个人/资金敏感信息。

---

## 十五、Schema 与兼容治理

- API、Command、Event、Ledger 和 Catalog Schema 分别版本化。
- 增量兼容变更不能改变旧字段含义；破坏性变更使用新 major 和迁移窗口。
- 生产者升级前验证所有关键消费者支持目标版本。
- 未识别的关键事件或风险字段必须失败关闭，而不是忽略。
- 参数、策略和权限变化属于业务版本，不用 Schema 版本掩盖。
- 历史重放固定使用事件产生时的 Schema/归一化版本。
- Schema 退役、数据迁移和回滚按 `DEC-GOV-001` 走发布审批。

---

## 十六、安全边界

- 外部和内部 API 均认证服务身份；网络位置不等于信任。
- Freqtrade 接口不直接暴露公网，不接受用户令牌。
- webhook、Telegram callback 和场所回调验证签名、nonce、时效和重放。
- 高风险命令必须动作级 MFA；权限撤销立即影响新命令。
- 资金和交易服务使用不同凭据、网络策略和审计域。
- Query 按 ABAC 脱敏；Observer 不因只读自动获得完整资金和身份数据。

具体身份、秘密、Telegram 和 PWA 安全由 `DEC-SEC-001`、`DEC-SEC-002`、`DEC-SEC-004`、`DEC-SEC-005` 冻结。

---

## 十七、验收门

详细设计和实现必须证明：

- 所有增险 API 最终汇合至 Trading Authorization、Risk 和 OMS。
- 相同幂等键在断线、超时和重复点击下只产生一个业务结果。
- 版本冲突不会覆盖已处理 Approval、AddUnit 或终态。
- Command 接受、外部发送、场所成交和最终对账是可区分状态。
- 重复、乱序、缺失和重放事件不会重复下单、重复释放 Heat 或重复移动资本。
- 主事实库与 outbox/inbox 故障触发 `DEC-OPS-007` 的安全降级。
- 查询总是展示 as-of、新鲜度和成熟度；Unknown 不显示为零或成功。
- 审计能从用户动作追踪到风险、订单、成交、仓位和资金结果。
- Schema 升级、回滚和历史重放不会改变旧业务含义。

尚需补充的外部事实或研究证据只引用统一 `DEC-*`；本契约不得形成第二份决策清单。
