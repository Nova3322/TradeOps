# Trading 交易系统

> 状态日期：2026-08-10
> 当前状态：Workspace / 团队权限边界、团队交易账户与账户事实隔离、团队 Perptape / 签名 Webhook 单一信号源、加密凭据、四场所一次性只读连接验证、两场所持续事实同步、版本化风控、团队/账户/策略/信号源绩效与风险事件报表、Telegram/Slack/Lark/邮件团队通知、LIVE/模拟资金隔离、分权风险恢复、受控资金路径和最小 Telegram 审核已实现；所有危险能力仍默认关闭

本项目面向一个资本所有者、一个内部组织和多个内部用户。用户可以提交和审核提案、查看仓位、处理异常；系统在风险可控的前提下辅助执行交易并判断是否赚钱。不开放外部注册，不管理第三方资金，不建设机构级多租户、通用合规或通用认证平台。

完整产品愿景包含 Binance、Hyperliquid、Web/PWA、Telegram、VenueAdapter、Freqtrade/OMS、Margin、Vault/CTO 和报表。这些目标不删除，但按可运行的端到端用户流程逐步开发。未实现能力保持关闭，不为未来可能性预建通用实体。

交易执行的默认底座现统一为场所隔离的 Freqtrade worker：Binance futures 与 Hyperliquid futures 各自独立，Hyperliquid 通过显式 `hip3_dexes` allowlist 加载 HIP-3。控制面仍拥有提案、审核、风险、OrderIntent、fencing 和审计；交易所官方 API 只读客户端继续作为账户事实源。仓库内旧直接发送客户端仅供隔离兼容测试，默认运行配置会拒绝这条路径。`docker compose --profile execution-workers` 提供的是本地 dry-run worker，不构成实盘认证。

`ExchangeAccount` 是当前团队内 `account_id + venue` 的持久化真源，允许同一交易所登记多个账户。Binance、Hyperliquid、OKX、Bybit 的凭据通过版本化 AES-256-GCM 信封保存，密文的认证上下文绑定 Team、账户、Venue 和轮换版本；API 与页面只返回脱敏元数据。凭据保存后连接状态仅为 `NOT_VERIFIED`，交易状态仍为 `DISABLED`。获权的账户管理员可运行一次官方只读接口验证：服务端在短事务中校验 Team / Account / Venue 权限、幂等键和账户版本并解密，事务外发起无副作用探针，再以凭据版本复核结果；轮换并发会拒绝旧探针写回。成功只更新连接事实，不导入余额、不绑定持续 worker，也不启用交易、资金、签名或广播。

| 场所 | 团队加密凭据 | 一次性连接验证 | 持续账户事实同步 | 交易执行 |
| --- | --- | --- | --- | --- |
| Binance | 已实现 | 已实现，标准 USD-M / Portfolio Margin 只读探针 | 已实现，但当前 worker 仍使用部署级凭据和单账户映射 | Freqtrade 外部 worker；默认关闭 |
| Hyperliquid | 已实现 | 已实现，仅使用公开账户身份读取 Info API | 已实现，但当前 worker 仍使用部署级身份和单账户映射 | Freqtrade 外部 worker；默认关闭 |
| OKX | 已实现 | 已实现，V5 私有只读余额探针 | 未实现 | 未实现 |
| Bybit | 已实现 | 已实现，V5 Unified Account 只读余额探针 | 未实现 | 未实现 |

“一次性连接验证成功”只证明该时刻的只读身份可用；它不等于持续事实新鲜、账户归属人工复核完成或交易就绪。运行读取/执行进程目前仍从受保护的部署环境加载凭据，尚未按多 Team / 多账户消费数据库信封。

`VenueOrder`、`VenueFill`、`Position`、`AccountEquity`、权益历史、`FundingPayment` 与计算型对账均持久化非空 Team 根；同一 `account_id + venue` 可在不同团队独立存在，服务端写入、查询、资金事实聚合和对账按当前团队过滤。旧事实只在迁移时回填到既有默认团队，不据此开启连接或交易。资金提案/授权/转移及 sender/task 根仍待后续迁移，当前不得把账户事实隔离等同于整条资金链已完成团队化。

