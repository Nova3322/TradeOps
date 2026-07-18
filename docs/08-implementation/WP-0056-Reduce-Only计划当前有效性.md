# WP-0056：Reduce-Only 计划当前有效性

## 1. 交付目标与边界

WP-0054 保存的 reduction plan 是不可变历史证据，不应因 target、position 或 intent state 后续变化而被当作
持续有效。WP-0056 新增 `CampaignReductionPlanValidityService`，对指定 plan snapshot 做 current-use 判定，
明确区分历史存在与当前仍匹配。

本包不修改或删除旧快照，不创建 replacement、claim 或 OrderIntent，也不把 `CURRENT` 解释为可发送。

## 2. 有界有效性状态

服务返回以下状态之一：

```text
CURRENT
SUPERSEDED
EXPIRED
POSITION_CHANGED
POSITION_UNAVAILABLE
INTENT_OCCUPIED
CAMPAIGN_STATE_INVALID
TARGET_NOT_ACTIONABLE
```

判定顺序：

1. 复核 snapshot record hash 与完整 plan payload；
2. latest durable target ID/version 不同则 `SUPERSEDED`；
3. query time 超过 stored plan validity 则 `EXPIRED`；
4. 重新运行 WP-0053 resolver，消费 fresh current position、Campaign state 与 WP-0055 occupancy；
5. 排除 query-time `planned_at/plan_hash` 后，服务器派生合同仍须与 stored plan 完全一致，否则
   `POSITION_CHANGED`；
6. 只有全部一致才返回 `CURRENT`。

结果携带 stored/current target identity、plan hash、stored valid-until、reprepare flag 与独立 validity hash。

## 3. CURRENT 仍不是发送许可

即使 status 为 `CURRENT`，响应仍固定：

```text
reason_code=EXECUTION_TERMS_UNAVAILABLE
order_type_status=UNAVAILABLE
venue_execution_terms_status=UNAVAILABLE
dispatch_eligible=false
```

任何非 CURRENT 状态都要求 `reprepare_required=true`。真正 claim、OrderIntent 创建和发送前必须在同一受控事务
再次验证；query-time validity 不能缓存或复用为 permit。

## 4. 失败关闭与监控

snapshot 不存在固定抛出：

```text
CAMPAIGN_REDUCTION_PLAN_SNAPSHOT_MISSING
```

已知 resolver 拒绝码被映射为有界状态；未识别错误不会被降格成业务状态，而是继续失败关闭。

新增 bounded metric：

```text
trading_campaign_reduction_plan_validity_evaluations_total{result}
```

## 5. 数据库与未完成范围

本包不新增 migration；schema 仍为 `20260718_0037`。旧 plan snapshot 保持 immutable，supersession 通过 latest
target identity 派生，而不是回写历史记录。

仍未完成：

- replacement preparation 的自动编排与旧 work item 消费阻断；
- reduction OrderIntent/数量预留、claim、sender fencing 与 execution state；
- 原生保护和控制面退出 single-writer；
- 逐场所执行条款与真实 OMS/Freqtrade/VenueAdapter。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
