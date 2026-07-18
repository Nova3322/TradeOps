# WP-0020：不可变托管资金范围与组合 MTM

## 1. 交付目标

WP-0020 在 WP-0019 的逐账户当前权益投影之上，补齐“哪些账户构成一个组织的完整受管资金
全集”这一前置事实。新增：

```text
ManagedCapitalScopeManifest
ManagedCapitalScopeManifestService
PortfolioMtmProjectionService
```

清单是不可变、显式 ID/版本绑定的目录事实，不是新的生命周期状态机。迁移不 seed 真实账户、
默认清单或任何真钱资格。

## 2. 清单契约

每份清单固定为：

```text
environment = SHADOW
real_funds_eligible = false
risk_inclusion_mode = EXCHANGE_ONLY
report_currency = USD
```

清单必须声明至少一个完整 `CurrentAccountEquityScope`：

```text
organization + venue + execution_domain + account
+ margin_mode + collateral_pool + settlement_currency
```

所有 scope 必须属于同一 organization、按完整键规范排序且不可重复。清单还携带显式有效期、
manifest hash、证据引用/hash 和来源引用。`organization + manifest_version` 全局唯一；查询必须绑定
精确 manifest ID 与版本，不存在“当前激活清单”的隐式选择。

只有精确内部 principal `capital-scope-catalog-service` 可以通过耐久命令注册清单。注册使用事务级
advisory lock 串行化同一 organization/version，并继承命令幂等、审计事件和 outbox 契约。

## 3. 数据库防绕过

PostgreSQL 同时执行：

- 固定 SHADOW、真钱关闭、EXCHANGE_ONLY 和 USD 报告币种 check；
- JSON scope 数量、完整字段、同组织、规范顺序和唯一性 insert guard；
- evidence 非空、规范排序和唯一性 insert guard；
- manifest 的 UPDATE/DELETE 不可变 trigger；
- 仍有清单事实时拒绝 downgrade。

服务层校验不能代替这些约束；直接 SQL 也不能写入乱序、重复、跨组织或改变固定政策的清单。

## 4. 组合 MTM 语义

查询必须显式提供 manifest ID/version、timezone-aware `as_of` 和调用方认证的 `max_age_ms`。
服务逐项读取 WP-0019 的 exact-scope current account projection。

只有同时满足以下条件才返回 `CONFIRMED` 的 USD `Current Portfolio MTM Equity`：

1. 清单存在、hash/证据完整、且处于显式有效期；
2. 清单中的每一个账户 scope 都有 `FRESH + VENUE_CONFIRMED` 当前权益事实；
3. 每个账户权益都明确包含最新 UPNL；
4. 所有 settlement currency 都已是报告币种 USD；
5. 风险计入口径保持 `EXCHANGE_ONLY`，因此 eligible Vault equity 明确为 0。

公式为：

```text
Current Portfolio MTM Equity (USD)
= Σ Exchange Margin Equity (USD)
+ Eligible Vault Equity (0 under EXCHANGE_ONLY)
```

`Exchange Margin Equity` 已包含 UPNL，不能再次加上 `Current UPNL`。UPNL 作为独立解释字段求和输出，
不参与二次加总。

## 5. 跨币种与失败关闭

尚无认证 FX/stablecoin 价格、时效和 depeg 折扣事实。只要存在 USDT、USDC 或其他非 USD
settlement currency，服务会保留已确认的原生币种分组组件，但报告币种组合值返回：

```text
projection_state = UNKNOWN
reason_code = FX_FACTS_REQUIRED
current_portfolio_mtm_equity = null
```

缺少任一 scope、源 Unknown、future、stale、manifest 缺失/错绑/未生效/过期/完整性失败时，整个
组合值同样为 Unknown/null；不会把已知账户的局部和冒充完整组合资金。

## 6. 投影与可观测性

响应携带：

- manifest ID/version/hash；
- organization、EXCHANGE_ONLY、USD 报告币种；
- 每个账户的 exact scope、状态、原因、source snapshot ID/hash、facts-as-of 和 age；
- 全部来源完整时的原生币种分组；
- `portfolio-mtm-v2` projection version；v2 在 WP-0021 增加组合级 `available_margin` 并供
  RiskPrecheck 强绑定，未改变 WP-0020 的账户全集和 FX 失败关闭语义。

新增指标：

```text
trading_capital_scope_manifest_registrations_total
trading_portfolio_mtm_projection_queries_total
```

组合投影是 manifest 与 canonical venue facts 的确定性查询函数，没有第二套组合经济写模型。

## 7. 明确未完成范围

WP-0020 不代表 Risk Engine 已使用可信资本输入，也不代表真实组合估值或真钱就绪：

- 没有真实部署账户清单与批准证据；
- 没有认证 Binance/Hyperliquid 账户权益公式或私有 collector；
- 没有 FX/stablecoin/depeg 事实，非 USD 组合值保持 Unknown；
- 没有 Vault、在途资金、外部现金流或最终 PnL；
- 尚未把 exact manifest/version/hash 与风险决策快照强绑定；
- 所有现实能力闸门继续关闭。

按用户特别约束，Codex Security 及其审计 Skill、插件和模块保持停用；本包只执行常规工程设计、
迁移、静态检查和自动化测试。