`TeamSignalSource` 是当前团队的唯一信号模式真源。Perptape Key 和 Webhook HMAC 密钥复用同一 AES-256-GCM 信封，认证上下文绑定 Team、Signal Source、模式与轮换版本。Webhook 统一接收 TradingView 和自研模型，服务端验证 HMAC-SHA256、请求与事件时效、nonce 重放、外部身份、幂等键和版本化格式。通过的 Webhook 只写入 `SignalEvent`；必须由获权人员手动创建并冻结 Proposal，一个 SignalEvent 最多关联一个 Proposal。

`/results` 由服务端 `results.view` 权限、当前 Workspace / Team、获授权账户和精确环境共同限定。页面按结算币种分别展示团队、账户、策略版本和信号源的已平仓净收益、未平仓当前值、绝对最大回撤、胜率、盈亏比及风险事件；没有形成 Campaign 的拒绝决策也保留。Webhook 归因只读取冻结 `SignalEvent`，不读取可变信号源配置。缺少覆盖完整范围的可信期初资本和 FX 真源时，百分比收益、百分比回撤与跨币种合计保持不可用，不用零值或静态换算代替事实。

`/notifications` 由服务端 `notification.view` / `notification.manage` 和当前 Team 限定。Telegram、Slack、Lark 与邮件路由的凭据使用与交易账户一致的版本化 AES-256-GCM 信封，页面只显示目的地提示，不回传密文或明文。业务事务冻结模板、对象版本、作用域、语义哈希和幂等事件身份，再为匹配路由写入耐久 delivery；独立 `trading-notification-worker` 只拥有解密和通知发送能力。明确限速会有界退避，网络中断等不确定结果进入 `OUTCOME_UNKNOWN` 且不盲重发。通知路由没有交易、资金、签名或广播方法。旧资金对象尚无可靠 Team 根，因此统一资金通知明确标记 `SCOPE_MIGRATION_REQUIRED`，不会猜测映射。

## 从这里开始

1. [当前实现基线](docs/08-implementation/当前实现基线.md)：当前代码、Schema、入口和明确缺口。
2. [核心业务不变量](docs/08-implementation/核心业务不变量.md)：当前必须保持的风险与执行语义。
3. [后续端到端开发路线](docs/08-implementation/后续端到端开发路线.md)：按用户流程推进的开发顺序。
4. [本次架构收敛记录](docs/08-implementation/本次架构收敛记录.md)：KEEP/MERGE/SIMPLIFY/DELETE 和迁移结论。
5. [交易系统总体方案](交易系统总体方案.md)：长期产品愿景和最高层原则。
6. [产品化文档中心](docs/README.md)：专项文档地图与权威边界。

## 当前不可绕过的规则

