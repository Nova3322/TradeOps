# Trading 产品化文档中心

> 状态日期：2026-07-18
> 工作状态：产品与系统设计
> 实盘效力：无；本文档中心不签发交易、资金或部署授权

本目录把根目录的理念与策略基线拆成可供产品、架构、开发、测试、安全和运营共同使用的专项文档。根目录两份权威文档已经回答“系统为什么存在、谁做决定、风险不能越过什么边界”；这里继续回答“产品怎样使用、系统怎样组成、合同怎样实现、怎样验证和怎样运行”。

## 权威入口

| 顺序 | 文档 | 作用 |
| ---: | --- | --- |
| 1 | [交易系统总体方案](../交易系统总体方案.md) | 不可绕过的系统原则、项目边界、风险与产品形态的最高层基线 |
| 2 | [策略合同与数值化验收门](../策略合同与数值化验收门.md) | 全市场趋势突破主策略的研究合同、认证单位、数值化验收和能力激活基线 |
| 3 | [文档治理与权威矩阵](00-governance/文档治理与权威矩阵.md) | 文档层级、状态、冲突裁决、变更和废弃规则 |
| 4 | [术语表](00-governance/术语表.md) | 跨产品、风险、执行和资金域的统一词义 |
| 5 | [决策与研究登记](00-governance/待确认决策清单.md) | 设计选择、证据冻结项和仅三类外部事实的唯一真源 |

## 文档状态

| 状态 | 含义 | 可否作为实现输入 |
| --- | --- | --- |
| `BASELINE` | 已经成为当前上位基线 | 可以，但仍须结合下位专项合同和验收门 |
| `APPROVED` | 已按治理流程批准且未失效 | 可以，仅限其明确适用范围 |
| `DRAFT` | 正在讨论或仍有空项 | 只能用于设计，不得签发生产能力 |
| `RESEARCH` | 参数或方法必须由预登记研究决定 | 只能用于研究、回放或影子证据 |
| `REFERENCE` | 历史材料、原型或示例 | 只能提供思想和证据线索，不能作为真源 |
| `SUPERSEDED` | 已被新文档替代 | 不得用于新实现 |
| `RETIRED` | 已退役，仅为审计保留 | 不得用于新实现或恢复旧能力 |

## 已有基线覆盖

根目录两份业务与策略基线已经覆盖：

- Binance、Hyperliquid 和未来 VenueAdapter 的总体边界；
- Perptape、Trading、RiskControl、Freqtrade、OMS、Margin Controller 与 Capital Transfer Orchestrator 的职责；
- SYSTEM / MANUAL 提案、人工初仓审核、有限授权和多标签权限原则；
- Trade Funding Envelope、1R、三档风险、Current Portfolio MTM Equity 和风险账本；
- 盈利里程碑、目标杠杆差额加仓、动态去杠杆、保护与退出；
- Web / PWA、Telegram、故障降级和资金治理理念；
- 策略研究、时间外验证以及逐认证单位的能力激活门。

本轮产品化套件用下列专项文件承接产品页面、服务部署、API、事件、数据模型、安全、测试追踪、SLO、值守、灾备和实施工作分解。所有文件仍受上位方案、决策状态和能力认证约束，不能单独签发现实交易权限。

## 产品化文档套件

专项内容过大时可以在原文件下按附件拆分，但附件必须声明上位文件、作用域与状态，不能新建一套平行真源。依照奥卡姆剃刀，只有当内容具有独立权威边界、生命周期或批准责任时才能新增文档；派生视图、纯规则模块和界面状态不得被包装成新的服务、领域实体或事实源。当前 UI 视觉合同归 [用户旅程与交互规格](01-product/用户旅程与交互规格.md) 管理，不另建视觉规范真源；当前也不建设或预建独立自动代理、权限、接口或数据实体。

