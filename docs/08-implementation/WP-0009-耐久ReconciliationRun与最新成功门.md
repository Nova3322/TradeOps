# WP-0009：耐久 ReconciliationRun 与最新成功门

> 状态：Implemented
> 上位合同：《领域模型与状态机》第 1、4、9、14、16、17 节、
> 《OMS、Freqtrade 与 VenueAdapter 执行规范》第 8、10、13、14、16 节、
> 《API 事件数据与审计契约》的事件重放边界、《测试验证与发布计划》第 5、6、8、9 节、
> 《SLO、可观测性、故障恢复与 Runbook》第 3、6、8 节
> 前置包：WP-0001..0008

## 交付边界

本包把 WP-0008 lease 中仅为字符串的 reconciliation evidence ref 收紧为耐久、可验证的
`ExecutionReconciliationRun`。每次运行精确绑定当前 SHADOW sender lease 与 fencing token，冻结输入源、
查询窗口和 deadline，并保存水位、差异及关闭证据：

```text
current exact-scope SHADOW lease + fencing token
  -> immutable ReconciliationRun + frozen seven-source manifest
  -> one immutable input snapshot and watermark per source
  -> RUNNING phase: COLLECTING -> COMPARING -> ADJUSTING
  -> immutable Finding -> optional immutable RESOLVED_SAFE evidence
  -> terminal UNKNOWN | SUCCEEDED | FAILED
  -> only latest same-lease SUCCEEDED run may enter ShadowDispatchClaim
     external_send_permitted=false
```

本包不实现真实 VenueAdapter、Freqtrade 查询、私有账户流、场所订单/成交/仓位投影或任何网络调用。测试输入
只使用虚构引用和 hash，不能证明 Binance、Hyperliquid、任何账户或任何 instrument 已完成真实对账。

## 冻结运行合同与输入水位

`execution-reconciliation-service` 只能在 exact sender scope 当前 lease 有效、token 匹配、worker/config/
credential fingerprint 完整且 `LIVE_ORDER_SEND=DISABLED` 时启动运行。root 创建后不可更新或删除，固定：

- organization、sender scope、lease ID 与 fencing token；
- `environment=SHADOW`、`live_dispatch_eligible=false`；
- STARTUP、私有流重连、订单 Unknown、部分成交、Campaign 关闭或人工恢复 trigger；
- observation window、deadline、发起主体、原因和来源；
- 七类输入源的完整 manifest；
- canonical run hash 和可选 supersedes run。

固定输入源为：

1. `TRADING_LEDGER`；
2. `VENUE_ORDERS`；
3. `VENUE_FILLS`；
4. `VENUE_POSITIONS`；
5. `VENUE_BALANCES`；
6. `VENUE_PROTECTION`；
7. `WORKER_LOCAL`。

每个源在一次 run 内最多保存一条 immutable snapshot，包含 COMPLETE/UNKNOWN、source version、watermark
type/value、覆盖窗口、观察与接收时间、item count、payload/evidence ref/hash 和 canonical input hash。输入水位
必须覆盖冻结 observation window；任一源缺失或 UNKNOWN 都不能进入 COMPARING，也不能得到 SUCCEEDED。
修正输入必须新建后续 run，不能原地改写旧事实。

## 状态、phase 与不可变差异

run 状态遵守上位领域合同：

- `RUNNING`：唯一可追加输入、Finding 或 Resolution 的状态；
- `UNKNOWN`：输入、分页、查询窗口或事实不完整的不可逆终态；
- `SUCCEEDED`：完整输入内全部差异可解释，且相同 lease/token 仍为当前 authority；
- `FAILED`：无法完成的安全终态，新增风险保持关闭。

`COLLECTING -> COMPARING -> ADJUSTING` 是单调 phase，不是平行状态机。phase 不能回退或跳级。终态不能
恢复为 RUNNING；需要继续恢复时必须创建新 run。

Finding root 不可变，保存 sequence、category、INFO/WARNING/BLOCKING/UNKNOWN severity、subject、expected/
observed snapshot/hash 和证据。BLOCKING/UNKNOWN 在没有 resolution row 时计入 unresolved count。关闭 Finding
只允许在 ADJUSTING 追加一条 `RESOLVED_SAFE` resolution，保存限定 resolution type、纠正动作和证据 hash；
不存在“忽略风险”或覆盖 Risk Engine 的 disposition。

SUCCEEDED 由服务和 deferred database graph guard 双重派生验证，调用方不能提交“已完整”布尔值：七源集合
必须精确相等且全部 COMPLETE、状态计数必须等于事实表、阻断/未知差异必须为零、terminal result 必须绑定
run/scope/lease/token，并明确 `no_historical_replay=true`、`external_send_permitted=false`。

## 最新运行与重启失效语义

同一 sender scope 最多一个 RUNNING run。后续 run 必须显式 `supersedes_run_id` 指向该 scope 最新终态 run，
数据库插入 trigger 独立验证链头，避免丢失一次失败或 Unknown 恢复尝试。

Shadow claim 不只检查“某次曾经成功”，还要求：

