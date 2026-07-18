# WP-0014：Canonical VenuePositionSnapshot 事实

> 状态：Implemented
> 上位合同：《领域模型与状态机》第 3.4、3.7、4、10 节、
> 《OMS、Freqtrade 与 VenueAdapter 执行规范》第 4、5、6、7、13、14、16 节、
> 《财务对账与 PnL 口径》的 Venue Position、私有事实优先级与 Campaign 结算边界、
> 《SLO、可观测性、故障恢复与 Runbook》的 Private Fact Freshness 合同
> 前置包：WP-0001..0013

## 问题与边界

WP-0009 已冻结 `VENUE_POSITIONS` input，WP-0010/0013 也保留了 `VENUE_POSITION` 执行事实类型，但当前调用方
仍能在 v3 命令中自报 `position_reconciled=true`。在关闭这条弱入口前，必须先建立一个与 Freqtrade 本地投影无关、
可去重、可追溯到 exact input 的私有场所仓位事实层。

本包新增 immutable `VenuePositionSnapshot`，表示一次场所私有更新或完整快照中的一条 position line。它是外部事实，
不是第二个 Campaign、不是独立仓位生命周期，也不是可直接修改的 Position Projection。后续投影只能按时间与来源
折叠这些事实；本包不会让它直接推进 `POSITION_RECONCILED`。

唯一归一化命令为：

```text
execution.venue-position-snapshot.record.v1
```

只允许 `execution-reconciliation-service` 在 latest、RUNNING、COLLECTING、未过期的 SHADOW run 内调用。

## Canonical identity 与 exact scope

全局外部更新 identity 固定为：

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

同一 identity 和同一 `snapshot_hash` 在后续 ReconciliationRun 复用原 fact，只新增该 input 的 immutable link；
同一 identity 出现不同经济语义则返回冲突，不能覆盖旧记录。

`position_mode`、`margin_mode` 和 `collateral_pool_id` 必须与 exact ExecutionSenderScope 完全一致：

- `ONE_WAY` 只允许 `position_side=BOTH`，方向单独为 LONG/SHORT/FLAT/UNKNOWN；
- `HEDGE` 只允许 `position_side=LONG|SHORT`；OPEN 行的方向必须与 position side 相同；
- 不同 margin mode、collateral pool、execution domain 或 account 的 fact 不能进入当前 run。

这保留了 Binance、Hyperliquid Core、每个 HIP-3 domain、cross/isolated 及 collateral pool 的独立事实边界；
任何范围的真实认证证据都不能自动继承到另一个范围。

## OPEN、FLAT 与 UNKNOWN

`VenuePositionSnapshot` 不用零值掩盖未知：

| 状态 | 固定语义 |
| --- | --- |
| `OPEN` | direction 为 LONG/SHORT；quantity、entry、mark、notional、UPNL 已知；quantity 大于零 |
| `FLAT` | direction=FLAT、quantity=0、notional=0、UPNL=0；entry/liquidation/leverage 为空 |
| `UNKNOWN` | direction=UNKNOWN，quantity、价格、notional、UPNL、保证金、清算价和杠杆全部为空 |

OPEN 的 canonical notional 固定为：

```text
quantity × mark_price × contract_multiplier
```

`unrealized_pnl` 保存场所私有事实，正负和零均合法；它尚未汇总成 Current Portfolio MTM Equity。liquidation price、
leverage、initial margin 和 maintenance margin 可为空，但只要出现就必须满足正值或非负值合同。UNKNOWN 不允许保留
看似可信的部分经济值，避免下游误把残缺事实当作零仓或完整仓位。

FLAT 是场所明确返回的一条归一化事实，不等同于“某 instrument 没出现在响应中”。真实 adapter 若要用完整列表缺席
证明零仓，必须在逐场所合同与分页完整性认证后确定性地产生 FLAT line；本包不预设该外部语义。

## Exact input membership

`VenueFactInputLink` 新增 nullable `venue_position_snapshot_id`，并把 exact one-of 扩展为：

- `VENUE_ORDERS` 只引用 VenueOrderObservation；
- `VENUE_FILLS` 只引用 VenueFill；
- `VENUE_POSITIONS` 只引用 VenuePositionSnapshot。

