# Trading 交易系统

> 状态日期：2026-07-19
> 当前状态：M8 默认关闭的自动利润归集与下一周期运营补充候选；LIVE 订单和真实资金发送不可用

本项目面向一个资本所有者、一个内部组织和多个内部用户。用户可以提交和审核提案、查看仓位、处理异常；系统在风险可控的前提下辅助执行交易并判断是否赚钱。不开放外部注册，不管理第三方资金，不建设机构级多租户、通用合规或通用认证平台。

完整产品愿景包含 Binance、Hyperliquid、Web/PWA、Telegram、VenueAdapter、Freqtrade/OMS、Margin、Vault/CTO 和报表。这些目标不删除，但按可运行的端到端用户流程逐步开发。未实现能力保持关闭，不为未来可能性预建通用实体。

## 从这里开始

1. [当前实现基线](docs/08-implementation/当前实现基线.md)：当前代码、Schema、入口和明确缺口。
2. [核心业务不变量](docs/08-implementation/核心业务不变量.md)：当前必须保持的风险与执行语义。
3. [后续端到端开发路线](docs/08-implementation/后续端到端开发路线.md)：按用户流程推进的开发顺序。
4. [本次架构收敛记录](docs/08-implementation/本次架构收敛记录.md)：KEEP/MERGE/SIMPLIFY/DELETE 和迁移结论。
5. [交易系统总体方案](交易系统总体方案.md)：长期产品愿景和最高层原则。
6. [产品化文档中心](docs/README.md)：专项文档地图与权威边界。

## 当前不可绕过的规则

