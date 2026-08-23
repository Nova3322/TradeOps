# TradeOps

**所有交易行为进入真实账户前的控制层。**

TradeOps 是交易员、交易 Bot 和 AI Agent 与交易所账户之间的开源交易控制层。TradeOps 让交易员、交易 Bot 和 AI Agent 的每笔交易先经过确定性规则、必要审批和完整审计，再发送到交易所。

[English](README.md) · [本地运行](#本地运行) · [文档](docs/README.md) ·
[API 快速接入](docs/API_QUICKSTART.md) · [安全政策](SECURITY.md)

> **当前状态：Alpha，可自托管。** 测试模式与生产模式复用同一套提案、审核、风控、执行、对账和审计流程。生产下单及所有资金能力仍由独立 Gate 控制，并在新安装中默认关闭。

![TradeOps 独立审核队列](artifacts/public/review-queue.png)

## 它解决什么问题

交易所连接器和资深交易员都不能代替权限系统。缺少控制层时：

- 交易员可能选错账户、标的、方向或数量；
- 人工订单可能超过团队批准的风险额度；
- 提交人与审批人可能没有真正分离；
- 人工订单和 Bot 订单可能争抢同一个仓位或亏损预算；
- 管理员可能临时扩大权限，却没有留下完整审批记录；
- 程序重试可能造成重复下单；
- AI Agent 可能提交异常仓位或杠杆请求。

因此，所有能够发起交易的主体都进入同一条控制链。

TradeOps 在交易意图与交易所账户之间建立服务端强制执行的决策边界：

1. 人、交易 Bot 或 AI Agent 提交交易提案。
2. TradeOps 固化关键条款，并检查工作空间、团队、账户、环境、数据时效和已配置的风险限制。
3. 确定性规则选择处理结果：明确规则允许时自动批准、转交独立人工审批，或直接拒绝。当前 Alpha 版本的可执行提案仍全部要求独立人工审批，自动批准尚未启用。
4. 达到独立审核阈值后，系统自动按最新账户事实运行风控，签发短时、账户范围固定的交易授权并预留风险。
5. 受控执行进程通过租约、fencing 和稳定的 client order ID 调用精确账户的 Freqtrade Worker；审核是正常交易流程的最后一个人工节点。
6. Facts Adapter 查询订单、成交与仓位并完成对账；超时或结果不明时只查询原订单，不重复下单。全链路事实写入审计。

```text
Trader / 交易 Bot / 策略程序 / AI Agent
  -> 交易提案
  -> 确定性规则检查
  -> 自动批准 / 人工审批 / 拒绝
  -> 受控执行
  -> 交易所对账
  -> 审计记录
```

## 当前已经实现的控制

- **范围隔离**：工作空间、团队、环境、交易所和账户范围由服务端检查，不信任客户端自行声明。
- **风险政策**：团队、账户和单笔风险上限；连续亏损冷却；禁止加仓、只减仓、暂停和 Kill Switch 状态。
- **独立审核**：版本化提案、批准/拒绝、禁止自审和短时交易授权。
- **防重复执行**：幂等、持久化能力 Gate、超时处理、未知结果恢复、取消、同步和对账。
- **事实缺失即阻断**：缺失、陈旧、丢失、不完整或限流的数据不会被当成实时数据，也不会自动补成零。
- **完整追溯**：操作者、原因、范围、环境、决策、命令和执行结果保留在同一条审计链路中。

## 人、Bot 与 Agent

- 同时进行人工交易并运行多个交易 Bot 的专业交易员。
- 由 Trader、Reviewer、Risk Manager 和 Administrator 组成的小型加密交易团队。
- 做市、套利、量化和自动化交易团队。
- 希望将 AI Agent 接入交易账户、但不授予无限执行权的开发者。

每个 Human、Bot、策略程序和 AI Agent 都应拥有独立身份、权限、额度和审计记录。**Trader** 提交人工或策略辅助提案，**Reviewer** 作出独立决定，**Risk Manager** 设置并处理风险规则，**Administrator** 管理成员与系统范围，但不拥有未记录的临时绕过能力。

API Key 归属于具体用户，动态继承当前 RBAC，并固定在一个工作空间和团队。人工交易和自动化交易都不能绕过规则、审批、执行 Gate 或账户范围。

## 当前集成边界

| 集成 | 当前状态 |
| --- | --- |
| Binance | 已有测试与生产账户事实、下单/取消、恢复和对账适配路径；凭据及生产 Gate 需按部署配置。 |
| Hyperliquid | 已有测试与生产账户事实、下单/取消、恢复和对账适配路径；凭据及生产 Gate 需按部署配置。 |
| Freqtrade | 已作为唯一交易策略/机器人生命周期引擎接入币安与 Hyperliquid 精确账户 Worker；TradeOps 保留提案、审核、风控、授权和审计边界。 |
| Perptape | 支持配置信号接入、时效检查、标准化和创建提案前重检。 |
| 签名 Webhook | 支持签名、Nonce、防重放、时效、幂等和载荷校验。 |
| Telegram | 支持团队通知路由和独立通知 Worker。 |
| Bot / AI Agent API | 用户所属 API Key、提案型接入和服务端 RBAC/范围检查。 |
| Vault / Safe | 已有生产资金路径配置；签名、广播和资金划转必须单独配置并启用。 |

仓库还包含可选通知渠道适配器。Hummingbot、NautilusTrader 和 QuantConnect LEAN 的正式引擎契约仍属于路线图，不作为当前开箱即用集成宣传。

## 本地运行

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/)、Docker 与 Docker Compose。

