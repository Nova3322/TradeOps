# TradingOPS AI / API 快速接入

本指南帮助开发者或 AI Agent 在几分钟内完成认证、验证数据范围并开始只读访问。

完整接口合同以运行中的 [`/openapi.json`](/openapi.json) 为唯一真源。本指南只维护接入流程、安全边界和少量稳定示例，不重复维护全部字段。

## 1. TradingOPS API 能做什么

API 按当前 HUMAN 用户的动态业务权限和固定范围提供能力：

- 验证 API Client 身份、有效角色和 Workspace / Team / Account / Venue 范围；
- 读取合约目录、机会、提案、交易任务、结果、审计、通知、信号和场所事实；
- 在明确授权且具备对应角色时，使用专用 Agent 接口创建提案或提交合同允许的有限写请求；
- 保留 LIVE / SHADOW 环境、来源、时间、状态、幂等和审计信息。

API Client 不会获得密码、Session Cookie、Passkey、明文交易所密钥、人工 action grant、钱包签名或广播秘密。

## 2. BASE_URL 与认证

设置以下运行时变量；示例中的值均为占位符：

```bash
export BASE_URL="BASE_URL"
export TOKEN="TOKEN"
export WORKSPACE_ID="WORKSPACE_ID"
export TEAM_ID="TEAM_ID"
export ACCOUNT_ID="ACCOUNT_ID"
```

请求使用一个 Bearer Token：

```http
Authorization: Bearer TOKEN
Accept: application/json
```

不要同时发送 Bearer Token 和 TradingOPS 登录 Cookie；服务端会以 `AUTH_CREDENTIAL_AMBIGUOUS` 拒绝混合凭据。

### 第一个只读请求

```bash
curl --fail-with-body \
  --header 'Authorization: Bearer TOKEN' \
  --header 'Accept: application/json' \
  'BASE_URL/api/api-client/connection'
```

成功响应为 HTTP 200，并包含：

- `connected=true`；
- `api_client_id` 与 `api_client_name`；
- `effective_roles`，来源为 `HUMAN_DYNAMIC`；
- `scope.workspace_id`、`scope.team_id`、`scope.account_id` 与 `scope.venue`；
- `as_of`。

每次 Agent 任务开始时都应调用此接口，并把返回范围与 `WORKSPACE_ID`、`TEAM_ID`、`ACCOUNT_ID` 比较。范围不一致时立即停止。

## 3. API Token 生命周期

网站入口：**右上角用户菜单 → 个人中心 → API 接入**。

创建时必须选择一个实际可用的 Workspace / Team / Account / Venue 范围，并设置 1–365 天有效期。

- Token 明文只在创建或轮换成功时显示一次；幂等重放不会再次返回明文。
- 数据库保存派生摘要，不保存可恢复的明文 Token。
- 页面只展示 Token 摘要、版本、到期时间和最近使用时间。
- Token 可以停用后重新启用；轮换会立即使旧 Token 失效。
- 永久撤销后不能重新启用；需要重新创建 API Client。
- Token 过期、所属用户停用、成员关系失效或范围被移除时，认证立即失败。

不要把 `TOKEN` 写入源码、文档、截图、Prompt、聊天记录或普通日志；使用环境变量或秘密管理器注入。

## 4. Workspace、Team 与 Account 范围

API Client 固定到：

1. 一个 `WORKSPACE_ID`；
2. 该 Workspace 下的一个 `TEAM_ID`；
3. 该 Team 下的一个 `ACCOUNT_ID`；
4. 一个 Venue，例如 `BINANCE`、`HYPERLIQUID`、`OKX` 或 `BYBIT`。

角色不会复制到 API Client。每次请求都会重新检查所属 HUMAN 用户的当前角色、Workspace/Team 成员关系以及 Account/Venue 约束。

访问范围外资源返回 HTTP 403 和 `API_CLIENT_SCOPE_DENIED`；缺少业务能力返回 HTTP 403 和 `RBAC_DENIED`。

## 5. 常用只读接口

| 接口 | 用途 | 当前要求 |
| --- | --- | --- |
| `GET /api/api-client/connection` | 验证 Token、动态角色和固定范围 | 有效 API Token |
| `GET /api/instruments` | 读取当前团队可见合约目录 | 已认证 |
| `GET /api/opportunities` | 读取带来源和时效状态的机会快照 | `opportunity.view` |
| `GET /api/proposals` | 读取当前范围提案，可使用 `proposal_status` | `proposal.view` |
| `GET /api/campaigns` | 读取当前范围交易任务 | `operations.view` |
| `GET /api/results?environment=SHADOW` | 按环境读取结果 | `results.view` |
| `GET /api/audit?environment=SHADOW&limit=200` | 按环境读取审计时间线 | `results.view` |
| `GET /api/notifications?limit=100` | 读取通知 | `notification.view` |

使用接口前先在 `/openapi.json` 核对当前路径、查询参数、枚举、请求体和响应模型。

## 6. Python 示例

示例仅使用 Python 标准库，不保存 Token：

