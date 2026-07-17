# WP-0005：TradingAuthorization 与 Campaign 授权基础

> 状态：Implemented
> 上位合同：《领域模型与状态机》第 3、7、8、10 节、《策略合同与数值化验收门》第 8 节、
> 《OMS、Freqtrade 与 VenueAdapter 执行规范》第 4、6 节
> 前置包：WP-0001..0004

## 交付边界

本包把已经冻结且达到人工审核 quorum 的初仓提案原子签发为：

```text
Approved FrozenProposalVersion
  + 完整 ReviewerVote / ApprovalDecision 证据
  + 当前 SystemRiskState
  + 冻结策略、账户、保证金、抵押品、Catalog、执行与证书引用
  -> immutable TradingAuthorization（Authorized Capacity）
  -> immutable Campaign identity + PENDING_ENTRY
  -> one-time Initial Order Authorization + ACTIVE
  -> optional Add Authorization Package + AddUnit 30/50/100
  -> command receipt + audit + outbox + append-only state history
```

`TradingAuthorization` 保存人工最多允许的全 Campaign 风险容量，但它不是 Reserved/Open/Unknown
Heat。签发不会创建 `RiskReservation`、RiskLedger 分录或 `OrderIntent`，也不会调用 Freqtrade、
VenueAdapter 或交易所。数据库固定 `authorization_mode=SHADOW`、`execution_eligible=false`；三项真实
能力门继续为 `DISABLED`。

处理器只接受 `trading-authorization-service` 的 INTERNAL/SYSTEM 命令。Proposal Review 仍只记录
人工审核事实，绝不在审核事务内隐式签发授权。

## 原子签发与冻结绑定

签发前对提案行使用 `SELECT ... FOR UPDATE`，并逐项验证：

- Proposal 仍为 `FROZEN`、用途为初仓、版本与命令一致、尚未过期；
- `spec` 与 `risk_summary` 的 canonical hash 仍等于冻结 hash；
- 唯一 ApprovalDecision 精确绑定该 proposal version、状态为 `APPROVED`、尚未过期；
- APPROVE vote 数量等于确定 quorum，且每票继续绑定相同 risk summary hash；
- SYSTEM 提案的 strategy/version 与冻结 spec 一致；
- 当前风险状态存在且只允许 `NORMAL` 或 `NO_PYRAMID`；
- 冻结 spec 完整包含策略参数、账户抽象、margin mode、collateral scope/pool、授权政策、仓位管理
  模板、Add 里程碑政策、Adapter、Freqtrade worker、账户能力和 CapabilityCertificate 引用。

签发快照保存 ApprovalDecision、每张批准 vote 的 Reviewer、审核授权决定、认证上下文和政策版本，
以及 proposal/risk hash、风险状态版本和全部运行绑定，再保存独立 canonical hash。相同 proposal
version 和 approval decision 在数据库均只能对应一个 authorization。不同幂等键并发请求在提案行锁
上串行，后到请求只返回既有权威身份；相同幂等键由 command receipt 重放。

ORM 写入按授权根、Campaign、Initial、状态、Add package/unit 的外键顺序显式 flush，但全部仍处于
一个 PostgreSQL 事务。任何中途异常会连同已 flush 的根、子事实、receipt、audit 和 outbox 一并
回滚。

## 固定容量与 Initial Authorization

- `1R_0 = Total Capital Snapshot_0 × 0.5%`；数据库再次验证公式。
- 低/中/高冻结损失上限分别为 `1R/2R/3R`；数据库再次验证档位公式。
- `authorized_loss_capacity = 1R_0 × proposal.requested_max_r`，且不得超过冻结档位上限。
- Initial 最大数量等于 `risk_approved_quantity`，不是用户原始请求数量。
- Initial 绑定 venue 下的 account、account abstraction、margin mode、collateral scope/pool、
  Instrument、方向和仓位管理模板版本。
- 价格参考优先使用冻结 limit price，否则使用 trigger price；上下界由冻结 `max_slippage_bps`
  对称计算。执行前仍必须重新通过实时风险和事实新鲜度门，本包的价格范围不构成发送许可。
- Initial 短窗口不超过 proposal/approval 的 `valid_until`。

Initial 状态由数据库限制为 `ACTIVE -> CONSUMED | EXPIRED | REVOKED | INVALIDATED`。本包只签发
ACTIVE，并实现过期、撤销和失效；`CONSUMED` 必须等下一包由非零真实初仓成交与原子 OrderIntent
闭环驱动。

## Campaign 与 Add 授权

Campaign 身份精确绑定 authorization、proposal root/version、策略/version、venue、execution
domain、账户、Instrument、方向、冻结 1R 和 Funding Envelope。主状态只使用上位合同的
`PENDING_ENTRY -> OPEN -> CLOSING -> CLOSED`；本包只创建 PENDING_ENTRY，不以授权过期冒充仓位或
对账事实。

