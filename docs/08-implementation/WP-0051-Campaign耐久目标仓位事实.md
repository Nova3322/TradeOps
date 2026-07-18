# WP-0051：Campaign 耐久目标仓位事实

## 1. 交付目标与领域边界

领域模型规定 `TargetPositionArbiter` 必须保持 Campaign 内的纯规则，不能拥有独立服务、表或状态机；但
Campaign 必须保存裁决后的目标与原因事实。WP-0051 新增 Campaign-owned immutable
`campaign_target_position_facts`，以及仅允许内部 `campaign-target-service` 调用的
`campaign.target-position.evaluate-record.v1` 命令。

命令在同一数据库事务内：

1. 取得 Campaign advisory lock、Campaign row/state lock 和 latest target fact lock；
2. 使用服务器时钟和显式 freshness TTL 调用 WP-0050 canonical 保护健康来源；
3. 通过 WP-0049 重新解析 current position 并执行 WP-0048 纯仲裁；
4. 保存完整 canonical decision payload、受约束数值投影、来源 hashes 和 Campaign target version；
5. zero target 时把 Campaign `OPEN -> CLOSING` 原子收紧；
6. 由既有 command receipt/audit/outbox 事务保存命令结果与领域事件。

本包没有改变纯仲裁器边界，也没有创建 OrderIntent、sender claim 或场所请求。

## 2. 不可变事实合同

每个 fact 保存：

- Campaign、organization、连续 `target_version`；
- server-resolved current-position binding hash 与 position snapshot ID/hash；
- current/target/reduction quantity、`HOLD/REDUCE/EXIT`、urgency 和 reduce-only requirement；
- selected source refs、reason codes 与 input candidate hashes；
- 完整 `target-position-decision-v1` canonical JSON payload 与原 decision hash；
- 忽略瞬时时间与 candidate hash 的 `target_semantic_hash`；
- `campaign-target-position-fact-v1` record hash；
- `environment=SHADOW`、`live_order_eligible=false`。

完整 canonical payload 是 hash 复核真源；数值列是数据库查询和约束投影。这样 PostgreSQL
`NUMERIC(38,18)` 对零值的不同文本表示不会破坏原始 decision hash，payload 与投影不一致仍会被模型和
数据库 insert guard 双重拒绝。

## 3. 并发、幂等与单调收紧

- 第一个 target fact 要求 `expected_version=null`；后续命令必须提供 latest target version；
- 同一 command/idempotency key 由现有 receipt 机制重放，不重复写 fact/event；
- 相同 target semantic hash 返回 `NO_CHANGE`，不以新的 evaluation time 制造重复 target version；
- 新 `target_quantity` 不得高于 latest durable target；保护恢复也不能自动取消已经耐久化的 zero target；
- target version 必须逐一连续，数据库唯一约束和 insert guard 同时执行；
- UPDATE/DELETE 被数据库 trigger 拒绝。

zero target 首次写入时 Campaign 主状态从 `OPEN` 原子迁移到 `CLOSING`，并沿用已有
`authorization_state_transitions` 留痕。事实写入失败时状态不会单独变化。

## 4. 数据库与监控

Alembic revision 从 `20260718_0035` 升至 `20260718_0036`，新增：

- `campaign_target_position_facts`；
- exact Campaign/position insert guard；
- contiguous version、monotonic target 与 immutable update/delete guards；
- Campaign/version 与 Campaign/semantic unique constraints；
- latest-by-Campaign index。

新增 bounded metric：

```text
trading_campaign_target_fact_recordings_total{result}
```

## 5. 未完成范围

- 当前 producer registry 只有 WP-0050 保护健康来源；独立硬止损、趋势、动态去杠杆和其他系统风险来源待实现；
- WP-0045 仍是 opening-only current-position binding，实际部分减仓后的新 current binding 尚未实现；
- durable target 尚未转换为 reduce-only OrderIntent；
- target revision 与已存在/Unknown/部分成交退出 intent 的互斥、取消替换及 over-reduction 防护未实现；
- 原生保护成交与控制面退出之间的 single-writer/对账语义未完成；
- OMS/Freqtrade/VenueAdapter、真实 collectors 与逐场所认证未完成。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
