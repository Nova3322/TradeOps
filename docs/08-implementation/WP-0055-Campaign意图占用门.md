# WP-0055：Campaign 意图占用门

## 1. 交付目标与边界

WP-0053/0054 已能派生并耐久化 reduce-only plan，但 exact current-position snapshot 仍可能与一个未解决的
INITIAL/ADD OrderIntent 并存。若该意图仍可成交或其结果未知，当前数量随时可能变化，继续准备固定差额会产生
over-reduction 风险。

WP-0055 新增 `CampaignOrderIntentOccupancyService`，在 reduction plan 形成前服务端检查同一 Campaign 的全部
OrderIntentState。它不创建 reduction OrderIntent、不取消既有订单、不替代 reconciliation，也不赋予任何发送
权限。

## 2. 稳定与占用分类

只有以下状态被视为 position-stable：

```text
CANCELLED_ZERO_FILL
REJECTED_ZERO_FILL
POSITION_RECONCILED
PROTECTION_CONFIRMED
COMPLETED
```

这表示订单已经有 canonical 零成交终态，或其正成交数量已经进入 canonical position reconciliation。其余状态
全部占用 reduction planning，包括：

- `INTENT_CREATED`、`DISPATCHING`、`VENUE_ACKNOWLEDGED`；
- `PARTIALLY_FILLED`、`FILLED`、`CANCEL_PENDING`、`CANCELLED_PARTIAL`；
- `RESULT_UNKNOWN`、`FAILED_SAFE` 以及任何尚未进入稳定集合的状态。

分类结果携带 observed/stable 数量、按 OrderIntent ID 确定性排序的 blocker 明细、状态版本、数量字段与自校验
occupancy hash。`UNKNOWN` 或部分成交绝不被当作零风险。

## 3. Reduction 计划强绑定

WP-0053 resolver 在 target/current/state 检查通过后继续解析 occupancy；结果不是 `CLEAR` 时固定拒绝：

```text
CAMPAIGN_REDUCTION_INTENT_OCCUPIED
```

普通只读 resolver 提供 query-time 判断。WP-0054 preparation 已先锁定 Campaign、CampaignState 与 latest target，
随后以 `lock_intents=true` 锁定同 Campaign 的 OrderIntent/State 行，因此准备事务不会与已存在未解决意图交错。
future OrderIntent 创建和真正发送前仍必须再次执行相同或更严格的事务门；本包不把一次 CLEAR 当作长期 permit。

## 4. 故障场景验证

专项场景先建立一个 `INTENT_CREATED` Add，再写入更新的 canonical position snapshot，但不为该新 snapshot 提供
保护事实。target recorder 因 `PROTECTION_MISSING` 形成 EXIT/CLOSING；reduction resolver 随后因活动 Add 拒绝
计划，而不是按旧数量生成退出动作。

新增 bounded metric：

```text
trading_campaign_order_intent_occupancy_evaluations_total{result}
```

## 5. 数据库与未完成范围

本包不新增 migration；schema 仍为 `20260718_0037`。occupancy 是 current-state query，不是 durable execution
permit。

仍未完成：

- reduction OrderIntent 自身的 active/Unknown/部分成交状态机与 over-reduction 数量预留；
- supersession、取消替换和旧 plan 作废；
- 原生保护与控制面退出之间的 single-writer/对账；
- 部分减仓后的 canonical Campaign current binding；
- 逐场所 reduce-only 参数与真实 OMS/Freqtrade/VenueAdapter。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
