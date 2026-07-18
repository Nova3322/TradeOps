# WP-0033：不可变 Instrument Catalog 分类强绑定

> 后续状态：WP-0034 已将风险/意图命令升级为 v7，删除 caller `protection_available`，并以不可变
> exact 原生保护能力记录绑定账户、凭证、worker、Catalog 和保护模板。当前输入合同以
> [WP-0034](WP-0034-不可变原生保护能力强绑定.md) 为准；本文件保留 WP-0033 历史证据。

## 1. 交付目标与边界

WP-0032 之后，`RiskPrecheckRequest.instrument_classified` 仍是调用方布尔值。即使 Capability
Certificate 已绑定 instrument scope/catalog version，Risk Engine 仍无法从耐久 Catalog 事实独立证明该
标的已完成分类。WP-0033 删除这个调用方断言，新增 SHADOW-only、不可变、版本化的 Instrument Catalog
记录，并在提案预检和最终 ORDER_PRECHECK 的同一数据库事务内按 exact scope 解析。

本包只关闭“标的身份与分类真源”边界，不声称完成：

- Binance、Hyperliquid 或其他场所的真实动态发现与规则采集；
- 市场数据健康、listing operability 或 `eligible_to_trade` 的完整派生；
- 真实场所、账户、标的或执行域认证；
- 现实资金或订单发送能力。

所有记录固定为 `environment=SHADOW`、`real_funds_eligible=false`；不存在 migration seed 或生产默认
Catalog 行。

## 2. v6 命令合同

```text
risk.precheck.evaluate.v6      payload_schema_version=6
execution.intent.create.v6     payload_schema_version=6
```

v1 至 v5 命令返回 `COMMAND_TYPE_MISMATCH`，v6 携带非 6 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。`RiskPrecheckRequest` 删除：

```text
instrument_classified
```

请求模型继续使用 `extra="forbid"`。风险请求携带旧布尔值返回 `RISK_INPUT_INVALID`；执行请求嵌套旧布尔
值返回 `EXECUTION_INPUT_INVALID`，不会静默忽略或回退为 caller-trusted 语义。

Catalog 写入口独立使用：

```text
instrument_catalog.record.register.v1   payload_schema_version=1
service_principal=instrument-catalog-service
```

命令绑定 record UUID、organization scope、固定 object version，并通过 advisory transaction lock 阻止同一
instrument/catalog/classification version 的并发重复注册。命令执行器继续提供幂等 receipt、audit event 与
outbox event。

## 3. 不可变 Catalog 记录

迁移 `20260718_0028` 新增 `instrument_catalog_records`。每行显式保存：

- organization、venue、execution domain、native/canonical instrument ID、display symbol；
- catalog、metadata、classification 三类版本；
- perpetual contract type、underlying、sector、一个或多个 risk cluster；
- quote、settlement、collateral asset 与 contract multiplier；
- tick、lot、minimum quantity、minimum notional；
- discoverable、classification complete、approval scope、listing status；
- valid window、source observed time、record/evidence hash、evidence/source 引用。

数据库和 Pydantic 双层约束保证：

- 只能写 SHADOW 且不能具备现实资金资格；
- 数量与合约规则为正，valid window 非空，source observation 不晚于 valid-from；
- risk cluster 与 evidence 列表非空、排序且唯一；
- complete classification 不能使用 `UNCLASSIFIED`；
- exact identity/version 唯一；
- `UPDATE`/`DELETE` 由 trigger 拒绝，变化必须创建新版本；
- 表中已有事实时 downgrade 明确失败，避免历史证据被静默删除。

## 4. Exact classification 解析

风险请求只提供原有冻结 `CertificationBinding`。服务端按以下键查询唯一 Catalog 行：

```text
organization_id
venue
execution_domain
canonical_instrument_id
catalog_version
classification_version = instrument_scope_version
```

解析器重建并校验完整记录/evidence hash、SHADOW 环境和有效期，再与冻结 binding 逐项比较：