- 信号和人工交易假设只能生成冻结 Proposal，不能直接生成审核、授权或订单；Webhook 信号还必须由人员手动创建 Proposal。
- SYSTEM 与 MANUAL 初仓都必须经过人工审核；Risk Engine 始终可以拒绝或缩量。
- 创建者不能自审；高风险提案需要两个不同 Reviewer。
- Approval 只产生短期、有限范围的 TradingAuthorization，不产生永久权限。
- 数据陈旧、仓位未知、保护未知或订单结果 Unknown 时禁止新增风险。
- Perptape 候选身份包含源场所 raw symbol；同一 canonical symbol 的不同报价合约不得合并。旧候选 ID 只有唯一匹配当前候选，且既有 Proposal 的 instrument/venue/direction 与冻结候选身份快照全部精确一致时才兼容；歧义或不一致时拒绝。
- Reservation、OrderIntent 和幂等回执必须原子提交；Unknown 不能提前释放或自动重发。
- 多个退出候选合并为唯一更小目标仓位；有活动 OrderIntent 时不重复生成减仓意图。
- 场所真实订单、成交、仓位、保护、余额和资金费必须与内部预期分开并对账；SHADOW、TESTNET、LIVE 使用独立事实作用域。
- 每个 execution scope 只有一个有效 sender；新 owner 接管后旧 fencing token 无效。
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD`、`AUTO_PROFIT_SWEEP` 与 `AUTO_OPERATING_REFILL` 默认 `DISABLED`。
- AUTO_ADD 从 `DISABLED` 变为 `ENABLED` 只能通过 `RiskControlChangeRequest` 受复核恢复流程；不能由管理员直接翻转 Gate。每个 Add 仍需冻结 Proposal、分档 AddUnit、后续 Perptape 候选、盈利仓位、足额保护、新鲜事实、剩余授权和最终 Risk Engine 同时通过。只有首个正成交消费 AddUnit，零成交取消/拒绝不消费，Unknown 冻结后续新增风险。
- 既有资金 Proposal/双人复核/Transfer Authorization 生命周期继续与交易授权分离。面向资金管理员的四条直接路径不再强制经过该旧界面，但仍要求显式最终确认、可信配置、地址/网络/资产/金额/限额/新鲜状态重验、完整审计和阶段回执；生产参数或 Adapter 缺失时 fail closed。活动仓位、未解决订单或 Unknown 禁止 Vault 救仓，Unknown 不释放或重发。
- 自动利润归集和自动运营补充使用两个独立 Gate；当前只根据空仓、无订单、无 Unknown、机器 MATCH、已确认余额和已关闭 Campaign 净 PnL 生成待双人复核的非生产候选，不自动提交资金。浮盈不能归集，净亏损不能触发运营补充。
- 风险恢复分两条受控路径：最高管理员可在全部实时条件满足时直接恢复；操作人员只能创建冻结申请，并由具备独立审核职责的他人审核后执行。两条路径都重验事实、版本和 scope、写入审计、保持 AUTO_ADD 关闭，并使旧 TradingAuthorization 与旧 AddUnit 永久失效。生产未配置 LIVE scope 时 fail closed，`KILL_SWITCH` 不进入常规恢复流程。
- Telegram 已提供默认关闭的真实 Bot API 私聊长轮询、中文 HTML 审核卡、`/start`/`/help`/`/status`/`/todo`、内部用户绑定，以及冻结提案的两步批准/拒绝。它只承担提醒、待办和独立审核；Campaign、资金、风险 Gate、成员权限、签名与广播入口在 review-only 模式全部抑制。创建者自审、对象版本、有效期、身份绑定和幂等仍由服务端重验。正式 IdP/Passkey 仍未接入：本服务的 `SignedTokenService` 仅以本地 HMAC 验证 action grant；只有 local/test 提供 Mock grant 发行，生产 issuer、IdP/WebAuthn 和外部签名验证仍未实现。Binance Unified Account 与 Hyperliquid Core 的危险写能力保持独立关闭。

## 当前代码入口

- API 进程：`uv run trading-api`
- 只读同步进程：`uv run trading-sync-worker`；`--once` 用于一次性生产边界验收。它只读取 Perptape、Binance、Hyperliquid 和已配置 NoTilt Vault，持久化事实并运行对账，不拥有订单发送、资金签名或广播方法
- 通知进程：`uv run trading-notification-worker --once` 可执行一次 delivery 验收；持续模式还要求 `TRADING_NOTIFICATION_WORKER_ENABLED=true`。该进程默认关闭，只消费团队通知 delivery，不导入订单、资金、签名或广播适配器
- Web/PWA：`/` 是唯一行动总览；`/signals` 选择团队 Perptape / Webhook 模式并展示签名事件，核心主线为信号或机会 → 冻结提案 → 独立审核 → 交易任务。`/shadow` 提供显式团队启用、虚拟资金初始化、模拟仓位、确定性成交和影子任务入口，并把真实下单、资金、签名、广播及场所连接器显示为关闭。运行告警详情位于 `/campaigns/alerts`，旧 `/exceptions` 只做兼容跳转。另有 `/risk`、`/positions`、`/venues`、`/capital`、`/admin/users`、`/results` 和 `/notifications`。资金中心默认只展示 LIVE；SHADOW/TESTNET 不计入真实净值。Vault 缺少事实时显示 `— · MISSING`，总净值也保持不完整，绝不把缺失投影为零
- HTTP：健康检查、内部会话、团队信号源配置、签名 Webhook / SignalEvent、Perptape 主站机会、Proposal/Review/Risk/Authorization、SHADOW/TESTNET/LIVE Campaign、AUTO_ADD/减仓/退出、资金事实/提案/授权、NoTilt 未签名计划/回执确认、按环境结果/审计/运行状态，以及 Binance、Hyperliquid Core 的只读、TESTNET 与受控 LIVE API
- 内部业务：`trading_control_plane.service.TradingService`
- 纯计算：`evaluate_risk`、`select_target_position`、`compute_pnl`
- 数据库：PostgreSQL，Alembic head `20260810_0024`；Workspace、权限、信号源/SignalEvent、交易账户、提案—执行聚合、账户事实、版本化风险政策与风险预留均使用团队范围。Team 以 `SETUP / SHADOW / LIVE` 明确执行模式；Shadow 复用现有 AccountEquity、Position、Proposal、Campaign、Order/Fill、RiskReservation、报表与审计，只增加团队模式列而不建立第二套模拟账本。最大总风险、单账户风险、最大单笔亏损、最大连续亏损和冷却期由同一政策版本驱动；遗留政策的新阈值保持未配置并拒绝新增风险。绩效报表复用既有 Campaign、Proposal、SignalEvent 与 RiskDecision，没有新增快照或第二套账本。通知仅新增团队路由与耐久 delivery 两个具有独立生命周期的实体；资金聚合和 sender/task 根仍按后续迁移 fail closed
- 场所边界：Binance 与 Hyperliquid 的交易发送默认只允许进入各自隔离的 Freqtrade worker；Hyperliquid worker 通过显式 `hip3_dexes` allowlist 加载 HIP-3。仓库原有 `binance_execution.py` / `hyperliquid_execution.py` 只保留隔离兼容测试，默认后端不会加载其签名密钥，也会拒绝直接发送。交易所官方只读接口继续提供账户、仓位和目录事实；数据库中的 `LIVE_ORDER_SEND` 初始仍为 `DISABLED`
- 资金边界：`capital.py` 提供 SHADOW/TESTNET Mock 提交和自动候选计算；`notilt.py` 通过官方 `@notilt/sdk` 固定支持 Ethereum、BNB Smart Chain、Arbitrum One，只读取官方部署/Registry/Vault、生成并持久化 `{chainId,to,data,value}` 未签名交易，并从可信生产 RPC 校验发送者、目标、函数、参数、事件、区块时间和逐链确认深度。服务没有 NoTilt 私钥字段，不签名、不广播，也不暴露 owner、白名单管理、Panic 或 Full Exit 能力；真实 `CAPITAL_TRANSFER` 与两个自动资金 Gate 均保持 `DISABLED`

正式身份源按冻结决策使用托管 IdP 与 Passkey，但外部 IdP 尚未接入。本地/测试环境可显式启用仅识别已存在内部用户的 Mock 会话和 Mock step-up；生产环境硬拒绝启用 Mock 身份。团队 Perptape Key 在 `/signals` 加密配置后可直接驱动现有机会页；迁移的旧团队保留明确标识的 `RUNTIME_FALLBACK`，不伪装成团队密钥已配置。当前常驻 WebSocket worker 仍只消费部署级 Perptape Key 和共享 feed；多团队常驻 worker 绑定尚未完成，不把按需页面读取声称为团队常驻同步。

Binance 私有事实读取必须同时显式配置 `TRADING_BINANCE_READ_ONLY_ENABLED=true`、API Key/Secret 和 `TRADING_BINANCE_FACT_ENVIRONMENT=TESTNET|LIVE`。Unified Account 使用 `TRADING_BINANCE_ACCOUNT_MODE=PORTFOLIO_MARGIN` 和官方 `https://papi.binance.com`。未配置时页面只显示 PostgreSQL 已保存事实，不尝试联网。

