# SLO、可观测性、故障恢复与 Runbook

> 文档状态：Draft，待运营、风险、安全和执行负责人批准
> 版本：0.1
> 日期：2026-08-01
> 适用范围：Trading 生产运行、Binance / Hyperliquid 执行、Web / PWA、Telegram、Freqtrade、CTO 与 Vault 集成
> 决策真源：`docs/00-governance/待确认决策清单.md`；DEC 状态以该文件为准

---

## 1. 运行目标

本系统的运行优先级固定为：

1. 不产生未授权增险。
2. 保持真实仓位的保护、reduce-only 减仓和退出能力。
3. 保持仓位、订单、余额、授权、风险和资金事实可对账。
4. 恢复提案、审核、加仓、资金划转和展示能力。

可用性不能覆盖安全不变量。某项服务不可用时，允许牺牲新开仓、加仓和便利功能，不能以继续交易为由接受未知事实或重复订单。

本文中的 SLO、RTO、复制 / 备份 RPO 和响应时间数字全部是设计候选，分别受 `DEC-OPS-002`、`DEC-OPS-003` 与 `DEC-OPS-011` 约束；在决策真源正式确认并绑定版本前，它们不是已生效承诺，也不是外部 SLA。另一方面，已确认的批准、授权、OMS 意图、幂等身份、风险与资金账本事件不得逻辑丢失；这是固定的数据完整性不变量，不是可放宽的候选 SLO。

## 2. 服务目录与故障边界

| 服务域 | 事实 / 责任 | 故障时最低能力 |
| --- | --- | --- |
| Market / Perptape 接入 | 候选、行情上下文和数据健康 | 停止新候选和 Add；已有仓位使用场所事实与原生保护 |
| Identity / Approval | 身份、标签、MFA、批准和撤权 | 无新批准；已有保护、减仓、退出继续 |
| Trading API / Control Plane | 意图、状态和控制动作 | 依赖原生保护；必要时进入人工交易所接管 |
| Risk Engine | 预算、Heat、档位和状态 | 禁止所有新增风险 |
| OMS / Event Ledger | 唯一意图、订单和生命周期账本 | 不发送新订单；从场所事实恢复并对账 |
| VenueAdapter / Freqtrade | 场所执行与状态归一化 | 冻结受影响执行域，确认保护，禁止未知接管 |
| Margin Controller | 场内保证金动作 | 停止非必要动作；保持更小仓位或退出 |
| CTO / Vault | 资金授权与划转 | 停止新划转，不影响交易所仓位退出 |
| Audit Store | 追加审计与证据 | 按 `DEC-OPS-007` 停止增险，保留预认证减险路径 |
| Web / PWA / Telegram | 展示、审核、通知和受限控制 | 不成为事实源；服务端安全动作继续 |
| Notification worker | Team 通知 delivery、明确限速重试与未知结果隔离 | 停止新投递，不重放未知结果；不影响服务端交易/资金边界 |

Binance、Hyperliquid Core、每个 HIP-3 DEX、账户 / 子账户、margin mode 和 `collateral_pool_id` 分别建立健康状态和告警，不允许一个聚合绿色状态掩盖局部故障。

## 3. SLI 与候选 SLO

### 3.1 关键 SLI

| SLI | 定义 | 安全动作 |
| --- | --- | --- |
| Market Freshness | 最新可用市场事件时间与当前时间差，按数据源 / 标的 / 周期 | 超门停止该范围候选和 Add |
| Private Fact Freshness | 仓位、订单、成交、余额和保护事实新鲜度 | 未知时按 `DEC-OPS-005` 停止新增风险 |
| Risk Decision Integrity | 可验证风险决策数 / 全部决策请求数 | 不可验证即拒绝增险 |
| Intent Uniqueness | 无重复发送意图 / 全部发送意图 | 任一重复增险为 `INC-P0` 事故 |
| Protection Coverage | 获得足额场所保护的真实仓位 / 全部真实仓位 | 超认证窗口停止增险并减仓 / 退出 |
| Reconciliation Lag | 场所事实出现至账本完成确认的时间 | 超门进入 Unknown / Frozen |
| Audit Durability | 已由耐久审计确认的关键事件 / 全部关键事件 | 不满足时按 `DEC-OPS-007` 停止增险 |
| Control Acknowledgement | 用户动作至 Trading 返回可验证终态 / 接受状态的时间 | 超门显示 Unknown，不假定成功 |
| Alert Delivery | 告警生成至至少一个独立渠道确认接收的时间 | 无可靠渠道时按 `DEC-OPS-006` 降级 |
| Recovery Correctness | 恢复后无旧意图重放且账实一致的演练比例 | 未通过不得恢复增险 |

