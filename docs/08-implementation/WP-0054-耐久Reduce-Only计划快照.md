# WP-0054：耐久 Reduce-Only 计划快照

## 1. 交付目标与边界

WP-0053 已能从 latest durable target 与 fresh canonical current position 派生 non-dispatchable
reduce-only plan，但该结果只存在于一次查询返回值中。WP-0054 新增内部命令
`campaign.reduction-plan.prepare.v1`，把同一 target fact 对应的服务器派生计划保存为不可变、可复核、可重放的
唯一准备快照。

本包仍不创建 `OrderIntent`，不生成 sender claim/client order ID，不选择订单类型、TIF、价格或逐场所参数，也不
调用 Freqtrade、VenueAdapter 或交易所。

## 2. 服务端准备合同

只有 `campaign-reduction-preparation-service` 通过 INTERNAL channel 才能调用准备命令。命令必须携带：

- exact Campaign 与 organization scope；
- 调用方已观察到的 latest target version；
- 与 target 计算一致的 canonical fact freshness 上限。

服务在同一事务中：

1. 使用与 target recorder 相同的 Campaign advisory-lock namespace；
2. 锁定 Campaign、CampaignState 与 latest target fact；
3. 要求 `expected_version` 等于 latest target version；
4. 重新运行 WP-0053 resolver，复核 target/current binding、Campaign state 与 validity；
5. 保存完整 `plan_payload`、`plan_hash` 和独立 `record_hash`；
6. 由既有 command executor 原子写入 receipt、audit 与 outbox。

## 3. 单 target 收敛与不可变性

`campaign_reduction_plan_snapshots` 对 `campaign_target_position_fact_id` 建立唯一约束。相同命令重放返回原 receipt；
不同幂等键并发准备同一 target 时，事务锁使结果收敛到同一 snapshot ID，第二个命令返回
`ALREADY_PREPARED`。

数据库 insert guard 独立复核：

- target 必须仍是 Campaign latest target 且 action 为 `REDUCE/EXIT`；
- Campaign、organization、target version/semantic hash 与 current-position binding 完全一致；
- position snapshot、方向、平仓 side、current/target/order quantity、urgency 和 reason codes 完全一致；
- `reduce_only=true`；
- 订单类型和场所执行条款均为 `UNAVAILABLE`；
- plan 与 record 继续为 `SHADOW`、不可派发。

UPDATE/DELETE trigger 保护快照不可变；非空表 downgrade 被拒绝，避免静默删除准备证据。

## 4. 失败关闭与监控

明确拒绝：

- durable target 不存在或不是 `REDUCE/EXIT`；
- expected target version 冲突；
- current position 缺失、陈旧或与 target binding 不一致；
- Campaign state 与 target action 不一致；
- 已存在准备快照超过 plan validity，必须先刷新 target/current binding；
- 错误 principal、channel、object 或 scope。

新增 bounded metric：

```text
trading_campaign_reduction_plan_preparations_total{result}
```

## 5. 数据库与未完成范围

新增 migration `20260718_0037` 和表 `campaign_reduction_plan_snapshots`。迁移不 seed 任何现实能力。

仍未完成：

- 逐场所 reduce-only order type/TIF/price/trigger/slippage 合同与证书；
- plan 的单次执行 claim 和真正 reduction OrderIntent；
- active/Unknown/部分成交 intent 去重、supersession、取消替换和 over-reduction 防护；
- 部分减仓后的 canonical Campaign current binding；
- 原生保护成交与控制面退出 single-writer/对账；
- OMS/Freqtrade/VenueAdapter 与真实 collectors。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