Binance TESTNET 订单还必须单独配置 `TRADING_BINANCE_TESTNET_ORDER_SEND_ENABLED=true` 和独立 TESTNET Key/Secret。LIVE 必须同时显式设置进程开关 `TRADING_BINANCE_LIVE_ORDER_SEND_ENABLED=true` 和数据库 Gate `LIVE_ORDER_SEND=ENABLED`；客户端只接受官方 PAPI 主机，使用不超过 32 字符的稳定 client order identity 先查询再发送。Unknown 只允许查询恢复，不盲重发。2026-07-31 的最小主网实证验证了默认 Gate 拒绝、真实开仓、幂等查询、fencing、reduce-only 保护、退出、保护取消、对账和最终空仓；实证结束后 Gate 已关闭。

Hyperliquid Core 默认使用 `TRADING_HYPERLIQUID_ACCOUNT_ADDRESS` 指定的主账户；若只配置 API Wallet，系统通过官方 `userRole` 解析所属主账户。只有显式设置 `TRADING_HYPERLIQUID_SUBACCOUNT_ADDRESS` 时，事实与动作才切换到子账户并在 Exchange 请求携带 `vaultAddress`。只读同步必须开启 `TRADING_HYPERLIQUID_READ_ONLY_ENABLED=true`；LIVE 还必须同时设置 `TRADING_HYPERLIQUID_LIVE_ORDER_SEND_ENABLED=true`、本地 API Wallet 私钥和数据库 `LIVE_ORDER_SEND` Gate。私钥只从运行环境读取且不写入仓库或日志。2026-07-31 的最小主网实证验证了主账户解析、显式价格 IOC、稳定 cloid 幂等、fencing、trigger 保护、退出、保护取消、对账、PnL 和最终空仓；实证结束后 Gate 已关闭。