### 3.2 候选目标

以下仅用于容量设计和影子观测，待 `DEC-OPS-002` 确认：

| 旅程 | 候选设计目标 | 备注 |
| --- | --- | --- |
| Risk Engine 决策 | 月度可用性候选 `≥ 99.99%`；正常负载 p99 候选 `≤ 500 ms` | 不可用时 fail closed，因此不是继续交易承诺 |
| OMS 意图耐久写入 | 成功发送前必须 `100%` 获得耐久意图 | 这是顺序不变量，不是月度平均值 |
| 真实成交后的保护覆盖 | p99 候选 `≤ 5 s`，最终以逐场所认证窗口为准 | 未认证前不能作为生产门 |
| 私有事实新鲜度 | 正常连接候选 `≤ 5 s`；超过候选门进入陈旧 / Unknown | 不同场所需独立校准 |
| 实时对账延迟 | p99 候选 `≤ 60 s` | 结果未知期间不得新增相关风险 |
| `INC-P0` 告警首路送达 | 候选 `≤ 30 s` | 备用渠道由 `DEC-OPS-006` 决定 |
| Web / PWA 只读控制台 | 月度可用性候选 `≥ 99.9%` | UI 低于安全链路优先级 |

任何候选目标若与场所实测、成本或风险容忍冲突，应回到决策清单修订，不能为“达标”而降低安全动作。

## 4. 候选 RTO / RPO 与灾备层级

以下仅是 `DEC-OPS-003` 对复制、备份、灾备形态与恢复时间的讨论起点。表中“已确认事件逻辑丢失为零”继承固定不变量，不是候选数字：

| 组件 / 数据 | 数据完整性不变量 / 候选复制 RPO | 候选 RTO | 恢复前限制 |
| --- | --- | --- | --- |
| Approval、Authorization、OMS 意图、风险账本 | 已确认事件逻辑丢失 `= 0`（固定）；复制时延待决策 | `≤ 15 min` | 只保护 / 减仓 / 退出，且先对账 |
| 交易与资金事件账本 | 已确认事件逻辑丢失 `= 0`（固定）；复制时延待决策 | `≤ 30 min` | 不得释放 Heat 或重放请求 |
| 审计证据 | 关键事件逻辑丢失 `= 0`（固定）；异地副本候选 `≤ 1 min` | `≤ 60 min` | 按 `DEC-OPS-007` 停止增险 |
| Market 缓存 | 可重建 | `≤ 15 min` 预热 | 预热和数据完整性通过前无新交易 |
| Web / PWA / Telegram | 可重建，无业务事实只存在客户端 | `≤ 60 min` | 不影响已有保护与退出 |
| CTO / Vault 观察 | 源链和场所事实可重建 | `≤ 4 h` | 停止划转，Vault 贡献按政策归零或降级 |

备份必须加密、版本化、跨主机 / 主库故障域保存并定期恢复验证。Redis、前端缓存、Freqtrade 本地状态和聊天记录都不能替代 Trading 持久账本。

## 5. 可观测性规范

### 5.1 四类信号

- **指标**：请求、延迟、错误、队列、连接、数据新鲜度、Heat、风险状态、保护覆盖、Unknown 持续时间、对账差异和资金在途。
- **结构化日志**：服务、环境、版本、账户 / 执行域、correlation ID、事件 ID、错误码；禁止 secret 和不必要个人数据。
- **分布式追踪**：从 Proposal / 操作意图贯穿身份、风险、授权、OMS、Venue、Freqtrade、场所事实、审计和告警。
- **业务 / 审计事件**：状态迁移、允许 / 拒绝、订单、保护、资金、权限、配置、break-glass 和恢复证据。

