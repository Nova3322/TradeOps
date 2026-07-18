# WP-0047：Campaign 资金费覆盖投影

## 1. 交付目标与边界

WP-0046 已建立 settled private-venue funding facts，但仅凭账户、标的和时间相同，仍不能把一笔资金费
直接归属于某个 Campaign。尤其在同账户多策略竞争、系统外订单或方向切换存在可能时，直接相加会虚构
Campaign PnL。

WP-0047 新增只读 `CampaignFundingCoverageProjection`。它证明最新成功 reconciliation v2 的
`VENUE_FUNDING` 完整水位覆盖当前 Campaign 的 opening-only 持仓区间，并列出 exact scope/time 内的
候选支付。本包不创建 attribution entry，不做 FX，也不计算 Frozen Return 或 `E_campaign`。

## 2. 覆盖区间与最新水位

投影先重建 WP-0045 的 current-position binding 和 WP-0044 的 opening projection，再固定：

```text
interval_start = earliest accepted INITIAL opening fill event time
interval_end   = fresh canonical current position facts_as_of
```

INITIAL Intent 必须存在 exact shadow dispatch claim，并解析到与 Campaign position 完全一致的 sender
scope。服务只读取该 scope 最新的一次 reconciliation run，要求：

- `schema_version=2`、SHADOW、`status=SUCCEEDED`；
- `VENUE_FUNDING` input 为 `COMPLETE`；
- input `observed_from <= interval_start`；
- input `observed_through >= interval_end`；
- input link 数量严格等于 frozen `item_count`，每条 link 的 run/org/input hash 均一致。

最新 run 为 FAILED/UNKNOWN/RUNNING、仍是 v1、缺少 funding input 或窗口不足时，固定返回
`CAMPAIGN_FUNDING_COVERAGE_UNAVAILABLE`，不会回退使用较旧成功水位。

## 3. Exact scope/time 候选

完整 funding input 可以包含同一 sender scope 下其他 instrument 的支付。投影只选择同时满足以下条件的
canonical facts：

- organization、venue、execution domain、account、instrument 完全一致；
- position side、isolated margin mode 与 collateral pool 完全一致；
- payment event time 位于闭区间 `[interval_start, interval_end]`；
- funding hash 与当前 input membership 完全一致。

候选按 event time/payment ID 确定性排序，并按原生币种汇总 signed economic cost：PAYMENT 为正成本，
RECEIPT 为负成本。完整零计数水位输出空候选和空 totals，而不是 UNKNOWN。

## 4. 明确的非归属合同

投影固定返回：

```text
scope_interval_coverage_status = EXACT
campaign_attribution_status = UNAVAILABLE
economic_equity_status = UNAVAILABLE
unavailable_reasons =
  CONTROLLED_ORDER_OUTLET_UNCERTIFIED
  FUNDING_PAYMENT_CAMPAIGN_OWNERSHIP_UNCERTIFIED
  FX_VALUATION_UNAVAILABLE
  EXIT_COST_MODEL_UNAVAILABLE
```

`native_signed_cost_totals` 只是该 exact scope/time 窗口内的候选总额，不能写入 Campaign Net Trading PnL。
后续只有在唯一订单出口、并发 Campaign 所有权、方向切换、外部介入和完整持仓区间规则得到认证后，才能用
独立不可变 entry 建立真实归属。

## 5. 确定性、监控与失败语义

输出绑定 current-position hash、sender-scope hash、run/input hash、水位、原始 payment/link IDs 和完整
候选集合，使用 `campaign-funding-coverage-projection-v1` 自校验 hash。同一 immutable sources 和查询
TTL 在有效期内重建相同结果。

新增 bounded metric：

```text
trading_campaign_funding_coverage_projections_total{result}
```

## 6. 数据库与未完成范围

本包不新增表或 Alembic revision；schema 仍为 `20260718_0035`。投影只读取既有 immutable facts、
reconciliation evidence 与 Campaign/current-position sources。

仍未完成：

- controlled sole order outlet、并发 Campaign ownership 和真实 funding attribution entry；
- reduce/exit Intent、成本基础、realized price PnL 与退出成本；
- FX/稳定币保守估值、Frozen Return 与 `E_campaign`；
- 真实 collector、OMS/Freqtrade/VenueAdapter 和逐场所认证；
- Web/PWA、Telegram、Margin、Vault/CTO、报表和运维认证。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