NoTilt 只保存公开 whitelist agent 与逐链 Vault 地址。配置 `TRADING_NOTILT_ENABLED=true` 后，可查询 Registry assignment；只有相应 `TRADING_NOTILT_*_VAULT_ADDRESS` 已配置、官方 Vault 身份匹配、事实和 USD 估值新鲜时才写入 LIVE 资金事实。Vault、Binance 和 Hyperliquid 的已确认 USD 净值合并展示；任一必需来源未知或过期时总净值和新增风险均 fail closed。当前受信 Registry assignment 尚未激活，且缺少可验证的生产 Vault scope，因此资金中心把 Vault 标记为缺失，不能生成可执行计划。四条直接路径只会在可信 Arbitrum/USDC 目录、授权自有地址、白名单、限额、延迟和实时预算全部通过后构建受限未签名请求；Binance→Vault 还需要受限提现 Adapter，Hyperliquid→Vault 保持“合约→授权自有地址→NoTilt deposit”两段路径。服务不读取钱包秘密、不签名、不广播，最终动作只能在独立人控钱包逐笔确认；`CAPITAL_TRANSFER` Gate 仍保持关闭。

Safe Spending Limits 是与 NoTilt Vault 并列的直接资金方案。它固定使用 Safe 官方 Allowance Module 部署目录、Arbitrum One 与原生 USDC：Safe 作为来源时实时读取模块启用状态、delegate、额度、已用额度、余额、重置周期与 nonce，只输出待人控 delegate 钱包确认的精确哈希；Safe 作为去处时只输出从已授权自有地址到目标 Safe 的精确 USDC `transfer` 无签名交易。系统不接受任意链、Token、模块或 calldata，不读取私钥、不创建钱包客户端、不签名、不广播。生产使用前须显式配置可信 HTTPS RPC、Safe Smart Account 和公开 delegate 地址，且资金 Gate 仍独立保持关闭。

只读同步进程默认关闭。启用时必须配置独立 `runtime-sync` SERVICE principal、两个内部账户 ID 和明确的读开关；每个周期独立刷新 Perptape、两个交易账户及已配置 Vault。某个来源失败不会伪造零值，旧事实会按风险政策自然转为陈旧；WebSocket 回补失败时，现有 `perptape_feeds` 共享快照会原地降级而不是继续声称 `READY`。周期只有在本周期 Binance、Hyperliquid、Vault 三类资本来源均明确成功且 LIVE 净值完整时才报告 `ready_for_new_risk=true`；`SKIPPED` 不能被旧快照掩盖。Perptape 仍是机会源，不单独决定资本 readiness。WebSocket 重连采用有上限的指数退避，`SIGINT`/`SIGTERM` 可中断等待；回滚或紧急停止只需关闭 `TRADING_PERPTAPE_WEBSOCKET_ENABLED` 并停止/重启 worker，HTTPS 周期同步和数据库 Schema 不变。2026-07-31 的一次真实 `--once` 验收读取 200 个 Perptape 候选，并同步 Binance Unified Account 与 Hyperliquid 主账户；由于尚无 Vault 地址，报告明确为 `ready_for_new_risk=false`。

## 本地开发

