# WP-0050：Canonical 保护失效退出候选

## 1. 交付目标与信任边界

WP-0048/0049 已能把多个规范化候选合并为服务端 current position 上的唯一 target，但还没有真实候选生产器。
WP-0050 新增 `CampaignProtectionExitCandidateService`：服务端读取 WP-0045 fresh Campaign current-position
binding 和 exact-scope canonical current protection；保护缺失、陈旧、来自未来、UNKNOWN 或与当前仓位不一致时，
生成 `target=0 / IMMEDIATE` 的系统风险退出候选。

本包只产生 query-time evaluation 和 WP-0048 输入，不持久化 target、不创建 OrderIntent、不申请 sender claim，
也不调用 OMS、Freqtrade、VenueAdapter 或场所接口。

## 2. 保护健康判定

`CLEAR` 必须同时满足：

- protection projection 为 fresh `CONFIRMED`；
- protection 的 source position snapshot ID 等于 Campaign current position snapshot ID；
- direction、quantity 完全一致；
- protection facts 不早于 current-position facts；
- projection 自身已经证明 venue-native、reduce-only、全量覆盖、无 replacement 且至少有一个有效 stop。

满足时 evaluation 保存 current/protection snapshot ID、hash、order-set hash、方向、数量、facts time 与自校验
hash，但不生成候选。

不满足时只使用 bounded reason：

```text
PROTECTION_MISSING
PROTECTION_STALE
PROTECTION_FROM_FUTURE
PROTECTION_UNKNOWN
PROTECTION_BINDING_CONFLICT
```

生成的候选固定为：

```text
source_type=SYSTEM_RISK_REDUCTION
policy_version=canonical-protection-health-v1
target_quantity=0
urgency=IMMEDIATE
current_position_binding_hash=<server-resolved exact binding>
valid_until=<current-position binding expiry>
```

候选送入 WP-0049 后得到 `EXIT` 和 `reduce_only_required=true`。这是未来退出执行链的输入合同，不代表订单
已生成或已发送。

## 3. 原生止损与价格触发边界

专项验证确认 canonical protection normalization 不允许“active stop trigger 已越过同一 position snapshot 的
current Mark”仍被记录为有效保护；这种输入会被 `VENUE_PROTECTION_TRIGGER_PRICE_INVALID` 拒绝。因此本包不
伪造 `HARD_STOP` 价格越界状态。

真实价格触发仍由场所原生 stop 独立于 Web/Telegram/控制面执行。后续若要额外生成 `HARD_STOP` 仲裁候选，
必须先建立独立、可信、可版本化的 stop-policy/market observation 来源；不能把已失效的 active protection
快照当作触发证据。WP-0050 处理的是“保护健康失败必须退出”，不是重复模拟场所撮合触发器。

## 4. 确定性、监控与失败关闭

evaluation 使用 `campaign-protection-exit-evaluation-v1`，候选使用 WP-0048 的自校验 hash。current position
missing/stale/conflict 会先由 WP-0045 失败关闭；position 在查询时已经没有剩余有效期也拒绝生成候选。

新增 bounded metric：

```text
trading_campaign_protection_exit_evaluations_total{result}
```

## 5. 数据库与未完成范围

本包不新增表或 Alembic revision；schema 仍为 `20260718_0035`。

仍未完成：

- durable Campaign target fact、revision、去重/替换与并发锁；
- 独立 stop-policy/market observation、趋势退出、动态去杠杆及其他系统风险候选；
- reduce-only OrderIntent、sender claim、部分成交、Unknown、重复/超量退出防护与 position reconciliation；
- 原生保护成交与控制面退出之间的单一所有权、互斥和恢复语义；
- OMS/Freqtrade/VenueAdapter、真实 collectors 和逐场所认证。

`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。按用户明确约束，Codex Security
及其所有审计 Skill、插件和模块保持停用；本包只执行常规架构约束、代码检查、数据库约束与测试。
