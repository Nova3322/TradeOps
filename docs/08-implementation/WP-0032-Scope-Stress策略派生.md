# WP-0032：Scope Stress 策略派生

> 后续状态：WP-0035 已将风险/意图命令升级为 v8；WP-0034 删除 caller `protection_available`，WP-0035
> 再删除 caller `facts` 并绑定不可变完整 Risk Fact Set。本文件保留 WP-0032 的 v5 历史合同。

## 1. 交付目标与权威边界

权威策略合同要求 Stress Heat 使用冻结压力场景独立重算，不是调用方可填写的第四个风险余额，也不能
与 planned loss 机械相加后再共同消费同一上限。WP-0031 之后，`ScopeRiskInput` 仍接受调用方提交
`requested_incremental_stress_loss`，因此调用方仍可选择场景、低报或让七层 scope 使用不一致口径。

本包升级风险与意图命令为 v5，并固定：

- 请求只提交各 scope 的耐久 current planned/stress 占用；
- Risk Policy 对每个 exact scope 显式冻结压力场景和证据引用；
- Risk Engine 从同一请求数量、可执行价和认证乘数派生 planned 与 stress 增量；
- Execution 只把 Risk Engine 决策中的派生值写入 Reservation allocation。

`DEC-RISK-010` 仍为 `RESEARCH_REQUIRED`。仓库不提供 Binance、Hyperliquid 或任何标的的生产默认值；
测试参数全部为 `test-only` SHADOW 证据，不能用于签发现实能力。

## 2. v5 命令与旧合同封锁

```text
risk.precheck.evaluate.v5      payload_schema_version=5
execution.intent.create.v5     payload_schema_version=5
```

v1 至 v4 命令返回 `COMMAND_TYPE_MISMATCH`；v5 携带非 5 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。`ScopeRiskInput` 删除：

```text
requested_incremental_stress_loss
```

模型继续使用 `extra="forbid"`。v5 风险请求携带旧字段返回 `RISK_INPUT_INVALID`，v5 执行请求携带旧字段
返回 `EXECUTION_INPUT_INVALID`，不会静默忽略或退回 v4 语义。

## 3. Per-scope 冻结场景

每个 `ScopeLimit` 必须包含 `stress_scenario`：

```text
model_version = planned-loss-plus-scope-shocks-v1
gap_bps
liquidity_degradation_bps
unprotected_window_bps
source_ref
```

三项基点均显式、非负且没有默认值，`source_ref` 不得为空。`ScopeLimit` 和场景对象均
`extra="forbid"`；旧策略缺少场景、使用未知模型或缺少来源时，Risk Policy 校验失败并关闭增险。
RiskPolicyRecord 原有 policy hash、证据引用和有效期继续绑定完整场景快照。

## 4. 规范派生与计划/压力分离

先复用 WP-0026/0027 的规范 planned 增量：

```text
requested notional
  = quantity × executable price × certified contract multiplier

incremental planned loss
  = canonical base Heat + policy-bound cost stress
```

再对每个 scope 独立派生：

```text
gap stress
  = requested notional × gap_bps / 10000

liquidity degradation stress
  = requested notional × liquidity_degradation_bps / 10000

unprotected window stress
  = requested notional × unprotected_window_bps / 10000

incremental stress loss
  = incremental planned loss
    + gap stress
    + liquidity degradation stress
    + unprotected window stress
```

每个分量都向上取到 `NUMERIC(38,18)` 精度。planned 与 stress 分别比较自己的 current usage 和 cap；
系统不会把二者相加后与同一个 cap 比较。这样 stress 是包含 planned 基线的独立场景结果，而不是遗漏
计划风险，也不是重复消费 planned 余额。

## 5. 决策、Reservation 与耐久重建

每个 `ScopeRiskDecision` 保存：

- `incremental_planned_loss` 与 `incremental_stress_loss`；
- gap、liquidity degradation、unprotected window 三项附加值；
- `scope_stress_model_version` 与 `scope_stress_source_ref`；
- planned/stress 动作后总额、上限和各自是否通过。

这些字段进入不可变 decision JSON/hash。执行成功时，Reservation `scope_allocations` 直接使用排序后的
`ScopeRiskDecision`，不再读取请求值；Durable Exposure 后续按真实成交比例分配同一 allocation。缺少
exact-scope policy 仍返回 `SCOPE_POLICY_MISSING`，压力超限返回 `SCOPE_STRESS_LIMIT_EXCEEDED`。

## 6. 数据库、监控、错误处理与回滚

本包不新增表或迁移：策略场景位于现有不可变 `risk_policies.parameters`，派生结果位于现有 decision JSON
和 Reservation allocation JSON。已有 Risk/Execution 决策计数、原因码和决策耗时指标继续覆盖 v5。

回滚时先保持 `LIVE_ORDER_SEND`、`AUTO_ADD` 为 `DISABLED` 并停止新 v5 命令。不能恢复接受 v4 或 caller
stress；代码回退只允许在确认没有需要继续消费的 v5 SHADOW intent/reservation 后进行。若已存在 v5
SHADOW 行，保留历史读取与 hash，不破坏性改写。

## 7. 需求追踪

| 权威要求 | 本包证据 |
| --- | --- |
| Stress Heat 使用冻结场景独立重算 | per-scope 版本化场景，planned/stress 分别比较各自 cap |
| Scope Worst-Case Loss 分层受限 | 七层 scope 各自派生、各自限额和各自来源 |
| 调用方不得决定规范风险结果 | 请求模型删除 stress 结果，Reservation 只消费 decision |
| 研究参数不得伪装生产默认 | 参数必填、无默认、来源必填、测试值明确 test-only |
| 可复算与可追溯 | 三项分量、模型版本、source、policy/input/decision hash |
| 缺少事实或政策时失败关闭 | 旧字段、旧命令、缺场景、缺 scope 和超限均拒绝 |

## 8. 明确未完成范围

- 真实 Binance/Hyperliquid、执行域、标的和流动性档位的 gap/liquidity/unprotected-window 参数未研究冻结；
- 持续 current stress 重估、场景随市场状态切换、退出仲裁和风险状态自动收紧尚未实现；
- current durable stress 仍沿用冻结 Reservation 场景并随成交比例重建，尚未由实时场景重新估值；
- 正式 cost 参数、FX/USD、稳定币折扣与脱锚、真实私有 collector 和逐场所认证仍未完成；
- 真实 OMS/Freqtrade/VenueAdapter、Web/PWA、Telegram、Margin、Vault/CTO、PnL 与运维仍未完成；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

按用户特别约束，Codex Security 及其审计 Skill、插件和模块保持停用；本包只使用常规架构约束、
代码检查、严格类型检查、数据库集成测试与全量回归。