```bash
git clone https://github.com/Nova3322/TradeOps.git
cd TradeOps
cp .env.example .env.local
export TRADING_LOCAL_ADMIN_USERNAME=trading-admin
uv sync --frozen
./scripts/run_local.sh
```

打开 <http://127.0.0.1:8014>。本地密码保存在 `.local/passwords/trading-admin`，权限为 `0600`；`.local/` 不会进入 Git。外部集成和危险能力 Gate 默认关闭。

需要同时启动只读同步 Worker 时：

```bash
TRADING_PUBLIC_PORT=8022 ./scripts/run_compose.sh --runtime
```

`--runtime` 仅开启只读事实同步，不会开启下单、资金划转、钱包签名、广播或自动化。

```bash
curl http://127.0.0.1:8014/health/live
curl http://127.0.0.1:8014/health/ready
open http://127.0.0.1:8014/openapi.json
```

Bot/API 接入见 [`docs/API_QUICKSTART.md`](docs/API_QUICKSTART.md)。运行中的 `/openapi.json` 是完整接口契约。

## 产品界面

以下截图使用本地样本数据展示当前控制台流程，不代表生产就绪状态，也不构成收益展示。

<table>
  <tr>
    <td><img src="artifacts/public/opportunity-snapshot.png" alt="Perptape 实时机会"></td>
    <td><img src="artifacts/public/webhook-signals.png" alt="经过校验的 Webhook 信号"></td>
  </tr>
  <tr>
    <td><img src="artifacts/public/current-proposals.png" alt="当前交易提案"></td>
    <td><img src="artifacts/public/capital-center.png" alt="生产资金中心"></td>
  </tr>
</table>

## TradeOps 不是什么

TradeOps 不是行情终端、K 线工具、策略回测平台、跟单服务、喊单平台、自动赚钱机器人、资产托管平台、基金管理系统或投资顾问。它不承诺盈利、保本，也不承诺避免全部运营或交易损失。

交易终端解决“在哪里交易”，执行算法优化“怎样成交”，钱包权限系统控制“谁能转移资金”。TradeOps 解决的是“这笔交易是否允许执行、需要谁批准、违反了什么规则，以及交易所最终发生了什么”。

它不会被描述为 Talos、Fireblocks、Elwood 或基金 PMS 的完整替代品。相邻产品边界见 [`docs/COMPETITIVE_POSITIONING.md`](docs/COMPETITIVE_POSITIONING.md)。

## 部署与信任边界

TradeOps 开源并支持自托管。PostgreSQL 保存身份、岗位、提案、审核、授权、Gate、回执和审计状态。交易所凭据加密保存，提交后不会由 API 回显；交易本身不需要提现权限。

当前仓库没有交付一个独立封装的“本地执行 Agent”，也不能据此声称远程控制平面在技术上永远无法绕过本地硬限制。这仍是设计方向，而非当前保证。真实资金生产就绪取决于具体部署。

新安装中以下持久化能力保持 `DISABLED`：

- `AUTO_ADD`
- `AUTO_OPERATING_REFILL`
- `AUTO_PROFIT_SWEEP`
- `CAPITAL_TRANSFER`
- `LIVE_ORDER_SEND`

页面按钮、岗位名称、环境标签、进程健康或 AI 输出不能代替服务端授权。配置真实账户前，请阅读 [`SECURITY.md`](SECURITY.md) 和 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 项目与文档

TradeOps 当前为 pre-1.0。部署方仍需独立完成威胁建模、供应商配置、监控、备份、事件响应、法律义务和真实资金验收。

- [文档首页](docs/README.md)
- [运营控制台](docs/OPERATIONS_CONSOLE.zh-CN.md)
- [架构与安全不变量](docs/ARCHITECTURE.md)
- [路线图](ROADMAP.md)
- [参与贡献](CONTRIBUTING.md)
- [安全政策](SECURITY.md)
- [支持](SUPPORT.md)

## 许可证

```text
GPL-3.0-only OR LicenseRef-TradingOPS-Commercial-1.0
```

GPL 允许商业使用、修改和分发，但传播受覆盖作品时需要遵守 GPLv3。需要闭源集成、专有分发或协商条款的用户可以选择单独商业许可证。

版权所有者及商业许可：`Nova3322` · `165258092+Nova3322@users.noreply.github.com`。安全问题请通过 `165258092+Nova3322@users.noreply.github.com` 或仓库的私有安全公告渠道报告。

详见 [`LICENSE`](LICENSE)、[GPLv3 正文](LICENSES/GPL-3.0-only.txt)、[商业许可证说明](LICENSES/LicenseRef-TradingOPS-Commercial-1.0.txt) 和 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