每个 position fact 首次创建必须在同一事务获得 first immutable link。服务与 PostgreSQL 都要求：

1. link 属于 exact run/input/organization/source/input hash；
2. fact event time 落在 frozen input watermark；
3. raw payload/evidence hash 与首次 observation 完全一致；
4. normalized link 数不能超过 input `item_count`；
5. run 从 COLLECTING 进入 COMPARING/ADJUSTING/SUCCEEDED 前，position link 数必须精确等于 item count。

因此 incomplete position manifest、过量 fact、wrong source、跨 run/input link 和直接数据库推进 phase 均失败关闭。

## 服务、数据库与可观测性

服务先取得 external identity advisory lock，再锁 run state，验证 current lease、scope、input 和数量上限；fact、link、
command receipt、audit 与 outbox 在同一事务提交。稳定错误包括：

- `VENUE_POSITION_SNAPSHOT_INVALID`
- `VENUE_POSITION_SCOPE_MISMATCH`
- `VENUE_POSITION_SNAPSHOT_ID_CONFLICT`
- `VENUE_POSITION_SNAPSHOT_CONFLICT`
- 既有 `VENUE_FACT_INPUT_*`、`VENUE_FACT_COLLECTION_*` 与 reconciliation count 错误

迁移 `20260718_0014` 在数据库侧独立实现：

- OPEN/FLAT/UNKNOWN 数量、价格、notional、UPNL 与 nullable 语义 check；
- ONE_WAY/HEDGE、position side 与 direction check；
- external update unique identity、first run/input FK 和 immutable UPDATE/DELETE trigger；
- sender scope、current lease、latest run、watermark 和 first-link insert guards；
- input link exact one-of、唯一 membership 和 deferred exact-count manifest guard。

沿用两个低基数指标：

```text
trading_venue_fact_normalizations_total{fact_type="POSITION_SNAPSHOT",result}
trading_venue_fact_input_links_total{source_type="VENUE_POSITIONS",result}
```

result 仍只使用既有受控值 NEW_FACT/EXISTING_FACT 与 LINKED/ALREADY_LINKED。

## 失败、恢复与回滚

- snapshot hash、evidence hash 或经济守恒不一致：拒绝，fact/link 均不创建。
- position/margin/collateral scope 不一致：拒绝，不能跨账户或跨保证金域归属。
- UNKNOWN：作为明确未知事实保留；本包不把它解释成 FLAT，也不开放增险。
- 同一 update 重读：返回原 canonical fact；后续 run 只新增 exact input link。
- 同一 update 改数量、mark、UPNL 或状态：identity conflict，旧事实保持不变。
- 数据库、audit/outbox、first link 或 deferred manifest 失败：整个事务回滚。

开发期回滚：

```bash
TRADING_DATABASE_URL="$DATABASE_URL" uv run alembic downgrade 20260718_0013
```

执行前必须停止 writer，并确认不存在必须保留的 WP-0014 position facts/links；含这些事实的环境不能直接降级，必须先
做前向兼容迁移或归档。回滚删除 position table/link column，并恢复只接受 order/fill membership 的 0013 guard。

## 明确未实现与现实边界

- canonical VenuePositionSnapshot 到 ExecutionFact/OrderIntent 的 exact binding；现有 `VENUE_POSITION` v3 弱入口仍
  不具备现实资格，下一独立工作包必须关闭它；
- current Position Projection、Campaign ownership、fill-to-position 差异、外部人工仓位和方向反转处理；
- Account Equity/Balance、Protection、Funding、liquidation event、费用与 PnL ledger；
- Binance/Hyperliquid private stream、REST backfill、分页/缺席语义、sequence 和真实 update identity 映射；
- 真实 ONE_WAY/HEDGE、cross/isolated、Core/HIP-3、断线、乱序、时钟偏差与双主认证；
- Freqtrade/VenueAdapter、Web/PWA、Telegram、Margin Controller、Vault/CTO、正式告警和 Runbook。

本包证据只来自 disposable PostgreSQL 和本地虚构 SHADOW facts；没有网络调用、真实账户、订单发送、保证金操作或
资金划转。它证明 canonical position fact 和 input membership 合同成立，不证明真实场所语义、Position Projection、
风险 MTM 或任何实盘能力已经认证。