指标和日志帮助诊断，但不能取代交易与审计账本。

### 5.2 必备看板

1. **全局安全态势**：系统风险状态、Current Portfolio MTM Equity、Open / Reserved / Unknown Heat、未保护仓位和未授权事件。
2. **逐场所执行**：私有 / 公开流新鲜度、worker、限速、订单生命周期、保护覆盖、Unknown 和对账差异。
3. **审核与身份**：待审、过期、拒绝、MFA、自审、标签变化、异常设备和会话撤销。
4. **资金与 Vault**：运营资金、CTO 状态、源端预留、在途、目的端确认、Vault 资格和控制权变化。
5. **平台健康**：数据库、审计、消息、时钟、备份、容量、错误预算和发布版本。

### 5.3 告警质量

- 每条告警绑定服务、执行域、严重度、runbook、correlation ID、首次 / 最近时间和当前风险。
- 同一根因聚合，禁止每个标的重复轰炸；恢复告警必须基于真实健康，不基于超时自动关闭。
- 维护静默需要审批、到期和审计，不能静默未保护仓位、未授权订单、双主或凭据泄漏。
- Telegram 不是唯一告警渠道；备用路径和升级顺序由 `DEC-OPS-006` 冻结。

### 5.4 本地开发凭据与轮换

本地开发所需的敏感值统一保存在 `/Users/vireo/Documents/trading/.env.local`；项目内相对路径为 `.env.local`。该文件必须保持 Git 忽略和仅当前操作系统用户可读写，不得进入提交、补丁、日志、截图、浏览器客户端、Telegram 消息、审计事件或文档正文。可提交的变量名模板是 `/Users/vireo/Documents/trading/.env.example`。

`.env.example` 只登记变量名和安全默认值；Telegram Token、Perptape API Key、Binance Key/Secret、Hyperliquid API Wallet 私钥及其他真实值只能写入未跟踪的 `.env.local`。它们只供对应服务端边界读取；Web/PWA、前端构建、通知正文和任何客户端均不得读取。生产环境必须迁移至经 `DEC-SEC-002` 认证的 Secrets/KMS，不得把 `.env.local` 当作生产秘密库。

任何曾出现在聊天、工单、日志或截图中的 Token 都按已暴露处理。当前 Telegram Token 在首次联调或上线前必须通过 BotFather 重新生成，并原位替换 `.env.local` 中的值；轮换后只验证变量存在、权限和密钥指纹，不在任何可提交材料中复制明文。发生泄漏时立即撤销旧 Token、暂停 Bot 增险入口、检查审计和绑定记录，再使用新 Token 恢复。

## 6. 事故等级与响应基线

`DEC-OPS-011` 已确认以下事故等级、首次响应目标、通知和复盘制度：

| 事故等级 | 示例 | 首次响应目标 | 自动动作 |
| --- | --- | --- | --- |
| `INC-P0` | 未授权真实增险、未保护真实仓位、重复增险、资金 / 主密钥泄漏、双主 | 立即分页；`5 min` 内确认 | KILL_SWITCH，撤增险并确认真实仓位 |
| `INC-P1` | 单所私有事实未知、Risk / OMS / 审计不可用、重大对账差异 | `15 min` 内确认 | NO_NEW_POSITION 或 REDUCE_ONLY |
| `INC-P2` | 单一非关键功能降级、备用通知失败、可恢复数据延迟 | `60 min` 内确认 | 限定范围降级 |
| `INC-P3` | 无交易影响的展示、报告或低风险缺陷 | 工作队列处理 | 记录并计划修复 |

未分类但可能涉及资金、安全或未授权风险的事件按最高合理等级处理，不能等待分类后才停止增险。

## 7. 通用故障处置流程

每份具体 Runbook 都采用同一顺序：

1. **Detect**：告警或人工发现，保存首个事实时间和 correlation ID。
2. **Declare**：确定事故等级、事故指挥和受影响账户 / 执行域。
3. **Contain**：自动收紧风险；撤销增险，不盲目重试。
4. **Establish Facts**：读取场所私有仓位、挂单、成交、余额、保护和资金事实。
5. **Protect / Reduce / Exit**：按健康度选择维护保护、reduce-only 或受控退出。
6. **Reconcile**：重建 Trading 账本、Heat、授权和资金状态。
7. **Recover**：修复、回放、冷却、人工逐级解锁；旧意图与授权不复活。
8. **Review**：固化时间线、损失、根因、检测缺口、整改负责人和回归用例。