- run 与当前 exact scope、lease ID、token、organization 完全一致；
- root/run result hash 在读取时重算一致；
- run 状态为 SUCCEEDED，完成时间不晚于 claim 且没有超过 lease；
- 它仍是当前 lease/token 下最新 run；
- terminal result 明确没有历史业务重放和外部发送权限；
- WP-0008 的 OrderIntent、CapabilityCertificate、route、owner、clock 与 live gate 检查继续全部通过。

因此同一 lease 后续只要启动了新 run，旧 SUCCEEDED 立即不能再用于 claim；新 run 为 RUNNING、UNKNOWN 或
FAILED 时都保持 fail closed。lease 被 fence/release/expire 或由新 token 接管后，旧 run 同样不能复用。

本包没有“重放历史订单”的命令。重启后的事件/账本重放仍只能重建投影、比较事实和追加修正证据，不能
创建或发送新的业务 OrderIntent。

## 数据库完整性

迁移 `20260718_0009` 新增：

- `execution_reconciliation_runs`；
- `execution_reconciliation_inputs`；
- `execution_reconciliation_findings`；
- `execution_reconciliation_finding_resolutions`；
- `execution_reconciliation_run_states`；
- `execution_reconciliation_run_state_history`。

同时为 `shadow_dispatch_claims` 增加 nullable historical-compatible 的 `reconciliation_run_id` 与
`reconciliation_result_hash`。0009 writer 和 claim trigger 强制新 claim 两项都有值；升级前的旧 SHADOW
claim 可继续作为历史 dry-run 事实保存，但不会被补写或提升为新合同。

数据库约束、immediate trigger 与 deferred constraint trigger 负责：

- root、input、Finding、Resolution 和 history 不可更新/删除；
- run/lease/scope/token 与 organization 复合外键绑定；
- 最新 scope run 的 supersedes 链与单 scope 单 RUNNING；
- child fact 只能在正确 RUNNING phase 插入；
- state version、phase、terminal 与时间单调；
- state counts 在事务提交时必须精确等于 input/Finding/Resolution 事实；
- COMPARING/ADJUSTING/SUCCEEDED 必须拥有精确七源 COMPLETE manifest；
- SUCCEEDED 必须保持同一 current lease/token 且零 unresolved blocking；
- terminal result snapshot 必须绑定 run/scope/lease/token、安全布尔值；
- 每次状态变化自动写 append-only history 和数据库 snapshot hash；
- claim 插入时再次验证最新 SUCCEEDED run、result binding 和 WP-0008 完整 fenced graph。

## 命令、监控、错误与追踪

新增六个内部幂等命令：start、record input、advance phase、record finding、resolve finding 和 finish。全部只
接受 `execution-reconciliation-service` 内部主体，沿用 durable command receipt、immutable audit、
transactional outbox、correlation/causation 和稳定错误码。

新增低基数指标：

- `trading_reconciliation_run_transitions_total{from_status,to_status,phase}`；
- `trading_reconciliation_inputs_total{source_type,collection_status}`；
- `trading_reconciliation_findings_total{severity,disposition}`。

稳定错误覆盖 scope/lease/token、版本、deadline、并发 active run、supersedes 链、输入 manifest、水位、
phase、Finding、Resolution、未关闭阻断差异、terminal mutation 和最新成功门。Unknown/Failed 终态均不会
打开 claim 或任何 live gate。

追踪：`RISK-025`、`REQ-EXEC-008/009`、`REQ-DATA-003`、`REQ-OPS-002/004`，以及
`TEST-008/009/011/016`、`EVID-006/009/011`。

## 迁移与回滚

应用回滚优先保留 0009 schema 和对账证据。只有停止 0009 writer、确认没有进行中的 run、导出所需审计
事实并保持全部 live capability gate 关闭后才执行：

```bash
TRADING_DATABASE_URL="$DATABASE_URL" uv run alembic downgrade 20260718_0008
```

降级会删除全部 ReconciliationRun、input、Finding、Resolution、state/history，并从 shadow claim 删除 0009
绑定列；WP-0008 sender scope、lease 和历史 dry-run claim 保留。降级不会打开 `LIVE_ORDER_SEND`，也不会
恢复任何旧发送权限。重新升级不 seed run 或 claim。

## 明确未实现与实盘边界

- Binance、Hyperliquid Core/HIP-3 或任何真实账户的 fact collector 与分页/限速实现；
- VenueOrder、Fill、Position、Protection 和 balance 权威投影及修正分录；
- Freqtrade/worker 启动编排、mTLS、sidecar、真实 sender 接口和发送瞬间 fencing；
- reduce-only 保护/减仓/退出的独立恢复路径；
- 私有流断线、部分成交、Unknown、双主、数据库/网络分区的真实故障演练；
- production/small-live reconciliation policy、deadline、watermark freshness、告警和人工解锁；
- Web/PWA、Telegram、Margin、Vault/CTO、Catalog、财务对账/PnL、备份恢复和正式运行资产。

本包只能证明本地 SHADOW 对账控制合同与 fail-closed claim 门成立。它不能把测试 hash 解释为真实场所事实，
不能把任何 scope 标为 `CERTIFIED`，不能发送真实订单，也不能开放真实资金权限。
