# WP-0011：继任 Lease 对账接管

> 状态：Implemented
> 上位合同：《OMS、Freqtrade 与 VenueAdapter 执行规范》第 7、8、13、14、16 节、
> 《领域模型与状态机》第 4、9、14、16、17 节、
> 《API 事件数据与审计契约》的恢复与重放边界、
> 《SLO、可观测性、故障恢复与 Runbook》第 6、8 节
> 前置包：WP-0001..0010

> 事实来源补充：WP-0013 保留本文的 current successor authority 与 lineage 规则，但把唯一新写入口升级为
> `execution.fact.record-reconciled.v3`；order/fill 必须同时引用 exact canonical venue fact 和 input membership。
> 本文的 v2 入口仅为历史合同。

## 问题与边界

WP-0010 首次关闭弱执行事实入口时，要求结果 ReconciliationRun 与原 ShadowDispatchClaim 使用完全相同的
lease ID 和 fencing token。该规则能阻止旧 owner，但也形成恢复死角：若原 worker 崩溃、lease 被 fence 或
过期，新的唯一 sender 获得更高 token 后即使查询到原订单的迟到成交，也无法把事实追加到原 OrderIntent。
真实仓位会永久停在 Reserved/Unknown，恢复只能失败而不能收敛。

本包把“谁曾经发送”和“谁当前有权对账”分开：

- immutable ShadowDispatchClaim 继续证明原 OrderIntent 由哪个旧 lease 领取，不能改写或重发；
- 当前最新 ReconciliationRun 证明现在由哪个唯一 lease/token 执行查询、比较和修正；
- successor run 只有沿完整 supersedes lineage 追溯到 claim-authorizing success 时，才能为原 claim 追加结果；
- 所有发送权限仍为 false，本包只开放事实收敛，不开放重新派发。

## 两种权威模式

每次新事实明确派生为以下固定模式之一：

| 模式 | 条件 | 允许动作 |
| --- | --- | --- |
| `ORIGINAL_LEASE` | run 与 claim 的 lease ID/token 完全相同 | 在原 sender session 中追加对账事实 |
| `SUCCESSOR_LEASE` | run token 严格大于 claim token，且 run lease 是当前唯一 sender | 只追加已查询到的迟到/恢复事实 |

相同 token 不允许切换 lease ID；较低 token 永远不能恢复为 authority。successor 不是 claim 的替换、续期或
重新批准，也不能产生第二个 client order identity。

## 继任接管不变量

successor fact 必须同时满足：

1. 原 claim 仍属于相同 OrderIntent、organization 和 exact sender scope，且保持 SHADOW、禁止外部发送；
2. 当前 run 与 claim 的 organization/scope 相同，token 不低于 claim；token 相等时 lease ID 必须相同；
3. run 是 scope 最新 run，状态 `RUNNING`，phase 为 `COMPARING` 或 `ADJUSTING`，deadline 未过；
4. run 的递归 supersedes lineage 必须包含 claim 使用的成功 run；
5. run 自身绑定当前 `execution_sender_scope_state` 的 active lease ID/token，不能借用 claim 的过期 authority；
6. input 仍为该 run 对应来源的 exact COMPLETE snapshot，hash、source version 和 event watermark 相等；
7. event 不早于原 claim，route、fact kind、source、status 和数量/Heat 语义继续通过 WP-0010/WP-0006；
8. OrderIntent、claim、run、input 与 ExecutionFact 全部不可变，重启重放只能返回同一事实。

这些条件允许新的 current owner 收敛历史订单，但不允许旧 owner、旧 run、terminal run、旁支 lineage、低 token
或任意新 payload 复活发送权。

## 服务与数据库执行

`execution.fact.record-reconciled.v2` 保持不变。服务在插入前派生 authority mode，并在结果和
`ExecutionFactReconciled` 事件中保存该模式；完全相同事实由新 service 实例读取时重新从 immutable
claim/run 关系派生相同模式。

迁移 `20260718_0011` 不新增业务表或平行状态机，只原位替换 PostgreSQL
`protect_reconciled_execution_fact_insert()`：

- run/claim 必须同 organization/scope；
- original 模式要求相同 lease/token；
- successor 模式要求更高 token；
- current sender state 改为精确匹配结果 run 的 lease/token；
- lineage、latest run、phase、deadline、input、event、route 和状态来源矩阵继续由数据库独立复核。

新增低基数指标：

```text
trading_execution_fact_authority_modes_total{authority_mode,result}
```

只允许 `ORIGINAL_LEASE`、`SUCCESSOR_LEASE`、历史读取保护值和固定结果，不记录 lease、token、run 或 claim ID。

## 失败与恢复语义

- 原 run 已 terminal 或不再 latest：不能继续写；必须由 current lease 启动 successor run。
- 原 lease 被 fence 但没有 current successor run：事实保持拒绝，不回落到旧 lease。
- successor run 输入 Unknown、缺失、过期或 lineage 不连续：保持 fail closed。
- successor lease 在事实提交前再次被 fence/expire：事实失败，新 owner 必须再建后续 run。
- 相同 external fact identity 在恢复重试中只返回原事实；不同语义复用 identity 返回冲突。

## 迁移与回滚

0011 只改变数据库 guard，并更新应用 readiness revision。回滚前必须停止 reconciliation writer、确认没有
successor recovery run 正在收敛迟到事实，并保持全部现实 capability gate 关闭：

```bash
TRADING_DATABASE_URL="$DATABASE_URL" uv run alembic downgrade 20260718_0010
```

降级恢复 WP-0010 的 same-lease-only guard；数据表和既有事实不删除，但新 token 将无法收敛旧 claim。该限制
是可预期的 fail-closed 回滚，不允许用旧 v1 或直接数据库写入绕过。

## 明确未实现与现实边界

- successor worker 的真实进程发现、leader election、mTLS、启动编排和告警；
- Freqtrade plugin/sidecar、VenueAdapter、私有 API 查询和迟到事件消费；
- VenueOrder、Fill、Position、Balance、Protection 的权威规范化对象；
- 真实双主、进程崩溃、网络/数据库分区、场所断线和时钟偏差演练；
- Web/PWA、Telegram、Margin Controller、Vault/CTO、Catalog、PnL 和正式 Runbook。

本包测试仍使用本地 SHADOW 数据。successor authority 只允许追加对账事实，不是订单发送、盲重试或现实能力
激活，不能打开 `LIVE_ORDER_SEND`，不能证明任何场所、账户或 worker 已通过认证。