```text
docs/
├── README.md
├── 00-governance/
│   ├── 文档治理与权威矩阵.md
│   ├── 术语表.md
│   └── 待确认决策清单.md（内容为决策与研究登记；保留路径稳定）
├── 01-product/
│   ├── 产品需求文档.md
│   ├── 用户旅程与交互规格.md
│   └── 身份权限与审批矩阵.md
├── 02-domain/
│   ├── 系统架构与部署拓扑.md
│   ├── 领域模型与状态机.md
│   └── 风险引擎规格.md
├── 03-contracts/
│   ├── API事件数据与审计契约.md
│   ├── 市场数据与Instrument-Catalog合同.md
│   └── 财务对账与PnL口径.md
├── 04-execution/
│   ├── OMS-Freqtrade-VenueAdapter执行规范.md
│   ├── Vault与CTO资金规范.md
│   ├── Binance执行认证清单.md
│   └── Hyperliquid执行认证清单.md
├── 05-quality/
│   ├── 安全与威胁模型.md
│   └── 测试验证与发布计划.md
├── 06-operations/
│   ├── SLO可观测性故障恢复与Runbook.md
│   └── 实施路线图与工作分解.md
├── 07-registers/
│   ├── 主策略参数与能力认证登记表.md
│   └── 需求追踪与证据矩阵.md
└── 08-implementation/
    ├── ADR-0001-生产技术栈与首包边界.md
    ├── WP-0001-耐久控制面基础.md
    ├── WP-0002-服务端授权判定核心.md
    ├── WP-0003-冻结提案审核内核.md
    ├── WP-0004-确定性风险预检与风险状态基础.md
    ├── WP-0005-TradingAuthorization与Campaign授权基础.md
    ├── WP-0006-原子风险预留与ShadowOrderIntent.md
    ├── WP-0007-耐久CapabilityCertificate与失效传播.md
    ├── WP-0008-耐久SenderFencing与ShadowClaim.md
    ├── WP-0009-耐久ReconciliationRun与最新成功门.md
    ├── WP-0010-对账绑定执行事实入口.md
    ├── WP-0011-继任Lease对账接管.md
    ├── WP-0012-Canonical-VenueOrder与VenueFill事实.md
    ├── WP-0013-Canonical-VenueFact执行强绑定.md
    ├── WP-0014-Canonical-VenuePositionSnapshot事实.md
    ├── WP-0015-Canonical-VenuePosition执行强绑定.md
    ├── WP-0016-Canonical-VenueProtectionSnapshot事实.md
    ├── WP-0017-Canonical-VenueProtection执行强绑定.md
    ├── WP-0018-Canonical-VenueAccountEquitySnapshot事实.md
    ├── WP-0019-可重建当前Venue投影.md
    ├── WP-0020-不可变托管资金范围与组合MTM.md
    ├── WP-0021-RiskPrecheck可信资本强绑定.md
    ├── WP-0022-最终ORDER-PRECHECK可信资本强绑定.md
    ├── WP-0023-最终预检Durable-Exposure强绑定.md
    ├── WP-0024-提案预检Durable-Exposure强绑定.md
    ├── WP-0025-风险预留损失组件冻结.md
    ├── WP-0026-规范化Base-Heat与Scope增量.md
    ├── WP-0027-策略绑定Cost-Stress.md
    ├── WP-0028-Canonical保护触发价事实.md
    └── evidence/
        ├── WP-0001-validation-20260718.md
        ├── WP-0002-validation-20260718.md
        ├── WP-0003-validation-20260718.md
        ├── WP-0004-validation-20260718.md
        ├── WP-0005-validation-20260718.md
        ├── WP-0006-validation-20260718.md
        ├── WP-0007-validation-20260718.md
        ├── WP-0008-validation-20260718.md
        ├── WP-0009-validation-20260718.md
        ├── WP-0010-validation-20260718.md
        ├── WP-0011-validation-20260718.md
        ├── WP-0012-validation-20260718.md
        ├── WP-0013-validation-20260718.md
        ├── WP-0014-validation-20260718.md
        ├── WP-0015-validation-20260718.md
        ├── WP-0016-validation-20260718.md
        ├── WP-0017-validation-20260718.md
        ├── WP-0018-validation-20260718.md
        ├── WP-0019-validation-20260718.md
        ├── WP-0020-validation-20260718.md
        ├── WP-0021-validation-20260718.md
        ├── WP-0022-validation-20260718.md
        ├── WP-0023-validation-20260718.md
        ├── WP-0024-validation-20260718.md
        ├── WP-0025-validation-20260718.md
        ├── WP-0026-validation-20260718.md
        ├── WP-0027-validation-20260718.md
        └── WP-0028-validation-20260718.md
```

### 专项索引

