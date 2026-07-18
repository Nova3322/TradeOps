# WP-0035：不可变 Risk Fact Set 强绑定

## 1. 交付目标与边界

WP-0034 后，`RiskPrecheckRequest.facts` 仍允许调用方直接提交 MARKET、ACCOUNT、VAULT、POSITIONS、
ORDERS、LEDGER、CATALOG、VENUE_CAPABILITY、PROTECTION 九类事实的状态、时间和 hash。即使 Risk Engine
会检查完整性、新鲜度和一致性，调用方仍能自行选择这些值。

WP-0035 删除请求中的 `facts`，新增 SHADOW-only、不可变、完整覆盖九类事实的 Risk Fact Set。提案预检和
最终 ORDER_PRECHECK 在各自事务内按 exact scope 读取服务端最新集合，风险计算只消费经过完整性验证的
耐久 observation。缺失、篡改、过期或处于有效期外的集合一律关闭新增风险。

本包建立的是事实集合合同和验证边界，不是现实数据采集器。本包不声称完成：

- Binance、Hyperliquid、Vault、Catalog 或其他来源的真实 collector；
- 九类 payload 的业务内容复算、现实来源认证或持续健康判定；
- production、small-live、真实账户、真实凭证或现实资金能力；
- 真实 OMS/Freqtrade/VenueAdapter 发送链。

迁移不 seed 任何集合。测试集合由固定测试夹具构造，只能用于 SHADOW 合同验证，不构成真实事实或实盘证据。

## 2. v8 命令合同

```text
risk.precheck.evaluate.v8      payload_schema_version=8
execution.intent.create.v8     payload_schema_version=8
```

v1 至 v7 风险/意图命令返回 `COMMAND_TYPE_MISMATCH`，v8 携带非 8 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。`RiskPrecheckRequest` 删除：

```text
facts
```

请求模型保持 `extra="forbid"`。风险 v8 携带旧 `facts` 返回 `RISK_INPUT_INVALID`；执行 v8 嵌套旧字段返回
`EXECUTION_INPUT_INVALID`，不会静默忽略或退回 caller-trusted 语义。

集合写入口独立使用：

```text
risk.fact-set.register.v1
payload_schema_version=1
service_principal=risk-fact-aggregator-service
channel=INTERNAL
```

只有 exact principal 可注册。命令绑定 Fact Set UUID、organization scope、固定 object version；统一命令执行器
继续提供 idempotent receipt、audit event 和 transactional outbox event。

## 3. 完整、不可变的 exact-scope 集合

迁移 `20260718_0030` 新增 `risk_fact_sets`。每行绑定：

- organization、venue、execution domain、account、canonical instrument；
- position mode、margin mode、collateral pool；
- fact set ID/version、assembled/valid window；
- 九个 canonical observation；
- record/evidence hash 与 evidence/source 引用；
- `environment=SHADOW`、`real_funds_eligible=false`。

每个 observation 固定包含：

```text
fact_type
status = KNOWN | UNKNOWN
source_ref
source_version
payload_hash
event_time
received_at
```

Pydantic 与 PostgreSQL 双层约束保证：

- 九类事实必须各出现一次，按 `fact_type` 排序；
- observation/evidence 必须完整、canonical，hash 格式有效；
- event/received/assembled/valid time 时区明确且单调；
- exact scope/version 唯一，Fact Set identity/version 可由决策复合外键引用；
- `UPDATE`/`DELETE` 由 trigger 拒绝，任何变化必须创建新集合；
- 有集合事实时 migration downgrade 明确失败，避免历史证据被静默删除。

## 4. 服务端解析与并发边界

Risk Fact Set Validator 使用以下 exact scope 查询最新 `assembled_at`：

```text
organization_id
venue
execution_domain
account_id
canonical_instrument_id
position_mode
margin_mode
collateral_pool_id
```

验证器从数据库行重建完整请求合同，复算 record/evidence hash，再校验集合有效期。Proposal 与 final precheck
均不接收 Fact Set ID/version 选择权，也不允许调用方回退到旧 observation。

