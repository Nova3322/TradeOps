# WP-0031：Canonical 保护风险接入最终预检

> 后续状态：WP-0032 已升级到 v5，删除 caller `requested_incremental_stress_loss`，并由 per-scope
> 版本化压力场景派生 Reservation stress allocation；本文件保留 v4 历史合同。

## 1. 交付目标与权威边界

WP-0030 已能从 current position/protection 事实互斥派生 Open Heat 与 Protected-Profit Giveback，但
v3 RiskPrecheck 仍接受调用方提交 `requested_protected_profit_giveback`。这既允许调用方低报，也会把现仓
从 Mark 回到保护价的风险误当成新增订单损失，造成重复记账。

本包升级风险与意图命令为 v4，并固定两条不同口径：

- 当前仓位风险：由 canonical current protected-position risk 替换战役旧的比例估算；
- 新增订单风险：只包含 canonical base Heat + policy-bound cost stress，不再附加 caller giveback。

初仓和提案预检没有既有 current position，当前保护风险明确为零。ADD 的最终预检则必须取得 fresh、
exact-scope、可追溯的 canonical 保护风险，否则命令失败关闭且不创建 OrderIntent/Reservation。

## 2. v4 命令合同

```text
risk.precheck.evaluate.v4      payload_schema_version=4
execution.intent.create.v4     payload_schema_version=4
```

v1/v2/v3 命令返回 `COMMAND_TYPE_MISMATCH`；v4 携带非 4 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。`RequestedRiskIncrease` 删除：

```text
requested_protected_profit_giveback
```

嵌套请求保持 `extra="forbid"`，旧字段不会被静默忽略。风险决策改为保存：

- 完整 `current_trade_loss` 五分量；
- `current_protected_position_risk_calculation_hash`；
- canonical position/protection source IDs、source hashes、scope、价格、数量、公式版本与事实时间；
- canonical 保护风险有效期。

全部内容进入不可变 input/decision JSON 及其 hash。

## 3. ADD 的 exact-scope 绑定

`CurrentProtectedPositionRiskResolver` 从冻结 Campaign、CertificationBinding 和请求币种构造 scope：

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
× settlement_currency
```

ONE_WAY 固定查询 `position_side=BOTH`；HEDGE 使用 Campaign direction。只有以下条件同时满足才可继续：

- position/protection current projection 均为 `CONFIRMED`；
- 保护仍精确引用 current position source snapshot；
- current quantity 等于请求的 `current_position_quantity`；
- direction 等于 Campaign direction；
- canonical Mark 等于 Risk Market Mark；
- contract multiplier 等于认证绑定；
- settlement currency 等于冻结 risk currency；
- AddEligibility 中 position/protection snapshot ref 与 hash 精确等于 canonical source。

篡改资格证据返回 `ADD_ELIGIBILITY_CANONICAL_FACT_MISMATCH`；事实不可用返回
`CURRENT_PROTECTED_POSITION_RISK_UNAVAILABLE`；数量、方向、Mark、乘数或币种漂移返回
`CURRENT_PROTECTED_POSITION_RISK_BINDING_MISMATCH`。

## 4. Freshness 与有效期

查询同时消费 Risk Policy 中 `POSITIONS` 和 `PROTECTION` 的 freshness limit，并使用两者较严格值。
canonical 组合的 `facts_as_of` 是两项事实中较早时间：

```text
protected risk valid_until = facts_as_of + min(position max age, protection max age)
```

如果该有效期不晚于最终预检时刻，命令拒绝。OrderIntent 最终有效期继续取 policy、capability、全部事实和
canonical 保护风险有效期中的最早值，不能靠较长的其他事实窗口延长旧保护证据。

## 5. 当前战役与七层 Scope 重算

Risk Ledger 的 durable snapshot 仍负责在途、Unknown、资金、保证金、cost 和并发锁；但已成交仓位的旧
base Heat/giveback 比例估算由 canonical 当前值替换：

```text
replacement delta
  = canonical Open Heat
  + canonical Protected-Profit Giveback
  - durable campaign Open Heat
  - durable campaign Giveback
```

最终当前 Trade Loss 为：

```text
canonical Open Heat
+ durable Reserved Heat
+ durable Unknown Heat
+ canonical Protected-Profit Giveback
+ durable active Cost Stress
```

同一 `replacement delta` 同时作用于七层 scope 的 current planned loss 与 current stress loss，避免保护
风险上升时只收紧单仓、不收紧聚合作用域。任一调整让 scope 小于零视为 durable integrity failure。

新订单增量固定为：

```text
requested incremental worst-case loss
  = canonical base Heat + policy-bound cost stress
```

v4 新建 Reservation 的 `protected_profit_giveback_reserved=0`。数据库列继续保留，以便解释 v3 之前的
历史 shadow 行，但新命令不再写入非零值。

## 6. 需求追踪

| 权威要求 | 本包证据 |
| --- | --- |
| `DEC-RISK-005` 完整互斥且同一利润不重复抵扣 | current Open Heat/Giveback 来自同一 canonical 互斥投影 |
| Add 前使用当前保护、风险与执行事实 | ADD 最终预检强制 current snapshot、Mark、数量、币种与 freshness |
| Scope Worst-Case Loss 分层受限 | canonical replacement delta 同步进入七层 planned/stress 当前占用 |
| 公式不可机械判定时不得增险 | missing/stale/unknown/mismatch 一律拒绝且不创建意图或预留 |
| 审计可复算 | source IDs/hashes、calculation hash、valid_until 与 input/decision hash 同时保存 |
| 当前事实与新增动作分离 | current risk 替换旧现仓估算；新增动作只计 base Heat + cost |

## 7. 明确未完成范围

- canonical current risk 目前只在 ADD 最终预检时强绑定；持续风险监控和退出仲裁尚未消费该投影；
- WP-0032 已移除 caller scope stress 并完成版本化派生；持续 current stress 重估与正式参数认证仍未完成；
- 已有 v3 shadow Reservation 的历史非零 giveback 列仍按旧合同可读，不进行破坏性回写；
- 真实 Binance/Hyperliquid collector、正式 cost 参数、FX/USD、稳定币折扣与脱锚证据仍未完成；
- 真实 OMS/Freqtrade/VenueAdapter、Web/PWA、Telegram、Margin、Vault/CTO、PnL 与运维仍未完成；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

按用户特别约束，Codex Security 及其审计 Skill、插件和模块保持停用；本包只使用正常架构约束、
代码检查、严格类型检查、数据库集成测试与全量回归。
