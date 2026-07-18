# WP-0040：ADD 不可变策略评估强绑定

> 后续状态：WP-0041 已把 ExecutionIntent 升级为 v14，并为 INITIAL 增加 exact fresh canonical
> FLAT position 绑定；WP-0042 又冻结 isolated INITIAL 的初始保证金经济基线。ADD 的 Strategy
> Evaluation 合同保持不变；当前执行与经济基线合同以
> [WP-0043](WP-0043-Canonical成交费用归属账本.md) 为准。本文件保留 WP-0040 历史证据。

## 1. 交付目标与边界

WP-0039 后，ADD 调用方仍提交 `trend_valid`。该布尔值既没有绑定策略版本，也没有证明使用了
最终预检的同一批市场、仓位和保护事实，调用方可以单方把它写成 `true`。

WP-0040 删除该字段，新增 SHADOW-only、不可变的 `StrategyEvaluationRecord`。Execution 服务只解析
同一 Campaign、策略/参数版本、Risk Fact Set 和 exact current position/protection 上的最新评估，并且
只有完整 PASS 才继续增险。本包建立可信记录、解析与最终预检合同，不声称已经实现或认证真实策略
evaluator。

## 2. 命令与数据库合同

Proposal RiskPrecheck 继续使用：

```text
risk.precheck.evaluate.v9      payload_schema_version=9
```

ExecutionIntent 升级为：

```text
execution.intent.create.v13    payload_schema_version=13
```

v1 至 v12 execution 命令返回 `COMMAND_TYPE_MISMATCH`；v13 携带非 13 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。旧 `trend_valid` 因 `extra="forbid"` 返回
`EXECUTION_INPUT_INVALID`，不存在静默兼容旁路。

新增迁移 `20260718_0031` 和表 `strategy_evaluation_records`。记录只能由 exact internal principal
`strategy-evaluation-service` 通过：

```text
strategy.evaluation.register.v1    payload_schema_version=1
```

注册合同固定：

- `environment=SHADOW` 且 `real_funds_eligible=false`；
- 一条 Campaign/evaluation version 只能有一个不可变记录；
- exact Campaign、strategy/version/parameter version 和执行 scope；
- exact `risk_fact_set_id/version/record_hash`；
- exact current position/protection snapshot ID/hash，且保护必须属于该仓位；
- 评估不得早于任一源事实，且 Risk Fact Set 在评估时必须有效；
- 三条规则必须完整、唯一、规范排序；
- record/evidence hash、有效期和来源证据进入不可变合同。

数据库 insert guard 再次验证规则集合、排序、结果一致性与 evidence refs；update/delete trigger 拒绝
篡改。迁移不会 seed 任何评估或开启能力。

## 3. 三条规则与聚合语义

`ADD_CONTINUATION` 目前强制包含：

1. `PULLBACK_ENTRY`：回调入场条件；
2. `STRATEGY_VALIDITY`：策略有效性；
3. `TREND_CONTINUATION`：趋势延续。

每条规则状态只能是 `PASS / FAIL / UNKNOWN`，并携带 reason code 与 evidence payload hash。聚合规则固定：

```text
任一 FAIL                 -> FAIL
无 FAIL 且任一 UNKNOWN    -> UNKNOWN
全部 PASS                 -> PASS
```

规则的真实算法、周期、阈值和数据源仍必须由后续研究、回放、实时影子与场所证据认证；测试夹具产生的 PASS
只验证控制合同，不是生产策略证据。

## 4. 最终 ADD 解析与失败语义

Execution v13 在 canonical capital、protected-position risk、Risk Fact Set 和普通风险评估完成后，解析
latest exact Strategy Evaluation：

- 无记录：`STRATEGY_EVALUATION_RECORD_NOT_FOUND`；
- record/evidence hash 不自洽：`STRATEGY_EVALUATION_INTEGRITY_FAILED`；
- Risk Fact Set 或 position/protection ID/hash 不一致：
  `STRATEGY_EVALUATION_FACT_BINDING_MISMATCH`；
- 不在有效窗口：`STRATEGY_EVALUATION_OUTSIDE_VALID_WINDOW`；
- 结果为 FAIL 或 UNKNOWN：`STRATEGY_EVALUATION_OUTCOME_NOT_PASS`。

这些结果形成持久化 `ORDER_PRECHECK` DENY；不创建 ADD OrderIntent 或 RiskReservation，不消费 AddUnit。
最新 exact 记录损坏或拒绝时不会回退到更旧 PASS。成功决策保存完整 validation snapshot，并在
`ExecutionRiskDecision`、返回值和 outbox event 上强绑定 evaluation ID/version/record hash。

最终 OrderIntent 有效期取风险结果、授权和策略评估有效期中的最早值。到期评估不能被长授权窗口延长。

## 5. 仍保留的明确 caller 边界

WP-0040 后 `AddEligibilitySnapshot` 仍包含：

```text
frozen_return_pct
target_effective_leverage
current_position_equity
position/protection snapshot ref + hash
```

其中 snapshot ref/hash 与 target leverage 已受服务端事实/冻结 package 约束；`frozen_return_pct` 与
submitted `current_position_equity` / `E_campaign` 仍未由 Campaign PnL Ledger 重建。不得把本包解释为
冻结收益率、净浮盈或真实仓位权益已经可信化。

## 6. 监控、错误处理与回滚

新增 bounded counters：

- `trading_strategy_evaluation_registrations_total{result}`；
- `trading_strategy_evaluation_validations_total{result,primary_reason}`。

注册失败使用稳定错误码并保持数据库无记录；验证失败只关闭 ADD 增险，不改变保护、对账、reduce-only
或退出路径。回滚先停止 v13 caller 与策略评估注册；仅当表中无记录时才允许迁移降级，防止删除耐久证据。

## 7. 需求追踪与未完成范围

| 权威要求 | 本包证据 |
| --- | --- |
| 回调、趋势与策略有效性仍成立才可 Add | 三条完整规则只有全部 PASS 才继续 |
| 决策必须使用同一批当前事实 | exact Risk Fact Set 与 position/protection ID/hash 强绑定 |
| 数据未知、过期或审计不可重放时禁止增险 | missing/integrity/mismatch/expired/UNKNOWN 全部 DENY |
| 不得由策略 caller 自证 | 删除 `trend_valid`，只解析服务端不可变记录 |

仍未完成：

- 真实 Strategy Evaluation evaluator、算法与数据源认证；
- Campaign PnL Ledger、`E_campaign` 和净平仓成本；WP-0042 已冻结 isolated 初始保证金参考额；
- frozen return 30%/50%/100% 的服务端派生；
- 真实 collector、OMS/Freqtrade/VenueAdapter、Web/PWA、Telegram、Margin、Vault/CTO、PnL 与运维；
- production/small-live CapabilityCertificate 和发送权限。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构安全默认、代码检查、数据库约束与测试。
