# 风险与目标页面验收（2026-08-04）

## 结果

- P0：纯 `REVIEWER` 原本可以通过前端路由，但页面先读取未授权的交易任务 API，整页最终显示权限错误。现已让风险恢复工作区不依赖未授权交易任务数据；审核员可以查看恢复条件和独立审核状态，但仍看不到未授权交易任务。
- P1：正常状态默认收起十项实时恢复条件；真正受限或有阻塞时仍自动展开，不影响 fail-closed 诊断。
- P1：当前恢复待办只展示 `PENDING_REVIEW` / `APPROVED`，过期和被后续控制状态替代的申请进入折叠历史，不再冒充当前待办。
- P1：风险页只展示运行中的交易任务；已关闭任务不再占据当前风险工作区。
- P2：政策、Gate、申请和审核记录优先显示用户名；无法映射的历史系统标识安全回退，不抛出异常。
- P2：`AUTO_ADD` 已关闭时，紧急入口明确显示“自动加仓已关闭”并禁用；“暂停所有新增风险”仍是只收紧入口。

## 身份与路由验收

| 身份 | 实际结果 |
| --- | --- |
| `kelly_oooo` / SYSTEM_ADMIN | 完整风险控制事实；当前正常时不显示恢复表单；保留只收紧入口 |
| `local-reviewer-two` / REVIEWER | 可进入风险恢复工作区；无待审申请时显示明确原因；无系统管理员入口 |
| `qa-observer` / OBSERVER | 可查看获准的风险状态；无收紧或恢复操作 |
| `local-proposer` / PROPOSER | 风险路由拒绝 |
| `qa-treasury` / TREASURY_ADMIN | 风险路由拒绝 |
| `qa-disabled` | 登录拒绝，不泄露页面数据 |

## 验证证据

- `01-risk-before.png`：修复前页面。
- `02-risk-after.jpg`：修复后完整页面。
- `03-risk-viewport.jpg`：修复后桌面视口。
- 聚焦回归：56 passed。
- 完整回归：515 passed in 204.69s。
- 静态检查：Ruff、`node --check`、`git diff --check` 全部通过。
- 运行检查：`/health/live` 与 `/health/ready` 均通过；本地 8014 服务保持运行。
- 危险 Gate：`LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD`、`AUTO_PROFIT_SWEEP`、`AUTO_OPERATING_REFILL` 均为 `DISABLED`。

## 外部状态

- 本批不执行任何订单、资金划转、签名或广播。
- 启动时 Hyperliquid 只读解析曾返回 `HYPERLIQUID_RATE_LIMITED`，按只读失败类别记录；不影响本批本地风险权限与页面验收，也没有放开任何交易能力。