| 领域 | 文档 | 主要回答 |
| --- | --- | --- |
| 治理 | [文档治理与权威矩阵](00-governance/文档治理与权威矩阵.md) | 权威、状态、冲突、变更和历史材料规则 |
| 治理 | [术语表](00-governance/术语表.md) | 资金、风险、授权、执行和状态的统一语言 |
| 治理 | [决策与研究登记](00-governance/待确认决策清单.md) | 所有 `DEC-*` 的唯一状态真源 |
| 产品 | [产品需求文档](01-product/产品需求文档.md) | 产品对象、完整范围、能力和非功能目标 |
| 产品 | [用户旅程与交互规格](01-product/用户旅程与交互规格.md) | SYSTEM/MANUAL 提案、审核、异常、多端交互及深浅主题视觉合同 |
| 产品 | [身份权限与审批矩阵](01-product/身份权限与审批矩阵.md) | 多标签、作用域、MFA、自审和职责分离 |
| 领域 | [系统架构与部署拓扑](02-domain/系统架构与部署拓扑.md) | 组件、信任边界、故障域和部署关系 |
| 领域 | [领域模型与状态机](02-domain/领域模型与状态机.md) | 提案、授权、战役、订单、保护、风险与资金生命周期 |
| 领域 | [风险引擎规格](02-domain/风险引擎规格.md) | 风险口径、账本、硬门、输入输出和失败语义 |
| 合同 | [API事件数据与审计契约](03-contracts/API事件数据与审计契约.md) | API、事件、数据、幂等、顺序和审计字段 |
| 合同 | [市场数据与 Instrument Catalog 合同](03-contracts/市场数据与Instrument-Catalog合同.md) | 标的发现、身份、数据质量、分类和交易资格 |
| 合同 | [财务对账与 PnL 口径](03-contracts/财务对账与PnL口径.md) | 余额、成交、费用、资金费、收益和资金划转对账 |
| 执行 | [OMS-Freqtrade-VenueAdapter 执行规范](04-execution/OMS-Freqtrade-VenueAdapter执行规范.md) | 唯一订单链、订单意图、worker、保护与恢复 |
| 执行 | [Vault 与 CTO 资金规范](04-execution/Vault与CTO资金规范.md) | 资金政策、Vault 资格、双向划转和互斥记账 |
| 执行 | [Binance 执行认证清单](04-execution/Binance执行认证清单.md) | Binance 逐账户/模式/标的的认证证据 |
| 执行 | [Hyperliquid 执行认证清单](04-execution/Hyperliquid执行认证清单.md) | Core/HIP-3、账户抽象、保证金和保护认证 |
| 质量 | [安全与威胁模型](05-quality/安全与威胁模型.md) | 资产、威胁、身份、密钥、边界和缓解措施 |
| 质量 | [测试验证与发布计划](05-quality/测试验证与发布计划.md) | 测试层级、故障重放及能力激活证据环境 |
| 运营 | [SLO、可观测性、故障恢复与 Runbook](06-operations/SLO可观测性故障恢复与Runbook.md) | SLO、告警、值守、恢复、灾备和操作手册 |
| 运营 | [实施路线图与工作分解](06-operations/实施路线图与工作分解.md) | 完整目标、六个并行工作流、依赖与三道交付门 |
| 登记 | [主策略参数与能力认证登记表](07-registers/主策略参数与能力认证登记表.md) | 参数、证书、适用范围和失效状态 |
| 登记 | [需求追踪与证据矩阵](07-registers/需求追踪与证据矩阵.md) | 需求到设计、测试、证据和认证的闭环骨架 |
| 实现 | [ADR-0001：生产技术栈与首包边界](08-implementation/ADR-0001-生产技术栈与首包边界.md) | 当前工程技术选择、边界和失效条件 |
| 实现 | [WP-0001：耐久控制面基础](08-implementation/WP-0001-耐久控制面基础.md) | 首包事务合同、安全默认、验证和回滚 |
| 实现 | [WP-0002：服务端授权判定核心](08-implementation/WP-0002-服务端授权判定核心.md) | 默认拒绝、RBAC/ABAC、自审、动作级认证和判定审计 |
| 实现 | [WP-0003：冻结提案审核内核](08-implementation/WP-0003-冻结提案审核内核.md) | 冻结风险快照、ReviewerVote、并发 quorum 和唯一终态 |
| 实现 | [WP-0004：确定性风险预检与风险状态基础](08-implementation/WP-0004-确定性风险预检与风险状态基础.md) | 最新 MTM/UPNL、九类事实、七类 scope 与单向风险状态收紧 |
| 实现 | [WP-0005：TradingAuthorization 与 Campaign 授权基础](08-implementation/WP-0005-TradingAuthorization与Campaign授权基础.md) | 人工批准后的冻结授权、Campaign、Initial 与 30/50/100 AddUnit |
| 实现 | [WP-0006：原子风险预留与 Shadow OrderIntent](08-implementation/WP-0006-原子风险预留与ShadowOrderIntent.md) | final precheck、原子 Reservation/Intent、部分/零/Unknown 对账 |
| 实现 | [WP-0007：耐久 CapabilityCertificate 与失效传播](08-implementation/WP-0007-耐久CapabilityCertificate与失效传播.md) | SHADOW 证书/证据事实、精确 scope 校验、单向状态与授权失效传播 |
| 实现 | [WP-0008：耐久 Sender Fencing 与 Shadow Claim](08-implementation/WP-0008-耐久SenderFencing与ShadowClaim.md) | exact-scope sender authority、单调 fencing token、短租约与不可发送 claim |
| 实现 | [WP-0009：耐久 ReconciliationRun 与最新成功门](08-implementation/WP-0009-耐久ReconciliationRun与最新成功门.md) | 七源输入水位、不可变差异/关闭证据、不可逆终态与 latest-success claim gate |
| 实现 | [WP-0010：对账绑定执行事实入口](08-implementation/WP-0010-对账绑定执行事实入口.md) | claim/run/input 绑定、事实来源矩阵、旧 v1 封锁与现有状态机防绕过 |
| 实现 | [WP-0011：继任 Lease 对账接管](08-implementation/WP-0011-继任Lease对账接管.md) | 更高 token 的 current successor 经连续 lineage 收敛旧 claim，保持发送权关闭 |
| 实现 | [WP-0012：Canonical VenueOrder 与 VenueFill 事实](08-implementation/WP-0012-Canonical-VenueOrder与VenueFill事实.md) | 私有场所订单观察、成交全局去重、逐输入 membership、费用和 exact count 门 |
| 实现 | [WP-0013：Canonical Venue Fact 执行强绑定](08-implementation/WP-0013-Canonical-VenueFact执行强绑定.md) | v3 exact fact/link/claim ownership、成交增量推导、订单身份连续和状态/风险原子应用 |
| 实现 | [WP-0014：Canonical VenuePositionSnapshot 事实](08-implementation/WP-0014-Canonical-VenuePositionSnapshot事实.md) | 私有仓位 OPEN/FLAT/UNKNOWN、ONE_WAY/HEDGE、margin/collateral exact scope、跨 run 去重和 exact membership |
| 实现 | [WP-0015：Canonical VenuePosition 执行强绑定](08-implementation/WP-0015-Canonical-VenuePosition执行强绑定.md) | exact position fact/link、post-intent 数量与作用域校验、`POSITION_RECONCILED` 原子推进 |
| 实现 | [WP-0016：Canonical VenueProtectionSnapshot 事实](08-implementation/WP-0016-Canonical-VenueProtectionSnapshot事实.md) | 原生保护 CONFIRMED/DEGRADED/UNKNOWN、仓位强引用、覆盖数量与 replacement 语义 |
| 实现 | [WP-0017：Canonical VenueProtection 执行强绑定](08-implementation/WP-0017-Canonical-VenueProtection执行强绑定.md) | exact protection fact/link、全覆盖 reduce-only 保护、`PROTECTION_CONFIRMED` 原子推进 |
| 实现 | [WP-0018：Canonical VenueAccountEquitySnapshot 事实](08-implementation/WP-0018-Canonical-VenueAccountEquitySnapshot事实.md) | 私有账户权益 CONFIRMED/UNKNOWN、margin/collateral/currency 精确作用域和 balance exact membership |
| 实现 | [WP-0019：可重建当前 Venue 投影](08-implementation/WP-0019-可重建当前Venue投影.md) | 仓位/账户权益当前只读视图、事件时间排序、冲突/Unknown/陈旧遮蔽及可重复重建 |
| 实现 | [WP-0020：不可变托管资金范围与组合 MTM](08-implementation/WP-0020-不可变托管资金范围与组合MTM.md) | 完整受管账户 manifest、EXCHANGE_ONLY USD MTM、跨币种 FX 缺失与整组失败关闭 |
| 实现 | [WP-0021：RiskPrecheck 可信资本强绑定](08-implementation/WP-0021-RiskPrecheck可信资本强绑定.md) | 提案风险事务内资本重算、manifest/projection/source 强绑定和自报数值拒绝 |
| 实现 | [WP-0022：最终 ORDER_PRECHECK 可信资本强绑定](08-implementation/WP-0022-最终ORDER-PRECHECK可信资本强绑定.md) | 授权冻结资金范围、执行前刷新 MTM/UPNL/available margin、保持 Snapshot_0/1R 不变并落库最终证据 |
| 实现 | [WP-0023：最终预检 Durable Exposure 强绑定](08-implementation/WP-0023-最终预检Durable-Exposure强绑定.md) | Ledger 当前态派生 funding/Heat/internal margin/七层 scope、拒绝高低报并保存可复算快照 |
| 实现 | [WP-0024：提案预检 Durable Exposure 强绑定](08-implementation/WP-0024-提案预检Durable-Exposure强绑定.md) | 公共聚合器、组织级并发锁、新初仓零 Heat、提案 funding/margin/scope 真源和不可变 hash |
| 实现 | [WP-0025：风险预留损失组件冻结](08-implementation/WP-0025-风险预留损失组件冻结.md) | reservation 三分量求和约束、snapshot v2、部分成交后五项当前 Trade Loss 可复算 |
| 实现 | [WP-0026：规范化 Base Heat 与 Scope 增量](08-implementation/WP-0026-规范化Base-Heat与Scope增量.md) | v2 风险/意图命令、服务端 base Heat 公式、七层 planned loss 共源和 18 位定点分量守恒 |
| 实现 | [WP-0027：策略绑定 Cost Stress](08-implementation/WP-0027-策略绑定Cost-Stress.md) | v3 风险/意图命令、无默认 cost policy、fee/穿透/不利 funding 规范计算和 18 位保守金额 |
| 实现 | [WP-0028：Canonical 保护触发价事实](08-implementation/WP-0028-Canonical保护触发价事实.md) | 保护 v2 命令、完整集合最差活动触发价、Mark 保护侧双层约束和防数据丢失迁移 |

