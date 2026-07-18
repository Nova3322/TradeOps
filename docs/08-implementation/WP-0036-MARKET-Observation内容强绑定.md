# WP-0036：MARKET Observation 内容强绑定

> 后续状态：WP-0037 已把 ExecutionIntent 升级为 v10，并删除 ADD caller `protection_valid` /
> `authorization_valid`。WP-0038 再将 ExecutionIntent 升级为 v11，并删除 caller current
> leverage；WP-0039 又将 ExecutionIntent 升级为 v12，并强制 canonical venue UPNL 为正。
> RiskPrecheck 继续为 v9。当前执行合同以
> [WP-0039](WP-0039-ADD-Canonical正UPNL硬门.md) 为准。本文件保留 WP-0036 历史合同。

## 1. 交付目标与边界

WP-0035 已删除 caller `facts`，但 v8 风险请求中的 `MarketRiskInput` 仍未与服务端 Risk Fact Set 的
`MARKET.payload_hash` 对应。调用方理论上可以提交与最新耐久 MARKET observation 不同的 Mark、Index、
可成交价格、资金费或交易规则，同时让事实集合本身保持健康。

WP-0036 建立规范化 MARKET payload 合同。Risk Engine 对请求中的客观市场/规则字段计算 canonical hash，
并与服务端最新 immutable Risk Fact Set 的 KNOWN MARKET observation 比较；不一致时 proposal 与最终
ORDER_PRECHECK 都关闭新增风险。

本包只关闭“风险计算使用的市场内容是否与耐久 MARKET observation 一致”这一边界，不声称完成：

- 真实公共/私有行情 collector、订单簿取价或可成交量计算；
- Binance、Hyperliquid 或其他场所的规则/价格来源认证；
- 多源价格仲裁、预言机、非加密交易时段或公司行动；
- production、small-live、真实账户或现实资金能力。

## 2. v9 命令合同

```text
risk.precheck.evaluate.v9      payload_schema_version=9
execution.intent.create.v9     payload_schema_version=9
```

v1 至 v8 命令返回 `COMMAND_TYPE_MISMATCH`；v9 携带非 9 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。请求字段形状不新增 caller assertion；版本升级表示风险语义已增加强制
MARKET 内容绑定，旧 v8 不能在没有该比较的情况下继续执行。

Risk Fact Set 注册合同保持：

```text
risk.fact-set.register.v1
service_principal=risk-fact-aggregator-service
environment=SHADOW
real_funds_eligible=false
```

## 3. Canonical MARKET payload

MARKET observation 只证明客观市场和交易规则字段：

```text
mark_price
index_price
executable_price
contract_multiplier
tick_size
minimum_quantity
minimum_notional
funding_rate
contract_rules_version
```

所有 Decimal 使用去尾零、非指数、`-0 -> 0` 的稳定文本表示，再由统一 canonical JSON hash 计算
`market_fact_payload_hash`。因此 `100`、`100.0` 和 `100.0000` 语义相同且 hash 相同。

下列字段刻意不进入 MARKET payload：

- `direction`：由冻结 Proposal/Campaign 绑定；
- `initial_invalidation_price`：由冻结 Proposal 绑定；
- `max_slippage_bps`：由冻结 Proposal/授权绑定；
- `loss_model_version`、`loss_calculation_ref`：由 Risk Engine 固定模型合同绑定。

这样 MARKET observation 不会被错误地做成 proposal-specific 事实，也不会替代已存在的冻结授权校验。

## 4. Fail-closed 语义

Risk Fact Set 无效时仍优先返回 `RISK_FACT_SET_UNAVAILABLE`。集合有效后：

- MARKET 为 `UNKNOWN`：沿用 `FACTS_UNKNOWN`，不把未知 payload 伪装成内容不匹配；
- MARKET 为 `KNOWN` 且 hash 一致：继续执行既有 freshness、风险和授权检查；
- MARKET 为 `KNOWN` 且 hash 不一致：`MARKET_FACT_BINDING_MISMATCH`；
- mismatch 时 `final_quantity=0`，最终预检不创建 OrderIntent 或 RiskReservation。

`MARKET_FACT_BINDING_MISMATCH` 排在 `FACTS_UNKNOWN` 之后的集合有效性检查、其他风险限额之前，使已知内容
漂移得到稳定主原因。事实陈旧、未来或跨窗口不一致仍保留各自 reason code。

## 5. 决策证据

Risk/Execution decision、Command outcome、audit/outbox payload 同步保存：

```text
market_fact_payload_hash
market_observation_payload_hash
```

完整 Risk Fact Set validation snapshot 继续保存 observation source/version/payload hash/time。ALLOW 必须两个
hash 相等；DENY 保存双方 hash，便于定位调用方输入、聚合器或上游数据漂移。数据库不新增列或迁移；两项 hash
属于已存在的 immutable JSON decision evidence，schema head 仍为 `20260718_0030`。

## 6. 需求追踪

| 权威要求 | 本包证据 |
| --- | --- |
| 风险输入必须来自可追溯市场事实 | v9 将客观 MarketRiskInput 字段绑定到 exact durable MARKET hash |
| 调用方不得用不同价格/规则绕过事实集合 | KNOWN mismatch 稳定 DENY，proposal/final 均执行 |
| 冻结提案字段与市场事实职责分离 | direction/invalidation/slippage 不进入 MARKET payload，继续走冻结绑定 |
| 数值表达不得制造假漂移 | Decimal canonicalization 覆盖等值不同 scale |
| Unknown/陈旧/漂移默认禁止增险 | UNKNOWN、freshness、consistency 与 payload mismatch 各自失败关闭 |
| 最终预检失败不得产生风险副作用 | mismatch 集成测试断言无 OrderIntent/Reservation |

## 7. 明确未完成范围

- `risk-fact-aggregator-service` 仍只有 SHADOW 注册合同，没有真实行情进程或来源身份部署；
- MARKET observation 尚未绑定真实 collector watermark、盘口深度、quote TTL 或场所证书；
- ACCOUNT、VAULT、POSITIONS、ORDERS、LEDGER、CATALOG、VENUE_CAPABILITY、PROTECTION 的 payload hash
  仍需后续逐类绑定到对应 canonical durable payload；
- ADD frozen return、趋势、回调和 campaign equity 仍存在 caller-trusted 临时输入；
- 正式 FX/USD、稳定币折扣/脱锚、真实 OMS/Freqtrade/VenueAdapter、Web/PWA、Telegram、Margin、
  Vault/CTO、PnL 与运维仍未完成；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

按用户明确约束，Codex Security 及其所有审计 Skill、插件和模块保持停用；本包只执行常规代码检查、严格
类型检查和测试。只有用户未来明确重新授权后，才会另行考虑 Codex Security。