```bash
uv sync
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
TEST_DATABASE_URL='postgresql+psycopg://.../trading_test' uv run pytest --cov=trading_control_plane
TRADING_DATABASE_URL='postgresql+psycopg://.../trading_test' uv run alembic upgrade head
TRADING_RUNTIME_SYNC_ENABLED=true uv run trading-sync-worker --once
TRADING_RUNTIME_SYNC_ENABLED=true uv run trading-sync-worker
TRADING_CREDENTIAL_ENCRYPTION_KEY='...' uv run trading-notification-worker --once
TRADING_NOTIFICATION_WORKER_ENABLED=true TRADING_CREDENTIAL_ENCRYPTION_KEY='...' uv run trading-notification-worker
TRADING_DATABASE_URL='postgresql+psycopg://.../trading_test' ./scripts/backup_postgres.sh /absolute/path/trading.dump
TRADING_DATABASE_URL='postgresql+psycopg://.../trading_restore_test' ./scripts/restore_test_postgres.sh /absolute/path/trading.dump
```

集成测试数据库名必须以 `_test` 结尾。测试夹具会删除并重建其 `public` schema，禁止指向任何真实交易数据库。
恢复脚本同样硬限制到预先创建、可丢弃的 `*_test` 数据库；当前不存在生产恢复自动化，不能把本地演练命令用于真实数据库。

本机敏感值只放在 `.env.local`；可提交变量名模板为 `.env.example`。不得把密钥值写入代码、文档、日志或测试制品。

### 本地真实 Telegram

首次运行使用独立的本地 PostgreSQL：

```bash
./scripts/run_local.sh
```

该命令会启动 `127.0.0.1:5434` 的 PostgreSQL、升级 Schema、幂等创建
`kelly_oooo` 内部超级管理员/Reviewer/Operator/Treasury、一个本地 Proposer 和第二 Reviewer，然后启动
API。Telegram 默认仍关闭；先在 BotFather 撤销任何曾出现在聊天或日志中的旧 Token，把新
Token 仅写入 `.env.local`，再设置：

```dotenv
TRADING_TELEGRAM_ENABLED=true
TRADING_TELEGRAM_ALLOWED_USERNAME=kelly_oooo
TRADING_TELEGRAM_INTERNAL_USERNAME=kelly_oooo
```

启动后用 `@kelly_oooo` 在 Bot 私聊发送 `/start`。首次绑定校验白名单用户名，成功后只认
Telegram 数字私聊 ID，并在每次按钮操作时重新加载 Trading RBAC、对象版本、有效期和幂等
状态。群聊、转发或另一账号点击均拒绝。review-only 模式可在私聊中对冻结提案执行两步
批准/拒绝，并写回统一审计；创建者自审与独立审核限制不变。管理员的“创建并直接批准”仍只在
Web 中提供二次确认和审计。Telegram 本身不等于强认证。`TRADING_PUBLIC_BASE_URL=http://127.0.0.1:8014` 只适合在同一台电脑
打开审核链接；手机访问需要一个能到达本机的受控 HTTPS 地址。

## 文档与参考材料边界

| 路径 | 定位 | 当前实现真源 |
| --- | --- | --- |
| `docs/08-implementation/` | 当前实现、核心不变量、路线与收敛记录 | 是 |
| `交易系统总体方案.md` | 长期产品原则与愿景 | 原则真源，不代表已实现 |
| `策略合同与数值化验收门.md` | 历史研究材料 | 不驱动当前实现；本仓库不建设回测或通用策略平台 |
| `docs/` 其他专项文档 | 产品、领域、执行、质量和运维长期合同 | 按状态和当前基线解释 |
| `DynamicPositionSizing-/` | 历史原型参考 | 否 |
| `low_vol_breakout_bn/` | 历史原型参考 | 否 |
| `交易系统 notion 文档/` | 历史 Notion 资料 | 否 |
| `仓位计算-新.xlsx` | 研究附件 | 否 |

如长期文档与当前代码能力冲突，以“尚未实现、对应 Gate 关闭”处理；不得创建证明平台、绑定层或快照层来填补文档与产品流程之间的空白。

当前产品尚未进入 Codex Security 审计阶段。按用户明确约束，Codex Security 及其所有审计 Skill、插件和模块保持停用；除非用户以后明确重新授权，否则只执行常规代码检查、测试和数据库一致性验证。
