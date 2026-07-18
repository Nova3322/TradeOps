# WP-0028：Canonical 保护触发价事实

## 1. 交付目标与权威边界

WP-0027 已把费用、止损穿透和不利 funding stress 改为 Risk Engine 的策略绑定计算，但
protected-profit giveback 仍缺少规范保护价输入。WP-0016 的 `VenueProtectionSnapshot` 只能证明覆盖数量、
活动止损单数量和订单集合 hash，不能回答完整保护集合当前最差的活动触发价。

本包只补齐这个耐久事实前提：

```text
worst_active_trigger_price
```

它是活动、场所原生、reduce-only 保护集合中的规范触发价，不是成交保证价，也不是预期退出价。WP-0027
的 stop-penetration stress 继续独立覆盖触发后的穿透；本包不计算 protected-profit giveback，不开启任何
现实交易能力。

## 2. 规范化合同

内部命令升级为：

```text
execution.venue-protection-snapshot.record.v2
payload_schema_version = 2
```

历史 v1 返回 `COMMAND_TYPE_MISMATCH`，v2 携带非 2 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。字段同时进入 normalized payload、evidence hash、snapshot hash 和
不可变 `VenueProtectionSnapshot`。

三态语义如下：

- `CONFIRMED`：`worst_active_trigger_price` 必填且大于零；
- `DEGRADED`：字段可未知，已知时必须大于零；
- `UNKNOWN`：字段必须为 `NULL`。

规范 collector 对完整活动止损集合取保守边界：

```text
LONG  -> 最低活动触发价
SHORT -> 最高活动触发价
```

服务层与 PostgreSQL 插入触发器还要求它位于当前 canonical Mark 的保护侧：LONG 必须低于 Mark，
SHORT 必须高于 Mark。数据库 check 使用显式 `IS NOT NULL`，避免 SQL 三值逻辑让 confirmed 空值通过。

## 3. 耐久性、错误处理与监控

迁移 `20260718_0026`：

- 新增 nullable `NUMERIC(38,18)`；nullable 用于三态，不代表 CONFIRMED 可缺失；
- 重建 coverage check 并建立跨表 Mark/direction 插入触发器；
- 只允许在旧保护事实表为空时升级，拒绝把缺少触发价的历史事实静默解释为 v2；
- 表内存在保护事实时拒绝 downgrade，防止丢失已 hash 绑定的触发价。

业务拒绝码：

```text
VENUE_PROTECTION_SNAPSHOT_INVALID
VENUE_PROTECTION_TRIGGER_PRICE_INVALID
COMMAND_TYPE_MISMATCH
PAYLOAD_SCHEMA_VERSION_MISMATCH
```

既有 `venue_fact_normalizations_total`、`venue_fact_input_links_total`、命令收据、审计事件和 outbox 继续覆盖
成功路径；拒绝结果保存在幂等命令收据中。没有新增独立运行服务或不可重建状态。

## 4. 需求追踪

| 权威要求 | 本包证据 |
| --- | --- |
| 原生保护不依赖 UI 且保护不足时 fail closed | v2 confirmed 必须含 canonical trigger，错误侧或未知触发价不能形成 confirmed fact |
| 数据未知禁止新增风险 | `UNKNOWN` 不携带价格，不能冒充 `CONFIRMED` |
| 事实库、不可变账本与可重建投影 | trigger 进入 immutable snapshot/hash/input link；未新增 Redis 唯一事实 |
| 每个执行域分别认证 | 字段继续绑定 venue/domain/account/instrument/mode/collateral scope |
| 迁移、测试、监控、错误处理、回滚和证据 | `0026`、双层约束、指标复用、拒绝码、防数据丢失 downgrade 与验证记录 |

## 5. 回滚

只有 `venue_protection_snapshots` 为空时允许：

```text
alembic downgrade 20260718_0025
```

回滚删除专用插入触发器和函数、恢复旧 coverage check，再删除新列。已有事实时精确拒绝：

```text
cannot remove canonical protection trigger prices while protection snapshots remain
```

## 6. 明确未完成范围

- 仓库尚无真实 Binance/Hyperliquid 私有保护 collector，无法从原始订单腿独立证明 LONG-min/SHORT-max；
- `order_set_hash` 和 evidence 目前只绑定测试构造的 collector 声明，未形成场所认证证据；
- 尚未持久化 campaign peak、受保护盈利、换算币种和 giveback 公式，protected-profit giveback 仍未规范派生；
- 触发价不是成交价，真实止损穿透继续依赖 `DEC-RISK-010` 的逐场所研究；
- Web/PWA、Telegram、真实 OMS/Freqtrade/VenueAdapter、Margin、Vault/CTO、PnL 和运维仍未完成；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

按用户特别约束，Codex Security 及其审计 Skill、插件和模块保持停用；本包仅使用正常架构约束、
代码检查、数据库迁移和工程测试。