## 8. 核心 Runbook

### RB-001 真实仓位缺少足额保护

- 触发：真实成交后超过逐场所认证窗口仍无足额保护，或保护被拒绝 / 撤销 / 数量不足。
- 自动：停止该账户开仓与 Add，撤销增险订单，升级至少 `INC-P0`。
- 处置：查询真实仓位与订单；仅在认证语义内修复保护，无法修复则 reduce-only 减仓或退出。
- 恢复证据：真实仓位全部覆盖、无未知订单、账本和 Heat 对账、根因回归测试通过。

### RB-002 订单发送结果未知或部分成交

- 自动：意图进入 Unknown，计入最坏 Unknown Heat，禁止同对象新意图。
- 处置：用 client / venue order ID 查询原订单和最近成交；不得生成替代订单。
- 部分成交：保护真实成交数量，未成交部分不算 Open Heat；按原意图完成撤销或终态。
- 恢复证据：唯一终态、无残余订单、真实仓位受保护、Add 消费与风险账一致。

### RB-003 单所私有仓位 / 订单 / 余额未知

- 按 `DEC-OPS-005` 执行；当前真源要求在例外认证前全局关闭新增风险。
- 健康场所不得接管、补做或对冲未知订单。
- 恢复需完成场所全量快照、成交补拉、账本差异解释和旧提案 / Add 失效。

### RB-004 Freqtrade / VenueAdapter 故障

- 冻结受影响 worker / 执行域，确认交易所原生保护。
- 备用 worker 只能按 `DEC-OPS-009` 使用 fencing 接管；未认证时走人工只减仓手册。
- 重启默认只对账，Trading 明确下发当前风险状态前不得增险。

### RB-005 Risk Engine、账本、数据库或审计故障

- Risk 不可验证：所有新增风险拒绝。
- OMS / 主账本不可写：不得发送新订单；已有减险只走预认证且可恢复的耐久路径。
- 审计故障：按 `DEC-OPS-007` 禁止开仓和 Add，不能以 Redis / 本地日志替代。
- 恢复：从场所事实、持久账本和异地备份重建；先对账，再恢复保护，最后人工开放增险。

### RB-006 Web、PWA、Telegram 或通知故障

- 无法创建或批准新提案；客户端显示最后快照必须带时间戳和离线标识。
- 既有自动 Add 是否继续严格按 `DEC-OPS-004`，客户端不得自行判断。
- 止损、动态去杠杆和退出由服务端继续；无可靠告警且有真实仓位时按 `DEC-OPS-001` / `DEC-OPS-006` 降级。
- 通知 delivery 的 `RETRY_WAIT` 只按已确认限速有界重试；`OUTCOME_UNKNOWN` 禁止自动重发，须先在渠道侧核对 event ID。恢复持续 worker 前先运行 `trading-notification-worker --once`，确认 Schema head、加密密钥、路由版本、失败码和审计投影；该 worker 不获得交易或资金能力。

### RB-007 未授权订单、旁路或凭据泄漏

- 立即 KILL_SWITCH，撤销增险订单，确认真实仓位与保护；不得先删除证据。
- 撤销 / 轮换受影响凭据，隔离入口，保全身份、网络、日志和交易所事实。
- 人工只可保护、reduce-only 或退出；恢复需安全负责人和风险负责人共同确认，具体复核按 `DEC-SEC-003`。

### RB-008 CTO、Vault、链或资金在途异常

- 停止新划转；未知 Vault 贡献按政策归零，绝不向活动仓位补资。
- 用唯一 transfer ID 核对源端预留、链上交易、确认数、目的端到账和费用。
- 未形成一端明确失败或目的端明确结算前，不释放源端预留、不重复发起。
- CTO 故障不阻断交易所仓位止损、减仓和退出。

### RB-009 系统时钟、规则或依赖版本异常

