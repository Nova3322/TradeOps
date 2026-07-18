# WP-0021：RiskPrecheck 可信资本强绑定

## 1. 交付目标

WP-0021 消除提案阶段 `RiskPrecheckService` 对调用方自报当前组合权益的信任。风险服务在同一
PostgreSQL 事务中按精确 manifest 重算 WP-0020 组合投影，只有绑定、完整性、新鲜度、币种和
账户成员关系全部成立时才进入确定性风险数学。

本包把 `PortfolioMtmProjection` 升级为 `portfolio-mtm-v2`，新增组合及原生币种分组的
`available_margin`，供 Risk Engine 使用。它没有创建第二套资本写模型。

## 2. 无旧协议回退

`RiskPrecheckRequest` 新增必填：

```text
capital_projection_binding.manifest_id
capital_projection_binding.manifest_version
capital_projection_binding.manifest_hash
capital_projection_binding.projection_version
```

缺少 binding 的旧请求无法通过 `extra=forbid` Pydantic 合同；服务没有“先信任调用方、以后再核对”
或无 manifest 的兼容分支。

## 3. 事务内可信资本解析

`CapitalProjectionResolver` 使用风险政策中 `FactType.ACCOUNT` 的显式 `max_age_ms`，以风险决策
时间作为 `as_of` 查询精确 manifest ID/version。调用方不能自行放宽资本事实新鲜度。

进入风险数学前必须同时满足：

1. 组合投影为 `CONFIRMED`；
2. manifest hash 与 `portfolio-mtm-v2` 完全匹配请求 binding；
3. 报告币种为 USD、风险计入口径为 `EXCHANGE_ONLY`；
4. 被提案交易账户的 venue、execution domain、account、margin mode、collateral pool 和
   settlement asset 是 manifest 中一个 exact scope；
5. 组合权益、当前 UPNL、可用保证金和 eligible Vault equity 均来自完整 canonical 投影。

服务从投影确定性派生：

```text
exchange_settled_equity_ex_upnl
= exchange_margin_equity - current_unrealized_pnl

exchange_risk_equity
= max(0, min(exchange_settled_equity_ex_upnl, exchange_margin_equity))

total_capital_snapshot_0
= current_portfolio_mtm_equity

eligible_vault_equity = 0
available_margin = Σ exact account available_margin
```

调用方提交的这些字段必须与派生结果逐项相等；不一致返回 `CAPITAL_INPUT_MISMATCH`，不创建风险
决策。`funding_used` 与 `funding_reserved` 尚来自上游风险请求，本包没有伪称已绑定不可变 Ledger；
该缺口仍须后续工作包关闭。

## 4. 失败关闭原因

资本解析在 RiskEvaluator 之前执行，稳定错误包括：

```text
CAPITAL_PROJECTION_UNAVAILABLE
CAPITAL_ACCOUNT_SCOPE_INCOMPLETE
CAPITAL_FX_FACTS_REQUIRED
CAPITAL_PROJECTION_BINDING_MISMATCH
CAPITAL_TRADE_ACCOUNT_OUTSIDE_MANIFEST
CAPITAL_PROJECTION_INTEGRITY_FAILED
CAPITAL_INPUT_MISMATCH
```

manifest 缺失/过期、账户事实缺失/Unknown/stale、非 USD 缺少认证 FX、hash/version 错绑、交易账户
不属于完整资金全集或调用方改写数值时，均不会运行风险公式，也不会写入看似有效的
`RiskDecisionSnapshot`。

## 5. 不可变决策证据

每个成功进入风险数学并持久化的提案风险决策新增：

```text
capital_scope_manifest_id
capital_scope_manifest_version
capital_scope_manifest_hash
capital_projection_version
capital_projection_hash
```

数据库使用 manifest ID + organization + version 外键和 hash/version check。完整组合投影（含每个
canonical source snapshot ID/hash、source/normalization version、facts-as-of 和 age）保存在已哈希
的 `input_snapshot.capital_projection` 中。风险决策表原有不可变 trigger 同时保护这些新字段。

迁移不猜测旧风险决策的资本来源：升级到 `0021` 前若存在无绑定的 legacy shadow
`risk_decision_snapshots`，迁移明确失败并要求先按运营流程导出/清理测试事实；它不会自动补一个
虚假 manifest。仍有绑定风险决策时同样拒绝 downgrade。

## 6. 明确未完成范围

本包只关闭提案阶段 `RiskPrecheckService` 的当前资本信任缺口：

- `ExecutionIntentService` 的最终 ORDER_PRECHECK 尚未调用同一 resolver；
- funding used/reserved、open/reserved/unknown heat 与 scope exposure 仍需继续绑定不可变 Ledger；
- 非 USD FX/stablecoin/depeg 事实仍未实现，因此相关提案保持失败关闭；
- 没有真实部署 manifest、私有 collector、场所公式认证或真钱能力；
- 三项现实能力闸门继续 `DISABLED`。

这不是目标范围缩减。最终下单预检绑定是紧接的工作包，完整产品仍包含全部 Web/PWA、Telegram、
Binance、Hyperliquid、Freqtrade、Margin、Vault/CTO、PnL 与运维目标。

按用户特别约束，Codex Security 及其审计 Skill、插件和模块保持停用；本包只执行常规工程设计、
迁移、静态检查和自动化测试。
