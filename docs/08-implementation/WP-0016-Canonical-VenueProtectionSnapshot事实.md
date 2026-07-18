# WP-0016：Canonical VenueProtectionSnapshot 事实

## 1. 交付目标

WP-0016 建立交易所私有保护集合的唯一规范事实：`VenueProtectionSnapshot`。

本工作包只解决“场所实际报告了什么保护集合、它对应哪一条规范仓位、该事实属于哪个完整对账输入”这三个问题。
它不发送保护单，也不把保护事实直接接入 `ExecutionFact.PROTECTION_CONFIRMED`；后者由下一工作包完成。

唯一新归一化命令为：

```text
execution.venue-protection-snapshot.record.v1
```

命令仅允许内部 reconciliation service 在最新、未过期、仍持有有效 fencing lease 的
`SHADOW / COLLECTING` 对账运行中使用。

## 2. 规范身份与归属

保护快照的全局外部身份为：

```text
organization
× venue
× execution_domain
× account
× instrument
× position_mode
× position_side
× margin_mode
× collateral_pool_id
× venue_update_id
```

每条保护快照必须引用一条已存在的 immutable `VenuePositionSnapshot`。两者必须在以下字段精确一致：

- organization、venue、execution domain、account、instrument；
- position mode、position side、margin mode、collateral pool；
- 被引用仓位必须为 `OPEN`；
- 保护事实事件时间不得早于被引用仓位快照；
- 已知保护状态的 direction 与 position quantity 必须精确等于被引用仓位。

同一外部身份与相同 `snapshot_hash` 可以跨后续对账运行重新链接；同一身份出现不同 immutable 语义时拒绝。

## 3. 三态保护合同

### 3.1 `CONFIRMED`

必须同时满足：

- `position_quantity > 0`；
- `covered_quantity = position_quantity`；
- `uncovered_quantity = 0`；
- `active_stop_order_count >= 1`；
- `venue_native = true`；
- `reduce_only_confirmed = true`；
- `replacement_in_progress = false`。

### 3.2 `DEGRADED`

仓位与覆盖量均为已知，且：

```text
covered_quantity + uncovered_quantity = position_quantity
```

同时至少存在一个缺口：未覆盖数量大于零、没有活动止损单、不是场所原生、reduce-only 未确认，或正处于替换窗口。

### 3.3 `UNKNOWN`

不得把未知保护解释为零保护或已保护：position/covered/uncovered/count 均为 `NULL`，方向为
`UNKNOWN`，所有保护确认布尔值均为 `false`。

## 4. 输入成员关系与数据库约束

`VenueFactInputLink` 扩展为 order/fill/position/protection 四选一：

- source 必须是 `VENUE_PROTECTION`；
- link 必须精确绑定 run、input、input hash、snapshot hash、原始载荷与 evidence；
- manifest 中 `item_count` 必须等于 immutable protection links 数量，缺项或超量均阻止进入
  `COMPARING / ADJUSTING / SUCCEEDED`；
- 新事实必须在同一事务中创建第一条 immutable input link；
- protection snapshot 与 input link 均禁止 update/delete；
- 服务层与 PostgreSQL trigger 同时验证 route、scope、position binding、lease 和水位窗口。

## 5. 未臆造的生产语义

`DEC-EXEC-004` 的 Binance/Hyperliquid 原生触发单类型、触发价来源、替换行为、极端穿透与
reduce-only 等价语义仍为 `RESEARCH_REQUIRED`。本工作包只保存完整归一化订单集合的
`order_set_hash` 和 immutable normalized payload，不把尚未认证的场所细节写成通用生产结论。

因此：

- 不开启 `LIVE_ORDER_SEND`；
- 不声称已获得逐场所原生保护认证；
- `CONFIRMED` 目前仅是 shadow canonical fact，不能单独证明实盘能力；
- 下一工作包仍需关闭现有弱 `VENUE_PROTECTION` 执行事实路径，并把 exact snapshot 强绑定到
  `PROTECTION_CONFIRMED`。