## 完整交付与能力激活

产品范围一次定义并完整交付，不以试制子集、场所先后或“先做一部分”缩小功能目标。六个工作流可以并行推进，但共同通过以下三道门：

1. **设计完整门**：完整目标、权威边界、领域不变量、接口、风险、资金、场所执行、深浅主题 UI、测试和运营合同互相一致；40 项设计选择保持 `CONFIRMED`，21 项证据问题按 `RESEARCH_REQUIRED` 管理，仅 3 类不可推断的外部事实保持 `OPEN`。
2. **工程完工门**：完整目标的代码、配置、迁移、Web/PWA、Telegram、Binance、Hyperliquid、Freqtrade、风险、资金、审计、监控、恢复和运维资产全部达到完成定义；未获得证书的能力以默认关闭保留，不能用未完成功能冒充安全开关。
3. **能力激活门**：每个交易所、账户、margin mode、标的范围、风险档位、Add 次数、Vault/CTO 能力按独立认证单位取得证据和批准后才获得现实权限；一个单位的证据不得外推到另一个单位。

历史回放、实时影子、场所仿真/测试网和小额实盘只是产生认证证据的环境，不是产品版本或功能分期。工程完工不等于同时开放全部真实资金权限，能力激活也不反向改变已经完整定义的产品目标。

