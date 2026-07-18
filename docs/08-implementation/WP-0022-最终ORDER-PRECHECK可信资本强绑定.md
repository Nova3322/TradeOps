# WP-0022：最终 ORDER_PRECHECK 可信资本强绑定

## 1. 交付目标

WP-0022 关闭 `ExecutionIntentService` 在最终增险预检中继续信任请求内当前资本的缺口。系统在创建
SHADOW `OrderIntent` 和原子风险预留前，使用授权时冻结的 exact managed-capital manifest，在同一
PostgreSQL 事务中重新构建最新 `portfolio-mtm-v2` 投影。

本包追踪 `REQ-RISK-005` 以及权威规则 3、6、13、15：最终预检必须使用最新 UPNL/MTM，同时
`Total Capital Snapshot_0`、1R 和 Funding Envelope 不得因行情或资本变化被静默重写。

## 2. 冻结范围，不冻结当前权益

冻结提案的 `risk_summary.capital_projection_binding` 现在是授权签发必填事实：

```text
manifest_id
manifest_version
manifest_hash
projection_version
```

`TradingAuthorizationService` 在签发时验证该合同并保存到已哈希、不可变的
`issuance_snapshot.capital_projection_binding`。缺少 binding 的旧提案返回
`CAPITAL_PROJECTION_BINDING_INVALID`，不存在无范围授权的兼容回退。

最终预检要求请求 binding 与授权快照逐项相同；任何替换返回
`FROZEN_CAPITAL_SCOPE_BINDING_MISMATCH`。因此执行阶段不能临时加入另一个账户全集来扩大资本或
可用保证金。

## 3. 最终预检资本语义

`CapitalProjectionResolver` 在风险政策 `FactType.ACCOUNT.max_age_ms` 边界内重算同一 manifest。
执行阶段派生：

```text
Current Portfolio MTM Equity(t) = latest confirmed portfolio projection
Current UPNL(t)                = latest confirmed portfolio projection
Available Margin(t)            = Σ latest exact-account available margin
Exchange Risk Equity(t)        = max(0, min(settled equity, margin equity))

Total Capital Snapshot_0       = frozen TradingAuthorization value
1R_0                           = Total Capital Snapshot_0 × 0.5%
```

调用方提交的资本字段必须与上述结果完全一致。当前 MTM 下跌会收紧 `Dynamic Mode Cap`；当前 MTM
上涨也不能扩大冻结 1R、档位上限、Funding Envelope 或 Add 数量。测试明确验证：冻结资本
100,000、1R=500 时，最新 MTM 降至 90,000 后动态低风险上限为 450，而 1R 仍为 500。

## 4. 事务顺序与失败关闭

最终增险事务顺序为：

1. 锁定 Campaign、授权状态、风险 scope 与竞争执行资源；
2. 验证授权、提案和 frozen capital binding；
3. 加载固定风险政策并按政策 freshness 重算 canonical capital；
4. 验证 durable funding/heat/scope/margin exposure 不得被低报；
5. 重新校验证书并运行确定性 Risk Engine；
6. 只有 ALLOW 才原子写入执行风险决策、OrderIntent、风险预留、Ledger 和状态历史。

capital resolver 的 `missing`、`Unknown`、`stale`、FX 缺失、账户不属于 manifest、binding 错误或
数值不一致沿用 WP-0021 稳定错误，并在风险数学、执行决策和预留之前拒绝。时钟不是 timezone-aware
时返回 `EXECUTION_CLOCK_INVALID`。

风险数学产生的确定性 DENY 仍保存完整 verified projection；resolver 自身无法形成可信投影时不写
看似有效的 `ExecutionRiskDecision`。

## 5. 不可变执行证据

每个 ALLOW/DENY `execution_risk_decisions` 新增：

```text
capital_scope_manifest_id
capital_scope_manifest_version
capital_scope_manifest_hash
capital_projection_version
capital_projection_hash
```

数据库用 manifest ID + organization + version 外键以及 hash/version check 保护绑定。完整投影、每个
canonical source snapshot ID/hash、facts-as-of、age 和 projection hash 保存在已哈希
`input_snapshot` 中；原有 immutable trigger 禁止修改这些字段。

命令结果、领域事件和现有 `trading_execution_risk_decisions_total` / portfolio projection 指标可关联
`capital_projection_hash`。resolver 失败同时留下耐久命令拒绝审计，投影指标记录 freshness/state。

## 6. 迁移与回滚

迁移 `20260718_0022` 增加五个非空绑定字段、check 和 manifest 外键。它不伪造旧执行决策的资本
来源：升级前若存在 legacy `execution_risk_decisions`，迁移明确失败，要求按运营流程导出并处理
SHADOW 测试事实。仍有绑定执行决策时也拒绝 downgrade。

空表回滚路径已实际验证：

```text
0022 -> 0021 -> 0022
```

## 7. 明确未完成范围

- funding used/reserved、open/reserved/unknown heat 与各 scope exposure 在本包交付时仍依赖上游申报
  加 durable 不得低报检查；最终预检的该缺口已由
  [WP-0023](WP-0023-最终预检Durable-Exposure强绑定.md) 关闭；
- 非 USD FX、stablecoin/depeg 认证事实尚未实现，相关交易继续失败关闭；
- canonical 账户权益仍只由自动化测试构造，没有真实私有 venue collector 或已认证场所公式；
- 没有真钱证书、真实 Freqtrade/VenueAdapter、Web/PWA、Telegram、Margin、Vault/CTO 或生产运维；
- `LIVE_ORDER_SEND`、`CAPITAL_TRANSFER`、`AUTO_ADD` 继续为 `DISABLED`。

按用户特别约束，Codex Security 及其审计 Skill、插件和模块保持停用；本包只使用常规架构设计、
数据库约束、静态检查和自动化测试。
