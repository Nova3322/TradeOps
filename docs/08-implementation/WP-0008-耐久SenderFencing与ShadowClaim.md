# WP-0008：耐久 Sender Fencing 与 Shadow Claim

> 状态：Implemented
> 上位合同：《OMS、Freqtrade 与 VenueAdapter 执行规范》第 4.2、8、10、13、14 节、
> 《领域模型与状态机》第 4、9、14 节、《测试验证与发布计划》第 5、6、8、9 节、
> 《SLO、可观测性、故障恢复与 Runbook》第 3、6、8 节
> 前置包：WP-0001..0007

## 交付边界

本包建立 Trading 内部的耐久 sender fencing authority，证明同一个精确执行域不能同时持有两个有效
发送租约，并允许不可发送的 SHADOW OrderIntent 留下一条 dry-run claim：

```text
immutable exact ExecutionSenderScope
  -> immutable lease issuance with monotonically increasing fencing token
  -> one current scope state + append-only state history
  -> current lease validation at claim time
  -> immutable ShadowDispatchClaim
     execution_mode=SHADOW
     external_send_permitted=false
     LIVE_ORDER_SEND=DISABLED
```

本包没有 worker 控制接口、VenueAdapter、Freqtrade 插件、VenueOrder、Fill 或任何网络发送代码。claim
不把 OrderIntent 从 `INTENT_CREATED` 改为 `DISPATCHING`，也不是未来发送方可缓存复用的 permit。实际发送
路径仍不存在。

## 精确 sender scope

sender scope 无 wildcard，身份由下列字段的 canonical hash 确定：

- organization；
- venue 与 execution domain；
- account；
- account abstraction 与 position mode；
- margin mode；
- collateral scope 与 collateral pool。

数据库对完整字段建立唯一约束，不能使用不同 `scope_id` 为同一个执行域创建平行 authority。每个 scope
固定为 `environment=SHADOW`、`live_dispatch_eligible=false`，root 创建后不可更新或删除。

worker 身份不属于 scope 本身，而属于每次 lease issuance；这样 owner 可以经过 fencing 接管，但同一
scope 在任一时刻仍只有一个当前 lease。lease 精确绑定：

- owner worker ID；
- worker config hash；
- 非秘密 credential fingerprint；
- reconciliation evidence ref 与 current risk-state acknowledgement ref；
- SHADOW fencing policy version；
- initial expiry 与不可突破的 maximum expiry；
- canonical lease hash。

上述 evidence ref 当前来自受信的内部 SHADOW fencing service，只证明测试合同已保存引用；正式
`ReconciliationRun`、真实 worker 身份认证和真实恢复证据尚未实现。

## 单调 fencing token 与租约

`execution-fencing-service` 使用 PostgreSQL advisory transaction lock 串行化同一 scope 的 acquire。
初次 acquire 得到 token 1；renew 只延长同一个 lease 的 expiry，不改变 token；owner 改变、release、
expire 或 fence 都必须把 token 严格递增。

当前状态只有：

- `LEASED`：存在一个未来到期的 current lease；
- `UNOWNED`：没有 owner，旧 token 已失效；
- `FENCED`：控制面显式隔离，旧 token 已失效。

`FENCE` 即使在 current lease 已不明时也会继续推进 token，避免未知旧 sender 沿用最后可见代数。
`RELEASE` 只允许 current owner；`EXPIRE/FENCE` 只允许 fencing service。终止 lease 后重新 acquire 会
再次递增 token，绝不复活旧 lease。

acquire、renew 与 claim 都比较 worker observation time 和 authority time。当前允许的 5 秒差值只是
SHADOW 工程测试参数，不是生产 SLO 或认证阈值；超过即返回 `WORKER_CLOCK_SKEW_EXCEEDED`。默认运行时
使用 PostgreSQL `clock_timestamp()` 作为 authority time，测试才注入确定性时钟。

## ShadowDispatchClaim

当前 owner 只能对仍为 `INTENT_CREATED`、仍在有效期内的 SHADOW OrderIntent 创建一次 claim。claim 前
同一事务重新读取并验证：

- exact scope、current lease ID、fencing token、expiry 与 lease hash；
- owner worker、config hash、credential fingerprint；
- OrderIntent 的 venue/domain/account/margin/collateral/worker 路由；
- OrderIntent state version 与有效期；
- ACTIVE SHADOW CapabilityCertificate 的完整性、scope、policy versions、额度和有效期；
- certificate 的 account abstraction、position mode、instrument 与方向；
- `LIVE_ORDER_SEND` 当前仍为 `DISABLED`。