- 信号和人工交易假设只能生成 Proposal，不能直接生成订单。
- SYSTEM 与 MANUAL 初仓都必须经过人工审核；Risk Engine 始终可以拒绝或缩量。
- 创建者不能自审；高风险提案需要两个不同 Reviewer。
- Approval 只产生短期、有限范围的 TradingAuthorization，不产生永久权限。
- 数据陈旧、仓位未知、保护未知或订单结果 Unknown 时禁止新增风险。
- Reservation、OrderIntent 和幂等回执必须原子提交；Unknown 不能提前释放或自动重发。
- 多个退出候选合并为唯一更小目标仓位；有活动 OrderIntent 时不重复生成减仓意图。
- 场所真实订单、成交、仓位、保护、余额和资金费必须与内部预期分开并对账；SHADOW、TESTNET、LIVE 使用独立事实作用域。
- 每个 execution scope 只有一个有效 sender；新 owner 接管后旧 fencing token 无效。
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD`、`AUTO_PROFIT_SWEEP` 与 `AUTO_OPERATING_REFILL` 默认 `DISABLED`。
- AUTO_ADD 只有管理员显式开启 Gate 后才可能执行；每个 Add 仍需冻结 Proposal、分档 AddUnit、后续 Perptape 候选、盈利仓位、足额保护、新鲜事实、剩余授权和最终 Risk Engine 同时通过。只有首个正成交消费 AddUnit，零成交取消/拒绝不消费，Unknown 冻结后续新增风险。
- 资金 Proposal、双人独立复核、Capital Transfer Authorization、源端预留、在途、目的端确认和对账与交易授权分离；活动仓位、未解决订单或 Unknown 禁止 Vault 救仓，Unknown 不释放或重发。
- 自动利润归集和自动运营补充使用两个独立 Gate；当前只根据空仓、无订单、无 Unknown、机器 MATCH、已确认余额和已关闭 Campaign 净 PnL 生成待双人复核的非生产候选，不自动提交资金。浮盈不能归集，净亏损不能触发运营补充。
- Telegram 当前只有不联网的 Mock 通知与受限收紧风险动作合同；资金通知不包含批准或执行入口。Binance 和 Hyperliquid Core 的只读/TESTNET 窄合同全部默认关闭。TESTNET/Mock 合同不等于真实账户实证，LIVE 没有发送入口。真实账户验证、实盘发送、HIP-3、Margin、真实 Vault/CTO 适配器与真实资金划转尚未实现，文档愿景不能冒充代码能力。

## 当前代码入口

- 进程：`uv run trading-api`
- Web/PWA：提案/审核、Campaign、AUTO_ADD 候选、原子减仓/退出、全局只收紧风险动作、`/venues/binance` 只读场所事实页和 `/capital` 资金中心；Hyperliquid 当前只有 HTTP 入口，没有专属页面
- HTTP：健康检查、内部会话、Perptape 机会、Proposal/Review/Risk/Authorization、SHADOW/TESTNET Campaign、AUTO_ADD/减仓/退出、资金事实/提案/授权/Mock 划转，以及 Binance、Hyperliquid Core 的只读和受控 TESTNET API
- 内部业务：`trading_control_plane.service.TradingService`
- 纯计算：`evaluate_risk`、`select_target_position`、`compute_pnl`
- 数据库：PostgreSQL，Alembic head `20260718_0001`
- 场所边界：`binance.py`/`binance_execution.py` 只覆盖 USDⓈ-M 只读与官方 TESTNET；`hyperliquid.py`/`hyperliquid_execution.py` 只覆盖 Core Info 与官方 TESTNET Exchange 合同。Hyperliquid “市价”固定为带显式批准价格的 IOC，不使用隐含 5% 滑点；HIP-3、LIVE、保证金和资金写入口不存在，`LIVE_ORDER_SEND` 仍为 `DISABLED`
- 资金边界：`capital.py` 提供确定性的 SHADOW/TESTNET Mock 提交和自动候选计算，没有网络、签名器或凭据字段；真实 `CAPITAL_TRANSFER` 与两个自动资金 Gate 均保持 `DISABLED`

正式身份源按冻结决策使用托管 IdP 与 Passkey，但外部 IdP 尚未接入。本地/测试环境可显式启用仅识别已存在内部用户的 Mock 会话和 Mock step-up；生产环境硬拒绝启用 Mock 身份。Perptape 使用其现有 `GET /api/v1/breakouts` 窄合同，需单独配置平台 API Key，未配置时机会入口明确返回不可用。

Binance 私有事实读取必须同时显式配置 `TRADING_BINANCE_READ_ONLY_ENABLED=true`、只读 API Key/Secret 和 `TRADING_BINANCE_FACT_ENVIRONMENT=TESTNET|LIVE`。当前仓库没有真实凭据或账户验证结果；未配置时页面只显示 PostgreSQL 已保存事实，不尝试联网。

Binance TESTNET 订单还必须单独配置 `TRADING_BINANCE_TESTNET_ORDER_SEND_ENABLED=true` 和独立 TESTNET Key/Secret。客户端严格拒绝 LIVE 主机，使用稳定 client order identity 先查询再发送；Unknown 只允许查询恢复，不盲重发。当前无真实测试账户或凭据，自动化仅验证官方合同形状和数据库语义，没有产生任何交易所订单。

Hyperliquid Core 只读同步必须显式配置 `TRADING_HYPERLIQUID_READ_ONLY_ENABLED=true`、与 `TRADING_HYPERLIQUID_FACT_ENVIRONMENT` 一致的官方 API 主机，以及真实账户或 subaccount 地址。TESTNET 发送还要求 `TRADING_HYPERLIQUID_TESTNET_ORDER_SEND_ENABLED=true` 和部署时注入的官方兼容 signer；仓库配置中没有私钥字段，默认进程也不会自行构造 signer。稳定 128-bit cloid、query-before-send、cancel-by-cloid、显式 IOC 限价和原生 trigger 保护均有本地合同测试，但当前没有真实 API Wallet、账户或签名实证，没有发送任何 Hyperliquid 订单。

## 本地开发

```bash
uv sync
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
TEST_DATABASE_URL='postgresql+psycopg://.../trading_test' uv run pytest --cov=trading_control_plane
TRADING_DATABASE_URL='postgresql+psycopg://.../trading_test' uv run alembic upgrade head
```

集成测试数据库名必须以 `_test` 结尾。测试夹具会删除并重建其 `public` schema，禁止指向任何真实交易数据库。

本机敏感值只放在 `.env.local`；可提交变量名模板为 `.env.example`。不得把密钥值写入代码、文档、日志或测试制品。

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
