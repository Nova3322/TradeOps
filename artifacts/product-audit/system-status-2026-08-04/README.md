# 系统状态与交易账户验收

## P0

- Hyperliquid API 钱包所属主账户解析不再只发生于进程启动。启动时遇到临时限流后，后续只读轮次会继续做有界重试；解析成功后缓存账户范围。
- 限流、网络失败、鉴权失败和配置不完整保持不同错误类别；旧账户快照不会被标成实时事实。
- Binance、Hyperliquid 与 Freqtrade 的写入、下单能力未因只读连接恢复而启用。

## P1

- 系统状态不再把“控制台可访问”表述为“交易执行就绪”；Freqtrade worker 未启动时明确显示所缺控制凭据、dry-run 下一步和关闭的 LIVE_ORDER_SEND。
- 交易账户只把非零仓位和非终态订单放在“当前”区域；终态订单进入折叠历史记录。
- 连接受限时，权益、仓位、订单和成交明确标记为最后快照，并显示稳定错误代码。
- REVIEWER 不再看到实际无法加载的系统状态入口；OBSERVER 和 SYSTEM_ADMIN 保留只读状态与账户页面。

## P2

- 外部连接原因和下一步使用中文可操作文案；机器错误码使用 `translate=no` 保持原值，避免被界面翻译改写。

## 验证证据

- 定向测试：52 passed。
- 全量测试：517 passed。
- 静态检查：Ruff、JavaScript 语法、`git diff --check` 通过。
- 六身份 API：SYSTEM_ADMIN 可访问成员、系统状态和交易账户；PROPOSER、REVIEWER、TREASURY_ADMIN 均被成员 API 拒绝；OBSERVER 可读系统状态和交易账户；停用账号登录被拒绝。
- 六身份页面：成员入口仅 SYSTEM_ADMIN 可见；PROPOSER、REVIEWER、TREASURY_ADMIN 不显示无权入口；OBSERVER 可读系统状态和交易账户；停用账号显示明确拒绝。
- 运行中曾观察到 Hyperliquid 从限流恢复为只读已连接；再次限流时页面切换为上游限流和最后快照，而不是错误的未配置。
- 本地数据库五个危险 Gate 均为 `DISABLED`。

## 外部限制

- Hyperliquid Info API 仍可能间歇限流；系统按有界退避重试并诚实投影状态。
- NoTilt 生产 Vault 范围仍不完整；页面保持配置阻断，未伪造可用。
- Freqtrade 控制凭据未加载，worker 未由控制台接管；真实下单保持关闭。