- 停止受影响适配器增险，冻结事件顺序和当前制品版本。
- 修复时间源或规则后重跑契约、历史回放和影子验证；受影响执行证书重新签发。

### RB-010 受复核风险恢复

1. `NO_PYRAMID` / `REDUCE_ONLY` 可进入 `/risk` 的 `RiskControlChangeRequest` 流程；政策已是 `NORMAL` 但 AUTO_ADD 仍关闭时，也可仅申请恢复该 Gate。`KILL_SWITCH` 保持关闭并按 RB-007/人工事故恢复处置。
2. 由 HUMAN SYSTEM_ADMIN 提交原因并冻结当前 RiskPolicy ID/version/revision、AUTO_ADD 状态/version 和运行时 scope。生产必须发现至少一个 LIVE scope；否则 `LIVE_SCOPE_CONFIGURATION_REQUIRED`，不得创建可执行恢复假象。
3. 申请人之外的两名不同 HUMAN 且有 `risk.restore.review` 权限的用户（REVIEWER 或 SYSTEM_ADMIN）分别在 Web 完成绑定 `risk.restore.review`、request ID 和当前 version 的动作级 step-up；Telegram、离线 PWA 和 break-glass 短链不能替代两票。
4. 最近一次相关收紧后等待至少 15 分钟，并在请求创建后 24 小时内由 HUMAN SYSTEM_ADMIN 以绑定 `risk.restore.execute` 和当前 version 的 grant 执行。
5. 执行事务重新锁定风险容量，比较 Policy/Gate version 和完整 scope，重验权益、仓位、保护、订单、Unknown 与机器 MATCH；任何 blocker 或漂移均停止，修复后新建请求，不能修改冻结请求绕过。
6. 成功只创建新的 NORMAL RiskPolicy，并按冻结选择更新 AUTO_ADD Gate version。暂停/关闭产生的旧 TradingAuthorization、旧 AddUnit 和旧订单永不复活；已发送/Unknown Add 的迟到正成交仍进入责任槽和对账。

本服务的 `SignedTokenService` 仅以本地 HMAC 验证 action grant 的 user/action/object/version/TTL；只有 local/test 提供 Mock grant 发行。生产 issuer、IdP/WebAuthn 和外部签名验证尚未实现；在这些能力和运行时 LIVE scope 配置完成前，生产恢复必须保持不可用/fail closed。

## 9. 人工交易所接管

只有 Trading 整体不可用且真实仓位需要处置时才启用，具体凭据和双人复核由 `DEC-SEC-003` 冻结：

1. 宣布事故并记录接管人、账户和理由。
2. 使用专用、限时凭据；只确认保护、撤销增险、reduce-only 或退出。
3. 禁止开仓、Add、换所、提款、Vault 注资或放宽止损。
4. 保存全部场所回执和最终仓位。
5. 系统恢复后保持 KILL_SWITCH，重建事实并对账；撤销接管凭据。

## 10. 对账、备份与恢复验证

- 对账采用“事件驱动增量 + 周期全量 + 日终财务”候选模式；频率、容差由 `DEC-OPS-008` 冻结。
- 日界和报告币种由 `DEC-OPS-010` 冻结；冻结前不执行依赖自然日的自动资金 / 损失政策。
- 历史账不得直接修改；更正使用可追踪补偿事件。
- 备份恢复演练必须证明授权、意图、订单、成交、保护、Heat、权限、资金和审计时间线一致。
- 恢复成功不等于可增险；还需根因、修复回放、冷却和人工逐级解锁。
- 演练结果直接记录在当前实现基线、变更提交或事故复盘中；不为每次演练建立独立证据平台。

当前预生产脚本入口：

```bash
TRADING_DATABASE_URL="$SOURCE_TEST_DATABASE_URL" \
  ./scripts/backup_postgres.sh /absolute/path/trading.dump

TRADING_DATABASE_URL="$DISPOSABLE_RESTORE_DATABASE_URL" \
  ./scripts/restore_test_postgres.sh /absolute/path/trading.dump
```