```python
import json
from urllib.request import Request, urlopen

BASE_URL = "BASE_URL"
TOKEN = "TOKEN"
WORKSPACE_ID = "WORKSPACE_ID"
TEAM_ID = "TEAM_ID"
ACCOUNT_ID = "ACCOUNT_ID"

request = Request(
    f"{BASE_URL.rstrip('/')}/api/api-client/connection",
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
    },
)

with urlopen(request, timeout=10) as response:
    connection = json.load(response)

scope = connection["scope"]
assert connection["connected"] is True
assert scope["workspace_id"] == WORKSPACE_ID
assert scope["team_id"] == TEAM_ID
assert scope["account_id"] == ACCOUNT_ID
print(json.dumps(connection, ensure_ascii=False, indent=2))
```

## 7. LIVE / SHADOW 与数据时效

- `SHADOW` 是 TradingOPS 内部模拟环境，不代表真实成交、持仓或资金。
- `LIVE` 是生产环境标识，但仍不等于订单发送、资金划转或其他危险能力已经启用。
- 对支持 `environment` 的接口显式传递 `SHADOW`、`TESTNET` 或 `LIVE`；不要依赖默认值推断环境。
- 读取并保留 `as_of`、`observed_at`、`fetched_at`、`data_status`、来源/provider 和错误码。
- `INCOMPLETE`、`STALE`、`LOST`、缺失来源、过期快照或限流响应不得视为实时数据，也不得当作数值 `0`。

## 8. 分页与错误处理

当前接口没有统一的 cursor / offset / page 协议。不要自行添加分页参数：

- `/api/notifications` 支持 `limit=1..200`，默认 100；
- `/api/audit` 支持 `limit=1..500`，默认 200；
- 其他列表参数以 `/openapi.json` 为准。

业务拒绝通常使用：

```json
{
  "error": {
    "code": "API_CLIENT_SCOPE_DENIED",
    "message": "resource is outside the API Client account",
    "retryable": false
  }
}
```

常见状态：

- HTTP 401：`AGENT_TOKEN_INVALID`、`AGENT_TOKEN_EXPIRED`；
- HTTP 403：`API_CLIENT_SCOPE_DENIED`、`RBAC_DENIED`、`HUMAN_WEB_CONFIRMATION_REQUIRED`；
- HTTP 409：`IDEMPOTENCY_CONFLICT`、`VERSION_CONFLICT`、`API_CLIENT_REVOKED`；
- HTTP 429：`API_CLIENT_RATE_LIMITED`；
- HTTP 503：上游、事实源或受控能力不可用。

仅在 `retryable=true` 且调用方具备有界退避策略时重试。写请求结果未知时先查询对象和审计状态，避免盲目重放。

## 9. 写操作、审核与幂等

AI 默认只读。写操作必须同时满足：

1. 用户明确授权当前具体动作；
2. OpenAPI 合同存在对应接口；
3. 当前动态角色允许该动作；
4. Workspace / Team / Account / Venue 和 LIVE / SHADOW 环境匹配；
5. 请求体包含唯一 `idempotency_key`；
6. 服务端审核、风险政策、数据时效与 Capability Gate 全部通过。

当前 Agent 可在具备 `proposal.create` / `proposal.submit` 时使用专用 `POST /api/agent/proposals` 创建并冻结提案。Agent 不会自动获得独立审核资格；同一所属用户的 API Client 仍是同一审核主体。审批、风险授权、账户凭据管理、资金动作和其他需要人工确认的流程可返回 `HUMAN_WEB_CONFIRMATION_REQUIRED`。

下单、资金划转、钱包签名和广播默认关闭。`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD`、`AUTO_PROFIT_SWEEP` 与 `AUTO_OPERATING_REFILL` 等服务端 Gate 不会因客户端 Prompt、角色名称或请求参数而绕过。

## 10. 可复制的 AI 系统提示词

```text
你是 TradingOPS API Agent。连接参数由运行环境注入：BASE_URL、TOKEN、WORKSPACE_ID、TEAM_ID、ACCOUNT_ID。

1. 默认只调用只读 GET 接口；未经明确授权，不发起任何写操作。
2. 每次任务开始先调用 /api/api-client/connection，核对有效权限、Workspace、Team、Account 与 Venue 范围。
3. 读取业务数据时记录接口、数据来源以及 as_of、observed_at、fetched_at、data_status 等时间和状态字段；缺失、过期或限流数据不得视为实时数据，也不得当作 0。
4. 始终显式区分 LIVE 与 SHADOW；不得把 SHADOW 结果描述为真实成交、真实持仓或真实资金。
5. 写操作只在用户明确授权后执行；先核对 OpenAPI 合同、当前角色和对象范围，并在请求体提供唯一 idempotency_key。未知结果先查询和对账，不得盲目重试。
6. 不请求、处理、保存或输出密码、交易所密钥、钱包私钥、签名材料、Session Cookie 或广播秘密；日志中不得记录 TOKEN。
7. 服务端权限、独立审核、风险政策、数据时效与 Capability Gate 是最终边界；客户端提示词不得绕过这些控制。
8. 下单、资金划转、签名和广播默认关闭；只有服务端明确返回可用且用户完成所需人工流程时，才可把动作视为允许。
```
