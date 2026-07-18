# Trading 交易系统

> 文档入口版本：2026-07-18
> 当前状态：产品与系统设计完成首轮基线，耐久控制面首包已实现；不构成实盘授权，也不表示现有原型已经满足目标方案

本目录用于设计一套由人工决定初仓、由程序在有限授权内管理风险和仓位的完整交易系统。目标范围同时包含 Binance USDⓈ-M Futures 与 Hyperliquid 的 U 本位永续合约，覆盖加密货币、美股及股指、贵金属、商品及商品期货参考资产四类板块；真实交易只限逐账户、执行域、保证金模式和标的完成认证的范围。其他交易场所只保留稳定 Venue Port 边界，没有现实需求时不实现适配器。

目标产品形态是 Web / PWA 主控制台与 Telegram 辅助入口。系统同时接收 SYSTEM 系统提案与 MANUAL 人工提案；初仓必须经过人工审核和确定性 Risk Engine，自动加仓只消费开仓时签发的有限授权，止损、减仓和退出不等待再次人工确认。Trading 是受管账户唯一日常订单发送者，Freqtrade 只是 Binance 与 Hyperliquid 的底层订单执行引擎。

## 从这里开始

1. 阅读 [交易系统总体方案.md](交易系统总体方案.md)：系统原则、范围、职责、风险和产品形态的最高层基线。
2. 阅读 [策略合同与数值化验收门.md](策略合同与数值化验收门.md)：主策略研究合同、认证单位、验收门和能力激活证据。
3. 阅读 [docs/README.md](docs/README.md)：产品化文档地图、建设状态和推荐阅读路径。
4. 阅读 [文档治理与权威矩阵](docs/00-governance/文档治理与权威矩阵.md)：发生冲突时以哪份材料为准。
5. 阅读 [术语表](docs/00-governance/术语表.md)：资金、风险、授权、仓位与执行的统一语言。
6. 阅读 [决策与研究登记](docs/00-governance/待确认决策清单.md)：40 项设计选择已自动确认，21 项等待研究/实证，只有 3 项外部事实需要用户补充；它是决策状态的唯一真源。

## 当前不可绕过的规则