```text
underlying_id
sector
risk_cluster_id
settlement_asset
contract_multiplier
```

同时要求 `discoverable=true`、`classification_complete=true`、sector 非 `UNCLASSIFIED`。Catalog 权威合同
允许一个标的属于多个 risk cluster，但当前 `CertificationBinding` 只支持单个 cluster。因此本包保留完整
cluster 数组；只在数组恰好等于冻结单 cluster 时放行，多 cluster 返回
`MULTI_CLUSTER_RISK_SCOPE_UNSUPPORTED`，不会忽略额外暴露。

Catalog 缺失、过期、hash 失败、不完整、UNCLASSIFIED、scope 不一致或多 cluster 均生成详细 validation
reason，并由 Risk Engine 统一关闭新增风险：

```text
primary_reason_code = INSTRUMENT_UNCLASSIFIED
final_quantity      = 0
valid_until         = decision_time
```

这属于可审计的业务 `DENY`，不是把缺失事实伪装为传输错误。

## 5. 决策证据和有效期

`RiskEvaluationInput` 保存完整 `instrument_classification` validation snapshot；Risk decision 保存 record
ID/hash、evidence hash 和详细 Catalog reason。`risk_decision_snapshots` 与
`execution_risk_decisions` 新增可空历史兼容列：

```text
catalog_record_id
catalog_version
catalog_classification_version
catalog_record_hash
```

四列必须全空或完整，并以 composite foreign key 绑定不可变 Catalog 行。旧历史行允许全空；v6 在找到
Catalog 行时写入完整绑定。ALLOW 的有效期取 Risk Policy、Capability Certificate、Catalog、facts 和当前
保护投影中的最早截止点，Catalog 过期不能被更长的策略或证书有效期覆盖。

## 6. 监控、错误处理与回滚

新增指标：

```text
trading_instrument_catalog_registrations_total{result}
trading_instrument_catalog_validations_total{result,primary_reason}
```

回滚前必须保持现实能力门关闭并停止新的 v6 请求。存在 Catalog 事实时 migration downgrade 拒绝；只有在
一次性环境中明确清空事实后，才允许 `0028 -> 0027 -> 0028` 往返。生产回滚不得删除历史 Catalog 或决策
绑定，也不得重新启用 v5 caller boolean。

## 7. 需求追踪

| 权威要求 | 本包证据 |
| --- | --- |
| UNCLASSIFIED 新增风险为零 | durable validation invalid 时统一 `INSTRUMENT_UNCLASSIFIED` DENY |
| 发现不等于可交易 | 只保存正交分类事实，不持久化或声称 `eligible_to_trade` |
| Catalog 变化不改写历史 | immutable row、exact version unique、decision composite FK |
| 风险/执行使用 exact Catalog | proposal 与 final precheck 同事务解析相同冻结 scope |
| 任一决策可按版本重放 | record/evidence/input/decision hash 与 validity snapshot |
| 多风险簇不能被遗漏 | 保留数组；单 cluster binding 无法表达时失败关闭 |

## 8. 明确未完成范围

- 真实 venue discovery、公共/私有市场数据 collector、规则刷新和 symbol mapping 未实现；
- listing/operability、数据健康、能力证书和完整 `eligible_to_trade` 派生未实现；
- Catalog 变化触发 Proposal/Authorization/Certificate 自动失效传播尚未实现；
- `CertificationBinding` 的多 risk-cluster 表达与聚合风险政策尚未实现；
- caller `protection_available`、事实 observation 和 ADD trend/equity 布尔输入仍需后续可信真源包；
- 正式 FX/USD、稳定币折扣/脱锚、真实 OMS/Freqtrade/VenueAdapter、Web/PWA、Telegram、Margin、
  Vault/CTO、PnL 与运维仍未完成；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

按用户特别约束，Codex Security 及其审计 Skill、插件和模块保持停用；本包只执行常规代码检查、严格类型
检查、数据库约束与测试。
