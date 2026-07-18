# WP-0045：Opening-Only 当前仓位一致性绑定

## 1. 交付目标与边界

WP-0044 已能从 immutable Campaign fill entries 重建 opening prefix，但这个历史前缀仍没有与 fresh
canonical current position 形成一个可重放的 exact binding。下游若只读取当前仓位，无法证明它与 Campaign
已接受的 INITIAL/ADD 增仓事实在数量、方向、范围和时间上仍一致。

WP-0045 新增只读 `CampaignCurrentPositionBinding`。它把 WP-0042 冻结基线、WP-0044 opening projection
和现有 venue current-position projection 拼成 opening-only 一致性证据。本包不持久化第二份仓位账本，不
选择减仓成本法，也不计算 `E_campaign`。

## 2. Exact binding 合同

服务先验证基线与 opening projection：

- Campaign、organization、venue、execution domain、account、instrument、direction 完全一致；
- isolated margin、collateral scope/pool 与 contract multiplier 完全一致；
- opening risk currency 等于冻结仓位范围的 settlement currency；成交原生 settlement currencies 保持独立；
- 所有 INITIAL fills 只属于冻结的 initial OrderIntent；
- INITIAL fill quantity 总和严格等于冻结 initial quantity；
- INITIAL fill facts 不晚于冻结 POSITION_RECONCILED fact。

随后用冻结基线构造 exact `CurrentPositionScope`，只接受：

```text
projection = CONFIRMED / FRESH / VENUE_CONFIRMED
position_state = OPEN
direction = Campaign direction
current quantity = opening INITIAL quantity + opening ADD quantity
contract multiplier = frozen/opening multiplier
current facts_as_of >= max(baseline facts, opening facts)
```

ADD fill 已被接受、但对应 current position 尚未被场所事实确认时，opening quantity 会先于 current quantity
增加，此时固定返回 `CAMPAIGN_CURRENT_POSITION_PREFIX_MISMATCH`，不会继续使用旧仓位。

## 3. 可重放输出

绑定包含：

- baseline ID/hash、初仓 Intent 与初始 position snapshot ID/hash；
- opening projection hash；
- current position snapshot ID/hash；
- exact scope、方向、position mode/side 与币种声明；
- initial/opening-add/opening-total/current quantities；
- current entry/Mark/notional、venue UPNL、initial/maintenance margin；
- baseline/opening/current facts time、POSITION TTL 与 `valid_until`；
- `campaign-current-position-binding-v1` 和自校验 binding hash。

输出不包含查询时的动态 age；同一组 immutable sources 和同一 `max_age_ms` 在有效期内重建相同 hash。
当前 position TTL 过期、缺失、冲突或 UNKNOWN 时不返回部分经济字段。

## 4. 明确的非归属与 UNAVAILABLE 合同

数量一致只能证明当前 venue position 与系统已接受 opening prefix 在观察时刻相容，不能证明场所上没有
控制面之外的净额相抵交易。因此绑定固定返回：

```text
quantity_consistency_status = EXACT
exclusive_ownership_status = UNAVAILABLE
economic_equity_status = UNAVAILABLE
unavailable_reasons =
  CONTROLLED_ORDER_OUTLET_UNCERTIFIED
  FUNDING_FACTS_UNAVAILABLE
  FX_VALUATION_UNAVAILABLE
  EXIT_COST_MODEL_UNAVAILABLE
```

`current_unrealized_pnl` 是 exact current venue snapshot 的原生事实，不会单独升级为 Campaign equity。
特别是本地夹具中 current position 的风险范围可为 USD、fill settlement 可为 USDT；本包不会暗示
USDT=USD，也不会跨币种相加。

## 5. 失败语义与指标

```text
缺少冻结基线
  -> CAMPAIGN_CURRENT_POSITION_BASELINE_UNAVAILABLE

基线与 opening source scope/INITIAL 数量冲突
  -> CAMPAIGN_CURRENT_POSITION_SOURCE_CONFLICT

current projection missing/UNKNOWN/stale/conflict
  -> CAMPAIGN_CURRENT_POSITION_UNAVAILABLE

current state/direction/quantity/multiplier/time 不等于 opening prefix
  -> CAMPAIGN_CURRENT_POSITION_PREFIX_MISMATCH
```

新增 bounded metric：

```text
trading_campaign_current_position_bindings_total{result}
```

## 6. 数据库与未完成范围

本包不新增表或 Alembic revision；schema 仍为 `20260718_0034`。绑定是查询时可重建视图，只读取既有
immutable facts 和 current projection。

仍未完成：

- controlled sole order outlet 与真实 venue ownership 认证；
- reduce/exit Intent、成本方法版本、已实现价格 PnL 与退出成本；
- funding facts/entries、FX/稳定币估值、Frozen Return 与 `E_campaign`；
- 真实 collector、OMS/Freqtrade/VenueAdapter 和逐场所认证；
- Web/PWA、Telegram、Margin、Vault/CTO、报表和运维认证。

后续 [WP-0046](WP-0046-Canonical场所资金费支付事实.md) 已补充 settled private-venue funding 的
canonical 原生事实和 reconciliation v2 完整水位，但尚未把资金费归属到 Campaign，因此本包的
`FUNDING_FACTS_UNAVAILABLE` / `economic_equity_status=UNAVAILABLE` 结论不变。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