- 不以精简子集或功能分期缩减目标范围。Binance、Hyperliquid、Web/PWA、Telegram、完整提案审核、三档风险、Add-1/2/3、保护退出和 Vault/CTO 均属于同一完整工程交付。
- 工程交付与真实能力激活分离。回放、影子、仿真和小额实盘是认证证据环境，不是残缺产品阶段；未获 `CERTIFIED` 证书的能力必须保持关闭。
- UI 采用 [thedankoe.com](https://thedankoe.com/) 启发的极简编辑风格，支持深色和浅色；风险状态不能只靠颜色，交易信息可读性和 WCAG 2.2 AA 高于视觉相似度。

- 信号或人工交易假设只能生成提案，不能直接生成订单。
- 系统提案和人工提案的初仓都必须由人工明确批准；Risk Engine 始终可以拒绝。
- 自动加仓默认关闭。启用后低、中、高风险默认最多 1 / 2 / 3 次，对应最大有效杠杆 3x / 5x / 10x。
- `1R_0 = Total Capital Snapshot_0 × 0.5%`；低、中、高整份经济仓位的最大计划损失分别为 1R / 2R / 3R，即 0.5% / 1.0% / 1.5%。
- `FundingEnvelope_0 = Total Capital Snapshot_0 × trade_funding_pct`。资金信封是资金占用上限，不是风险；1.5% 只是示例，正式 `N%` 尚待冻结。
- 30% / 50% / 100% 冻结收益率只解锁 Add 候选。每次 Add 按当时真实仓位权益计算到冻结 `L_target` 的差额，不预设每级手数或风险比例。
- 任意大于零的真实成交完整消费一次 Add；终态确认零成交不消费。风险和流动性只能缩量或阻断，不能为了达到目标杠杆放宽限制。
- 每次增险前使用包含最新未实现盈亏的 Current Portfolio MTM Equity 重算动态预算，并与冻结授权取更严格者。
- 程序只做盈利金字塔，不亏损摊平。止损、动态去杠杆、减仓和退出不等待人工再次批准。
- Web、PWA、Telegram、FreqUI、策略回调和其他脚本都不能绕过 Trading 直接向交易所增险。
- Vault 风险计入默认关闭；风险计入、资金划转、自动利润归集和自动运营补充是相互独立的权限。Vault 资金必须在初仓前到账，绝不救援活动仓位。
- 交易所、订单、仓位、成交和余额以交易所私有事实为准；未知状态禁止扩大风险。

## 目录定位

| 路径 | 定位 | 能否作为生产真源 |
| --- | --- | --- |
| `交易系统总体方案.md` | 系统原则与总体架构基线 | 是，最高层原则真源 |
| `策略合同与数值化验收门.md` | 主策略研究与认证基线 | 是，但从属于总体方案且当前仍为 Draft |
| `docs/` | 产品化、架构、合同、安全、测试和运维文档 | 按各文档状态决定 |
| `DynamicPositionSizing-/` | 目标杠杆区间与布林偏差过滤的原型参考 | 否，不能直接迁移执行代码 |
| `low_vol_breakout_bn/` | 低波动突破和退出行为的旧实现参考 | 否，不是目标 OMS、风控或执行引擎 |
| `交易系统 notion 文档/` | 历史 Notion 导出和研究材料 | 否，发生冲突时一律服从当前权威文档 |
| `仓位计算-新.xlsx` | 情景分析与研究附件 | 否，不是运行时事实源 |

## 当前安全边界

目录内原型可能包含本地配置、日志和 `.env`。在任何开发、联调、部署或共享开始前，必须完成凭据轮换、秘密移出仓库、忽略规则、生产/测试/研究账户隔离和安全基线审查。不要把当前目录直接视为可发布产品，也不要使用历史脚本连接真实账户下单。

本机开发凭据统一放在 `/Users/vireo/Documents/trading/.env.local`，可提交的变量名模板为 `.env.example`。当前 Telegram Bot 使用变量 `TELEGRAM_BOT_TOKEN`；文档和日志只记录变量名与文件地址，绝不记录密钥值。由于当前 Token 曾出现在对话中，第一次联调前必须通过 BotFather 重新生成并原位替换。详细保管、轮换和读取边界见 [SLO、可观测性、故障恢复与 Runbook](docs/06-operations/SLO可观测性故障恢复与Runbook.md#本地开发凭据与轮换)。

## 当前工程节点

当前已完成二十六个本地工程工作包：从 [WP-0001：耐久控制面基础](docs/08-implementation/WP-0001-耐久控制面基础.md) 的命令幂等、不可变审计和 outbox/inbox，推进到 [WP-0026：规范化 Base Heat 与 Scope 增量](docs/08-implementation/WP-0026-规范化Base-Heat与Scope增量.md)。WP-0012 至 WP-0017 将 canonical order/fill/position/protection 事实逐层强绑定到 OrderIntent、风险暴露与原生保护；WP-0018 至 WP-0020 建立账户权益、当前投影和不可变受管账户 manifest；WP-0021/0022 分别在提案与最终预检重算 canonical 组合资本，并保持冻结 `Snapshot_0` 与 1R 不变；WP-0023/0024 通过同一公共聚合器，在最终预检和提案预检从 Risk Ledger 当前态派生 funding、Heat、内部 margin 和七层 scope；WP-0025 冻结 base Heat、protected-profit giveback、cost stress 三项分量；WP-0026 移除调用方自报 base Heat 和 scope planned loss，改由版本化 Risk Engine 公式统一计算，并以 18 位定点比例保持当前分量严格守恒。技术选择见 [ADR-0001](docs/08-implementation/ADR-0001-生产技术栈与首包边界.md)，逐包验证证据位于 [docs/08-implementation/evidence](docs/08-implementation/evidence/)。

当前代码仍只有不可发送的 SHADOW OrderIntent、只允许 `real_funds_eligible=false` 的 SHADOW 证书与资金范围合同，以及 `external_send_permitted=false` 的 SHADOW sender lease/claim。canonical VenueOrderObservation、VenueFill、VenuePositionSnapshot、VenueProtectionSnapshot 和 VenueAccountEquitySnapshot 只能由本地测试构造的完整对账输入产生；提案与最终 OrderIntent 预检已绑定组合资本、Ledger 当前占用和规范化 base Heat，当前损失可从 frozen component 与 Ledger bucket 复算，但新增 giveback、cost 和 scope stress 仍未绑定实时保护、场所费用、滑点和资金费真源。没有真实部署账户 manifest、真实私有 venue collector，也没有认证 Binance/Hyperliquid 的逐场所账户权益公式或 FX/stablecoin/depeg 事实。尚缺 production/small-live 证书或 lease、worker/sidecar 发送瞬间 fencing、Freqtrade/VenueAdapter、跨币种 USD 组合 MTM、最终 PnL、Web/PWA、Telegram、Margin、Vault/CTO 或场所认证证据。迁移不 seed manifest、证书、sender scope、lease、run、claim 或 venue fact；`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 均为 `DISABLED`。工程节点不等于获得实盘授权。

`OPEN` 只表示缺少真实外部事实；`RESEARCH_REQUIRED` 表示必须靠研究或执行证据冻结。两者都不能签发现实交易权限，也不用于删减完整工程目标。
