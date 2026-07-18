# WP-0039：ADD Canonical 正 UPNL 硬门

> 后续状态：WP-0040 已把 ExecutionIntent 升级为 v13，删除 caller `trend_valid`，并要求同一
> Risk Fact Set 与 exact position/protection 上的 latest immutable PASS Strategy Evaluation。
> 当前执行合同以 [WP-0041](WP-0041-INITIAL-Canonical空仓强绑定.md) 为准。本文件保留 WP-0039 历史合同。

## 1. 交付目标与边界

系统的固定策略语义禁止亏损加仓，但 WP-0038 的 ADD 路径仍只校验 caller frozen return、
canonical position/protection 风险和服务端派生杠杆，没有使用已存在的 venue-confirmed
`VenuePositionSnapshot.unrealized_pnl`。因此 caller 理论上可在当前 UPNL 非正时伪造里程碑。

WP-0039 把 canonical position UPNL 纳入 current protected-position projection 和计算哈希，并要求
ADD 的当前 venue UPNL 必须严格大于零。该门是“不亏损加仓”的必要条件，不是完整的净
浮盈证明。

## 2. 命令与计算合同

Proposal RiskPrecheck 继续使用：

```text
risk.precheck.evaluate.v9      payload_schema_version=9
```

ExecutionIntent 升级为：

```text
execution.intent.create.v12    payload_schema_version=12
```

v1 至 v11 execution 命令返回 `COMMAND_TYPE_MISMATCH`，v12 携带非 12 schema 返回
`PAYLOAD_SCHEMA_VERSION_MISMATCH`。RiskPrecheck 没有 ADD-only 字段，因此不随之升级。

current protected-position 计算版本从 `protected-position-risk-v1` 升级为
`protected-position-risk-v2`。v2 的计算合同在既有 position/protection ID、hash、数量、价格、乘数和
Open Heat/Giveback 之外，新增 exact canonical `unrealized_pnl`。任意 UPNL 变化都改变计算
hash，不能重放旧证据表示新收益状态。

## 3. 最终 ADD 顺序

`CurrentProtectedPositionRiskResolver` 依次要求：

1. exact current position/protection projection 为 `CONFIRMED` 且 fresh；
2. quantity、direction、Mark、contract multiplier 和 settlement currency 与 Campaign/request 一致；
3. caller 引用的 position/protection snapshot ID/hash 与 canonical current projection 一致；
4. `canonical_unrealized_pnl > 0`；
5. 投影剩余有效期仍为正。

UPNL 为零或负数返回 `ADD_CURRENT_UNREALIZED_PNL_NOT_POSITIVE`。拒绝发生在风险决策、预留
和 OrderIntent 创建前，AddUnit 仍为 `AVAILABLE`。

## 4. 证据语义

通过 ADD 的 ExecutionRiskDecision input snapshot 保存完整
`protected_position_risk`，其中 `unrealized_pnl`、`calculation_version` 和 `calculation_hash` 可重放。
Risk decision 仍引用同一 protected-position calculation hash。

该 UPNL 是场所私有仓位快照中的当前未实现 PnL，不等于：

- 扣除预计平仓手续费和滑点后的净浮盈；
- 含费用、返佣、资金费和已实现 PnL 的 Campaign Net Trading PnL；
- 初仓冻结保证金参考额上的 frozen return；
- `E_campaign`。

因此 `UPNL > 0` 只能阻断明确亏损或零收益的 ADD，不能单独解锁 30% / 50% / 100% 候选。

## 5. 需求追踪

| 权威要求 | 本包证据 |
| --- | --- |
| 加仓只做盈利金字塔，不亏损摊平 | canonical UPNL 非正时无副作用拒绝 |
| 当前收益不得由 caller 单方声称 | UPNL 来自 exact venue position snapshot 并进入 v2 hash |
| 决策必须可重放 | snapshot ID/hash、UPNL、计算版本和哈希同步留证 |
| 未完整证明时不得扩大声明 | 文档明确限定为 gross venue UPNL 必要门 |

## 6. 明确未完成范围

- submitted `current_position_equity` / `E_campaign` 尚未由 Campaign PnL Ledger 重建；
- frozen return 仍是 caller 输入，平仓成本、资金费、已实现 PnL 与初仓保证金参考额尚未
  整合；
- 趋势延续、布林回调和策略有效性仍未绑定已认证 Strategy Evaluation；
- 真实 collector、OMS/Freqtrade/VenueAdapter、Web/PWA、Telegram、Margin、Vault/CTO、PnL 与运维
  仍未完成；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

本包不新增数据库结构，schema head 仍为 `20260718_0030`。按用户明确约束，Codex Security 及其
所有审计 Skill、插件和模块保持停用；只执行常规代码检查、严格类型检查和测试。