claim 的 client order identity 由 `scope_id + order_intent_id` 确定性生成；同一 OrderIntent 最多一条
claim，同一 scope 下 client order identity 不可复用。claim 保存 intent、scope、lease、certificate
hash、原因码、worker observation time、claim time 和 canonical claim hash。

数据库 `BEFORE INSERT` trigger 会独立读取 current scope state、lease、OrderIntent、risk decision、
OrderIntent state、CapabilityCertificate state 和 live gate，再次阻断旧 token、跨组织/跨 scope、错误
worker、过期窗口、非 SHADOW、已变更 OrderIntent、inactive certificate 或 live gate 非关闭状态。

claim root 不可更新或删除。它只证明“这个影子意图在这个时间点通过了 fencing dry-run”；它没有发送
权限，不改变风险账本，不产生场所订单，也不能被未来 production sender 当作发送授权。

## 数据库完整性

迁移 `20260718_0008` 新增：

- `execution_sender_scopes`；
- `execution_sender_leases`；
- `execution_sender_scope_states`；
- `execution_sender_scope_state_history`；
- `shadow_dispatch_claims`。

数据库约束和 trigger 负责：

- exact scope 唯一、scope/lease/claim root 与 history 不可变；
- active lease、scope 和 token 使用复合外键绑定；
- state version 与 fencing token 单调；
- 同 lease renew 只能延长 expiry，不能换 token；
- ownership change 或 invalidation 必须推进 token；
- inactive scope 不能保留 active lease；
- 每次 state insert/update 自动生成 append-only history 和 SHA-256 snapshot hash；
- 每个 OrderIntent 最多一条不可发送 claim；
- claim 插入时按当前数据库事实重新校验完整 fenced graph。

## 监控、错误与追踪

新增低基数指标：

- `trading_sender_lease_operations_total{operation,result}`；
- `trading_sender_lease_validations_total{result,primary_reason}`；
- `trading_shadow_dispatch_claims_total{result}`。

稳定错误区分 input/object/org/version、scope/lease 缺失或完整性、active owner、token、expiry、最大
lifetime、worker clock skew、OrderIntent state/window、route、CapabilityCertificate 和 live gate。
所有 acquire/renew/tighten/claim 命令继续由 durable receipt 提供幂等；成功和拒绝都进入 immutable audit，
成功事件通过 transactional outbox 发布。

追踪：`RISK-004/025`、`REQ-EXEC-001/003/004/007/009`、`REQ-DATA-003`、`REQ-OPS-002/004`，以及
`TEST-003/004/005/009/016`。

## 迁移与回滚

应用回滚优先保留 0008 schema 和 fencing 历史。只有停止 0008 writer、确认没有任何外部发送路径、并
导出所需审计事实后才执行：

```bash
TRADING_DATABASE_URL="$DATABASE_URL" uv run alembic downgrade 20260718_0007
```

降级会删除全部 sender scope、lease、history 和 shadow claim。它不会恢复旧 sender 权限，也不会开启
`LIVE_ORDER_SEND`。重新升级不会 seed scope、lease 或 claim。

## 明确未实现与实盘边界

- production/small-live sender lease 或真实 dispatch permit；
- Freqtrade 受控接口、mTLS worker 身份、VenueAdapter、VenueOrder、Fill 和交易所调用；
- 真实 `ReconciliationRun`、重启时全量场所事实对账、旧意图查询/失效与恢复解锁；
- 备用 worker 的真实接管、网络分区/数据库故障/时钟源故障演练和 P0 告警路由；
- worker 端或 sidecar 对 fencing token 的强制验证及发送瞬间的原子权威检查；
- reduce-only 保护/减仓/退出的预认证故障路径；
- Binance、Hyperliquid Core/HIP-3 的真实账户、worker、凭据权限和执行证据；
- Web/PWA、Telegram、Margin、Vault/CTO、PnL、备份恢复和正式运行资产。

测试 worker、account、credential fingerprint、reconciliation ref 和 risk-state ref 均为虚构值。本包
只能支持本地 SHADOW 工程证据，不能把任何 venue/account/domain 标记为 `CERTIFIED`，更不能开放真实
订单或资金权限。