目标恢复数据库必须预先创建，名称必须以 `_test` 结尾；脚本会清理并覆盖该目标，硬拒绝其他名称。容器内 PostgreSQL 可额外设置 `TRADING_PG_CONTAINER`。当前脚本不是生产灾备自动化，不能指向共享库或真实交易库。

当前完整 Compose 入口是 `./scripts/run_compose.sh`，本机 Python 入口是 `./scripts/run_local.sh`。两者都使用 `trading_local` PostgreSQL 16，先升级至当前 Alembic head，再幂等初始化内部用户。`run_local.sh` 强制连接 `127.0.0.1:5434/trading_local`，不会把共享 `.env.local` 的数据库 URL 当成本地目标。准确的本地管理员用户名是小写 `kelly_oooo`（四个 `o`），另有 `local-proposer`、`local-reviewer-two` 及 SERVICE principals。本地密钥与密码仅保存在权限 `0600` 的 `.local/`；其他 Token、API Key 和私钥只从服务端秘密环境读取，不复制到命令历史、文档或提交。

当前 Schema 为 42 张业务/运行表（另有 Alembic 版本表），Alembic head 为 `20260810_0025`。本地启动不使用 SQLite，也不应连接真实交易数据库；测试和恢复演练继续使用名称以 `_test` 结尾的独立 PostgreSQL。运行 `uv run trading-doctor` 可以无秘密地核对配置、Schema、Gate 和连接能力。详细流程见 [开源部署、配置、升级与恢复](开源部署配置升级与恢复.md)。

2026-07-19 已完成一次本地演练：custom-format 归档可由 `pg_restore --list` 解析；独立 `trading_m9_restore_test` 恢复后有 26 张业务表、Alembic revision `20260718_0001`、五个默认关闭 Gate，数据库 readiness 与 `alembic check` 均通过；演练库和归档随后删除。

### 10.1 当前本地资源观测

在单个 FastAPI 进程和单个一次性 PostgreSQL 容器上，对 readiness 连续请求 20 次后进行一次观测：API 约 91,440 KiB RSS、0.1% CPU；PostgreSQL 容器约 166.9 MiB、0.03% CPU、7 个进程。当前实现没有常驻自动资金、Telegram、Perptape、Binance 或 Hyperliquid worker。上述数字只描述该开发机、该时刻，不是压测结果、容量规划或生产 SLO；真实部署前仍需在目标环境测量延迟、连接数、数据库增长和峰值资源。

## 11. 错误预算与持续改进

- 正式错误预算待 `DEC-OPS-002`；安全不变量没有可消费的错误预算。
- SLO 接近耗尽时依次冻结扩容、关闭非必要变更、提高演练与修复优先级，必要时降为 NO_PYRAMID / NO_NEW_POSITION。
- 每个 `INC-P0` / `INC-P1`、未授权事件、重复订单、保护超窗、账实差异和 break-glass 使用都产生复盘与永久回归用例。
- 日常复盘看异常、保护、Unknown 和对账；周期复盘看 SLO、容量、权限、备份恢复和趋势性缺陷。

## 12. 决策与研究引用

| 决策 | 影响 |
| --- | --- |
| `DEC-OPS-001` | 值守范围、无值守时的交易限制 |
| `DEC-OPS-002` | SLI、SLO、错误预算与正式阈值 |
| `DEC-OPS-003` | 分级 RTO / RPO、备份与灾备形态 |
| `DEC-OPS-004` | 控制面故障时既有自动 Add |
| `DEC-OPS-005` | 单所私有事实未知的冻结范围 |
| `DEC-OPS-006` | Telegram 以外告警渠道与升级顺序 |
| `DEC-OPS-007` | 审计不可写时的故障边界 |
| `DEC-OPS-008` | 对账频率、时间边界与容差 |
| `DEC-OPS-009` | 备用 worker、fencing 与接管 |
| `DEC-OPS-010` | 日终时区与报告币种 |
| `DEC-OPS-011` | `INC-P0` 至 `INC-P3`、响应时限、通知与复盘义务 |
| `DEC-SEC-003` | break-glass 与人工接管复核 |

`DEC-OPS-002`、`DEC-OPS-003`、`DEC-OPS-008` 的候选数字在取得证据前只用于设计、测试和容量评估；其余已确认政策按本文件安全默认执行。
