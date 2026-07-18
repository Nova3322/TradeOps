# WP-0015：Canonical VenuePosition 执行强绑定

## 1. 交付目标

WP-0015 关闭 `VENUE_POSITION` 作为可自报执行事实推进 `POSITION_RECONCILED` 的路径。
唯一新写入口升级为：

```text
execution.fact.record-reconciled.v4
```

所有新 `ExecutionFact` 固定使用 `fact_contract_version=4`。历史 v1/v2/v3 行仅可读取；
对应旧命令和直接数据库新写均失败关闭。

## 2. 唯一可接受的仓位证据

`POSITION_RECONCILED` 必须同时引用：

- 一个不可变的 `VenuePositionSnapshot`；
- 该快照在本次 `VENUE_POSITIONS` 对账输入中的唯一 `VenueFactInputLink`；
- 快照哈希、输入哈希、运行哈希和原 dispatch claim 哈希；
- 当前有效或合法继任的 sender lease 与 fencing token。

请求只投影快照身份，不重复序列化仓位经济字段：

```json
{
  "venue_fact_type": "VENUE_POSITION_SNAPSHOT",
  "venue_fact_id": "<snapshot-uuid>",
  "venue_fact_hash": "<snapshot-hash>",
  "venue_fact_input_link_id": "<link-uuid>"
}
```

仓位数量、状态、方向和估值字段由快照哈希及不可变数据库行承载，避免两份数值文本产生歧义。

## 3. 推进规则

仅在以下条件全部成立时，才可推进到 `POSITION_RECONCILED`：

1. 当前意图状态为 `FILLED` 或 `CANCELLED_PARTIAL`；
2. 快照状态为 `OPEN`，`FLAT` 与 `UNKNOWN` 都不能推进；
3. 快照数量精确等于：

   ```text
   OrderIntent.current_position_quantity
   + OrderIntentState.cumulative_filled_quantity
   ```

4. organization、venue、execution domain、account、instrument、position mode、position side、
   margin mode、collateral pool 和方向均与原意图、claim、sender scope、risk reservation 精确一致；
5. 快照事件时间不得早于该意图最近一条 canonical order/fill 事实；
6. filled/remaining/terminal 标记必须原样保持当前执行状态，且
   `position_reconciled=true`、`protection_confirmed=false`；
7. 来源必须是 `VENUE_POSITION + VENUE_POSITIONS`，目标必须是
   `POSITION_RECONCILED`。

`VENUE_POSITION` 不再允许证明 `FAILED_SAFE`。失败安全状态必须由具有相应语义的 worker 或
protection 证据证明，不能由仓位来源自行声明。

## 4. 数据库防绕过

数据库迁移 `20260718_0015` 增加：

- `execution_facts.venue_position_snapshot_id` 外键与全局唯一约束；
- v4 exact-one canonical fact binding 检查；
- v4 前置仓位所有权、数量、时序、payload 和输入成员关系守卫；
- 对既有 claim/run/input/lease、canonical order/fill 守卫的复用；
- v4 延迟原子应用守卫，要求 ExecutionFact、OrderIntentState 和 RiskExposureState
  在同一事务中一致落库；
- 迁移降级时若仍有 v4 事实则明确拒绝，避免静默丢失 canonical position 证据。

## 5. 明确不在本工作包内

- 不发送真实订单，不开启 `LIVE_ORDER_SEND`；
- 不实现 protection order 或完整持仓投影；
- 不把 `FLAT` 当成零风险成功，也不把 `UNKNOWN` 当成零仓位；
- 不改变 3x/5x/10x、1R/2R/3R、加仓次数或风险预算规则；
- 不修改仓库中的用户资产目录与表格。

后续工作包应在本强绑定基础上实现 canonical protection facts 与原生保护单闭环。
