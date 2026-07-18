# WP-0044：可重建 Opening Fill 经济投影

## 1. 交付目标与边界

WP-0043 已把每个 accepted canonical VenueFill 及原生费用逐笔归属到 Campaign，但下游仍缺少一个
确定性的只读聚合，无法直接回答当前已接受多少 opening fills、累计增仓数量/名义价值、各原生币种费用
净额以及 venue realized PnL 是否完整。

WP-0044 从 immutable `CampaignFillEconomicEntry` 重建 `CampaignOpeningFillProjection`。它只处理当前系统
已有的 INITIAL/ADD `POSITION_INCREASE` prefix，不持久化第二份账本，也不声称这是成本基础、当前持仓或
`E_campaign`。

## 2. 可确定重建的内容

对同一 Campaign 的 source entries 按 `facts_event_time + entry_id` 规范排序，并强制 exact scope、方向、
margin/collateral 与 contract multiplier 一致。投影包含：

- fill count、唯一 OrderIntent count、INITIAL/ADD fill count；
- cumulative quantity 与 cumulative notional；
- 按原生 fee currency 排序汇总的 signed fee totals；
- 仅对 KNOWN venue realized PnL 按 settlement currency 汇总的 totals；
- UNKNOWN realized PnL 条数和 settlement currency 集合；
- 每个 source entry ID/hash、Intent/AddUnit、数量、名义价值、费用和事实时间；
- facts_as_of、projection version/hash。

这些值只使用同方向增仓事实的交换律求和，不选择 FIFO/加权平均等减仓成本方法，也不把不同币种相加。
任何 source entry 的 hash、scope 或 multiplier 不一致都使重建失败。

## 3. 明确的 UNAVAILABLE 合同

投影固定返回：

```text
economic_equity_status = UNAVAILABLE
unavailable_reasons =
  CURRENT_POSITION_ECONOMICS_UNBOUND
  FUNDING_FACTS_UNAVAILABLE
  FX_VALUATION_UNAVAILABLE
  REDUCE_EXIT_LEDGER_UNAVAILABLE
```

因此本包不会输出 `E_campaign`、Frozen Return、当前未实现净收益、平均持仓成本或 Add 数量。原生费用即使
数值可求和，只要没有认证 FX/稳定币政策就不能进入 USD 风险资本；venue realized PnL 中一个 UNKNOWN
也不会按零补齐。

## 4. 可重放与失败语义

投影没有当前时间字段；同一组 immutable source entries 在同一 projection version 下必须得到同一 hash。
它是查询时可重建视图，source entries 才是耐久事实，避免引入会与账本漂移的可变副本。

```text
Campaign 无 accepted opening fill
    -> CAMPAIGN_OPENING_FILL_PROJECTION_UNAVAILABLE

同一 Campaign 的 source scope / direction / multiplier 冲突
    -> CAMPAIGN_OPENING_FILL_PROJECTION_SCOPE_CONFLICT

source snapshot、计数、金额、币种、时间或 projection hash 不一致
    -> validation failure；不返回部分经济结果
```

新增 bounded metric：

```text
trading_campaign_opening_fill_projections_total{result}
```

## 5. 数据库与现实能力边界

本包不新增数据库表或 Alembic revision；当前 schema 仍为 `20260718_0034`。投影只读取 WP-0043 的
immutable entries，不写 OrderIntent、风险、资金或执行状态，也不开放真实 collector/OMS/sender。

`risk_currency` 只是来源 Intent 的报告币种声明，不表示 fee/settlement amount 已完成换算。报告币种 USD
和稳定币 1:1 不是同一事实。

## 6. 需求追踪与未完成范围

| 权威要求 | 本包证据 |
| --- | --- |
| Fill 序列必须可确定性重建 | 规范 source order、ID/hash 与 projection hash |
| INITIAL 与 Add 贡献必须可拆分 | intent-kind fill counts 和逐 entry AddUnit refs |
| 费用/返佣不得跨币种误加 | native fee totals 按 currency 独立排序汇总 |
| Unknown 不得伪装为零 | KNOWN totals 与 unknown count 分离，equity 固定 UNAVAILABLE |

仍未完成：

- current position exact binding 和剩余成本基础；
- reduce/exit Intent、成本方法版本和已实现价格 PnL；
- funding facts/entries 与场所对账；
- FX/稳定币估值、当前 Mark、退出成本、Frozen Return 与 `E_campaign`；
- 真实 venue collector、OMS/Freqtrade/VenueAdapter 与逐场所认证；
- Web/PWA、Telegram、Margin、Vault/CTO、报表和运维认证。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
