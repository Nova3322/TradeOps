# WP-0007：耐久 CapabilityCertificate 与失效传播

> 状态：Implemented
> 上位合同：《API、事件、数据与审计契约》第 11.1 节、
> 《实施路线图与工作分解》第 5 节、《主策略参数与能力认证登记表》第 2、9、10 节、
> 《Binance USDⓈ-M Futures 执行认证清单》第 3、14、15 节
> 前置包：WP-0001..0006

## 交付边界

本包把 `CapabilityCertificate` 从 Proposal/Risk 调用方提供的字符串和布尔声明，提升为 Trading 自己
持有、校验和收紧的耐久事实：

```text
immutable CapabilityEvidenceBundle
  -> immutable exact-scope CapabilityCertificate
  -> ACTIVE current state + append-only state history
  -> Proposal PRECHECK / TradingAuthorization issuance / final ORDER_PRECHECK
     每次从 PostgreSQL 重新校验证书、证据、范围、版本、额度和有效期
  -> SUSPENDED / REVOKED / EXPIRED
     同事务不可逆失效现有 Initial / Add 增险授权
```

迁移与数据库约束把本包限制为 `environment=SHADOW`、`real_funds_eligible=false`。签发命令名称也是
`capability.certificate.issue-shadow.v1`，不存在 production 或 small-live 签发入口。数据库不 seed
任何证书，迁移后的证书表为空；`LIVE_ORDER_SEND`、`AUTO_ADD`、`CAPITAL_TRANSFER` 继续为
`DISABLED`。

因此本包建立的是证书控制合同和影子验证基础，不是 Binance、Hyperliquid 或任何账户的认证结果。

## 统一证书与精确范围

系统继续使用上位合同规定的单一 `CapabilityCertificate` 对象，`certificate_type` 只取：

- `STRATEGY_EVIDENCE`；
- `EXECUTION`；
- `RISK_COVERAGE`；
- `MARGIN_NORMALIZATION`。

当前 TradingAuthorization 和 final precheck 只接受 `EXECUTION` 类型的 SHADOW 证书。生产能力未来仍须
实现策略、风险、执行、保证金（适用时）和发布证据的交集门，本包没有用一张测试证书替代该交集。

证书 `scope` 无 wildcard，逐项精确绑定：

- SYSTEM/MANUAL 来源、strategy/version；
- venue、execution domain、account、account abstraction、position mode、margin mode；
- collateral scope/pool、instrument、contract multiplier、underlying、sector、risk cluster、方向；
- risk tier、允许的 Add count、settlement asset、capital-transfer capability；
- worker ID、worker config hash、非秘密 credential fingerprint。

`policy_versions` 进一步绑定策略参数、Risk、Authorization、Catalog、execution capability、Adapter、
Freqtrade worker、账户能力、凭证权限摘要、venue client、instrument scope/whitelist、position-management
和 Add milestone 版本。证书还冻结最大订单名义价值、最大交易损失、owner、issuer、独立 approver、
批准引用、监控引用、退出/恢复路径、失效条件和有效期。

worker 不能签发或批准自己的证书，issuer 不能自批。当前这些身份/批准字段只用于测试型内部 SHADOW
签发合同；真实审批事实、真实账户/凭据和正式认证证据尚不存在，不能据此声称职责分离已生产认证。

## 不可变证据与完整性

`CapabilityEvidenceBundle` 保存 SHADOW evidence refs、摘要、创建主体、环境和 profile，并生成 canonical
hash。证书保存 scope hash、policy-version hash、evidence-bundle hash 和整张证书 hash。

每次资格判断都会重新计算并核对：

- 证书、current state 和 evidence bundle 全部存在；
- current state 为 `ACTIVE`；
- `valid_from <= validation_time < expires_at`；
- organization、certificate type、SHADOW 环境和 `real_funds_eligible=false`；
- 完整 scope、policy versions、hash 和证据链；
- 当前订单名义价值和交易损失不超过证书冻结额度。

数据库 trigger 禁止更新或删除 evidence bundle、certificate root 和 state history。旧的 pre-WP-0007
Authorization/OrderIntent 引用通过 `NOT VALID` 外键保留可读性，但不会被追认；迁移后所有新写入都必须
引用真实存在的耐久证书。TradingAuthorization 的 certificate/organization 使用复合外键，OrderIntent
另有数据库 trigger 要求证书引用与其 TradingAuthorization 完全相同，不能在 OMS 层替换证书。

## Risk 与 Authorization 接入

`RiskPrecheckRequest` 已删除 `capability_certificate_valid`。Pydantic contract 使用 `extra=forbid`，旧调用方
继续提交该布尔值会稳定得到输入拒绝，无法再声明自己的证书有效。

