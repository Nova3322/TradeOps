# WP-0046：Canonical 场所资金费支付事实

## 1. 交付目标与边界

WP-0045 已能把 opening fill prefix 与 fresh canonical current position 做 exact binding，但 Campaign
经济权益仍缺少场所确认的实际资金费支付事实。若直接从费率、仓位或持有时长估算资金费，会把预测值误当成
已结算账户事实。

WP-0046 新增 immutable `VenueFundingPayment`，把私有场所已经结算的资金费逐笔规范化，并将
`VENUE_FUNDING` 纳入完整对账 manifest。本包只建立原生币种事实层，不把支付归属到 Campaign，不做 FX，
也不计算 Frozen Return 或 `E_campaign`。

## 2. Canonical funding 合同

每条事实精确绑定：

- organization、venue、execution domain、account 和 instrument；
- venue payment ID、position side、margin mode 与 collateral pool；
- 原生 funding currency、signed economic cost 与 effect；
- reconciliation run/input、source/normalization version、原始 payload/evidence 引用和 hash；
- event、venue observed、first received 与 recorded time。

符号合同从系统经济成本视角定义：

```text
PAYMENT -> funding_amount > 0   # 账户支付，成本
RECEIPT -> funding_amount < 0   # 账户收取，负成本
ZERO    -> funding_amount = 0
```

模型、命令服务和数据库三层同时拒绝 effect/sign 不一致。事实固定为
`VENUE_PRIVATE / SHADOW / live_dispatch_eligible=false`，不能开启任何现实发送能力。

## 3. 对账 manifest v2

新 reconciliation run 固定使用 `schema_version=2` 和八个精确来源：

```text
TRADING_LEDGER
VENUE_ORDERS
VENUE_FILLS
VENUE_FUNDING
VENUE_POSITIONS
VENUE_BALANCES
VENUE_PROTECTION
WORKER_LOCAL
```

即使本次没有资金费，collector 也必须提交 `VENUE_FUNDING` 的零计数水位，不能以缺失来源冒充零结果。
历史 `schema_version=1` 的七来源证据仍可读取；数据库只允许新建 v2/八来源 run。

## 4. 去重、归属与失败关闭

全局外部身份为：

```text
organization + venue + execution_domain + account_id + venue_payment_id
```

同一外部支付可在后续 reconciliation input 再次出现并链接到同一 canonical fact；内容冲突固定拒绝，
不能覆盖历史。每个 input link 必须且只能引用与 source type 匹配的一种 fact。表级 immutable trigger
禁止 update/delete，deferred first-link guard 和 exact manifest guard 阻断孤立事实或不完整水位。

本包没有把 `VenueFundingPayment` 连接到某个 Campaign。即使 instrument、账户和时间相同，也不能在
controlled sole outlet、exclusive ownership、仓位区间与归属规则未认证时自行推断经济所有者。

后续 [WP-0047](WP-0047-Campaign资金费覆盖投影.md) 只把最新成功 v2 水位覆盖的 exact scope/time
payments 列为候选，并继续固定 `campaign_attribution_status=UNAVAILABLE`；它没有改变本包的非归属边界。

## 5. 数据库迁移与降级边界

Alembic revision 从 `20260718_0034` 升至 `20260718_0035`，新增：

- `venue_funding_payments` immutable table；
- `venue_fact_input_links.venue_funding_payment_id`、FK、unique 与 exact-one-fact check；
- v2/八来源 reconciliation 约束和 funding-specific insert/manifest guards。

若已存在 canonical funding row 或任何 v2 reconciliation evidence，downgrade 明确拒绝，避免删除已持久化
的对账来源或资金费事实。

## 6. 未完成范围与能力门

仍未完成：

- Campaign funding attribution、exclusive ownership 与持仓生效区间；
- realized price PnL、reduce/exit 成本方法和退出费用；
- FX/稳定币保守估值、Frozen Return 与 `E_campaign`；
- 真实私有 collector、OMS/Freqtrade/VenueAdapter 和逐场所认证；
- Web/PWA、Telegram、Margin、Vault/CTO、报表和运维认证。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
