# TradingOPS API Key 快速接入

完整接口合同以运行中的 [`/openapi.json`](/openapi.json) 为唯一真源。

## 身份与权限

API Key 是用户凭证，只绑定一个 Workspace / Team 上下文，不保存独立角色，
也不设置独立 Account / Venue Scope。每次请求都使用所属用户当前 RBAC 权限；
资源接口在请求时逐项校验精确 Team、Account 与 Venue。

从 **右上角用户菜单 → API Key** 创建凭证。明文只在创建或轮换成功时显示一次，
应存入秘密管理器，不得写入源码、Prompt、截图、聊天或普通日志。

```bash
export BASE_URL="BASE_URL"
export API_KEY="API_KEY"
export WORKSPACE_ID="WORKSPACE_ID"
export TEAM_ID="TEAM_ID"

curl --fail-with-body \
  --header 'Authorization: Bearer API_KEY' \
  --header 'Accept: application/json' \
  'BASE_URL/api/api-key/connection'
```

成功条件：HTTP 200、`connected=true`、Workspace / Team 与预期一致，且
`scope.scope_model=USER_RBAC`。Bearer API Key 与登录 Cookie 不得混用。
旧 `/api/api-client/connection` 路径及旧响应字段别名继续保留用于兼容。

## 读取、写入与环境边界

默认从 `/api/instruments`、`/api/opportunities`、`/api/proposals`、
`/api/campaigns`、`/api/results`、`/api/audit` 等只读接口开始。必须保留环境、
来源和时效字段；缺失、过期、丢失、不完整或限流数据既不是实时数据，也不是 0。

当前执行环境只有 `TESTNET` 与 `LIVE`；`SETUP` 是团队尚未完成配置时的内部状态，
不能作为提案或订单环境。服务端从团队持久化的当前模式确定新提案、授权、订单意图
和执行环境；客户端传入冲突环境时会失败关闭，而不是把它当作路由参数。

`LIVE` 只表示生产环境，不代表执行能力已开启。写操作必须同时通过所属用户当前权限、
精确资源授权、幂等、独立审核、风控和所有服务端 Gate。`LIVE_ORDER_SEND`、
`CAPITAL_TRANSFER`、`SIGNING`、`BROADCAST` 保持关闭，除非另行通过受治理流程启用。

API Key 生命周期操作只允许交互式用户会话执行。凭证可停用、轮换或永久撤销；
所属用户停用、成员关系或 RBAC 收紧会在下一次请求立即生效。