注册和增险预检使用相同 exact-scope PostgreSQL transaction advisory lock。若注册先获得锁，预检在注册提交后
读取新集合；若预检先获得锁，新的集合要等待该预检事务结束。最终 ORDER_PRECHECK 另对选中行加锁，决策
始终绑定本事务验证过的 immutable exact row，不会在预检中途切换集合。

## 5. Fail-closed 语义与风险有效期

集合层失败保留详细 reason code：

```text
RISK_FACT_SET_RECORD_NOT_FOUND
RISK_FACT_SET_INTEGRITY_FAILED
RISK_FACT_SET_OUTSIDE_VALID_WINDOW
```

Risk Engine 统一转换为：

```text
primary_reason_code = RISK_FACT_SET_UNAVAILABLE
final_quantity      = 0
valid_until         = decision_time
```

集合验证通过后，既有事实检查继续生效：

- 任一 `UNKNOWN`：`FACTS_UNKNOWN`；
- 事件时间超过政策 freshness：`FACTS_STALE`；
- 未来时间越界：`FACT_TIMESTAMP_IN_FUTURE`；
- receive time 早于 event time：模型层拒绝；
- 九类 event time 超出 consistency window：`FACTS_INCONSISTENT`。

ALLOW 的 `valid_until` 取 Risk Policy、Capability Certificate、Instrument Catalog、Protection Capability、
Risk Fact Set、逐事实 freshness 及当前保护投影的最早截止点。最新集合即使无效也会遮蔽更旧的健康集合，系统
不会为了放行而回退历史事实。

## 6. 决策证据、迁移与指标

`RiskEvaluationInput` 保存完整 Fact Set validation snapshot。`risk_decision_snapshots` 与
`execution_risk_decisions` 新增历史兼容列：

```text
risk_fact_set_id
risk_fact_set_version
risk_fact_set_record_hash
```

三列必须全空或完整，并以 composite foreign key 绑定 immutable Fact Set。旧历史行允许全空；v8 找到行时，
ALLOW 或 DENY 都保存 exact ID/version/hash，输入快照、决策、audit/outbox 同步保存验证结果与 reason code。

新增指标：

```text
trading_risk_fact_set_registrations_total{result}
trading_risk_fact_set_validations_total{result,primary_reason}
```

在 disposable test DB 清空集合后，允许 `0030 -> 0029 -> 0030` 迁移往返。生产回滚不得删除历史集合或重新
启用 v7 caller facts。

## 7. 需求追踪

| 权威要求 | 本包证据 |
| --- | --- |
| 九类事实必须完整、唯一且可追溯 | canonical complete set、source/version/hash/time 双层约束 |
| Unknown、陈旧或不一致禁止增险 | Validator + 既有 freshness/consistency evaluator 失败关闭 |
| 调用方不得选择或伪造风险事实 | v8 删除 `facts`，服务端 exact-scope 解析最新耐久集合 |
| 提案和最终预检使用相同事实边界 | 两条服务链复用同一 Validator 与 scope lock |
| 决策可复算且事实变化不改写历史 | immutable row、decision composite FK、snapshot/hash |
| 事实有效期不得被更长策略覆盖 | ALLOW `valid_until` 由 set 和 observation freshness 收紧 |

## 8. 明确未完成范围

- 当前 `risk-fact-aggregator-service` 只有注册合同，没有真实进程、调度、HA、健康或身份部署；
- 九类 observation 尚未逐一绑定现实 canonical payload、collector watermark 和来源证书；
- Market/Vault/Catalog/venue-capability 的真实采集、规则变化与主动失效传播未实现；
- ADD trend/equity 等专用盈利候选事实仍需后续可信真源包；
- 正式 FX/USD、稳定币折扣/脱锚、真实 OMS/Freqtrade/VenueAdapter、Web/PWA、Telegram、Margin、
  Vault/CTO、PnL 与运维仍未完成；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

按用户明确约束，Codex Security 及其所有审计 Skill、插件和模块保持停用；本包只执行常规代码检查、严格
类型检查、数据库约束与测试。只有用户未来明确重新授权后，才会另行考虑 Codex Security。