关闭自动加仓时不创建 Add package。开启时：

- 低/中/高最多 1/2/3 个 AddUnit；
- unit 1/2/3 由数据库分别固定为 30%/50%/100% 里程碑；
- NORMAL 下 package 为 `DORMANT`、unit 为 `AVAILABLE`；
- NO_PYRAMID 下仍可签发尚需实时风险复核的 Initial，但 package 和所有 unit 从创建起即为
  `INVALIDATED`，不会在未来 NORMAL 时复活；
- Add package 绑定 Campaign、同方向、次数、3x/5x/10x 范围内的冻结目标杠杆、里程碑政策与最
  长有效期；不预先固定未来 Add 数量，也不占用 Heat。

数据库为 Add package 和 AddUnit 实现上位合同的完整单向迁移白名单，包括未来所需的 DORMANT 到
ACTIVE、AVAILABLE 到 CLAIMED、CLAIMED 零成交返回 AVAILABLE，以及任意非零成交后 CONSUMED。
本包不调用这些扩风险迁移。`CLAIMED` 必须在下一包与唯一 `RiskReservation + OrderIntent` 同事务
建立，禁止用字符串 lock ref 或单独状态更新伪造占用。

## 收紧、过期与风险状态同步

内部收紧命令支持：

- `SYNC_RISK_STATE`：NO_PYRAMID 永久失效未消费 Add；NO_NEW_POSITION、REDUCE_ONLY、
  KILL_SWITCH 或缺失/UNKNOWN 同时失效未消费 Initial 与 Add；NORMAL 不执行复活；
- `EXPIRE`：只有根有效期已经过去才把仍可消费的 Initial/Add 变为 EXPIRED；
- `REVOKE_ADD`：撤销 package、失效未消费 AddUnit，不影响仍有效 Initial；
- `REVOKE_ALL`：撤销仍 ACTIVE 的 Initial 和 Add；
- `INVALIDATE_ALL`：用于账户、Instrument、方向、版本、保护或对账事实变化后的不可逆失效。

已终结对象保持原状态，重复同步不会追加伪迁移。扩档位、增加次数、放宽价格/时间、换 venue 或
重新开启 Add 均没有原地修改接口，必须产生新 proposal version 和新人工审核。

## 持久化与防篡改

迁移 `20260718_0005` 增加：

- `trading_authorizations`；
- `campaigns / campaign_states`；
- `initial_order_authorizations / initial_authorization_states`；
- `add_authorization_packages / add_authorization_package_states`；
- `add_units / add_unit_states`；
- `authorization_state_transitions`。

授权根和所有 spec/identity 表拒绝 UPDATE/DELETE；状态表拒绝 DELETE、跳版本、倒退时间、非法迁移
和终态复活。每次状态 INSERT/UPDATE 由数据库触发器自动写入 append-only 历史，历史本身拒绝
UPDATE/DELETE。服务事件仍与 command receipt、不可变 audit 和 transactional outbox 同事务提交。

低基数指标记录签发结果/风险状态，以及收紧 action/风险状态。稳定拒绝码区分内部服务、对象与
组织作用域、版本、proposal/approval/hash/quorum、冻结绑定、价格边界、系统风险状态和未到期
过期请求。

## 明确未实现

- 风险 Reservation、RiskLedger 与 Funding 预留；
- OrderIntent、VenueOrder、Fill、真实仓位或保护对账；
- Initial/AddUnit 的原子 CLAIM、发送、Unknown 锁定、零成交释放和非零成交消费；
- 初仓真实成交后 Add package 激活；
- Campaign OPEN/CLOSING/CLOSED 与保护覆盖投影；
- 真实 CapabilityCertificate 存储、验证、吊销和失效传播；
- Freqtrade、VenueAdapter、Web/PWA、Telegram、Margin、Vault/CTO；
- 任何历史回放、测试网、影子实时流或小额实盘证书。

因此本包不构成真实交易、自动加仓或资金划转能力，也不能用于宣称工程或能力认证完成。

## 回滚

先停止授权签发/收紧实例并导出新增事实，再执行：

```bash
TRADING_DATABASE_URL="$DATABASE_URL" uv run alembic downgrade 20260718_0004
```

这会删除全部 WP-0005 授权、Campaign、状态历史和守卫，但保留 WP-0001..0004。已有真实授权事实
后，schema downgrade 是破坏性操作而非业务恢复手段；优先回滚应用并保留 schema。

下一工作包必须把 `RiskReservation + OrderIntent + Initial/AddUnit claim` 放在同一数据库事务，
然后才允许受控发送路径继续向执行域推进。
