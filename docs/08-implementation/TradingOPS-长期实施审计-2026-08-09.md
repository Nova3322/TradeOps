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
| 风控 | `RiskPolicy`、`RiskDecision`、`RiskReservation` | 团队化版本政策、总风险、单账户风险、最大单笔亏损、连续亏损、冷却期、事实新鲜度、状态机和原子占用 | 外部阈值 `n` 尚无事实，不预填；政策未完整配置时同一 Risk Engine 拒绝新增风险 |
| 账户事实 | `ExchangeAccount` 与 `account_id + venue` 贯穿提案、授权、任务、订单、仓位、权益与报表 | 团队内同场所多账户、AES-GCM 凭据版本、连接/交易状态分离；四场所可用数据库信封执行一次性官方只读连接验证；Proposal 与订单/成交/仓位/权益/资金费事实由 Team 和账户复合边界约束；对账由 Team 隔离 | 持续运行连接仍读取部署配置且只覆盖 Binance/Hyperliquid；OKX/Bybit 持续事实 Adapter 未实现；资金提案/转移与 sender/task 根尚未团队化，不声称整条账户链已完成 |
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
4. `TeamSignalSource` / `SignalEvent`：团队模式选择、签名密钥版本、重放窗口和已验证信号需要独立生命周期；提案仍复用 `Proposal`。
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
- `20260810_0020`：复用 `RiskPolicy`、`RiskDecision`、`RiskReservation` 和既有提案—审核—执行链，不新增平行风控实体。政策版本、修订号、唯一活动政策、恢复申请和风险预留均按 Team 隔离；RiskDecision、Campaign、Authorization 与 Reservation 使用 Team 复合外键，跨团队错绑由数据库拒绝。政策新增单账户最大风险、最大单笔亏损、最大连续亏损和亏损冷却期；Risk Engine 在决策和下单意图最终检查两处使用同一不可变快照。连续亏损分别按团队和 `账户 + 场所 + 环境` 的最近已关闭 Campaign 计算，非亏损结束连续序列，冷却期内服务端返回 `LOSS_COOLDOWN_ACTIVE`。单笔超限返回 `SINGLE_LOSS_LIMIT_EXCEEDED`，团队或账户容量不足只按已有安全缩量语义处理。
- `0020` 配置与迁移语义：遗留政策保留原总风险和事实时效，但四个新增阈值保持 `NULL`，并以 `RISK_LIMITS_UNCONFIGURED` 阻断新增风险；新团队没有政策时 `/api/risk-controls` 返回结构化 `RISK_POLICY_MISSING` 而不是读取其他团队。最高管理员可通过幂等、预期修订号保护的 Team API 明确创建或收紧政策；直接放宽返回 `REVIEWED_POLICY_CHANGE_REQUIRED`，政策变化使本团队旧 TradingAuthorization 失效。外部 Binance 页面在 2026-08-10 仍不可访问，规则 4 继续排除，其余规则不补写任何未知阈值。
- `0020` 五维验收：隔离测试库位于 `20260810_0020 (head)`，`alembic check` 无漂移，`test_schema` 已覆盖遗留政策回填与迁移往返；变更源码 Ruff、mypy、Node 语法和 `git diff --check` 通过，单元/API 定向 41 个、完整集成 221 个通过。禁用运行同步、真实下单、测试网下单、资金和签名的本地 API 通过 `/health/ready`，真实密码登录后 `/api/risk-controls` 与 `/risk` 一致显示团队政策 `100 / 80 / 20 / 3 / 3600`，桌面端无浏览器错误。当前全站移动布局在 390px 仍会把内容压成不可读的窄列，因此阶段 9 响应式 UI 验收未完成，不以“无横向溢出”代替视觉可用性。本机 32771 数据库的归属与隔离边界尚未确认，未执行备份或升级；旧 8014 API 已停止，不使用旧 Schema 对外服务。
- `20260810_0021`：每个 Team 只有一个 `TeamSignalSource`，启用模式为 Perptape 或 Webhook。Perptape Key 与 Webhook Secret 使用绑定 Team、SignalSource、用途和轮换版本的 AES-256-GCM 信封；API/页面只投影配置状态和尾号。Perptape 继续复用既有实时机会、SYSTEM 提案和独立审核链，并优先使用团队专属 service principal；旧团队只保留明确标记的 `RUNTIME_FALLBACK` 兼容状态，新团队没有信号源时失败关闭。
- `0021` Webhook 契约：TradingView 与自研模型统一写入版本化 `SignalEvent`；原始请求使用 `timestamp.nonce.raw_body` 的 HMAC-SHA256 v1 签名，并同时校验 64 KiB 上限、JSON 格式、30–900 秒团队时效、30 秒未来偏差、nonce、Team 幂等键及 `provider + external_id`。接收成功只冻结信号并返回 `proposal_created=false`。只有精确获权 HUMAN 能把未消费事件转换为一个 MANUAL、`PENDING_REVIEW` 提案；SignalEvent 消费、Proposal 创建、冻结提交、幂等回执和审计位于同一数据库事务。服务端重新校验 Team、账户/场所权限、Instrument、symbol、direction 与当前 Webhook 模式，数据库唯一约束阻止第二个提案。后续审核、风控、授权和订单链没有旁路。
- `0021` 五维验收：空隔离库完成全量升级至 `20260810_0021 (head)` 且 `alembic check` 无漂移，`test_schema` 覆盖旧 Team 的显式兼容回填及 `0021 → 0020 → 0021` 往返。Node 语法、变更源码 Ruff/mypy 和 `git diff --check` 通过；单元/API 共 372 个、完整集成 223 个通过。关闭 runtime sync、Perptape WebSocket、Freqtrade worker、真实/测试网下单、资金发送和签名的本地 API 在 8015 通过 `/health/live` 与 `/health/ready`；真实 Webhook 分别得到首次 `202`、相同语义幂等重放 `200`、错误签名 `401`。真实密码登录后的 `/signals` 页面显示 Team Webhook v2、密文状态、统一签名合同和一条已验证信号；页面手动创建后数据库为 `MANUAL / SHADOW / PENDING_REVIEW`，浏览器错误/警告为 0，页面密钥输入为空，审计明文匹配为 0，五个危险 Gate 全部 `DISABLED`。本机 32771 数据库继续未触碰；当前多 Team 持久 Perptape WebSocket worker 仍读取部署级连接，团队 Key 已用于页面按需实时读取但尚未绑定独立连续 worker，因此不把“团队 Key 已保存”表述为多团队持续连接已完成。阶段 9 的 1440/1024/430/390、深浅主题与可访问性全站验收继续后置。
- `20260810_0022`：复用 `ExchangeAccount` 的连接/交易双状态，不新增账户或权限真源；只增加 `last_connection_check_at` 以区分最近尝试与最近成功。获权管理员通过幂等、预期账户版本保护的 Team API 启动探针；服务端先完成账户范围授权和 AES-GCM 解密，关闭事务后调用固定官方主机，再锁定同一账户并复核账户/凭据版本后写回。凭据轮换与探针并发时旧结果返回 `VERSION_CONFLICT`，不会覆盖新凭据状态。成功写 `VERIFIED`，失败只保存稳定错误代码和检查时间；两者都不改变交易资格。账户凭据的幂等语义由加密主密钥派生的 HMAC 指纹保护，不再保存可离线枚举的无密钥凭据派生值。
- `0022` 场所边界：Binance 只调用 USD-M balance 或 Portfolio Margin account 读取；Hyperliquid 只用公开账户身份调用 `clearinghouseState`；OKX 只调用 V5 `account/balance`；Bybit 只调用 V5 Unified Account `wallet-balance`。四类客户端固定官方 HTTPS 主机且没有下单方法。当前 `VERIFIED` 只是一时连接事实；持续余额/仓位/委托/成交/资金费同步仍只有 Binance 与 Hyperliquid，且 worker 尚未按 Team / ExchangeAccount 消费数据库信封。OKX/Bybit 写执行 Adapter 继续未实现。
- `0022` 五维验收：专用可抛弃 PostgreSQL 测试库完成 `0022 → 0021 → 0022` 往返，`alembic check` 无漂移；变更源码 Ruff/mypy、Node 语法和 `git diff --check` 通过，单元/API 共 `381` 个、完整集成 `225` 个通过。关闭 runtime sync、Perptape WebSocket、Freqtrade worker、真实/测试网下单、资金和签名的本地 API 在 8016 通过 `/health/live` 与 `/health/ready`；真实密码登录后 `/venues` 显示 4 个团队账户、4 个只读验证入口、3 个已验证和 1 个 `OKX_AUTHENTICATION_FAILED` 失败事实，4 个账户的交易能力均为关闭。当前页在 1440 与 390 响应式视口无横向溢出，验证按钮未越界，页面未包含明文 fixture 凭据，浏览器错误/警告为 0；五个危险 Gate 全部 `DISABLED`。截图保存在 `/private/tmp/trading-0022-venues-desktop.png` 与 `/private/tmp/trading-0022-venues-mobile-390.png`。本次仅使用 5434 专用测试库，32771 数据库继续未触碰；阶段 9 的全站多断点、深浅主题与可访问性验收仍按依赖顺序后置。
- `0023` 报表实现：复用 `Campaign`、`Proposal`、`SignalEvent`、`RiskDecision`、`RiskPolicy` 与既有 `results.view`，没有增加报表快照、账本或 Schema。查询先绑定当前 Workspace / Team、精确 `SHADOW|TESTNET|LIVE` 和获授权账户，再按场所、账户、策略/版本、信号模式/提供方和时间筛选。Webhook 只从冻结 SignalEvent 归因；Perptape 与人工/其他系统来源保持不同类别，不读取可变团队配置改写历史。已平仓净收益、未平仓当前值、绝对最大回撤、胜率、盈亏比和利润因子按结算币种分开；完整零值保留为可用，分母或期初资本事实缺失则明确不可用。风险决策独立于 Campaign 查询，因此被服务端拒绝且未进入执行的提案仍进入团队、账户、策略和信号源风险事件统计。
- `0023` 五维验收：该阶段复用既有表，Schema 保持 `20260810_0022 (head)`，专用可抛弃 5434 fixture 完成空库升 head，最终 `alembic current` 与 `alembic check` 均通过且无新增迁移。Ruff、mypy、Node 语法和 `git diff --check` 通过；单元/API `383` 个、完整集成 `225` 个通过。关闭 runtime sync、Perptape WebSocket、Freqtrade worker、真实/测试网下单、资金和签名的本地 API 在 8017 通过 `/health/live` 与 `/health/ready`；真实密码登录后的 TESTNET `/results` 与 API 一致展示 2 个已平仓 Campaign、3 条风险决策（含 1 条无 Campaign 的拒绝）、USDT 已平仓净收益 `120`、胜率 `50%`、盈亏比 `3`、绝对最大回撤 `60`，SHADOW/LIVE 均为空且未混算。1440、1024、430、390 视口无横向溢出，移动表格转为可读卡片，筛选控件均有文本标签，页面未包含 fixture 密码，浏览器错误/警告为 0；五个危险 Gate 全部 `DISABLED`。截图保存在 `/private/tmp/trading-0023-results-desktop-1440.png`、`/private/tmp/trading-0023-results-tablet-1024.png`、`/private/tmp/trading-0023-results-mobile-430.png` 和 `/private/tmp/trading-0023-results-mobile-390.png`。本次仅使用 5434 专用测试库，32771 数据库继续未触碰；阶段 9 的全站深浅主题、完整键盘和 WCAG AA 验收仍按依赖顺序后置。
- `20260810_0023` 通知实现：只新增具有独立安全生命周期的 `NotificationRoute` 与 `NotificationDelivery`。路由是 Team 级渠道、事件选择、启用状态、版本和 AES-256-GCM 密文真源；delivery 冻结模板、业务对象版本、Team/Account/Venue/Environment、语义哈希、correlation ID、幂等键和 route version。提案提交、风险决定、Campaign 状态、签名 Webhook 信号和账户连接失败都在源事务中写入 delivery；Webhook 继续只写 SignalEvent，不自动建提案。路由轮换取消旧任务，明确 429 最多有界重试五次，网络或 worker 中断造成的不确定结果进入 `OUTCOME_UNKNOWN` 且不盲重发。
- `0024` 渠道与权限边界：Telegram、Slack Incoming Webhook、飞书/Lark 自定义机器人和 TLS 邮件复用一套事件模板、Team 路由、投递状态与审计；Slack/Lark 只接受官方 HTTPS 主机，Lark 签名遵循时间戳 HMAC 合同，邮件只接受公开 DNS 主机与 465/587。API/页面只投影目的地提示和凭据版本，不返回密文或明文。`trading-notification-worker` 默认关闭，只拥有路由解密和文本发送能力，不导入交易、资金、签名或广播适配器。旧资金根尚未完成 Team 迁移，`CAPITAL_STATUS_CHANGED` 明确为 `SCOPE_MIGRATION_REQUIRED` 且不可选，不猜测团队归属。
- `0024` 五维验收：专用可抛弃 5434 测试库完成 `20260810_0023 → 20260810_0022 → 20260810_0023` 往返，最终 `alembic current`、`alembic check` 通过，Schema 为 42 张业务/运行表。变更源码 Ruff/mypy、Node 语法和 `git diff --check` 通过；单元/API `410` 个、完整集成 `229` 个通过，通知模块定向 28 个测试达到配置的 `85%` 分支覆盖门，实测 `86.13%`。全仓覆盖率探针为 `81.56%`，低于既有 `85%` 门，缺口主要来自此前未覆盖的通用 API、场所和资金模块；本阶段不降低阈值，并把它保留为仓库级质量债。关闭 runtime sync、notification continuous mode、Perptape WebSocket、Freqtrade worker、真实/测试网下单、资金和签名的本地 API 在 8018 通过 `/health/live`、`/health/ready` 与真实密码登录；`/api/notifications` 和 `/notifications` 一致显示 4 条脱敏路由及 `SENT / RETRY_WAIT / OUTCOME_UNKNOWN / DEAD_LETTER` 四类 fixture 终态，资金通知缺口如实显示。`uv run --offline trading-notification-worker --once` 在空队列返回 `selected=0`。1440 深色、1024 浅色、430 深色和 390 浅色视口均无横向溢出，四条路由/四条投递可读，Tab 后焦点可见，主题切换重载后保持，页面未包含 fixture 密码、Token、Webhook 或 SMTP 密钥，认证后浏览器错误/警告为 0；五个危险 Gate 全部 `DISABLED`。截图保存在 `/private/tmp/trading-0024-notifications-desktop-1440-dark.png`、`/private/tmp/trading-0024-notifications-tablet-1024-light.png`、`/private/tmp/trading-0024-notifications-mobile-430-dark.png` 和 `/private/tmp/trading-0024-notifications-mobile-390-light.png`。没有使用真实外部渠道凭据，因此本次不把适配器合同测试声称为生产送达；32771 数据库继续未触碰，阶段 9 全站可访问性验收仍后置。
- `20260810_0024` 影子模式实现：只为 Team 增加 `SETUP / SHADOW / LIVE` 执行模式列；旧可交易 Team 明确回填为 LIVE，新 Team 保持 SETUP。SETUP 进入 SHADOW 前必须存在已启用信号源、配置完整的活动风险政策、活动交易账户、同一账户/场所范围内不同的提案人与审核人，以及交易运维权限；转换使用预期版本、幂等回执和统一审计，LIVE Team 不经此入口降级。虚拟资金、仓位、Proposal、Approval、RiskDecision、Authorization、Reservation、Campaign、Order/Fill、Protection、PnL、报表和审计全部复用既有 Team/Account/Venue/Environment 真源，没有新增表或第二套账本。
- `0025` Shadow 执行与边界：管理员显式初始化一次 `CONTROLLED` SHADOW AccountEquity 及按标的空仓；已初始化资金不能重置，处于 Team SHADOW 模式时旧通用事实入口也不能改写 Equity/Position。确定性模拟器按不利方向滑点、Instrument tick、合约乘数和手续费原子生成 SHADOW Order/Fill，更新仓位、保护、风险预留、Campaign PnL、虚拟净值和审计；同一幂等键重放返回相同结果。Team SHADOW 模式从服务端拒绝 LIVE/TESTNET Proposal，模拟器不加载场所适配器。`/api/shadow` 与 `/shadow` 只返回脱敏账户状态，并固定投影 `VIRTUAL_ONLY`、真实下单/资金/签名/广播为 false、场所连接器未调用；现有 `/results?environment=SHADOW` 继续按环境、团队和账户独立统计。
- `0025` 五维验收：专用可抛弃 5434 测试库完成 `20260810_0024 → 20260810_0023 → 20260810_0024` 往返，最终 `alembic current`、`alembic check` 通过；Schema 仍为 42 张业务/运行表。变更源码 Ruff/mypy、Node 语法和 `git diff --check` 通过；完整单元 `399` 个、API `22` 个、完整集成 `233` 个通过，新纯计算 Shadow 模块分支覆盖 `94.29%`，达到既有 `85%` 门；全仓覆盖率最近一次探针仍为 `81.56%`，本阶段未降低门槛。关闭 runtime sync、notification worker、Perptape WebSocket、Freqtrade worker、真实/测试网下单、资金、签名和广播的本地 API 在 8019 通过 `/health/live`、`/health/ready` 与真实密码登录；真实 HTTP 投影为 SHADOW、1 个虚拟账户/仓位/任务、虚拟净值 `9999.859960000000000000`，数据库 Order、Fill、Position、Equity 和 Campaign 各 1 条且全部仅为 SHADOW，五个危险 Gate 全部 `DISABLED`，模拟审计 1 条且 fixture 密码审计匹配为 0。1440 深色、1024 浅色、430 深色和 390 浅色视口均无页面横向溢出，移动任务表转为完整卡片；主题切换重载后保持、Tab 焦点为 3px 可见轮廓，认证后浏览器错误/警告/失败响应为 0。截图保存在 `/private/tmp/trading-0025-shadow-desktop-1440-dark.png`、`/private/tmp/trading-0025-shadow-tablet-1024-light.png`、`/private/tmp/trading-0025-shadow-mobile-430-dark.png` 和 `/private/tmp/trading-0025-shadow-mobile-390-light.png`。本次没有配置真实场所凭据或调用外部连接器，32771 数据库继续未触碰；阶段 9 全站 WCAG AA 与一致性验收仍按依赖顺序后置。
- `20260810_0025` Agent 身份与凭据：没有新增 Agent、权限、审核或审计表；复用 `User(SERVICE)`、Workspace/TeamMembership、RoleAssignment、Proposal、Approval、CommandReceipt 与 AuditEvent。User 只增加 `INTERNAL / AGENT` 服务种类和 Agent Token keyed digest、摘要、版本、创建/到期/最近使用时间；迁移将既有 SERVICE 明确回填为 INTERNAL。Token 格式为 `tradingops_agent_v1.<user UUID>.<opaque secret>`，数据库仅保存由凭据主密钥派生的 HMAC digest。创建和轮换只在首次响应返回明文，幂等回放不重放秘密；Cookie 与 Bearer 同时出现直接拒绝，轮换、到期、停用或成员失效都使认证失败。
- `0026` Agent 权限与交易链：每个 Agent 固定一个 Team 和一个现有活动 Account/Venue，只能选择 OBSERVER、PROPOSER、REVIEWER，不提供 OPERATOR、TREASURY_ADMIN、SYSTEM_ADMIN 或通配 scope。Agent 提案入口校验五分钟时效、30 秒未来偏差、模型/版本/request ID、账户 RBAC 和 Instrument，复用 SYSTEM Proposal 并在源事务冻结为 PENDING_REVIEW；模型归因随冻结 payload 保存。Agent 审核复用同一 Approval、创建者自审拒绝、对象版本、幂等回执和审计；Agent 不需要也不能签发 HUMAN step-up。通用 Perptape 提案入口对 Agent 拒绝，避免改用内部信号 principal 丢失归因。风险决定、账户凭据、订单、资金、签名与广播继续由服务端角色拒绝；`/admin/agents` 页面只显示一次新 Token，刷新后列表只有摘要与生命周期事实。
- `0026` 五维验收：专用可抛弃 5434 测试库完成 `20260810_0025 → 20260810_0024 → 20260810_0025`往返，最终 `alembic current`、`alembic check` 通过，Schema 为 42 张业务/运行表。变更源码 Ruff、mypy、Node 语法和 `git diff --check` 通过；完整单元 `407` 个、API `22` 个、完整集成 `235` 个通过，Agent 模块分支覆盖 `94.87%`，达到既有 `85%` 门。关闭 runtime sync、notification worker、Perptape WebSocket、Freqtrade worker、真实/测试网下单、资金、签名和广播的本地 API 在 8020 通过 `/health/live`、`/health/ready`、真实密码登录与 Bearer 认证；实际 HTTP 完成 Agent SYSTEM 冻结提案与独立 Agent 审核，数据库中 2 个 Agent、2 个已批准 SYSTEM 提案、2 条独立审批，Token 明文在用户表、回执与审计中的匹配数为 0，五个危险 Gate 全部 `DISABLED`。`/admin/agents` 在 1440 深色、1024 浅色、430 深色和 390 浅色视口均无水平溢出，键盘焦点可见，页面只显示 Token 摘要且未出现 fixture 密码/完整 Token，认证后浏览器错误/警告/失败响应为 0。截图保存在 `/private/tmp/trading-0026-agents-desktop-1440-dark.png`、`/private/tmp/trading-0026-agents-tablet-1024-light.png`、`/private/tmp/trading-0026-agents-mobile-430-dark.png` 和 `/private/tmp/trading-0026-agents-mobile-390-light.png`。本次未配置真实场所凭据或触发真实订单/资金/签名/广播，32771 数据库继续未触碰；阶段 8 进入开源与运维交付。
- `0027` 开源与运维基线：新增固定 Python 3.12/uv 版本的非 root Dockerfile，Compose 把 PostgreSQL、一次性迁移、幂等安全初始化和 API 依赖排序；API/migrate/setup 使用只读根文件系统、`cap_drop=ALL`、`no-new-privileges`和本地端口绑定。`run_compose.sh` 在未跟踪的 `.local/` 生成 `0600` Session/凭据加密/管理员密码；`run_local.sh` 也生成稳定本地密钥，并硬绑 `127.0.0.1:5434/trading_local`，不使用共享 env 中的数据库 URL。新增 `trading-doctor` 只读输出配置、Schema、Gate、运输开关和可机读连接能力矩阵，不输出任何秘密/指纹；`.env.example` 由测试保证覆盖全部 119 个 Settings 字段。新增部署/升级/备份/恢复文档、`SECURITY.md` 和 `CONTRIBUTING.md`；开源许可证类型属于不可推断的产品/法务决策，未经选定时不伪造 `LICENSE`。
- `0027` 五维验收：Schema 未新增表，Compose 实际将本地库从 `20260810_0023` 升级到 `20260810_0025`，容器内 doctor 核对 revision 一致、五个 Gate 全部 `DISABLED`、六个危险进程运输开关全部为 false。Docker 镜像实际构建成功，运行用户为 `uid=999(tradingops)`，API 容器为只读根、移除全部 capabilities，仅映射 `127.0.0.1:8021`；`/health/live`、`/health/ready`、真实密码登录和 `/admin/agents` 实际页面通过。1440 深色浏览器无水平溢出、焦点可见、未回显密码，错误/警告/失败响应为 0，截图为 `/private/tmp/trading-0027-compose-agents-1440-dark.png`。完整单元 `414` 个、API `22` 个、完整集成 `236` 个通过，运维诊断模块 8 个定向测试达到 `100%` 分支覆盖。custom-format 备份实际恢复至一次性 `*_test` 库，验证 `20260810_0025`、43 张含 Alembic 表的数据表和 5 个关闭 Gate，随后已清理。本次没有配置真实场所凭据或触发真实订单/资金/签名/广播，32771 数据库继续未触碰；运行验收后 API 容器已停止，原本 5434 PostgreSQL 与 dry-run workers 已恢复原状。
- `0028` 源码与商业许可：按 `DEC-GOV-005` 提交根 `LICENSE` / `NOTICE` 和三份许可文件。PolyForm Noncommercial 1.0.0 使用官方 `1.0.0` 原文，SHA-256 为 `c0ea4a896d2c8c394b29f9427589996db826cd501c512279ff0ed3ef48fabbe5`；社区许可必须与 TradingOPS Community Team Exception 1.0 组合使用，不能单独解释为标准 PolyForm 授权。附加条款允许个人及最多 3 名自然人团队只用成员自有资金交易并保留利润，同时完整替换 PolyForm 对非商业组织的默认许可，因此公司、基金、工作室、非营利机构、政府机构和其他组织均须另签商业许可。组织内部使用、SaaS、托管/代运维、白标/OEM 和转售是独立商业范围，授权一项不推定其他项；仓库商业协议模板在双方完成主体、费用、范围、管辖、责任和签字前不授予商业权利。项目对外统一称“源码可用”，不称 OSI 开源；贡献规则与 PR 模板记录社区和商业双重分发授权。
- `0028` 五维验收：本阶段不改变数据库、迁移、后端 API、实际控制台页面或危险能力，五维中的 Schema/API/页面/交易运行行为均保持既有真源与默认关闭，发布产物和自动化测试为本阶段运行面。`uv lock --check --offline`、Ruff 与 `git diff --check` 通过；完整单元加 API 为 `445 passed`。定向许可测试验证官方文本哈希、组合条款、商业模板非自执行、贡献授权和 PEP 639 元数据。实际构建的 sdist 与 wheel 均包含 `LICENSE`、`NOTICE` 和三份许可文件，wheel 为 Core Metadata 2.4，`License-Expression` 为 `LicenseRef-TradingOPS-Community-1.0 OR LicenseRef-TradingOPS-Commercial-1.0`。完整测试同时发现已提交错误页引用未定义的 `--shadow-soft`，已改为既有 `--shadow-quiet` 并由设计系统测试覆盖。首次公开发布或首份商业签约仍需补齐适用版权方/签约主体、价格、管辖、责任和支持范围并完成适用法域专业复核；这些商业合同事实不会在仓库中猜测。
- Proposal / Campaign 查询、实际结果和审计时间线按当前团队过滤；跨团队详情与变更返回 `TEAM_SCOPE_DENIED`。审核、授权、Intent 等强生命周期子对象继续经 Proposal / Campaign 根派生团队；可独立到达且可能没有 Campaign 的场所事实直接持久化 Team，避免相同账户字符串跨团队混算。
- `AuditEvent.account_id` 对交易链事件由服务端对象真源推导；组织级事件保留为空，不伪造账户。
- 创建者自审旁路已从服务端和页面移除；团队切换后即使账户字符串相同，也不能读取、提交、审核或执行另一团队的提案。
- 当前阻断：资金提案/授权/转移、sender/task 与运行源配置仍未完成团队根迁移，所以统一资金通知保持不可路由；数据库账户密文已接入四场所一次性连接验证，但尚未接入多 Team / 多账户持续事实 worker；多 Team Perptape 连续 worker 也尚未按 Team 绑定；OKX、Bybit 持续事实与写执行 Adapter 未实现。通知 worker 尚无生产进程监督、真实渠道凭据或生产送达认证。新增风险阈值必须由产品负责人明确配置，迁移不会代填。新团队继续不可增险，“已登记/已加密/一次验证成功/事实已隔离/政策已保存/测试通知已发送”都不是“持续连接正常”或“交易就绪”。
