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
| 权限 | `RoleAssignment`、`ROLE_ACTIONS`、服务端 `_require_role` | 六类岗位、团队/账户/交易所范围、默认拒绝；角色只在当前团队计算 | 后续把持久化账户根接入同一授权器，不另建平行 ACL |
| 提案 | `Proposal`、`CommandReceipt` | 冻结载荷、版本、语义哈希、幂等、有效期；提案—风控—授权—Campaign 根已由团队约束 | 账户、策略和信号配置继续引用该状态机，不复制提案真源 |
| 审核 | `Approval`、`review_proposal` | 服务端阻止普通创建者自审、高风险双审核 | 审计发现 SYSTEM_ADMIN 本人提案直批旁路；本批次删除 API、服务方法和页面入口 |
| 风控 | `RiskPolicy`、`RiskDecision`、`RiskReservation` | 版本化总风险、事实新鲜度、状态机、原子占用 | 扩展单笔最大亏损、连续亏损、冷却期及团队/账户限制；仍由同一 Risk Engine 拒绝 |
| 账户事实 | `ExchangeAccount` 与 `account_id + venue` 贯穿提案、授权、任务、订单、仓位、权益与报表 | 团队内同场所多账户、AES-GCM 凭据版本、连接/交易状态分离；Proposal 与订单/成交/仓位/权益/资金费事实由 Team 和账户复合边界约束；对账由 Team 隔离 | 运行连接仍读取部署配置，OKX/Bybit Adapter 未实现；资金提案/转移与 sender/task 根尚未团队化，不声称整条账户链已完成 |
| 审计 | `AuditEvent` | actor、对象、版本、correlation、idempotency key、Workspace、Team；交易链事件带账户范围 | 后续为账户/资金独立根补齐同一范围；继续使用统一追加事件流 |
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

- 先创建默认 Workspace 与默认团队，把现有权限及根聚合回填到该范围；业务根只持久化非空 `team_id`，`workspace_id` 由 Team 唯一派生，避免两个可漂移真源。API 对外物化 Workspace、Team、Account 三层范围。
- 请求必须携带或从会话选择当前 Workspace 与团队；服务端验证 Workspace 成员、Team 成员和团队角色三层状态。
- 权限顺序固定为 `Workspace → Team → Account → Venue → Action`；同一用户在不同团队的角色不继承、不合并。
- Workspace 管理员只管理组织与团队；跨团队汇总使用显式授权路径，不自动获得团队交易、风控或资金动作。
- 所有读取按团队过滤，所有写入从服务端团队上下文赋值；客户端字段不作为授权真源。
- 账户、策略、提案、审核、风险、通知、报表与审计不得用相同字符串 ID 跨团队关联。
- 新团队在账户、信号源和风险政策完成前保持不可增险；迁移不得自动开启任何危险 Gate。

## 5. 风控来源登记

本地权威文档继续以 `交易系统总体方案.md`、`docs/02-domain/风险引擎规格.md` 及版本化风险决策登记为准。外部 Binance 参考页在 2026-08-09 可公开读取，阈值均为待配置的 `n`，仅登记以下语义。2026-08-10 本阶段重新访问时页面不可用，因此以下内容只是已登记语义，不作为刷新后的外部事实；页面恢复前不新增或猜测任何规则与阈值：

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

## 7. 已落地迁移与当前阻断

- `20260809_0016`：新增 Workspace、Team、成员关系和团队化角色；现有用户、角色和审计回填默认范围。新团队 `trading_enabled=false`，账户与风险根尚未配置时拒绝业务动作。
- `20260810_0017`：为 Proposal、ProposalDefaultConfig、RiskDecision、TradingAuthorization、Campaign 增加非空团队根和复合外键；系统候选去重、默认配置版本、未关闭 Campaign 唯一性全部按团队计算。
- `20260810_0018`：新增团队 `ExchangeAccount`，唯一键为 `team_id + account_id + venue`；旧账户引用无猜测回填为未配置/交易关闭，Proposal 增加数据库复合外键。Binance、Hyperliquid、OKX、Bybit 凭据使用绑定团队、账户、场所和轮换版本的 AES-256-GCM 信封；API/页面只投影脱敏元数据。保存或轮换凭据只把连接置为 `NOT_VERIFIED`，交易保持 `DISABLED`。
- `0018` 五维验收：本地生产结构完成 `0017 → 0018` 升级，隔离测试库完成 `0018 → 0017 → 0018` 往返且 `alembic check` 无漂移；单元、API、集成分别为 `347 / 22 / 216` 通过。全部危险能力关闭的本地 API 通过 `/health/live` 与 `/health/ready`；真实密码登录后的 `/venues` 与数据库一致显示 4 个迁移账户全部未配置、交易关闭，1440px 与 390px 均无横向溢出，页面未回显登录密码，登录后的浏览器控制台、页面异常和请求失败为 0。
- `20260810_0019`：为订单、成交、仓位、账户权益、权益历史、资金费和对账增加非空 Team 根；团队、账户复合外键阻止跨团队错绑，外部订单/成交/资金费身份和账户事实唯一性按团队计算。服务端写入从当前团队赋值，场所事实页、资金事实聚合、计算型对账及其读取按团队过滤；遗留无团队事实只回填默认团队，迁移不会配置凭据或开启交易。
- `0019` 五维验收：升级前备份为 `/private/tmp/trading-pre-0019-20260810.dump`；本地业务库完成 `0018 → 0019`，订单 8、成交 13、仓位 11、权益 6、权益历史 4151、资金费 0、对账 4421 条记录的 `team_id` 均非空，6 个 Team/账户复合约束存在，5 个危险 Gate 全部 `DISABLED`。隔离测试库完成 `0019 → 0018 → 0019` 往返及 `alembic check`，并从 `0015` 构造订单、成交、仓位、权益、权益历史、资金费和对账遗留事实，验证全部回填到默认团队；相同账户字符串的双团队事实/对账隔离和跨团队保护写入由测试覆盖。Ruff、变更模块 mypy、单元/API/集成分别为 `通过 / 通过 / 347 / 22 / 217`。本地 API `/health/live`、`/health/ready` 通过；真实密码登录后的 `/venues` 及 Binance/Hyperliquid 事实 API 的 Team 均与会话当前团队一致，4 个账户交易能力全部关闭，1440px/390px 无横向溢出、未泄露密码、浏览器错误为 0。Hyperliquid dry-run worker 启动探针因上游 `429 Too Many Requests` 未就绪，本次 API 验收显式关闭 runtime sync、Perptape WebSocket 与 Freqtrade workers，页面如实显示历史快照而非实时事实。
- Proposal / Campaign 查询、实际结果和审计时间线按当前团队过滤；跨团队详情与变更返回 `TEAM_SCOPE_DENIED`。审核、授权、Intent 等强生命周期子对象继续经 Proposal / Campaign 根派生团队；可独立到达且可能没有 Campaign 的场所事实直接持久化 Team，避免相同账户字符串跨团队混算。
- `AuditEvent.account_id` 对交易链事件由服务端对象真源推导；组织级事件保留为空，不伪造账户。
- 创建者自审旁路已从服务端和页面移除；团队切换后即使账户字符串相同，也不能读取、提交、审核或执行另一团队的提案。
- 当前阻断：团队风险政策、资金提案/授权/转移、RiskReservation、sender/task 与运行源配置仍未完成团队根迁移，数据库密文也尚未接入独立运行连接；OKX、Bybit Adapter 未实现。因此新团队继续不可增险，“已登记/已加密/事实已隔离”不是“连接正常”或“交易就绪”。
