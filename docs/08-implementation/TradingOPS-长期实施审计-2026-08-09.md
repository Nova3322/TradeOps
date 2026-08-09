# TradingOPS 长期实施审计（2026-08-09）

## 1. 审计基线

- 分支：`codex/trading-console-hardening`
- 基线提交：`25d561724278084615eba961bf8df8de67bcd17d`
- 目标：在现有唯一控制链上增加 Workspace / 团队隔离、多信号源、多账户、多场所、通知、影子模式与 Agent 能力，不复制权限、风控、审计或执行真源。
- 安全基线：`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD`、签名和广播继续默认关闭；缺少团队、账户、凭据、事实或政策时拒绝增险。

## 2. 可复用真源与缺口

| 领域 | 当前真源 | 已具备 | 当前缺口 / 处理原则 |
| --- | --- | --- | --- |
| 身份 | `User`、密码认证、短期 Session | HUMAN / SERVICE、scrypt、会话版本撤销 | 复用 `User` 表达 Agent；不新建 Agent 身份真源 |
| 权限 | `RoleAssignment`、`ROLE_ACTIONS`、服务端 `_require_role` | 六类岗位、账户/交易所范围、默认拒绝 | 当前全局生效；增加 Workspace / Team 范围后由同一授权器裁决，不另建平行 ACL |
| 提案 | `Proposal`、`CommandReceipt` | 冻结载荷、版本、语义哈希、幂等、有效期 | 增加团队、账户、策略和信号来源绑定；不复制提案状态机 |
| 审核 | `Approval`、`review_proposal` | 服务端阻止普通创建者自审、高风险双审核 | 审计发现 SYSTEM_ADMIN 本人提案直批旁路；本批次删除 API、服务方法和页面入口 |
| 风控 | `RiskPolicy`、`RiskDecision`、`RiskReservation` | 版本化总风险、事实新鲜度、状态机、原子占用 | 扩展单笔最大亏损、连续亏损、冷却期及团队/账户限制；仍由同一 Risk Engine 拒绝 |
| 账户事实 | `account_id + venue` 贯穿提案、授权、任务、订单、仓位、权益与报表 | 账户范围已进入多数权限与执行检查 | 缺少持久化交易所账户与加密凭据；同场所多账户不能由进程级配置准确表达，因此需新增独立账户实体 |
| 审计 | `AuditEvent` | actor、对象、版本、correlation、idempotency key | 增加 `team_id` 与账户范围；继续使用统一追加事件流 |
| 执行 | Proposal → Approval → Risk → Authorization → Intent → Venue facts / Reconciliation | Binance、Hyperliquid 受 Gate 控制的执行链 | OKX、Bybit 实现同一 Venue Port；不新建第二套 OMS 或 Risk Engine |
| 信号 | `PerptapeFeed`、实时机会页 | 缓存版本、新鲜度、只读同步、冻结系统提案 | 当前全局单 Key；需新增团队信号源配置。Webhook 只持久化已验证信号，暂不自动创建提案 |
| 通知 | Telegram 发送与去重键 | 提案/资金通知、审核链接 | 统一事件 Outbox 后扩展 Slack、Lark、Email；渠道不进入权限主体或交易链 |
| 影子 | `environment=SHADOW/TESTNET/LIVE` | 影子提案、模拟执行与结果语义 | 仍需独立资金账本、账户与写入适配器硬隔离，禁止复用 LIVE 签名/发送配置 |
| 页面 | 单页控制台与服务端 capability 导航 | 角色化首页、提案、审核、任务、风险、资金、审计 | UI 收尾排在事实、安全边界和测试之后；当前只做必要功能入口与阻断提示 |

## 3. 必要实体判定

只新增现有对象无法准确表达独立生命周期或安全边界的实体：

1. `Workspace` / `WorkspaceMembership`：组织与跨团队管理授权是独立边界；Workspace 管理员不自动获得任一团队的交易角色。
2. `Team` / `TeamMembership`：同一成员可加入多个团队，而成员启停与团队角色有独立生命周期；现有 `User` 不能准确承载这些关系。
3. `ExchangeAccount`：同交易所多账户、凭据轮换、连接状态和交易能力是独立生命周期，不能继续由单个进程环境变量充当真源。
4. `SignalSourceConfig` / `WebhookSignal`：团队模式选择、签名密钥版本、重放窗口和已验证信号需要独立生命周期；提案仍复用 `Proposal`。
5. `NotificationRoute` / `NotificationDelivery`：路由、去重、重试与送达审计独立于用户权限；渠道不成为授权主体。

以下能力不新增平行实体：团队岗位和账户范围继续扩展 `RoleAssignment`；Agent 复用 `User(principal_type=SERVICE)`；风险限制扩展 `RiskPolicy`；执行和报表继续引用现有账户、任务、成交与权益事实。

## 4. Workspace / 团队隔离迁移原则

- 先创建默认 Workspace 与默认团队，把现有权限及根聚合回填到该范围，再把业务根聚合的 `workspace_id` / `team_id` 改为非空。
- 请求必须携带或从会话选择当前 Workspace 与团队；服务端验证 Workspace 成员、Team 成员和团队角色三层状态。
- 权限顺序固定为 `Workspace → Team → Account → Venue → Action`；同一用户在不同团队的角色不继承、不合并。
- Workspace 管理员只管理组织与团队；跨团队汇总使用显式授权路径，不自动获得团队交易、风控或资金动作。
- 所有读取按团队过滤，所有写入从服务端团队上下文赋值；客户端字段不作为授权真源。
- 账户、策略、提案、审核、风险、通知、报表与审计不得用相同字符串 ID 跨团队关联。
- 新团队在账户、信号源和风险政策完成前保持不可增险；迁移不得自动开启任何危险 Gate。

## 5. 风控来源登记

本地权威文档继续以 `交易系统总体方案.md`、`docs/02-domain/风险引擎规格.md` 及版本化风险决策登记为准。外部 Binance 参考页在 2026-08-09 可公开读取，阈值均为待配置的 `n`，仅登记以下语义：

- 规则 1：单标的或全账户浮亏超限后平仓并进入冻结期。
- 规则 2：账户净值相对历史高点回撤超限后清仓并冷却。
- 规则 3：全账户或单标的杠杆超限后按约束减仓。
- 规则 5：24 小时订单数超限后进入冷却并阻断新增交易。

按需求明确排除规则 4（方向信号一致性限制）。外部文档没有给出生产阈值，代码不得猜测 `n`；任何数值必须进入版本化政策并通过证据门。

## 6. 五维验收门

每个阶段均保留以下证据后才进入下一阶段：

1. **代码**：唯一真源、默认拒绝、危险 Gate 不变、`git diff --check`。
2. **数据库迁移**：upgrade、downgrade、re-upgrade、`alembic check`，并验证旧数据回填与非空约束。
3. **后端 API**：跨团队、跨账户、越权、自审、重放、幂等冲突和陈旧版本均由服务端拒绝。
4. **实际页面 / 端到端运行**：真实登录和团队切换路径，页面状态与 API/数据库一致，不把旧缓存标为实时。
5. **自动化测试**：单元、集成、契约、浏览器、响应式与可访问性；UI 阶段另保留深浅主题截图。
