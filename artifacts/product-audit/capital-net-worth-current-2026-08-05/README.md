# 资金净值看板当前验收（2026-08-05）

## 当前生产事实

- Binance：当前只读净值约 9.9678 USD，最后更新时间 2026-08-05 12:46（Asia/Kuala_Lumpur）。
- Hyperliquid：当前只读净值约 9.9291 USD，最后更新时间 2026-08-05 12:39；同步频率显著低于 Binance，但仍在 30 分钟有效窗口内。
- Vault：没有生产资金事实，因此当前三方汇总必须阻断；页面保留 Binance 与 Hyperliquid 单项曲线。
- 最近 6 小时 Binance 最大单次只读波动约 -0.00195 USD（-0.0196%）；旧图的“末端突然下跌”是纵轴过度放大与密集点压缩造成的视觉误判，不是资金被补零或突然转出。

## 页面行为

- 固定且仅展示 Binance、Hyperliquid、Vault、三方汇总四条筛选项。
- 总额不可计算时显示“当前不可汇总”，不再显示“— USD”；单项金额统一为小额四位、大额两位。
- 三方汇总只使用 60 秒内对齐的事实。缺失、过期、时间错位和断档均不补零、不强连，并显示缺少来源、最后有效时间和影响。
- 图表提供明确时间轴、USD 刻度、悬停金额、最近变化和异常变化说明；纵轴保留至少 0.5% 观察范围，避免微小波动被夸大。
- 桌面图例为四列紧凑筛选；390 px 与 430 px 使用两列图例和单列净值卡，无横向溢出。

## 当前运行验收

- `01-desktop.png`：桌面总额、三方位置、四条筛选、时间轴、USD 刻度、断档及最近变化。
- `02-390.png`：390 px 全页；`innerWidth=390`、`scrollWidth=390`、图表宽 338 px。
- `03-430.png`：430 px 全页；`innerWidth=430`、`scrollWidth=430`、图表宽 378 px。
- `/health/live` 与 `/health/ready` 均通过；本地服务继续运行在 8014。

## 测试与安全

- `tests/api/test_health.py`：19 passed。
- `tests/integration/test_m7_capital_center.py`：10 passed；覆盖完整、缺失、过期、时间错位、权限、时间对齐和历史保留。
- `node --check`、`git diff --check` 通过。
- AUTO_ADD、AUTO_OPERATING_REFILL、AUTO_PROFIT_SWEEP、CAPITAL_TRANSFER、LIVE_ORDER_SEND 全部为 DISABLED；未执行订单、资金划转、签名或广播。
