# Trading 交易系统

> 状态日期：2026-07-18
> 当前状态：紧凑的预生产 SHADOW 交易核心；没有实盘授权

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
- 场所真实订单、成交、仓位、保护、余额和资金费必须与内部预期分开并对账。
- 每个 execution scope 只有一个有效 sender；新 owner 接管后旧 fencing token 无效。
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 默认 `DISABLED`。
- Web/PWA、Telegram、真实 VenueAdapter、实盘发送、Margin、Vault/CTO 尚未实现，文档愿景不能冒充代码能力。

## 当前代码入口

- 进程：`uv run trading-api`
- HTTP：`/health/live`、`/health/ready`、`/metrics`
- 内部业务：`trading_control_plane.service.TradingService`
- 纯计算：`evaluate_risk`、`select_target_position`、`compute_pnl`
- 数据库：PostgreSQL，Alembic head `20260718_0001`
- 订单边界：当前只有合成 SHADOW 场所事实，不连接交易所、不发送真实订单

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
| `策略合同与数值化验收门.md` | 策略研究与未来能力验收 | 研究输入，不代表已实现 |
| `docs/` 其他专项文档 | 产品、领域、执行、质量和运维长期合同 | 按状态和当前基线解释 |
| `DynamicPositionSizing-/` | 历史原型参考 | 否 |
| `low_vol_breakout_bn/` | 历史原型参考 | 否 |
| `交易系统 notion 文档/` | 历史 Notion 资料 | 否 |
| `仓位计算-新.xlsx` | 研究附件 | 否 |

如长期文档与当前代码能力冲突，以“尚未实现、对应 Gate 关闭”处理；不得创建证明平台、绑定层或快照层来填补文档与产品流程之间的空白。

当前产品尚未进入 Codex Security 审计阶段。按用户明确约束，Codex Security 及其所有审计 Skill、插件和模块保持停用；除非用户以后明确重新授权，否则只执行常规代码检查、测试和数据库一致性验证。