纯 `RiskEvaluator` 只消费由 Trading 服务从数据库派生的 `CapabilityValidationResult`。Proposal precheck
会把完整验证快照写入 immutable RiskDecision；证书缺失或无效时产生持久 `DENY`，主原因是
`CAPABILITY_CERTIFICATE_INVALID`，具体缺失、范围、版本、额度、状态、有效期或完整性原因保留在输入
快照中。

`TradingAuthorizationService.issue` 在人工 Approval 之后、创建任何授权根之前，用冻结 Proposal 和完整
执行绑定重新校验证书。证书缺失或错配时不产生 TradingAuthorization、Campaign、Initial 或 Add 行。

`ExecutionIntentService.create` 在 final ORDER_PRECHECK 内再次加锁读取证书。验证有效期也进入 OrderIntent
的最短 `valid_until`；即使 Proposal 阶段曾通过，后续漂移、暂停或过期也不能沿用旧结论。

## 单向状态、并发与失效传播

证书 current state 从 `ACTIVE` 只能进入 `SUSPENDED`、`REVOKED` 或 `EXPIRED`；`SUSPENDED` 只可继续
收紧到 `REVOKED/EXPIRED`，不能回到 `ACTIVE`。过期命令只有在 `expires_at` 到达后才成功；即使状态
投影尚未执行过期命令，每次资格判断也会按时间边界立即失败关闭。

证书收紧和授权传播在同一 PostgreSQL 事务中发生：

- `InitialAuthorizationState.ACTIVE -> INVALIDATED`；
- Add package 的 `DORMANT/ACTIVE -> INVALIDATED`；
- AddUnit 的 `AVAILABLE/CLAIMED -> INVALIDATED`；
- 已消费 Initial/Add、现有仓位、保护、对账、reduce-only 和退出事实不被删除或伪造。

恢复只能签发新证书，并通过 `supersedes` 引用同组织、同类型、同精确 scope 的已暂停/撤销/过期证书。
新证书不会复活旧 Proposal、TradingAuthorization、Initial、AddUnit 或 OrderIntent。

签发按 certificate ID 获取 PostgreSQL advisory transaction lock。两个不同命令并发签发同一 ID 时，
只会产生一张证书和一个证据包，后到事务稳定返回 `CAPABILITY_CERTIFICATE_ALREADY_EXISTS`。

## 监控、错误与追踪

新增低基数指标：

- `trading_capability_certificate_issuance_total{certificate_type,environment,result}`；
- `trading_capability_certificate_transitions_total{action,target_status,result}`；
- `trading_capability_certificate_validations_total{result,primary_reason}`。

稳定错误/原因区分：缺失、组织/类型/环境、inactive、有效期、scope、version、notional/loss limit、证书
完整性、证据缺失/完整性、重复身份、非法 supersedes、提前过期、终态和版本冲突。签发和收紧命令继续
通过 durable receipt、immutable audit 和 transactional outbox 保存关联证据。

追踪：`STATE-009`、`RISK-003/024`、`REQ-RISK-008`、`REQ-EXEC-003/004/005/011`、
`REQ-DATA-001/003`、`REQ-OPS-002`，以及 `TEST-002/004/005/009/012`。

## 迁移与回滚

迁移 `20260718_0007` 新增：

- `capability_evidence_bundles`；
- `capability_certificates`；
- `capability_certificate_states`；
- `capability_certificate_state_history`；
- 新 Authorization/OrderIntent 到证书根的外键；
- root/evidence/history 不可变守卫、current-state 单向迁移守卫和自动历史。

应用回滚优先保留 0007 schema 和证书审计事实。只有在一次性环境或完成证据导出、停止 0007 writer 后
才执行：

```bash
TRADING_DATABASE_URL="$DATABASE_URL" uv run alembic downgrade 20260718_0006
```

该操作会删除全部 0007 证书、证据和状态历史，并移除新外键；它不会恢复证书、授权或交易资格。
重新升级不会 seed 证书，旧引用继续按未认证处理。

## 明确未实现与实盘边界

- production/small-live 证书签发、真实能力 gate 激活和真实订单派发；
- 正式证据采集、证据阶段判定、认证审批工作流、到期扫描任务和告警路由；
- 策略、风险、执行、Margin 和发布证书的生产交集门；
- Binance、Hyperliquid Core/HIP-3 的真实账户、权限、规则、worker、场所和保护证据；
- sender/fencing、Freqtrade/VenueAdapter、VenueOrder/Fill、真实 position/protection 和重启对账；
- Web/PWA 证书管理页、Telegram、Margin、Vault/CTO、PnL、备份恢复和运营演练；
- 历史回放、长期实时影子、场所仿真/测试网、故障演练和受限小额实盘证据。

测试证书的 fingerprint、账户、Adapter、worker、审批和 evidence refs 均为虚构值。它们只能证明本地
SHADOW 控制合同，不证明任何真实 venue/account/instrument 已认证，更不允许真实资金动作。
