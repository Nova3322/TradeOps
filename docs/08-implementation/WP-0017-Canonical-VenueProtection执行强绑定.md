# WP-0017：Canonical VenueProtection 执行强绑定

## 1. 交付目标

WP-0017 把 `ExecutionFact.PROTECTION_CONFIRMED` 从可自报的弱 payload 路径升级为对
canonical `VenueProtectionSnapshot` 的强引用。

唯一新执行事实命令为：

```text
execution.fact.record-reconciled.v5
```

本工作包不发送保护单，不连接实盘 venue，也不认证 Binance 或 Hyperliquid 的逐场所原生保护
语义。它只规定：一个已经规范化、属于完整对账输入的保护快照，在什么条件下可以推进本地执行
状态机。

## 2. 唯一允许的推进

只有下列状态转换可由 `VENUE_PROTECTION` 事实证明：

```text
POSITION_RECONCILED
  -> PROTECTION_CONFIRMED
```

必须同时满足：

- `protection_state = CONFIRMED`；
- 保护快照引用的 `venue_position_snapshot_id` 与该 intent 既有 canonical
  `VENUE_POSITION` ExecutionFact 完全相同；
- organization、venue、execution domain、account、instrument、position mode、position side、
  margin mode、collateral pool 和 direction 全部匹配；
- `position_quantity` 与 `covered_quantity` 均精确等于 post-intent position quantity；
- `uncovered_quantity = 0`；
- 至少存在一条活动止损单；
- `venue_native = true`、`reduce_only_confirmed = true`；
- `replacement_in_progress = false`；
- protection event 不早于 position reconciliation evidence；
- request 的数量、terminal flags、事件时间、raw source、evidence 和 payload 都是 canonical
  snapshot/link 的精确投影。

`DEGRADED`、`UNKNOWN`、错误仓位、错误数量、错误 scope、陈旧事件或不完整引用均失败关闭。

## 3. v5 精确引用合同

`VENUE_PROTECTION` ExecutionFact 只允许以下 canonical 引用形状：

```text
venue_protection_snapshot_id = NOT NULL
venue_fact_input_link_id     = NOT NULL
venue_fact_hash              = 64-char hash
venue_order_observation_id   = NULL
venue_fill_id                = NULL
venue_position_snapshot_id   = NULL
canonical_venue_order_id     = NULL
```

payload 必须精确等于：

```json
{
  "venue_fact_type": "VENUE_PROTECTION_SNAPSHOT",
  "venue_fact_id": "<venue_protection_snapshot_id>",
  "venue_fact_hash": "<snapshot_hash>",
  "venue_fact_input_link_id": "<venue_fact_input_link_id>",
  "venue_position_snapshot_id": "<bound_position_snapshot_id>"
}
```

相同外部事实可幂等 replay；同一外部身份出现不同语义时拒绝。successor lease 可以沿已验证的
reconciliation lineage 为原 claim 补记事实，但不能改变 claim ownership、canonical position 或
venue fact semantics。

## 4. 状态来源收紧

本工作包关闭两条弱路径：

- `VENUE_PROTECTION -> COMPLETED`；
- `VENUE_PROTECTION -> FAILED_SAFE`。

`COMPLETED` 当前没有可接受的新事实来源，必须等待后续独立、确定性的完成工作流；不能由保护
快照自报完成。`FAILED_SAFE` 仍只接受受 fencing 约束的 `WORKER_RECEIPT` 来源。

## 5. 数据库绕过防护

迁移 `20260718_0017` 增加：

- `execution_facts.venue_protection_snapshot_id` 外键和唯一约束；
- execution-fact v5 exact-one binding check；
- 按顺序串联的 v5 → v4 → v3 → v4 → v5 BEFORE INSERT guards；
- canonical protection ownership、coverage、position identity、time 与 payload 的数据库验证；
- v5 原子 state/exposure application constraint trigger；
- 新 v1/v2/v3/v4 execution-fact 写入关闭；
- 存在 v5 facts 时拒绝降级，防止证据被静默丢失。

因此直接数据库插入不能把 `DEGRADED`/`UNKNOWN` 保护、错误仓位或伪造 payload 提升为
`PROTECTION_CONFIRMED`。

## 6. 明确边界

本工作包仍处于 `SHADOW`：

- `AUTO_ADD`、`CAPITAL_TRANSFER`、`LIVE_ORDER_SEND` 均保持 `DISABLED`；
- 没有真实 venue credential、下单连接、保护单发送或故障演练；
- `DEC-EXEC-004` 的逐场所 stop type、trigger source、穿透和 replacement 语义仍为
  `RESEARCH_REQUIRED`；
- 不能据此声称实盘保护已认证或系统已满足真钱上线条件；
- 按用户要求，本工作包未运行 Codex Security 审计，只执行常规工程测试与数据库验证。
