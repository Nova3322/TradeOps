# WP-0026：规范化 Base Heat 与 Scope 增量

> 后续状态：WP-0027 已移除 caller-reported cost，Risk Engine 改从无默认的版本化 Risk Policy 参数
> 派生 fee、stop penetration 与 adverse funding stress，并将命令升级为 v3。当前合同以
> [WP-0027](WP-0027-策略绑定Cost-Stress.md) 为准。本文件保留 WP-0026 的 base/scope 共源证据。

## 1. 交付目标

WP-0025 已把 base Heat、protected-profit giveback 与 cost stress add-on 独立冻结到
`RiskReservation`，但新增 base Heat 仍由调用方通过 `requested_reserved_heat` 自报，七层 scope 的
`requested_incremental_planned_loss` 也由调用方重复提交。两项都不是权威风险结果，可能与价格、数量、
失效价或彼此不一致。

本包把新增 base Heat 改为 Trading Risk Engine 内部的确定性计算，并把同一个规范化增量同时用于：

- trade worst-case loss；
- authorization capacity；
- reservation 三分量；
- Risk Ledger combined Heat；
- 七层 scope planned loss allocation；
- 提案与最终预检的不可变决策证据。

不新增数据库表、迁移、状态机或第二套风险账本。

## 2. 唯一 Base Heat 公式

当前唯一接受的损失模型版本为：

```text
directional-entry-to-invalidation-v1
```

服务端计算：

```text
base Heat
  = abs(executable_price - initial_invalidation_price)
    × requested_quantity
    × certified_contract_multiplier

incremental worst-case loss
  = base Heat
    + requested protected-profit giveback
    + requested cost stress add-on
```

方向规则继续独立失败关闭：LONG 的失效价必须低于可执行价，SHORT 的失效价必须高于可执行价。市场
输入中的 `contract_multiplier` 必须精确等于 capability/certification binding 中的乘数；不允许调用方
用另一个乘数计算较小损失。

## 3. v2 命令与旧输入封锁

本包升级两项内部命令：

```text
risk.precheck.evaluate.v2      payload_schema_version=2
execution.intent.create.v2     payload_schema_version=2
```

旧命令类型返回 `COMMAND_TYPE_MISMATCH`；v2 命令携带非 2 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。

v2 从嵌套请求模型删除：

- `requested.requested_reserved_heat`；
- `scope_risks[].requested_incremental_planned_loss`。

`RequestedRiskIncrease` 与 `ScopeRiskInput` 都设置 `extra="forbid"`，旧字段不会被静默忽略，而是使整个
风险输入失败。每个 scope 的 requested planned loss 由 Risk Engine 直接使用同一
`incremental worst-case loss`。调用方只保留当前耐久使用量与 requested stress loss；stress 输入不得
低于 canonical planned loss，低报在进入风险计算前即拒绝。

## 4. 审计与精度合同

`RiskEvaluationResult` 明确保存：

- `requested_base_heat`；
- `requested_protected_profit_giveback`；
- `requested_cost_stress_add_on`。

三项随 proposal/final decision JSON 一起 hash 和不可变落库；最终 reservation 再以数据库求和约束冻结。

Durable Exposure 的分量复算改为与 `NUMERIC(38,18)` 一致的 18 位定点比例分配：

1. giveback 与 cost 按 active Heat 比例向下取到 18 位；
2. 定点舍入余量归入 base Heat，保证三项之和严格等于 active combined Heat；
3. active base Heat 在 Open/Reserved/Unknown 非零 bucket 间分配，最后一个非零 bucket 吸收余量；
4. scope planned/stress 按同一 18 位精度缩放。

这消除了循环小数比例导致 `5.25` 被重建为 `5.249999…` 后触发精确相等拒绝的问题，同时保持现有
Ledger/Exposure 守恒与 Unknown 锁定语义不变。

## 5. 明确未完成范围

- protected-profit giveback 仍未从 canonical 仓位峰值、保护价与已锁定利润推导；
- cost stress add-on 仍未从 canonical 场所费用、滑点、资金费和订单类型事实推导；
- requested scope stress loss 仍是后续需要规范化的输入；
- 提案阶段 executable price/market fact 尚未接入真实私有 venue collector；最终预检虽受冻结授权价格
  边界约束，也不代表真实交易适配器已经存在；
- 真实 VenueAdapter/Freqtrade、Web/PWA、Telegram、Margin、Vault/CTO、PnL、部署与运维目标仍未完成；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

按用户特别约束，Codex Security 及其审计 Skill、插件和模块保持停用；本包只使用常规架构约束、
静态检查、数据库集成测试和全量回归。
