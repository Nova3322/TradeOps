# WP-0053：服务端 Reduce-Only 执行计划

## 1. 交付目标与边界

WP-0051/0052 已产生可延续的 durable Campaign target，但 future OMS 仍需要一个明确的服务端计划合同，不能
让调用方自行解释方向、差额或 current binding。WP-0053 新增只读
`CampaignReductionExecutionPlanService`：解析 latest immutable target fact 与 fresh canonical Campaign
current position，生成非派发的 reduce-only OMS input。

本包不创建 OrderIntent，不持久化 plan，不持有 sender lease，不选择场所订单类型，也不调用 Freqtrade、
VenueAdapter 或交易所。

## 2. Exact binding 与计划内容

服务只接受 latest target 为 `REDUCE/EXIT`，并要求：

- target fact 通过 canonical decision payload、projection、semantic hash 和 record hash 复核；
- target 的 current-position binding hash、snapshot ID/hash、current quantity 与 fresh WP-0045 binding 完全一致；
- `EXIT` 对应 Campaign `CLOSING`，`REDUCE` 对应 `OPEN`；
- fresh position 在 query time 后仍有剩余有效期；
- target 严格小于 current quantity。

计划派生：

```text
LONG  -> SELL
SHORT -> BUY
order_quantity = current_quantity - target_quantity
target=0 -> EXIT
target>0 -> REDUCE
reduce_only=true
```

计划携带 target fact ID/version/semantic hash、current binding/snapshot、reason codes、urgency、valid-until、
稳定 idempotency ref 和自校验 plan hash。

## 3. 未认证参数显式不可用

本包不猜测退出订单类型、TIF、触发/限价或逐场所滑点语义：

```text
order_type_status=UNAVAILABLE
venue_execution_terms_status=UNAVAILABLE
environment=SHADOW
live_order_eligible=false
```

因此 plan 不能直接变成场所请求。后续 OrderIntent 创建必须先通过逐场所认证合同，并再次核验 target、current
position、active/Unknown intent、sender fencing 和 over-reduction 边界。

## 4. 失败关闭与监控

固定失败包括：

- target missing；
- latest target 为 HOLD；
- fresh current position missing/stale；
- target/current binding mismatch，要求先运行 WP-0052 refresh；
- Campaign 主状态与 actionable target 不一致；
- plan validity 为空。

新增 bounded metric：

```text
trading_campaign_reduction_plan_evaluations_total{result}
```

## 5. 数据库与未完成范围

本包不新增表或 migration；schema 仍为 `20260718_0036`。

后续 WP-0054 已把 exact plan 保存为单 target 唯一、不可变、不可派发的准备快照；仍未创建 OrderIntent 或
sender claim。

仍未完成：

- 逐场所 reduce-only order type/TIF/price/trigger/slippage 合同与证书；
- durable target 的单次 claim 和 reduction OrderIntent；
- active/Unknown/部分成交 intent 去重、supersession、取消替换和 over-reduction 防护；
- 部分减仓后的 canonical Campaign current binding；
- 原生保护成交与控制面退出 single-writer/对账；
- OMS/Freqtrade/VenueAdapter 与真实 collectors。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
