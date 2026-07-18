# WP-0027：策略绑定 Cost Stress

## 1. 交付目标与权威边界

WP-0026 已让 base Heat 由 Risk Engine 规范计算，但 cost stress 仍由调用方通过
`requested_cost_stress_add_on` 自报。权威决策 `DEC-RISK-010` 仍是 `RESEARCH_REQUIRED`：真实 Binance、
Hyperliquid、标的、订单类型和流动性档位的费用、止损穿透及资金费窗口不能由工程代码臆造。

本包关闭的是“谁计算”的架构缺口，而不是替研究决定数值：

- Risk Policy 必须显式携带版本化 cost-stress 参数和证据引用；
- 参数没有生产默认值，旧策略缺少参数时返回 `RISK_POLICY_INVALID`；
- Risk Engine 从策略参数和市场事实派生 cost，不接受调用方提交结果；
- 测试参数全部标注 `test-only`，不能签发现实能力。

## 2. 策略参数合同

`RiskPolicyParameters.cost_stress` 必须包含：

```text
model_version = fee-stop-funding-stress-v1
round_trip_fee_bps
stop_penetration_bps
funding_interval_count
source_ref
```

参数对象及整个 Risk Policy 都使用 `extra="forbid"`。`source_ref` 不能为空；RiskPolicyRecord 原有的
policy hash、不可变约束和 `evidence_refs` 继续覆盖完整参数快照。仓库迁移不会 seed 任一正式数值。

## 3. 规范计算

先计算：

```text
notional
  = requested_quantity × executable_price × certified_contract_multiplier

fee stress
  = notional × round_trip_fee_bps / 10000

stop penetration stress
  = notional × stop_penetration_bps / 10000
```

资金费只计算不利方向，不提前计入预期收入：

```text
LONG  adverse rate = max(0,  funding_rate)
SHORT adverse rate = max(0, -funding_rate)

adverse funding stress
  = notional × adverse rate × funding_interval_count

cost stress add-on
  = fee stress + stop penetration stress + adverse funding stress
```

`base Heat`、临时输入的 protected-profit giveback 和三个 cost 分量都向上取到
`NUMERIC(38,18)` 精度。这样风险决策不会小于后续数据库保存值，decision、reservation 和 durable
exposure 的金额边界一致。

最终新增损失为：

```text
incremental worst-case loss
  = canonical base Heat
    + normalized protected-profit giveback
    + canonical cost stress add-on
```

同一结果继续驱动 trade limit、authorization capacity、reservation、Risk Ledger 与七层 scope planned
allocation。requested scope stress 低于该结果时返回 `SCOPE_STRESS_INPUT_INVALID`。

## 4. v3 命令与审计

本包升级内部命令：

```text
risk.precheck.evaluate.v3      payload_schema_version=3
execution.intent.create.v3     payload_schema_version=3
```

v1/v2 命令返回 `COMMAND_TYPE_MISMATCH`；v3 携带非 3 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。v3 请求删除：

```text
requested.requested_cost_stress_add_on
```

旧字段因嵌套 `extra="forbid"` 被明确拒绝。不可变 decision JSON 新增：

- `requested_fee_stress`；
- `requested_stop_penetration_stress`；
- `requested_adverse_funding_stress`；
- `requested_cost_stress_add_on`；
- `requested_incremental_worst_case_loss`；
- `cost_stress_model_version`。

最终 reservation 冻结 Risk Engine 输出的 cost，而不再读取请求字段。

## 5. 明确未完成范围

- 仓库没有正式 Binance/Hyperliquid cost-stress 参数；`DEC-RISK-010` 仍需逐场所/标的研究和认证；
- 当前 `funding_rate`、`executable_price` 与 policy source 仍由本地测试构造，尚未接入真实 collector；
- protected-profit giveback 仍未从 canonical 仓位峰值、保护价与覆盖事实推导；
- requested scope stress 的超出 planned 部分仍未由版本化 scope stress 场景推导；
- 真实 VenueAdapter/Freqtrade、Web/PWA、Telegram、Margin、Vault/CTO、PnL、部署和运维仍未完成；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

按用户特别约束，Codex Security 及其审计 Skill、插件和模块保持停用；本包只使用常规架构约束、
静态检查、数据库集成测试与全量回归。