本地敏感值统一保存在 `/Users/vireo/Documents/trading/.env.local`，可提交的空变量模板为 `/Users/vireo/Documents/trading/.env.example`；只记录变量名，不记录值。当前 Telegram 变量为 `TELEGRAM_BOT_TOKEN`，保管、权限、轮换和读取边界以 [SLO、可观测性、故障恢复与 Runbook：本地开发凭据与轮换](06-operations/SLO可观测性故障恢复与Runbook.md#本地开发凭据与轮换) 为唯一操作合同。

## 历史资料规则

`../交易系统 notion 文档/`、`../DynamicPositionSizing-/`、`../low_vol_breakout_bn/` 和 `../仓位计算-新.xlsx` 均为 `REFERENCE`。可以提炼指标、数学、退出行为和失败案例，但不得直接复制以下历史语义进入生产：

- Telegram 逐次确认加仓；
- 保证金乘杠杆直接决定仓位；
- 每轮 YAML 热更新交易参数；
- 策略或脚本直接通过 CCXT 下单；
- 每级固定手数、固定风险份额或未持久化的 Add 状态；
- 旧 Vault/FundPool/FundShare 文档中的人数、时间窗或额度参数。

若历史材料与当前基线冲突，先按 [文档治理与权威矩阵](00-governance/文档治理与权威矩阵.md) 裁决；不得在代码里自行选择看起来更方便的一版。
