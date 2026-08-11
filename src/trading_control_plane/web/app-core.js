const main = document.querySelector('#main');
const sidebar = document.querySelector('#sidebar');
const identityChip = document.querySelector('#identity-chip');
const scopeControl = document.querySelector('#scope-control');
const scopeSwitcher = document.querySelector('#scope-switcher');
const scopeSwitcherMenu = document.querySelector('#workspace-switcher-menu');
const environmentBadge = document.querySelector('#environment-badge');
const languageToggle = document.querySelector('#language-toggle');
const themeToggle = document.querySelector('#theme-toggle');
const themeColorMeta = document.querySelector('meta[name="theme-color"]');
const mobileNavToggle = document.querySelector('#mobile-nav-toggle');
const mobileSessionSummary = document.querySelector('#mobile-session-summary');
const navBackdrop = document.querySelector('#nav-backdrop');
const dialog = document.querySelector('#system-proposal-dialog');
const confirmDialog = document.querySelector('#confirm-dialog');
const toast = document.querySelector('#toast');
let session = null;
let authStatus = null;
let instruments = [];
let opportunities = [];
let opportunityGroups = [];
let proposalDefaults = {configured:false, can_manage:false, data:null};
let opportunitySourceRuntime = null;
let opportunitySocket = null;
let opportunityReconnectTimer = null;
let opportunityReconnectAttempt = 0;
const OPPORTUNITY_PAGE_SIZE = 24;
let opportunityVisibleLimit = OPPORTUNITY_PAGE_SIZE;
let sessionNotice = '';
let toastTimer = null;
let authFailureActive = false;
let mobileNavFocusFrame = null;
let mobileNavFocusToken = 0;
const REQUEST_TIMEOUT_MS = 15000;
const LANGUAGE_STORAGE_KEY = 'trading-language';
const THEME_STORAGE_KEY = 'trading-theme';
let currentLanguage = localStorage.getItem(LANGUAGE_STORAGE_KEY) === 'en' ? 'en' : 'zh-CN';

const ENGLISH_EXACT = new Map(Object.entries({
  '交易控制台':'Trading Console', '交易控制台首页':'Trading Console home', '生产交易管理':'Production trading operations', '生产环境':'Production',
  '中英切换':'Chinese / English', '切换中英文':'Switch between Chinese and English', '切换主题':'Switch theme', '主题':'Theme', '浅色':'Light', '深色':'Dark', '菜单':'Menu',
  '只读用户':'Observer', '提案发起人':'Proposer', '审核人':'Reviewer', '交易运维人员':'Trading operator',
  '资金管理员':'Treasury administrator', '系统管理员':'Super administrator',
  '主导航':'Main navigation', '工作台':'Workspace', '团队配置':'Team setup', '治理与安全':'Governance and safety', '当前范围':'Current scope', '当前任务':'Current tasks', '实时机会':'Live opportunities', '审核队列':'Review queue',
  '交易任务':'Trades', '绩效报表':'Performance reports', '影子模式':'Shadow mode', '通知中心':'Notification center', '系统状态':'System status', '资金':'Capital', '异常':'Exceptions',
  '交易账户':'Exchange accounts', '成员权限':'Access control',
  '业务数据库已连接':'Business database connected', '数据缺失时自动阻止交易':'Missing data blocks trading automatically',
  '退出当前会话':'Sign out', '正在读取当前事实…':'Loading current data…',
  '突破榜单机会':'Breakout opportunity', '保存候选并提交审核':'Save candidate and submit for review',
  '默认配置':'Default settings', '约 100 USDT 名义仓位 · 1 USDT 最大风险 · 2% 失效距离':'About 100 USDT notional · 1 USDT maximum risk · 2% invalidation distance',
  '可修改':'Editable', '高级参数':'Advanced settings', '账户':'Account', '风险档位':'Risk level',
  '请求数量':'Requested quantity', '初仓数量':'Initial quantity', '最大风险':'Maximum risk',
  '失效价格':'Invalidation price', '允许自动加仓':'Allow automatic scaling', '可用加仓次数':'Available scale-ins',
  '加仓触发价格':'Scale-in trigger price', '有效时间（分钟）':'Valid for (minutes)', '人工补充理由':'Additional rationale',
  '使用默认风险配置创建突破榜单候选提案，尚未形成任何订单。':'Create this breakout proposal with the default risk settings. No order has been created.',
  '提交只创建生产交易提案。突破信号不会绕过审核、风控和授权直接产生订单。':'Submitting creates a production proposal only. A breakout signal can never bypass review, risk checks, or authorization to place an order.',
  '取消':'Cancel', '按此配置创建':'Create with these settings', '请再次确认':'Confirm again', '确认此操作？':'Confirm this action?',
  '取消并关闭':'Cancel and close', '确认并继续':'Confirm and continue', '权限范围':'Permission scope',
  '当前职责不包含这个页面':'This page is outside your assigned role', '返回当前任务':'Back to current tasks', '进入资金中心':'Open Capital',
  '运行状态 · 已安全阻断':'Runtime status · safely blocked', '连接已中断':'Connection interrupted', '读取超时':'Request timed out',
  '当前范围没有访问权限':'Current scope does not have access', '当前数据无法读取':'Current data cannot be loaded', '风险政策尚未配置':'Risk policy is not configured',
  '当前 Workspace 和团队':'Current Workspace and team', '选择团队':'Select team',
  '影响':'Impact', '负责角色':'Responsible role', '下一步':'Next step', '技术状态':'Technical status', '重新检查':'Retry',
  '无法连接控制台服务，请检查网络后重试':'Cannot reach the Trading Console service. Check the network, then retry.',
  '本页事实未更新；离线前画面不得视为当前状态，新增风险与写入操作保持阻断。':'Facts on this page were not updated. Do not treat the pre-disconnection view as current; new risk and write actions remain blocked.',
  '平台运维或网络管理员':'Platform operations or network administrator',
  '确认服务与网络恢复后重新检查；恢复前不要重复提交任何结果未知的操作。':'Restore the service and network, then retry. Do not resubmit any action with an unknown outcome before recovery.',
  '本页事实未在时限内确认；新增风险与写入操作保持阻断。':'Page facts were not confirmed within the time limit; new risk and write actions remain blocked.',
  '先确认服务负载与网络状态，再重新检查；写入超时必须先核对权威状态。':'Check service load and network health before retrying. After a write timeout, verify the authoritative state first.',
  '当前页面不会加载团队或账户数据，也不会开放任何操作。':'This page will not load team or account data and exposes no actions.',
  '团队管理员':'Team administrator', '返回当前任务，或由团队管理员核对岗位、账户与交易所范围。':'Return to current tasks, or ask a team administrator to verify the role, account, and venue scope.',
  '新增风险、影子启用及相关写入保持阻断；既有减仓与退出边界不因页面提示而改变。':'New risk, Shadow activation, and related writes remain blocked. Existing reduction and exit boundaries are unchanged by this page.',
  '保存当前团队的版本化风险政策后重新检查。':'Save a versioned risk policy for the current team, then retry.',
  '本页事实不可用；依赖这些事实的操作保持阻断。':'Page facts are unavailable; actions depending on them remain blocked.',
  '当前功能负责人或系统管理员':'Feature owner or system administrator', '根据上方原因恢复所需事实后重新检查。':'Restore the required facts described above, then retry.',
  '页面不存在':'Page not found', '返回机会页':'Back to Opportunities', '内部访问':'Internal access',
  '进入交易控制台':'Open Trading Console', '账户密码验证':'Account password',
  '账户':'Account', '密码':'Password', '登录':'Sign in', '请输入账户名':'Enter your account name',
  '请输入密码':'Enter your password', '用户名或密码不正确。':'Incorrect account name or password.',
  '登录尝试过多，请稍后再试。':'Too many sign-in attempts. Try again later.',
  '审核工作台':'Review workspace', '当前没有需要你审核的提案':'There are no proposals waiting for your review',
  '当前没有需要你处理的审核待办':'There are no review tasks requiring your action', '风险恢复':'Risk restoration',
  '当前没有风险恢复审核待办':'No risk-restoration review is pending', '风险恢复状态读取失败':'Risk-restoration status could not be loaded',
  '提案无待办；风险恢复状态暂不可用':'No proposal review is pending; risk-restoration status is temporarily unavailable',
  '审核人只处理非本人提案与风险恢复申请；不能发起提案、查看资金或操作交易任务。':'Reviewers handle only independent proposal reviews and risk-restoration requests. They cannot create proposals, view capital, or operate trades.',
  '进入审核队列':'Open review queue', '提案工作台':'Proposal workspace', '从机会开始形成交易判断':'Start a trading thesis from an opportunity',
  '查看机会':'View opportunities', '查看提案记录':'View proposal records', '我的当前提案':'My current proposals',
  '等待独立审核':'Waiting for independent review', '只统计草稿和等待审核':'Counts drafts and pending reviews only',
  '创建者不能审核自己的提案':'Creators cannot review their own proposals', '待审核':'Pending review', '资金工作台':'Capital workspace',
  '你可以查看机会、发起提案并跟踪自己的当前提案；不能审核、查看资金或操作交易任务。':'You can review opportunities, create proposals, and track your current proposals. You cannot review, view capital, or operate trades.',
  '当前任务只显示你的资金职责':'Current tasks shows only your capital responsibilities', '尚未分配职责':'No role assigned',
  '当前身份尚未分配业务职责':'No business responsibilities are assigned to this account', '风险提醒':'Risk alert',
  '处理风险异常':'Resolve risk exceptions', '查看风险异常':'View risk exceptions', '新增风险受限':'New risk restricted',
  '查看限制与恢复条件':'View restrictions and recovery conditions', '需要审核':'Review required', '查看审核队列':'View review queue',
  '交易运行中':'Trades are active', '查看运行中交易任务':'View active trades', '当前无待办':'Nothing requires action',
  '交易待启动':'Trade setup pending', '查看当前提案':'View current proposals', '启动窗口已过期':'Launch window expired', '有效期 / 结果':'Validity / outcome',
  '审核已批准，但启动窗口已过期':'Approved, but the launch window has expired',
  '审核结论已保留，但不能再运行风险检查、签发授权或创建交易任务。需要按当前市场条件创建新提案。':'The review decision is retained, but risk checks, authorization, and trade creation are no longer allowed. Create a new proposal using current market conditions.',
  '批准不会自动下单。交易运维需要按当前账户事实重新风控、签发短期授权，再创建交易任务。':'Approval never places an order automatically. Trading operations must rerun risk checks against current account facts, issue short-lived authorization, and then create a trade.',
  '查看市场机会':'View market opportunities', '受影响交易任务':'Affected trades', '非本人待审核':'Independent reviews pending',
  '运行中交易任务':'Active trades', '新增风险状态':'New-risk status', '风险政策':'Risk policy', '真实下单':'Live order sending',
  '只读观察':'Read-only monitoring', '处理顺序':'Priority order',
  '现在按这个顺序处理':'Handle items in this order', '刷新当前数据':'Refresh data', '新的交易判断':'New trading thesis',
  '市场观察':'Market watch', '开始新的判断':'Start a new thesis', '继续观察市场机会':'Continue watching opportunities',
  '查看突破榜单机会':'View breakout opportunities', '创建人工提案':'Create manual proposal', '系统边界':'System controls',
  '当前控制状态':'Current control state', '站点环境':'Runtime', '风险政策':'Risk policy', '自动加仓':'Automatic scaling',
  '安全原则':'Safety rule', '数据缺失即阻断':'Missing data blocks trading', '连接不可用':'Connection unavailable',
  '人工提案':'Manual proposal', '刷新机会':'Refresh opportunities', '突破榜单数据源':'Breakout data source',
  '外部机会当前不可用':'External opportunities are unavailable', '当前候选':'Current candidates', '可创建提案':'Eligible proposals',
  '可交易合约':'Tradable instruments', '数据截止':'Data as of', '数据源状态':'Source status', '不可用':'Unavailable',
  '连接正常':'Connected', '交易所':'Exchange', '全部':'All', '币对':'Symbol', '共振周期':'Resonance', '突破周期':'Breakout timeframe',
  '至少 1 个周期':'At least 1 timeframe', '至少 2 个周期':'At least 2 timeframes', '至少 3 个周期':'At least 3 timeframes', '4 个周期':'4 timeframes',
  '全部周期':'All timeframes', '方向':'Direction', '做多':'Long', '做空':'Short', '最低成交量':'Minimum volume',
  '最低持仓量':'Minimum open interest', '不限':'No minimum', '清除筛选':'Clear filters', '参考价格':'Reference price',
  '触发时间':'Triggered at', '数据状态':'Data status', '成交量':'Volume', '持仓量':'Open interest',
  '交易所图表 ↗':'Exchange chart ↗', '突破详情 ↗':'Breakout details ↗', '高级配置':'Advanced settings', '一键创建':'Create now',
  '实时推送':'Live feed', '正在连接':'Connecting', '实时连接正常':'Live connection active', '页面更新正常':'Page updates active', '连接已中断，正在重连':'Disconnected, reconnecting',
  '突破币对':'Breakout markets', '周期信号':'Timeframe signals',
  'Perptape · 实时机会流':'Perptape · live opportunity feed',
  '没有符合条件的机会':'No opportunities match these filters', '等待机会数据恢复':'Waiting for opportunity data',
  '当前没有突破候选':'No breakout candidates right now', '人工创建交易提案':'Manual trading proposal',
  '创建人工提案':'Create manual proposal', '返回机会':'Back to Opportunities', '交易意图':'Trading intent',
  '风险边界':'Risk limits', '交易标的':'Instrument', '触发价格':'Trigger price', '最大持仓数量':'Maximum position size', '最大持仓金额':'Maximum position value',
  '先选交易所，再输入币对':'Choose an exchange, then enter a symbol', '输入完整币对':'Enter the full exchange symbol',
  '高级执行参数':'Advanced execution settings', '限价（可选）':'Limit price (optional)', '提案理由':'Rationale',
  '创建并提交审核':'Create and submit for review', '提案预览':'Proposal preview', '提交前摘要':'Summary before submission',
  '选择交易标的':'Select an instrument', '计划名义价值':'Planned notional', '失效距离':'Distance to invalidation',
  '有效期':'Expires in', '补全交易意图':'Complete the trading intent', '补全风险边界':'Complete the risk limits',
  '只创建提案，不直接下单':'Creates a proposal only; no order is placed', '提案审核':'Proposal review',
  '当前提案':'Current proposals', '历史提案':'Proposal history', '历史记录':'History', '已批准':'Approved', '已进入交易':'Entered trading',
  '等待审核':'Waiting for review', '高风险':'High risk', '30 分钟内到期':'Expires within 30 minutes',
  '需两人审核':'Two reviewers required', '最早到期':'Earliest expiry',
  '待我审核':'Assigned to me', '搜索标的':'Search instrument', '全部方向':'All directions',
  '风险':'Risk', '全部档位':'All levels', '提交时间':'Submitted', '到期':'Expires', '状态':'Status',
  '数量':'Quantity', '最多':'Up to', '人工':'Manual', '版本':'Version', '当前没有待你审核的提案':'No proposals are waiting for your review',
  '当前没有进行中的提案':'No active proposals', '当前没有历史提案':'No proposal history',
  '返回当前提案':'Back to current proposals', '没有符合条件的提案':'No proposals match these filters',
  '默认生产账户':'Default production account', '创建人':'Created by', '系统自动创建':'Created automatically', '已冻结来源快照':'Frozen source snapshot',
  '风险参数来自管理员保存的默认配置。':'Risk parameters use the administrator-saved defaults.',
  '提案已冻结，仍需人工审核；不会自动授权或下单。':'The proposal is frozen and still requires human review; it will not authorize or place an order automatically.',
  '这里只保留已批准、已过期或已拒绝的审计记录，不会把历史数量混入当前待办。':'Only approved, expired, or rejected audit records appear here; history is not counted as current work.',
  '这里只展示仍在草稿或等待审核中的提案；批准后进入历史，后续交易生命周期转到交易任务。':'Only drafts and proposals awaiting review appear here. Approved proposals move to History and continue as trades.',
  '已批准、已过期或已拒绝的提案会保留在这里供审计。':'Approved, expired, or rejected proposals remain here for audit.',
  '这里展示草稿、等待审核和已批准但仍需跟踪的提案；批准后仍须完成实时风险检查与短期授权。':'Drafts, pending reviews, and approved proposals that still require follow-up appear here. Approval still requires live risk checks and short-lived authorization.',
  '这里只展示仍在草稿或等待审核中的提案；批准后进入历史，后续交易生命周期由交易运维接手。':'Only drafts and pending reviews appear here. Approved proposals move to History and are handed to trading operations.',
  '这里只保留已进入交易任务的已批准提案，以及已过期或已拒绝记录；待启动提案仍留在当前列表。':'Approved proposals that have entered a trade, plus expired or rejected records, appear here. Proposals awaiting setup remain current.',
  '交易系统状态':'Trading system status', '刷新状态':'Refresh status', '查看风险控制':'View risk controls', '当前结论':'Current conclusion',
  '无需立即动作':'No immediate action', '核心服务':'Core services', '开仓与加仓':'Entry and scaling',
  '减仓与退出':'Reduce and exit', '止损与保护监控':'Stop-loss and protection monitoring', '风险敞口监控':'Exposure monitoring',
  '对账监控':'Reconciliation monitoring', '突破榜单机会源':'Breakout opportunity source', '服务可用':'Available',
  '服务不可用':'Unavailable', '当前无运行中任务':'No active trades', '当前无监控对象':'Nothing to monitor',
  '暂无对账对象':'Nothing to reconcile', '监控正常':'Monitoring normally', '对账一致':'Reconciled',
  '外部数据连接':'External connections', '生产数据与资金连接':'Production data and capital connections', '数据源':'Source', '数据可用':'Data available',
  '默认账户未配置':'Default account not configured', '账户范围缺失，未读取账户数据':'Account scope is missing; no account data was read', '事实新鲜度':'Fact freshness',
  '已配置默认账户':'Default account configured', '未配置默认账户':'Default account not configured',
  '不会回退到示例账户或猜测范围':'No fallback to a sample or guessed account', '未读取账户数据':'No account data was read',
  '请由系统管理员配置唯一默认生产账户。配置完成前，余额、仓位、委托、成交和资金费全部保持不可用，不会使用旧的 acct-1 或其他示例账户代替。':'Ask a system administrator to configure the one default production account. Until then, balances, positions, orders, fills, and funding remain unavailable; the console will not substitute acct-1 or another sample account.',
  '当前没有已保存的资金费记录。':'No saved funding records are available.',
  '当前没有已保存的成交记录。':'No saved fills are available.',
  '当前没有已保存的成交；这不代表交易所没有历史成交。':'No fills are saved yet; this does not mean the exchange has no fill history.',
  '当前没有已保存的资金费；这不代表交易所没有历史资金费。':'No funding is saved yet; this does not mean the exchange has no funding history.',
  '读取状态':'Read status', '运行范围':'Operating scope', '写入能力':'Write capability', '实时只读数据':'Real-time read-only data', '查看账户数据 →':'View account data →',
  '查看机会 →':'View opportunities →', '当前阻断':'Current blockers', '需要处理的问题类型':'Issues requiring action',
  '等待资金库绑定':'Waiting for vault binding', '生产资金':'Production capital', '只读网关已连接':'Read-only gateway connected',
  '只读与未签名计划':'Read-only data and unsigned plans', '由资金管理员配置':'Configured by treasury administrator', '查看资金 →':'View capital →',
  '查看恢复步骤':'View recovery steps', '交易账户数据 · 只读':'Exchange account data · read only',
  '连接状态':'Connection status',
  '运行模式':'Operating mode', '最后同步':'Last sync', '账户数据已保存':'Account data saved', '尚无数据':'No data yet',
  '权益':'Equity', '可用余额':'Available balance', '账户状态':'Account status', '最近对账':'Latest reconciliation',
  '仓位与风险保护':'Positions and protection', '当前委托':'Open orders', '最近成交':'Recent fills', '资金费':'Funding',
  '标的':'Instrument', '标记价':'Mark price', '更新时间':'Updated', '交易所订单':'Exchange order', '关联操作':'Related action',
  '成交编号':'Fill ID', '价格':'Price', '手续费':'Fee', '成交时间':'Filled at', '支付编号':'Payment ID', '金额':'Amount',
  '支付时间':'Paid at', '当前没有已保存的数据。':'No saved data.', '暂无数据':'No data', '资金中心':'Capital', '总净值':'Total net worth',
  '链上资金库净值':'On-chain vault net worth', '净值状态':'Net-worth status', '资金划转控制':'Transfer control',
  '在途 / 占用':'In transit / reserved', '资金快照':'Capital snapshot', '资金构成':'Capital composition',
  '生产资金 · 缺失即阻断 · 不代签不广播':'Production capital · missing data blocks · no delegated signing or broadcast',
  '生产资金 · 只读事实':'Production capital · read-only facts',
  '先看总额、资金位置和数据可信度，再处理资金路径。缺失、过期或时间错位的数据不会补零，也不会参与汇总。':'Review the total, capital locations, and data reliability before using a capital route. Missing, stale, or misaligned data is never filled with zero or included in the total.',
  '当前资金净值':'Current net worth',
  '当前三方总净值':'Current combined net worth', '当前不可汇总':'Combined total unavailable',
  '尚无三方同一时间口径的完整记录':'No complete three-source record is available for the same time window',
  '当前汇总已阻断':'Combined total blocked', '需关注':'Attention required',
  '数据来源：':'Source: ',
  '四条固定资金曲线':'Four fixed capital series',
  '币安、Hyperliquid、Vault 与三方汇总。汇总只使用 60 秒内对齐的三方事实；缺失、过期、错位和断档都不会补零或强行连线。':'Binance, Hyperliquid, Vault, and Combined total. The total uses only three-source facts aligned within 60 seconds; missing, stale, misaligned, or interrupted data is never filled with zero or forcibly connected.',
  'Binance、Hyperliquid、链上金库与三方汇总。汇总只使用 60 秒内对齐的三方事实；缺失、过期、错位和断档都不会补零或强行连线。':'Binance, Hyperliquid, Vault, and Combined total. The total uses only three-source facts aligned within 60 seconds; missing, stale, misaligned, or interrupted data is never filled with zero or forcibly connected.',
  '最近变化':'Latest change', '资金位置明细':'Capital location details',
  'USD 金额统一精度：小额四位、大额两位；原资产余额保留在明细中。历史快照不会计入当前净值。':'USD values use four decimals for small balances and two for larger balances. Native-asset balances remain in the details, and historical snapshots are excluded from current net worth.',
  '当前链上金库':'Current on-chain treasury', '链上金库状态':'On-chain treasury status',
  '二选一':'Choose one', '链上金库':'On-chain treasury', '实时额度预检':'Live limit preflight',
  '人控确认':'Human confirmation', '授权地址':'Authorized wallet', '目标入金':'Deposit to destination', '回执验证':'Verify receipt',
  '选择 NoTilt Vault 或 Safe Spending Limits，再按对应额度规则转入币安。':'Choose NoTilt Vault or Safe Spending Limits, then deposit to Binance under that provider’s limit rules.',
  '选择 NoTilt Vault 或 Safe Spending Limits，先到授权自有地址，再进入 Hyperliquid。':'Choose NoTilt Vault or Safe Spending Limits, move funds to the authorized owned wallet first, then deposit to Hyperliquid.',
  '回流到用户选择的 NoTilt Vault 或 Safe Smart Account。':'Return funds to the selected NoTilt Vault or Safe Smart Account.',
  '先从合约提回授权自有地址，再进入所选链上金库。':'Withdraw from the contract to the authorized owned wallet, then deposit into the selected on-chain treasury.',
  '统一安全边界：系统只使用当前选定的 NoTilt Vault；每条路径都重新校验地址、网络、资产、额度、实时状态与安全开关，且不在服务内签名或广播。':'Shared safety boundary: the system uses only the currently selected NoTilt Vault. Every route revalidates the address, network, asset, limits, live state, and safety controls; the service never signs or broadcasts.',
  '统一安全边界：系统只使用当前选定的 Safe Spending Limits；每条路径都重新校验地址、网络、资产、额度、实时状态与安全开关，且不在服务内签名或广播。':'Shared safety boundary: the system uses only the currently selected Safe Spending Limits configuration. Every route revalidates the address, network, asset, limits, live state, and safety controls; the service never signs or broadcasts.',
  'Binance、Hyperliquid、链上金库和三方汇总四条 USD 资金趋势':'Four USD capital series: Binance, Hyperliquid, Vault, and Combined total',
  '只保留四条明确资金路径。每次操作都先最终确认，再校验地址、网络、金额、额度和实时安全开关；当前缺少生产参数时只记录阻断与阶段，不会生成订单、签名或发送资金。':'Four fixed capital routes only. Every operation requires final confirmation and live validation of the destination, network, amount, limits, and safety gate. Missing production settings record a blocked stage only; no order, signature, or transfer is created.',
  '三方总净值':'Combined net worth', '资金库':'Vault', '币安 净值':'Binance net worth',
  'Hyperliquid 净值':'Hyperliquid net worth', '链上永续 净值':'Hyperliquid net worth', '资金操作':'Capital operations', '资金统计':'Capital history',
  '资金净值趋势':'Net-worth trend', '三方汇总':'Combined total', '选择显示的资金曲线':'Select visible capital series',
  '币安、Hyperliquid、链上金库和三方汇总四条资金趋势':'Four capital series: Binance, Hyperliquid, Vault, and Combined total',
  '固定四条线：币安、Hyperliquid、链上金库和三方汇总。缺失来源显示“等待数据”，不会补成 0；历史曲线不会冒充当前净值。':'Exactly four series: Binance, Hyperliquid, Vault, and Combined total. Missing sources show “Waiting for data” instead of zero; historical trends are never presented as current net worth.',
  '完成生产资金同步后才显示曲线；缺失数据不会补零。':'The chart appears after production capital is synchronized; missing data is never replaced with zero.',
  '链上资金库尚未同步':'The on-chain vault has not synchronized', '链上永续数据已过期':'Hyperliquid data is stale',
  '净值不完整：':'Net worth incomplete: ', '净值不完整：链上资金库尚未同步':'Net worth incomplete: the on-chain vault has not synchronized',
  '净值不完整：链上资金库尚未同步；链上永续数据已过期':'Net worth incomplete: the on-chain vault has not synchronized; Hyperliquid data is stale',
  '资金位置':'Capital locations', '资金提案':'Capital proposals', '资金划转':'Capital transfers', '位置':'Location',
  '固定展示三处资金；缺失金额显示为“—”，历史快照不会计入当前净值。':'Three capital locations are shown. Missing amounts appear as “—”, and historical snapshots are excluded from current net worth.',
  '默认账户':'Default account', '等待配置':'Waiting for configuration', '数据缺失':'Data missing',
  '未配置或未同步':'Not configured or not synchronized', '当前美元估值不可采信':'The current USD value is not trustworthy',
  '只读 / 待发送':'Read only / not submitted', '已安装':'Installed', '不完整':'Incomplete',
  '生产配置预检':'Production configuration preflight', '只显示是否配置，不回显地址或凭据':'Shows configuration status only; addresses and credentials are never revealed',
  '单账户模式':'Single-account mode', '网络 / 资产':'Network / asset', 'NoTilt 官方 SDK':'Official NoTilt SDK',
  'NoTilt 范围':'NoTilt scope', '缺少官方金库或 Agent 范围':'Missing the trusted vault or agent scope',
  '自有钱包':'Owned wallet', '币安受限路径':'Restricted Binance route', 'Hyperliquid 路径':'Hyperliquid route',
  '金额 / 费用上限':'Amount / fee limits', '签名 / 广播':'Signing / broadcast',
  '始终由独立人控钱包处理；Agent 不支持':'Always handled by a separate human-controlled wallet; agents cannot perform it',
  '四条直达路径':'Four direct routes', '选择资金从哪里到哪里':'Choose the capital source and destination',
  '配置固定资金路径':'Configure fixed capital routes', '尚无数据库配置；当前读取安全环境配置':'No saved database configuration; using fail-closed environment settings',
  '管理员配置':'Administrator settings',
  '授权自有 Arbitrum 地址':'Authorized owned Arbitrum wallet address',
  '授权的自有 Arbitrum 钱包地址':'Authorized owned Arbitrum wallet address',
  'Hyperliquid Bridge 地址':'Hyperliquid bridge address',
  'Hyperliquid 充值桥地址（Bridge）':'Hyperliquid bridge address',
  'Safe Spending Limit delegate':'Safe delegate address',
  'Safe 委托地址（delegate）':'Safe delegate address',
  'Safe Smart Account 和委托地址（delegate）':'Safe Smart Account and delegate address',
  '缺少官方金库或 NoTilt Agent 授权范围':'Missing the trusted vault or NoTilt agent scope',
  '始终由独立人控钱包处理；系统不会代为签名或广播':'Always handled by a separate human-controlled wallet; the system never signs or broadcasts on the user’s behalf',
  '先选路径，再在一个确认窗口里填写金额；安全说明不再重复四遍。':'Choose a route, then enter the amount in one confirmation dialog. The shared safety boundary is stated once.',
  '统一安全边界：':'Shared safety boundary: ',
  '每条路径都重新校验地址、网络、资产、额度、实时状态与安全开关。NoTilt 只构建官方 SDK 无签名请求；签名和广播只能由独立人控钱包逐笔完成。':'Every route revalidates the address, network, asset, limits, live state, and safety gate. NoTilt only builds unsigned requests from the official SDK; a separate human-controlled wallet must confirm every signature and broadcast.',
  '已确认可用':'Confirmed available', '美元净值':'USD value', '源端预留':'Source reserved', '有效可用':'Effective available',
  '控制 / 充值':'Control / deposit', '提案':'Proposal', '路径':'Route', '动作':'Actions', '划转记录':'Transfer record',
  '当前估值':'Current value', '历史快照':'Historical snapshot', '不计入当前净值':'Excluded from current net worth',
  '历史趋势':'Historical trend', '当前数据':'Current data', '操作':'Operation', '路径 / 金额':'Route / amount',
  '阶段':'Stage', '状态 / 回执':'Status / receipt', '精确阻断':'Exact blockers',
  '固定路径':'Fixed route', '10 分钟等待':'10-minute delay', '两段路径':'Two-stage route', '受限提现':'Restricted withdrawal',
  '申请释放':'Request release', '等待 10 分钟':'Wait 10 minutes', '到期重检':'Revalidate after the delay', '进入币安':'Deposit to Binance',
  '到达自有地址':'Arrive at owned address', '合约入金':'Deposit to contract', '提现预检':'Withdrawal preflight',
  'SDK 无签名入金':'Unsigned SDK deposit', '合约提现':'Contract withdrawal',
  '检查转入币安条件':'Check Binance deposit requirements', '检查转入 Hyperliquid 条件':'Check Hyperliquid deposit requirements',
  '检查币安回流条件':'Check Binance return requirements', '检查 Hyperliquid 回流条件':'Check Hyperliquid return requirements',
  '释放到期后重新校验，再转入已授权币安地址。':'Revalidate after the release delay, then deposit to the authorized Binance address.',
  '先释放至已授权 Arbitrum 自有地址，再存入 Hyperliquid 合约。':'Release to the authorized owned Arbitrum address, then deposit into the Hyperliquid contract.',
  '先提现到已授权自有地址，再构建 NoTilt SDK 无签名入金。':'Withdraw to the authorized owned address, then build an unsigned NoTilt SDK deposit.',
  '先从合约提回已授权自有地址，再构建 NoTilt SDK 无签名入金。':'Withdraw from the contract to the authorized owned address, then build an unsigned NoTilt SDK deposit.',
  '资金路径安全预检':'Capital-route safety preflight', '金额（USDC）':'Amount (USDC)', '输入划转金额':'Enter transfer amount',
  '我已核对资金方向与金额':'I verified the direction and amount', '最终确认并检查':'Confirm and run preflight',
  '提交只会重新校验地址、网络、资产、额度和实时安全开关。任何条件缺失都会阻断；系统不会签名、广播或发送资金。':'Submitting only revalidates the address, network, asset, limits, and live safety gate. Any missing condition blocks the operation; the system never signs, broadcasts, or transfers funds.',
  '操作日志、阶段与回执':'Operation log, stages, and receipts', '生成无签名预检':'Build unsigned preflight',
  '申请资金库释放 → 等待 10 分钟 → 到期重新校验 → 转入已授权币安地址':'Request vault release → wait 10 minutes → revalidate → deposit to the authorized Binance address',
  '已安全阻断':'Safely blocked', '回执：未提交':'Receipt: not submitted',
  '历史资金划转':'Historical capital transfers', '尚无历史资金划转。':'No historical capital transfers.',
  '旧流程只保留为只读审计记录，不再是四条直达操作的必经界面。':'The legacy workflow remains as read-only audit history and is no longer required for the four direct routes.',
  '划转总额':'Gross amount', '状态 / 对账':'Status / reconciliation', '外部引用':'External reference',
  '运行告警':'Runtime alerts', '刷新当前数据':'Refresh data', '阻断问题':'Blocking issues', '结果未知':'Unknown outcome',
  '数据过期':'Stale data', '恢复队列':'Recovery queue', '下一步：':'Next: ', '打开交易任务并按顺序处理':'Open trade and follow the steps',
  '当前运行中的交易任务没有阻断异常':'No blocking exceptions in active trades', '成员权限':'Access control', '权限分离原则':'Separation of duties',
  '审核与发起分开':'Proposal and review are separate', '交易与资金分开':'Trading and treasury are separate',
  '身份与权限分开':'Identity and authorization are separate', '新增内部成员':'Add internal member', '展开':'Expand',
  '账户范围':'Account scope', '交易所范围':'Exchange scope', '常用模板':'Role templates', '只审核':'Review only',
  '只发起提案':'Propose only', '交易运维':'Trading operations', '创建成员':'Create member', '当前用户':'Current users',
  '现有成员':'Existing members', '已启用':'Enabled', '已停用':'Disabled', '保存权限':'Save access',
  '允许登录和使用已分配权限':'Allow sign-in and assigned permissions', '低':'Low', '中':'Medium', '高':'High',
  '否':'No', '是':'Yes', '未知':'Unknown', '未配置':'Not configured', '已关闭':'Disabled', '只读':'Read only',
  '可用':'Available', '数据不完整':'Incomplete data', '数据已过期':'Stale data', '正常':'Normal', '未运行':'Not run', '未提交':'Not submitted', '生产':'Production', '已结束':'Closed',
  '当前安全，但新增风险受限':'The system is safe, but new risk is restricted',
  '当前系统风险状态为“仅允许减仓”。先完成恢复条件，不会自动放开旧授权。':'The system is in reduce-only mode. Complete the recovery requirements first; previous authorizations will not be restored automatically.',
  '没有派生异常':'No derived exceptions', '创建者不可审核自己的提案':'Proposers cannot review their own proposals',
  '当前没有活动仓位流程':'No active position workflow', '自动加仓已关闭':'Automatic scaling is disabled',
  '必须先处理':'Resolve first', '新增风险受限':'New risk is restricted', '查看恢复条件 →':'View recovery requirements →',
  '恢复条件':'Recovery requirements', '新的交易判断':'New trading thesis',
  '先看机会或写交易判断；这两条路径都只会创建提案，并进入独立审核。':'Review an opportunity or write a trading thesis. Both paths create a proposal and send it to independent review.',
  '是否允许真实发送，由具体交易任务、短期授权、交易所配置和服务端控制开关共同决定。':'Live sending requires an eligible trade, a valid short-lived authorization, configured exchange access, and an enabled server-side control.',
  '突破榜单 · 生产机会':'Breakout list · production opportunities',
  '这里只展示突破榜单返回的生产候选；没有候选时，你仍可点击“人工提案”录入自己的交易判断。所有入口只创建提案，并进入独立审核。':'This page shows production candidates from the breakout feed. When no candidate is available, use Manual proposal to record your own thesis. Every path creates a proposal and sends it to independent review.',
  '＋ 人工提案':'+ Manual proposal', '突破榜单数据':'Breakout feed data',
  '突破榜单尚未配置。人工提案仍可使用，但系统机会会保持关闭。系统不会用旧数据或示例数据填充。':'The breakout feed is not configured. Manual proposals remain available, but feed-based opportunities stay disabled. The system never substitutes stale or sample data.',
  '人工提案始终可用；突破榜单恢复后可直接刷新。':'Manual proposals remain available. Refresh this page after the breakout feed recovers.',
  '这里只保留真正需要你独立判断的提案；批准不等于下单。':'Only proposals that require your independent judgment appear here. Approval does not place an order.',
  '查看全部提案':'View all proposals', '自己的提案、已经投过票、已到期或已结束的提案不会留在这里。':'Your own proposals, proposals you already reviewed, expired proposals, and completed proposals are excluded.',
  '每个交易任务覆盖一笔交易从授权、风险占用和下单意图，到成交、保护、减仓、对账与最终结果的完整生命周期。':'Each trade tracks the full lifecycle from authorization, risk reservation, and order intent through fills, protection, reductions, reconciliation, and final results.',
  '交易任务记录':'Trade records', '建仓中 / 持仓中':'Opening / open', '运行范围':'Operating scope', '生产交易':'Production trading',
  '标的 / 方向':'Instrument / direction', '账户 / 场所':'Account / venue', '仓位目标':'Position target', '当前目标':'Current target', '最终盈亏':'Final PnL',
  '已平仓':'Flat', '保护不适用（当前无仓位）':'Not applicable (no open position)',
  '风险预留与平仓结果':'Risk reservation and close result', '当前总盈亏':'Current total PnL',
  '提案通过审核和风险检查后，交易运维人员才能发起开仓。':'Trading operations can open a position only after proposal approval and risk checks pass.',
  '这里直接说明系统能否工作、哪些能力受限，以及是否需要处理。绿色表示当前证据正常；黄色表示能力受限；红色表示必须先处理；灰色表示当前没有监控对象。':'This page shows whether the trading system is operational, which capabilities are limited, and what needs attention. Green is healthy, yellow is limited, red requires action, and gray means there is nothing to monitor.',
  '交易管理可用，但突破榜单机会源受限':'Trading operations are available, but the breakout feed is limited',
  '尚未配置。现有交易任务仍可管理，但新的突破榜单机会暂不可用。':'The breakout feed is not configured. Existing trades remain manageable, but new feed-based opportunities are unavailable.',
  '业务数据库和交易服务运行正常。':'The business database and trading services are healthy.',
  '核心服务可用，当前无运行中交易任务':'Core services available; no active trades',
  '当前没有需要监控的交易任务；系统不会把“无监控对象”误报为“监控正常”。':'There are no active trades to monitor. The system does not report “nothing to monitor” as “healthy monitoring”.',
  '风险政策：仅允许减仓；自动加仓：已关闭。':'Risk policy: reduce only. Automatic scaling: disabled.',
  '政策变化会立即重新检查所有新增风险':'Policy changes immediately recheck every new-risk action',
  '当前没有需要减仓或退出的交易任务。':'There are no active trades that need reduction or exit.',
  '有交易任务进入持仓后，系统会持续检查止损和保护覆盖。':'When a trade has an open position, the system continuously checks stop-loss and protection coverage.',
  '有交易任务进入运行后，系统会检查仓位和风险占用。':'When a trade becomes active, the system monitors positions and reserved risk.',
  '当前没有运行中的交易任务需要对账。':'There are no active trades to reconcile.',
  '只有计算结果为“对账一致”才可作为恢复依据':'Only a computed reconciliation match can support recovery',
  '突破榜单尚未配置；人工提案仍可使用。':'The breakout feed is not configured. Manual proposals remain available.',
  '只读 · 最近数据':'Read only · latest data', '可用能力':'Available capability', '只读连接':'Read-only connection',
  '生产账户':'Production account', '市场机会':'Market opportunities', '连接正常':'Connected',
  '横向滑动或使用方向键查看完整表格':'Scroll horizontally or use the arrow keys to view the full table',
  '交易所与机会源，可横向滚动':'Exchanges and opportunity source; horizontally scrollable',
  '生产交易数据':'Production trading data', '订单与成交':'Orders and fills', '风险与目标':'Risk and targets',
  '这里显示当前确认的数据；能够重新计算的汇总会按最新数据生成。':'This page shows confirmed current data. Recomputable summaries use the latest available records.',
  '当前没有可展示的数据':'There is no data to display', '系统风险控制':'System risk controls',
  '当前政策':'Current policy', '政策时间':'Policy time', '自动加仓控制':'Automatic scaling control', '控制时间':'Control time',
  '变更原因':'Change reason', '控制状态':'Control status', '控制时间':'Control time',
  '“仅允许减仓”会阻止所有新增风险，但减仓和退出仍然可用。':'Reduce-only mode blocks all new risk while keeping reductions and exits available.',
  '恢复条件检查':'Recovery requirements', '当前没有可验证账户':'No verifiable account',
  '尚未发现生产账户范围，恢复保持关闭。':'No production account scope is available, so recovery remains disabled.',
  '受审核恢复':'Reviewed recovery', '申请恢复':'Request recovery', '等待独立审核':'Waiting for independent review',
  '申请人':'Requester', '申请原因':'Request reason', '申请时间':'Requested at', '审核状态':'Review status',
  '需要两名独立审核人通过后才能执行':'Two independent approvals are required before execution',
  '当前控制状态没有可执行的恢复申请。':'There is no executable recovery request for the current control state.',
  '收紧风险':'Tighten risk', '只允许收紧风险':'Risk can only be tightened',
  '这些入口只能关闭自动加仓，或把系统切换为“仅允许减仓”；不能从这里恢复新增风险。':'These controls can disable automatic scaling or switch the system to reduce-only mode. They cannot restore new-risk capacity.',
  '关闭全局自动加仓':'Disable automatic scaling globally', '暂停所有新增风险':'Pause all new risk',
  '这里只显示运行中交易任务的阻断问题，并按安全顺序说明发生了什么、是否影响交易，以及下一步该做什么。':'This page shows blocking issues for active trades, ordered by the safest recovery sequence with impact and next steps.',
  '没有发现结果未知、数据过期、保护不足或对账差异。已关闭的历史记录不会因为数据变旧而重新报警。':'No unknown outcomes, stale data, protection gaps, or reconciliation differences were found. Closed historical records do not create new alerts as they age.',
  '生产交易记录':'Production trading records',
  '先看盈亏和当前结论，再查看每个交易任务的成交、成本、对账与操作记录。这里只显示生产数据。':'Start with P&L and the current conclusion, then inspect fills, costs, reconciliation, and activity for each trade. Only production data is shown.',
  '该环境尚未形成可结算结果':'No settlement-ready result is available',
  '没有已保存的交易任务数据，因此这里不会推测盈亏。请先从机会和提案流程形成可审计的交易记录。':'There are no saved trades, so the system will not estimate P&L. Start from an opportunity or proposal to create an auditable trading record.',
  '进行中显示当前值，交易任务关闭后才显示最终值':'Active trades show current values; final values appear only after closure',
  '暂无结果':'No results yet', '系统没有收到可追溯到交易任务的数据，因此不会展示推测数字。':'No traceable trade data is available, so the system does not display estimated figures.',
  '待处理交易任务':'Trades requiring attention', '审计事件':'Audit events',
  '该环境尚无可追溯的交易任务。':'There are no traceable trades in this environment.', '当前环境没有交易任务。':'There are no trades in the current environment.',
  '没有可靠期初资本时只展示结算币种绝对值，不伪造百分比收益率或回撤。':'Without reliable opening capital, the system shows absolute settlement-currency values and does not invent percentage returns or drawdown.',
  '没有已关闭交易任务的曲线数据。':'There is no curve data for closed trades.',
  '可以打开提案或交易任务继续追查；关联编号用于定位同一条操作记录。':'Open a proposal or trade to investigate further. Reference IDs connect events from the same operation.',
  '当前身份下没有可见操作记录。':'There are no visible activity records for this account.',
  '生产账户 · 数据读取':'Production accounts · data access', '生产账户 · 自动读取':'Production accounts · automatic read',
  '在这里查看币安和 Hyperliquid 账户的连接、余额、仓位、委托、成交、资金费与对账。日常交易仍从交易任务、系统状态和运行告警页面进入。':'Use this page to inspect Binance and Hyperliquid connections, balances, positions, orders, fills, funding, and reconciliation. Daily operations still start from Trades, System status, and Runtime alerts.',
  '刷新账户数据':'Refresh account data', '刷新页面':'Refresh page', '选择交易所':'Select exchange', '账户数据只读':'Account data read only',
  '统一账户':'Unified account', '账户数据已保存':'Account data saved',
  '最后更新':'Last updated', '账户数据自动更新中':'Account data updates automatically', '账户数据自动同步':'Account data sync is current', '自动同步等待连接恢复':'Automatic sync is waiting for the connection', '账户自动更新尚未启用':'Automatic account updates are not enabled',
  '统一查看币安和 Hyperliquid 的余额、当前仓位、当前委托、最近成交与资金费。系统按账户自动覆盖全部活跃标的，不需要逐个输入币对。':'View balances, positions, open orders, recent fills, and funding for Binance and Hyperliquid in one place. Account-wide synchronization automatically covers every active instrument; no symbol input is needed.',
  '当前只展示已经保存的生产数据；配置连续读取服务后会自动更新。':'Only saved production data is shown. Configure the continuous reader to update it automatically.',
  '切换账户时只读取当前身份获准查看的范围。点击同步后，系统会从交易所获取数据、保存并立即运行对账。':'Changing accounts only reads scopes assigned to the current identity. Sync fetches exchange data, saves it, and immediately runs reconciliation.',
  '生产读取连接尚未配置或已关闭。页面只展示已经保存的数据，不会用其他数据填充。':'The production read connection is not configured or is disabled. This page shows saved data only and never fills gaps with substitute data.',
  '权益状态':'Equity status', '仓位与风险保护':'Positions and risk protection', '当前仓位与风险保护':'Current positions and protection',
  '最后快照':'Last snapshot', '历史快照':'Historical snapshot', '历史结果':'Historical result',
  '当前连接不可用，以下仅为最后一次保存快照':'The current connection is unavailable; the data below is the last saved snapshot',
  '这些余额、仓位、订单与成交不能作为实时交易依据。恢复只读连接并完成新一轮同步后，页面才会重新标记为当前事实。':'These balances, positions, orders, and fills are not live trading facts. The page returns to current status only after the read-only connection and a fresh sync recover.',
  '当前账户没有持仓；零仓位行情不会冒充当前仓位。':'There are no open positions. Zero-position market observations are not shown as positions.',
  '当前账户没有未完成委托。':'There are no open orders.', '最近订单记录':'Recent order history', '查看记录':'View history',
  '最后快照中的仓位与风险保护':'Positions and protection in the last snapshot',
  '最后一次保存快照中没有持仓；这不能确认当前账户仍为空仓。':'The last saved snapshot has no position; this does not confirm that the account is currently flat.',
  '最后快照中的委托':'Orders in the last snapshot',
  '最后一次保存快照中没有未完成委托；这不能确认当前仍无挂单。':'The last saved snapshot has no open orders; this does not confirm that there are currently no open orders.',
  '最后快照中的订单记录':'Order history in the last snapshot',
  '最后快照中的成交记录':'Fills in the last snapshot',
  '最后一次保存快照中没有成交记录；这不代表连接中断后没有成交。':'The last saved snapshot has no fills; this does not mean no fills occurred after the connection was lost.',
  '最后快照中的资金费':'Funding in the last snapshot',
  '最后一次保存快照中没有资金费记录；这不代表连接中断后没有资金费。':'The last saved snapshot has no funding records; this does not mean no funding occurred after the connection was lost.',
  '最后快照的对账差异':'Reconciliation differences in the last snapshot',
  '已确认':'Confirmed', '数量 / 入场':'Quantity / entry', '保护':'Protection',
  '足额':'Fully covered', '不足':'Insufficient', '无保护数据':'No protection data',
  '成交 / 委托':'Filled / ordered', '方向 / 数量':'Side / quantity',
  '外部未关联':'External / unlinked', '对账差异':'Reconciliation differences',
  '系统管理 · 权限配置':'System administration · access configuration',
  '按岗位勾选权限，不需要逐个页面配置。一个人可以组合多个岗位，还可以按账户和交易所限制每个用户能够查看和操作的数据。':'Assign permissions by role instead of configuring each page. A user can hold multiple roles, with optional account and exchange scopes limiting visible and actionable data.',
  '审核人不能审核自己的提案；提案发起人不会自动获得执行权限。':'Reviewers cannot review their own proposals, and proposers do not automatically receive execution permission.',
  '交易运维人员看不到资金中心；系统管理员拥有最高管理权限，但资金动作仍受实时校验、最终确认和 Gate 约束。':'Trading operators cannot access Capital. System administrators have the highest administrative permission, while capital actions still require live validation, final confirmation, and safety gates.',
  '生产环境仍由统一身份登录和通行密钥（Passkey）认证；这里仅管理内部授权，不创建密码。':'Production uses centralized identity and passkey authentication. This page manages internal authorization only and does not create passwords.',
  '生产环境仍由统一身份登录和通行密钥认证；这里仅管理内部授权，不创建密码。':'Production uses centralized identity and passkey authentication. This page manages internal authorization only and does not create passwords.',
  '先创建授权记录，再由生产身份服务完成身份绑定':'Create the authorization record first, then bind it through the production identity service',
  '全部交易所':'All exchanges', '等待生产身份源绑定':'Waiting for production identity binding',
  '这是当前账号。为避免误锁死，必须由另一名系统管理员修改。':'This is the current account. Another system administrator must make changes to prevent accidental lockout.',
  '只读观察':'Read-only observer', '查看机会、提案、交易任务、系统状态和交易账户；不能执行动作。':'View opportunities, proposals, trades, system status, and exchange accounts without taking actions.',
  '发起提案':'Proposal creator', '查看机会并创建提案；不能审核自己的提案，也不能操作交易任务。':'View opportunities and create proposals. Cannot review own proposals or operate trades.',
  '独立审核':'Independent reviewer', '独立审核冻结提案与风险恢复申请；不能发起提案、操作交易或查看资金。':'Independently review frozen proposals and risk restoration requests. Cannot create proposals, operate trades, or view capital.',
  '运行风险、授权、订单、减仓、对账和交易所同步；不自动获得资金权限。':'Run risk checks, authorizations, orders, reductions, reconciliation, and exchange sync without treasury access.',
  '资金管理':'Treasury management', '查看与管理资金数据、划转和资金对账；与交易运维职责分离。':'View and manage capital data, transfers, and capital reconciliation separately from trading operations.',
  '系统管理':'System administration', '管理成员与系统控制；资金动作仍受独立实时安全检查。':'Manage users and system controls; capital actions remain subject to independent live safety checks.',
  '留空 = 全部账户':'Blank = all accounts',
  '自动资金划转':'Automatic capital transfer', '审核后自动准备，钱包只做最终确认':'Automatically prepared after approval; the wallet only confirms',
  '1. 自动复核':'1. Automatic checks', '重新检查空仓、未决订单、对账、资金余额和链上额度。':'Recheck flat positions, unresolved orders, reconciliation, balances, and on-chain limits.',
  '2. 自动准备':'2. Automatic preparation', '预留资金并生成严格限定目标、资产和金额的链上交易计划。':'Reserve funds and generate an on-chain plan restricted to the approved target, asset, and amount.',
  '3. 自动跟踪':'3. Automatic tracking', '验证链上回执并持续对账；结果未知时立即阻断后续动作。':'Verify on-chain receipts and keep reconciling. Unknown outcomes immediately block the next action.',
  '交易控制台不保存私钥，也不替钱包签名或广播。钱包确认前会明确显示链、目标地址、资产、金额和资金库。':'The console never stores private keys or signs and broadcasts for the wallet. Before confirmation it shows the chain, target address, asset, amount, and vault.',
  '创建生产资金提案':'Create production capital proposal', '创建并进入审核':'Create and send for review',
  '提交后由两名独立审核人确认。审核通过后，一键启动自动划转：系统复核额度、预留资金、生成链上计划并跟踪回执；独立钱包负责最终签名确认。':'Two independent reviewers must approve. After approval, one action starts the automated transfer workflow: the system rechecks limits, reserves funds, prepares the on-chain plan, and tracks receipts; an independent wallet provides the final signature.',
  '开始自动划转':'Start automatic transfer', '继续自动划转':'Continue automatic transfer',
  '资金统计':'Capital history', '三方资金趋势':'Three-source capital trends', '等待数据':'Waiting for data',
  '最近完整历史：币安、Hyperliquid、NoTilt 与汇总趋势':'Latest complete history: Binance, Hyperliquid, NoTilt, and total',
  '上方卡片只显示仍在新鲜度窗口内的当前净值；曲线保留已确认历史快照。两者时间语义不同，缺失来源不会补零。':'The cards above show only current values inside the freshness window; the chart retains confirmed historical snapshots. These have different time semantics, and missing sources are never filled with zero.',
  '每条线分别显示币安、Hyperliquid 和链上资金库的美元净值；只使用生产环境的已确认同步记录。':'Each line shows the USD net worth of Binance, Hyperliquid, or the on-chain vault. Only confirmed production synchronization records are used.',
  '完成首次生产资金同步后，将在这里显示三方资金曲线。':'The three capital curves appear after the first production capital synchronization.',
  '统一统计币安、Hyperliquid 和链上资金库的生产资金，并保留每次同步的净值变化。交易所与资金库之间可双向划转，但必须经过独立审核、限时授权和链上额度检查。':'Track production capital across Binance, Hyperliquid, and the on-chain vault, including each synchronized net-worth change. Transfers work in both directions between venues and the vault, subject to independent review, short-lived authorization, and on-chain budget checks.',
  '新建人工提案':'New manual proposal', '查看提案':'View proposals',
  '新增风险已受限，减仓和退出仍可用':'New risk is restricted; reductions and exits remain available',
  '减仓和退出不受阻断。':'Reductions and exits remain available.', '查看突破榜单机会':'View breakout opportunities',
  '突破榜单 · 连接不可用':'Breakout list · connection unavailable',
  '先查看突破榜单候选；没有合适信号时，也可以点击“人工提案”自行录入。无论哪种方式，都只会创建提案，并且必须经过独立审核。':'Review breakout candidates first. If none fits, use Manual proposal to enter your own thesis. Either path creates a proposal and requires independent review.',
  '突破榜单数据源':'Breakout feed',
  '突破榜单尚未配置。人工提案仍可使用，外部机会将在完成配置后恢复。 系统不会把过期候选当成当前机会。':'The breakout feed is not configured. Manual proposals remain available, and external opportunities will return after configuration. Stale candidates are never presented as current opportunities.',
  '人工提案仍然可用；突破榜单恢复后可以再次刷新。':'Manual proposals remain available. Refresh again after the breakout feed recovers.',
  '人工提案仍然可用；突破榜单恢复后会自动重连。':'Manual proposals remain available. The live feed reconnects automatically after recovery.',
  '实时汇总同一币对、同一方向的多个突破周期。点击突破详情核对 Perptape 原始信号；创建提案仍需独立审核和系统风险检查。':'Signals for the same market and direction are grouped across timeframes in real time. Open Breakout details to verify the original Perptape signal; proposals still require independent review and risk checks.',
  '一个币对的多个周期会合并显示，方向冲突时分开显示':'Multiple timeframes for one market are grouped; conflicting directions remain separate',
  '默认先显示可创建机会；待补齐和仅查看仍可切换。':'Actionable opportunities are shown first by default; waiting and view-only signals remain available.',
  '查看状态':'View status', '可创建':'Actionable', '待补齐':'Waiting for data', '仅查看':'View only',
  '行情状态':'Market data', '行情可用':'Market data ready', '等待补齐':'Waiting for data', '信号已过期':'Signal expired',
  '各周期分别计数，不是币对数量':'Each timeframe is counted separately; this is not a market count',
  '向上突破':'Breakout higher', '向下突破':'Breakout lower', '突破详情 ↗':'Breakout details ↗',
  '查看突破榜单':'View breakout feed', '突破榜单机会源':'Breakout opportunity source',
  '尚未配置':'Not configured', '仅允许减仓':'Reduce only', '一项安全条件尚未满足':'One safety requirement is not satisfied',
  '政策更新时间':'Policy updated', '4 项阻塞':'4 blockers', '政策原因':'Policy reason', '政策更新人':'Policy updated by',
  '控制原因':'Control reason', '控制操作人':'Control operator', '控制更新时间':'Control updated',
  '“仅允许减仓”仍允许减仓与退出；暂停新增风险后，当时所有未过期的新风险授权都会永久失效。':'Reduce-only mode still allows reductions and exits. When new risk is paused, every unexpired new-risk authorization is permanently invalidated.',
  '实时恢复条件':'Live recovery requirements', '受控账户':'Controlled accounts',
  '尚未配置生产账户范围，因此恢复执行保持关闭。':'No production account scope is configured, so recovery execution remains disabled.',
  '双人复核流程':'Two-person review workflow', '恢复申请与独立审核':'Recovery requests and independent review',
  '恢复申请':'Recovery request', '等待双人审核':'Waiting for two reviewers', '恢复自动加仓':'Restore automatic scaling',
  '最早执行':'Earliest execution', '原控制状态':'Previous control state',
  '已完成异常处置，并准备由两名独立审核人复核全部恢复条件':'Exception handling is complete and all recovery requirements are ready for review by two independent reviewers',
  '审核记录':'Review history', '尚无审核票。':'No review votes yet.', '当前身份或申请状态没有可用审核动作。':'No review action is available for the current identity or request state.',
  '管理员暂停了所有新增风险':'Administrator paused all new risk', '管理员关闭了全局自动加仓':'Administrator disabled automatic scaling globally',
  '4 项恢复条件尚未满足。 减仓和退出不受阻断。':'Four recovery requirements are not yet satisfied. Reductions and exits remain available.',
  '尝试降低共振、成交量或持仓量门槛，或者清除部分筛选。':'Lower the resonance, volume, or open-interest threshold, or clear some filters.',
  '交易执行底座':'Execution backend', '仿真执行进程已连接':'Simulation workers connected',
  'Freqtrade 执行进程未启动':'Freqtrade workers not started', 'Freqtrade 执行进程检查未通过':'Freqtrade worker checks failed',
  'Telegram 审核通知':'Telegram review notifications', '通知可用':'Notifications available', '通知受阻':'Notifications unavailable',
  '等待首次轮询':'Waiting for the first poll',
  '机器人尚未完成一次成功轮询；网页端审核队列仍是权威入口。':'The bot has not completed a successful poll yet. The web review queue remains the authoritative source.',
  '网页端审核队列保持可用；资金、订单、风险开关与权限操作不对 Telegram 机器人开放':'The web review queue remains available. Capital, orders, risk controls, and access management are not exposed to the Telegram bot.',
  'Perptape 机会源':'Perptape opportunity feed',
  '连续接入':'Continuous transport', 'WebSocket 实时流':'Live WebSocket stream',
  'WebSocket 启动中':'WebSocket starting', 'HTTPS 轮询回退':'HTTPS polling fallback',
  'HTTPS 定时轮询':'Scheduled HTTPS polling', '连续接入失败':'Continuous transport failed',
  '轮询失败':'Polling failed', '等待首次运行事实':'Waiting for the first runtime fact',
  '机会页复用当前团队唯一事实':'The opportunity page uses the current team’s single authoritative feed',
  '查看机会':'View opportunities', '上游 WebSocket 实时流':'Upstream live WebSocket stream',
  '上游流启动中':'Upstream stream starting', '上游轮询失败':'Upstream polling failed',
  '上游流不可用':'Upstream stream unavailable', '等待首次同步':'Waiting for first synchronization',
  '机会快照':'Opportunity snapshot', '页面正在连接':'Page connecting',
  '页面更新正常':'Page updates connected', '页面连接已中断，正在重连':'Page connection interrupted; reconnecting',
  '连续接入降级':'Continuous transport degraded', '当前使用 HTTPS 轮询事实':'Using HTTPS polling facts',
  '连续信号接入未通过':'Continuous signal transport is unavailable',
  '仅验证执行适配与合约目录；不会发送真实订单':'Execution adapters and instrument catalogs only; no live orders are sent',
  'Telegram 私聊机器人最近一次长轮询成功；批准和拒绝仍需二次确认并写入统一审计。':'The Telegram direct-message bot completed its latest long poll. Approvals and rejections still require confirmation and are written to the shared audit log.',
  '团队启用状态':'Team activation status', '团队启用路径':'Team activation path', '服务端前置条件':'Server-enforced prerequisites',
  '成员与职责':'Members and duties', '信号源':'Signal source', '交易账户范围':'Exchange account scope',
  '版本化风控':'Versioned risk controls', '明确进入影子模式':'Explicitly enter Shadow mode',
  '已满足':'Satisfied', '需处理':'Action required', '等待前置条件':'Waiting for prerequisites',
  '责任角色':'Responsible role', '处理此项 →':'Resolve this item →', '查看此项 →':'View this item →',
  '配置成员权限':'Configure member access', '查看信号源设置':'View signal-source settings',
  '查看交易账户':'View exchange accounts', '查看风险配置':'View risk controls',
  '创建第一笔影子提案':'Create the first Shadow proposal', '查看影子报表':'View Shadow reports',
  '初始化虚拟资金':'Initialize virtual capital', '团队管理员':'Team administrator',
  '交易运维或团队管理员':'Trading operator or team administrator', '未知服务端阻断':'Unknown server blocker',
  '提案、独立审核与交易运维职责已在至少一个精确账户范围内形成闭环。':'Proposal creation, independent review, and trading operations form a complete control loop for at least one exact account scope.',
  '当前团队已启用且只启用一种信号源模式。':'Exactly one signal-source mode is enabled for the current team.',
  '当前团队至少有一个启用的精确交易所账户范围。':'The current team has at least one active exact exchange-account scope.',
  '版本化风险政策已覆盖账户风险、单笔亏损、连续亏损与冷却期。':'The versioned risk policy covers account risk, single-trade loss, consecutive losses, and cooldown.',
  '先启用当前团队的 Perptape 或 Webhook 信号源':'Enable Perptape or Webhook for the current team',
  '先保存当前团队的版本化风险政策':'Save a versioned risk policy for the current team',
  '补齐单账户、单笔亏损、连续亏损与冷却期限制':'Complete account, single-loss, consecutive-loss, and cooldown limits',
  '先登记至少一个当前团队交易账户':'Register at least one exchange account for the current team',
  '至少配置两名不同成员承担提案与独立审核':'Assign proposal and independent-review duties to two different members',
  '至少配置一名交易运维人员':'Assign at least one trading operator',
  '全部服务端前置条件已满足；仍需团队管理员明确启用，系统不会自动打开业务能力。':'All server prerequisites are satisfied. A team administrator must still enable Shadow mode explicitly; the system never opens business capabilities automatically.',
  '按下面的依赖顺序处理。每项都标明影响、责任角色和下一步；缺少任何一项都会保持交易关闭。':'Resolve the dependencies below in order. Every item states its impact, owner, and next step; any missing item keeps trading closed.',
  '当前团队只允许 SHADOW 对象；真实订单、资金、签名和广播仍由服务端双重阻断。':'This team accepts Shadow objects only. Live orders, funding, signing, and broadcast remain blocked by the server.',
  '缺口不会放宽生产或影子边界；按依赖顺序补齐后再运行完整模拟链路。':'The gap does not relax Production or Shadow boundaries. Resolve it in dependency order before running the full simulation flow.',
  '生产与影子事实按环境、团队、账户、场所和标的分别存储；影子操作不会成为真实发送输入。':'Production and Shadow facts are stored separately by environment, team, account, venue, and instrument. Shadow actions never become live-send inputs.',
  '此清单直接投影':'This checklist directly projects',
  '返回的阻断码；页面不自行放宽条件。通知路由可独立配置，但不是当前影子启用的服务端门槛。':'blocker codes. The page never relaxes them. Notification routes are configured separately and are not a server prerequisite for Shadow activation.',
}));

const ENGLISH_PATTERNS = [
  [/^([0-9a-f]+…) · 查看详情$/i, '$1 · view details'],
  [/^(\d+) 个交易任务$/, '$1 trades'], [/^(\d+) 项阻断$/, '$1 blockers'], [/^(\d+) 项需要处理$/, '$1 issues require action'],
  [/^(\d+) 项阻断，查看详情$/, '$1 blockers; view details'],
  [/^(\d+) 项敞口不确定$/, '$1 exposure issues'], [/^(\d+) 项未一致$/, '$1 reconciliation issues'],
  [/^(\d+) 名启用成员$/, '$1 active users'], [/^(\d+) 个结果$/, '$1 results'], [/^(\d+) 条记录$/, '$1 records'],
  [/^显示 (\d+) \/ (\d+) 个机会$/, 'Showing $1 of $2 opportunities'], [/^(\d+) 分钟$/, '$1 minutes'],
  [/^截止 (.+)$/, 'As of $1'], [/^数据截止 (.+)$/, 'Data as of $1'], [/^完成于 (.+)$/, 'Completed $1'],
  [/^最近数据 (.+)$/, 'Latest data $1'], [/^创建于 (.+)$/, 'Created $1'], [/^版本 (\d+)$/, 'Version $1'],
  [/^提案 (.+)$/, 'Proposal $1'], [/^交易任务 (.+)$/, 'Trade $1'], [/^数量 (.+)$/, 'Quantity $1'],
  [/^最多 (.+)$/, 'Up to $1'], [/^上次 (.+)$/, 'Previous $1'], [/^触发价 (.+)$/, 'Trigger $1'],
  [/^(\d+) 笔成交$/, '$1 fills'], [/^(\d+) 个意图$/, '$1 intents'], [/^(\d+) 笔未签名交易$/, '$1 unsigned transactions'],
  [/^(\d+) 项恢复条件尚未满足。$/, '$1 recovery requirements are not yet satisfied.'],
  [/^(\d+) 笔已批准提案等待风险检查或启动$/, '$1 approved proposals await risk checks or trade setup'],
  [/^(\d+) 笔已批准提案尚未形成交易任务$/, '$1 approved proposals have not formed a trade'],
  [/^(\d+) \/ (\d+) 连接正常$/, '$1 of $2 connections healthy'],
  [/^(\d+) \/ (\d+) 可用$/, '$1 of $2 available'],
  [/^等待生产身份源绑定 · 创建于 (.+)$/, 'Waiting for production identity binding · created $1'],
  [/^(\d+) 项总阻断$/, '$1 total blockers'],
  [/^(\d+) 项条件仍需完成$/, '$1 prerequisites remain'],
  [/^生产团队仍有 (\d+) 项影子准备度缺口$/, 'Production Shadow-readiness gaps: $1'],
  [/^(\d+) 个影子交易任务已进入可复盘链路$/, '$1 Shadow trades are ready for review'],
  [/^(\d+) 个运行中交易任务$/, '$1 active trades'],
  [/^已读取 (\d+) 个候选，可用于机会筛选和提案。$/, '$1 candidates loaded and available for opportunity screening and proposals.'],
  [/^只读 · 最近数据 (.+)$/, 'Read only · latest data $1'],
  [/^(\d+) 项阻塞$/, '$1 blockers'],
  [/^核心服务可用，但 (?:币安|Binance)、Hyperliquid 的实时开仓条件受阻$/, 'Core services are available, but live entry requirements are blocked for Binance and Hyperliquid'],
  [/^(\d+) 个默认账户范围共有 (\d+) 项实时条件未通过；请进入风险控制逐项查看原因、负责人和下一步。通过检查的范围仍需逐笔复核；自动加仓保持关闭。$/, '$1 default-account scopes have $2 failed live requirements. Open Risk controls for the reason, owner, and next step. Passing scopes still require per-order checks; automatic scaling remains disabled.'],
  [/^(\d+) 个生产范围受阻$/, '$1 production scopes blocked'],
  [/^风险政策正常，但 (?:币安|Binance)、Hyperliquid 的实时安全条件未通过；通过检查的范围仍需逐笔复核。自动加仓已关闭。$/, 'The risk policy is normal, but live safety requirements failed for Binance and Hyperliquid. Passing scopes still require per-order checks. Automatic scaling is disabled.'],
  [/^(\d+) 项实时条件待处理；查看风险控制了解精确原因$/, '$1 live requirements need attention; open Risk controls for exact reasons'],
  [/^(?:币安|Binance) (\d+) 个合约；Hyperliquid (\d+) 个合约，其中 HIP-3 (\d+) 个。$/, 'Binance: $1 instruments; Hyperliquid: $2 instruments, including $3 HIP-3 markets.'],
  [/^最近成功 (.+)$/, 'Latest success $1'],
  [/^(Binance|Hyperliquid)：数据已过期$/, '$1: data stale'],
  [/^Vault：尚未同步$/, 'Vault: not synchronized'],
  [/^(\d+) 小时前更新 · 当前有效窗口 (\d+) 分钟 · (.+)$/, 'Updated $1 hours ago · current freshness window $2 minutes · $3'],
  [/^数据来源：(Binance|Hyperliquid|Vault) 只读账户$/, 'Source: $1 read-only account'],
  [/^尚无有效时间$/, 'No valid timestamp'],
  [/^Vault：尚未同步；(?:币安|Binance)：数据已过期（最后记录 (.+)）；Hyperliquid：数据已过期（最后记录 (.+)）。影响：当前三方总净值不计算，但其他有效单线继续保留。$/, 'Vault is not synchronized; Binance data is stale (last record $1); Hyperliquid data is stale (last record $2). Impact: the combined total is not calculated, while other valid series remain visible.'],
  [/^最近 (\d+) 小时 · (.+) 至 (.+) · (\d+) 处断档未连线$/, 'Last $1 hours · $2 to $3 · $4 gaps left unconnected'],
  [/^断档按实际采样节奏的 (\d+) 倍判定（至少 (\d+) 秒）；纵轴至少保留 (.+) 观察范围$/, 'A gap is detected at $1× the observed sampling interval (minimum $2 seconds); the y-axis keeps at least a $3 viewing range'],
  [/^(Binance|Hyperliquid) \$(.+) · (Binance|Hyperliquid)：数据已过期 · (.+)$/, '$1 $$$2 · $3: data stale · $4'],
  [/^\$(.+) · (Binance|Hyperliquid)：数据已过期 · (.+)$/, '$$$1 · $2: data stale · $3'],
  [/^在途 \/ 占用 (.+)$/, 'In transit / reserved $1'],
  [/^三方汇总 等待数据$/, 'Combined total · Waiting for data'],
  [/^币安、Hyperliquid、Vault 和三方汇总四条 USD 资金趋势$/, 'Four USD capital series: Binance, Hyperliquid, Vault, and Combined total'],
  [/^Hyperliquid 最新 ([^；]+)；图中最大单次变化为 (Binance|Hyperliquid) ([^，]+)，未达到 (.+) 异常阈值$/, 'Hyperliquid latest $1; the largest plotted change is $2 $3, below the $4 alert threshold'],
  [/^(Binance|Hyperliquid)：数据已过期；不计入当前净值$/, '$1: data stale; excluded from current net worth'],
  [/^系统约每 (\d+) 秒读取一次完整账户；新出现的仓位和委托会自动纳入，最近成交与资金费同步保存。$/, 'The system reads the complete account about every $1 seconds. New positions and orders are included automatically, with recent fills and funding saved together.'],
  [/^(.+) · 向上突破$/, '$1 · breakout higher'],
  [/^(.+) · 向下突破$/, '$1 · breakout lower'],
];

const ENGLISH_TERMS = [
  ['突破榜单','Breakout list'], ['链上资金库','On-chain vault'], ['链上永续','Hyperliquid'], ['币安','Binance'],
  ['生产环境','Production'], ['实盘','Live'], ['交易所账户','exchange account'], ['交易账户','exchange account'],
  ['交易任务','trade'], ['提案','proposal'], ['审核','review'], ['风险','risk'], ['对账','reconciliation'],
  ['仓位','position'], ['保护','protection'], ['数据','data'], ['状态','status'], ['账户','account'], ['交易所','exchange'],
  ['资金库','vault'], ['资金','capital'], ['订单','order'], ['成交','fill'], ['系统','system'], ['当前','current'],
  ['查看','view'], ['创建','create'], ['刷新','refresh'], ['处理','resolve'], ['等待','waiting'], ['需要','required'],
  ['已经','already'], ['没有','no'], ['尚未','not yet'], ['可以','can'], ['不能','cannot'], ['只读','read only'],
  ['正常','normal'], ['未知','unknown'], ['可用','available'], ['关闭','disabled'], ['开启','enabled'], ['来源','source'],
  ['时间','time'], ['数量','quantity'], ['金额','amount'], ['方向','direction'], ['结果','result'], ['原因','reason'],
  ['全部','all'], ['最新','latest'], ['范围','scope'], ['权限','permission'], ['成员','user'], ['记录','record'],
];

function translateEnglishText(value) {
  const source = String(value ?? '');
  const trimmed = source.trim();
  if (!trimmed) return source;
  const canonical = trimmed
    .replace(/\bPERPTAPE\b/g, 'Perptape')
    .replace(/\bHYPERLIQUID\b/g, 'Hyperliquid')
    .replace(/\bBINANCE\b/g, '币安')
    .replace(/([\u3400-\u9fff])\s+突破榜单/g, '$1突破榜单')
    .replace(/突破榜单\s+([\u3400-\u9fff])/g, '突破榜单$1');
  let translated = ENGLISH_EXACT.get(trimmed) || ENGLISH_EXACT.get(canonical);
  if (!translated) {
    for (const [pattern, replacement] of ENGLISH_PATTERNS) {
      if (pattern.test(canonical)) { translated = canonical.replace(pattern, replacement); break; }
    }
  }
  if (!translated) {
    translated = canonical;
    ENGLISH_TERMS.forEach(([from, to]) => { translated = translated.split(from).join(` ${to} `); });
    translated = translated.replace(/\s+([,.;:!?])/g, '$1').replace(/\s{2,}/g, ' ').trim();
    if (/[\u3400-\u9fff]/.test(translated)) translated = canonical;
  }
  if (!/[\u3400-\u9fff]/.test(translated)) {
    translated = translated.replaceAll('，', ',').replaceAll('；', ';').replaceAll('：', ':').replaceAll('。', '.').replaceAll('（', '(').replaceAll('）', ')');
  }
  return source.replace(trimmed, translated);
}

function translateChineseText(value) {
  return String(value ?? '')
    .replaceAll('Trading Console', '交易控制台')
    .replaceAll('PostgreSQL', '业务数据库')
    .replaceAll('HYPERLIQUID', 'Hyperliquid')
    .replaceAll('BINANCE', '币安')
    .replaceAll('Binance', '币安')
    .replaceAll('PERPTAPE', 'Perptape')
    .replaceAll('Passkey', '通行密钥')
    .replaceAll('Ethereum', '以太坊')
    .replaceAll('授权自有 Arbitrum 地址', '授权的自有 Arbitrum 钱包地址')
    .replaceAll('已授权 Arbitrum 自有地址', '已授权的自有 Arbitrum 钱包地址')
    .replaceAll('已授权自有地址', '已授权的自有钱包地址')
    .replaceAll('Hyperliquid Bridge 地址', 'Hyperliquid 充值桥地址（Bridge）')
    .replaceAll('Safe Allowance Module 的 delegate 额度', 'Safe 额度模块（Allowance Module）的委托额度')
    .replaceAll('Safe Smart Account 与 delegate 地址', 'Safe Smart Account 与委托地址（delegate）')
    .replaceAll('Safe Spending Limit delegate', 'Safe 委托地址（delegate）')
    .replaceAll('Safe Smart Account 和 delegate', 'Safe Smart Account 和委托地址（delegate）')
    .replaceAll('当前 USDC 额度、余额、重置周期与 nonce', '当前 USDC 额度、余额、重置周期与交易序号（nonce）')
    .replaceAll('人控 delegate 钱包', '人工控制的委托钱包（delegate）')
    .replaceAll('独立 delegate 钱包', '独立的委托钱包（delegate）')
    .replaceAll('API secret', 'API 密钥')
    .replaceAll('Agent 不支持', '系统不会代为签名或广播')
    .replaceAll('缺少官方金库或 Agent 范围', '缺少官方金库或 NoTilt Agent 授权范围')
    .replaceAll('Freqtrade worker', 'Freqtrade 执行进程')
    .replaceAll(' worker', ' 执行进程')
    .replaceAll('dry-run', '仿真模式')
    .replaceAll('Web 审核', '网页端审核')
    .replace(/([\u3400-\u9fff])\s+突破榜单/g, '$1突破榜单')
    .replace(/突破榜单\s+([\u3400-\u9fff])/g, '突破榜单$1')
    .replace(/\bLIVE\b/g, '实盘')
    .replace(/\bSHADOW\b/g, '模拟')
    .replace(/\bTESTNET\b/g, '测试网')
    .replace(/([\u3400-\u9fff）])\s+([\u3400-\u9fff（])/g, '$1$2');
}

const localizedText = (value) => currentLanguage === 'en' ? translateEnglishText(value) : translateChineseText(value);

function applyLanguageToDocument(root = document.body) {
  document.documentElement.lang = currentLanguage;
  document.title = currentLanguage === 'en' ? 'Trading Console' : '交易控制台';
  const description = document.querySelector('meta[name="description"]');
  if (description) description.content = currentLanguage === 'en'
    ? 'Production trading proposals, reviews, execution, capital, and risk controls'
    : '生产交易提案、审核、执行、资金与风险控制台';
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((node) => {
    if (node.parentElement?.closest('script, style, [translate="no"]')) return;
    const next = localizedText(node.nodeValue);
    if (next !== node.nodeValue) node.nodeValue = next;
  });
  root.querySelectorAll?.('[placeholder], [aria-label], [title]').forEach((element) => {
    ['placeholder','aria-label','title'].forEach((attribute) => {
      if (!element.hasAttribute(attribute)) return;
      const value = element.getAttribute(attribute);
      const next = localizedText(value);
      if (next !== value) element.setAttribute(attribute, next);
    });
  });
}

const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const shortId = (value) => value ? `${value.slice(0, 8)}…` : '—';
const fmtDate = (value) => value ? new Intl.DateTimeFormat(currentLanguage, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}).format(new Date(value)) : '—';
const fmtNumber = (value) => value === null || value === undefined ? '—' : new Intl.NumberFormat('en-US', {maximumFractionDigits: 6}).format(Number(value));
const fmtCompact = (value) => value === null || value === undefined ? localizedText('暂无数据') : new Intl.NumberFormat(currentLanguage, {notation:'compact', maximumFractionDigits:1}).format(Number(value));
const fmtAmount = (value, currency) => value === null || value === undefined ? '—' : `${fmtNumber(value)}${currency ? ` ${currency}` : ''}`;
const campaignCollateralCurrency = item => item.collateral_currency || item.instrument?.collateral_currency || '';
const isClosedFlatCampaign = item => item.status === 'CLOSED' && Number(item.current_target_quantity) === 0;
const campaignTargetLabel = item => isClosedFlatCampaign(item) ? localizedText('已平仓') : fmtNumber(item.current_target_quantity);
const campaignPnlLabel = (item, value) => fmtAmount(value, campaignCollateralCurrency(item));
const proposalAwaitingLaunch = item => item.execution_status === 'AWAITING_LAUNCH';
const proposalLaunchWindowExpired = item => item.execution_status === 'WINDOW_EXPIRED';
const isCurrentProposalItem = (item, operationsView = false) => ['DRAFT','PENDING_REVIEW'].includes(item.status)
  || (operationsView && proposalAwaitingLaunch(item));
const proposalStatusSupplement = item => proposalLaunchWindowExpired(item) ? localizedText('启动窗口已过期') : '';
const proposalExpiryPresentation = item => {
  if (proposalLaunchWindowExpired(item)) return {at:item.expires_at, state:localizedText('启动窗口已过期')};
  if (item.status === 'EXPIRED') {
    const expiryReached = new Date(item.expires_at).getTime() <= Date.now();
    return {at:expiryReached ? item.expires_at : item.updated_at || item.expires_at, state:localizedText('已结束')};
  }
  return {at:item.expires_at, state:fmtTimeRemaining(item.expires_at)};
};
const statusLabels = {DRAFT:'草稿',PENDING_REVIEW:'待审核',APPROVED:'已批准',REJECTED:'已拒绝',EXPIRED:'已过期',ALLOW:'通过',SCALE:'缩小仓位',DENY:'拒绝',PENDING:'等待中',RETRY_WAIT:'等待重试',SENDING:'发送中',DEAD_LETTER:'投递失败',OUTCOME_UNKNOWN:'发送结果未知',RESERVED:'已预留',READY:'待发送',DISPATCHING:'已派发，等待确认',SENT:'已发送',PARTIALLY_FILLED:'部分成交',FILLED:'已成交',CANCELLED:'已取消',UNKNOWN:'结果未知',KNOWN:'已确认',OPENING:'建仓中',OPEN:'持仓中',REDUCING:'减仓中',CLOSING:'退出中',CLOSED:'已结束',ACTIVE:'有效',DEGRADED:'保护不足',RELEASED:'已释放',MATCH:'对账一致',DIFFERENCE:'存在差异',MANUAL_REQUIRED:'需要人工处理',RESOLVED:'已解决',NORMAL:'正常',URGENT:'紧急',IMMEDIATE:'立即',ENABLED:'已开启',DISABLED:'已关闭',SUCCESS:'连接正常',FAILED:'连接失败',SKIPPED:'未运行',STALE:'数据已过期',WAITING:'等待首次同步',NOT_CONFIGURED:'未配置',ON_DEMAND:'按需读取',MISSING:'缺失',CURRENT:'当前有效',INCOMPLETE:'数据不完整',EMPTY:'暂无数据',AVAILABLE:'可用',CONTROLLED:'受控',READ_ONLY:'只读',BLOCKED:'已安全阻断',NOT_SUBMITTED:'未提交',SOURCE_RESERVED:'源端已预留',SUBMITTED:'已提交',IN_FLIGHT:'划转中',DESTINATION_CONFIRMED:'目的端已确认',SETTLED:'已结算',FAILED_SOURCE_RESTORED:'失败，源端已恢复',DEPOSIT_PLAN_READY:'充值计划待执行',DEPOSIT_CONFIRMED:'充值已确认',RELEASE_REQUEST_PLAN_READY:'释放申请计划待执行',RELEASE_REQUEST_CONFIRMED:'释放申请已确认',RELEASE_EXECUTION_PLAN_READY:'释放执行计划待执行',RELEASE_EXECUTION_CONFIRMED:'释放执行已确认',RELEASE_CANCELLATION_PLAN_READY:'释放取消计划待执行',RELEASE_CANCELLED:'释放已取消'};
const riskLabels = {LOW:'低风险',MEDIUM:'中风险',HIGH:'高风险'};
const intentKindLabels = {INITIAL:'初仓',ADD:'加仓',REDUCE:'减仓',EXIT:'退出'};
const directionLabels = {LONG:'做多',SHORT:'做空'};
const sideLabels = {BUY:'买入',SELL:'卖出'};
const sideEnglishLabels = {BUY:'Buy',SELL:'Sell'};
const capitalDirectionLabels = {VAULT_TO_VENUE:'资金库转入交易所',VENUE_TO_VAULT:'交易所转回资金库'};
const capitalPurposeLabels = {AUTO_PROFIT_SWEEP:'自动归集利润',AUTO_OPERATING_REFILL:'自动补充运营资金',MANUAL:'人工调配资金'};
const capitalTransportLabels = {MOCK:'模拟执行',NOTILT_UNSIGNED_HANDOFF:'NoTilt 未签名交接'};
const environmentLabels = {LIVE:'生产环境',SHADOW:'影子模式',TESTNET:'测试网',production:'生产环境',test:'测试环境',development:'开发环境',local:'本地环境'};
const currentWorkflowEnvironment = () => {
  const requested = new URLSearchParams(location.search).get('environment');
  if (['LIVE','SHADOW','TESTNET'].includes(requested)) return requested;
  return session?.active_team?.execution_mode === 'SHADOW' ? 'SHADOW' : 'LIVE';
};
const roleLabels = {OBSERVER:'只读用户',PROPOSER:'提案发起人',REVIEWER:'审核人',OPERATOR:'交易运维人员',TREASURY_ADMIN:'资金管理员',SYSTEM_ADMIN:'系统管理员',SYSTEM:'系统'};
const readinessLabels = {READY:'可用',DEGRADED:'数据不完整',INCOMPLETE:'数据不完整',STALE:'数据已过期'};
const connectionCategoryLabels = {
  READ_ONLY_CONNECTED:'只读已连接',
  READ_ONLY_CONNECTED_HISTORY_INCOMPLETE:'只读已连接，历史补全受限',
  CREDENTIALS_NOT_LOADED:'启动配置未加载',
  CONFIG_INCOMPLETE:'生产范围配置不完整',
  EXPLICITLY_DISABLED:'只读连接已关闭',
  NOT_YET_VERIFIED:'等待首次只读检查',
  PROBE_SKIPPED:'本轮检查已跳过',
  AUTH_OR_PERMISSION_FAILED:'只读鉴权或权限失败',
  UPSTREAM_RATE_LIMITED:'上游只读接口限流',
  NETWORK_OR_UPSTREAM_FAILED:'网络或上游不可达',
  UPSTREAM_RESPONSE_INVALID:'上游响应无效',
  READ_ONLY_PROBE_FAILED:'只读检查失败',
};
const connectionCategoryEnglishLabels = {
  READ_ONLY_CONNECTED:'Read-only connected',
  READ_ONLY_CONNECTED_HISTORY_INCOMPLETE:'Read-only connected; history incomplete',
  CREDENTIALS_NOT_LOADED:'Startup configuration not loaded',
  CONFIG_INCOMPLETE:'Production scope incomplete',
  EXPLICITLY_DISABLED:'Read-only connection disabled',
  NOT_YET_VERIFIED:'Waiting for the first read-only probe',
  PROBE_SKIPPED:'Latest probe skipped',
  AUTH_OR_PERMISSION_FAILED:'Read-only authentication or permission failed',
  UPSTREAM_RATE_LIMITED:'Upstream read-only API rate-limited',
  NETWORK_OR_UPSTREAM_FAILED:'Network or upstream unavailable',
  UPSTREAM_RESPONSE_INVALID:'Invalid upstream response',
  READ_ONLY_PROBE_FAILED:'Read-only probe failed',
};
const connectionEnglishCopy = {
  READ_ONLY_CONNECTED:['The latest side-effect-free read-only probe succeeded.','No action is required. Independent gates still block writes, orders, signing, and capital actions.'],
  READ_ONLY_CONNECTED_HISTORY_INCOMPLETE:['Current balances, positions, and orders are connected; historical fills or funding are incomplete.','Wait for the upstream history source to recover. New risk remains blocked.'],
  CREDENTIALS_NOT_LOADED:['This process did not load the required local credentials or public account identity.','Check the protected startup configuration source. Never paste credentials into the page or logs.'],
  CONFIG_INCOMPLETE:['Credentials or public identity are loaded, but the production account mapping or network scope is incomplete.','Complete the non-sensitive account mapping and authorized production scope, then retry.'],
  EXPLICITLY_DISABLED:['The process configuration explicitly disables this read-only connection.','Enable only the corresponding read-only setting and restart the reader.'],
  NOT_YET_VERIFIED:['Configuration is loaded, but this process has not completed a verifiable read-only probe.','Start read-only synchronization and wait for one bounded probe.'],
  PROBE_SKIPPED:['The latest read-only probe was skipped; an older result is not treated as current.','Check the reader setting and target mapping, then run read-only synchronization again.'],
  AUTH_OR_PERMISSION_FAILED:['Read-only authentication or account permission validation failed.','Confirm the credential belongs to the target production account and has only the required read permissions.'],
  UPSTREAM_RATE_LIMITED:['The upstream read-only API is rate-limiting requests; no new account facts were accepted.','Wait for the bounded automatic retry. If failures persist, check the upstream quota.'],
  NETWORK_OR_UPSTREAM_FAILED:['The official read-only API or local read-only gateway is currently unreachable.','Check the network and upstream status, then run one read-only retry.'],
  UPSTREAM_RESPONSE_INVALID:['The upstream response failed strict validation and was not accepted.','Check the upstream API version and account type, then retry.'],
  READ_ONLY_PROBE_FAILED:['The latest read-only probe failed; the data is not marked available.','Inspect the non-sensitive error category and rerun the read-only probe.'],
};
const venueModeLabels = {USER_DATA_READ_ONLY:'账户数据只读',INFO_READ_ONLY:'账户数据只读',READ_ONLY:'只读'};
const accountModeLabels = {PORTFOLIO_MARGIN:'统一账户',MAIN_ACCOUNT:'主账户',SUBACCOUNT:'子账户'};
const fmtIntentKind = (value) => intentKindLabels[value] || value || '未知意图';
const fmtDirection = (value) => directionLabels[value] || value || '未知方向';
const fmtSide = (value) => currentLanguage === 'en'
  ? sideEnglishLabels[value] || value || 'Unknown side'
  : sideLabels[value] || value || '未知方向';
const deploymentEnvironmentLabels = {
  LOCAL:'本地运行', TEST:'测试运行', PRODUCTION:'生产运行',
  LIVE:'真实环境', TESTNET:'测试网', SHADOW:'影子模式',
};
const deploymentEnvironmentEnglishLabels = {
  LOCAL:'Local runtime', TEST:'Test runtime', PRODUCTION:'Production runtime',
  LIVE:'Live environment', TESTNET:'Testnet', SHADOW:'Shadow mode',
};
const fmtEnvironment = (value, withCode = false) => {
  const code = String(value || '').trim().toUpperCase();
  const labels = currentLanguage === 'en'
    ? deploymentEnvironmentEnglishLabels
    : deploymentEnvironmentLabels;
  const label = labels[code] || (currentLanguage === 'en' ? 'Unknown environment' : '环境未确认');
  return withCode && code ? `${label} · ${code}` : label;
};
const fmtVenueLabel = (value) => currentLanguage === 'en'
  ? ({BINANCE:'Binance', HYPERLIQUID:'Hyperliquid', OKX:'OKX', BYBIT:'Bybit', '币安':'Binance', '链上永续':'Hyperliquid'}[value] || value || 'Unknown venue')
  : ({BINANCE:'币安', HYPERLIQUID:'Hyperliquid', OKX:'OKX', BYBIT:'Bybit', '币安':'币安', '链上永续':'Hyperliquid'}[value] || value || '交易所未配置');
const fmtDefaultAccountLabel = (accountId) => accountId
  ? localizedText('默认账户')
  : localizedText('账户未配置');
const fmtRole = (value) => roleLabels[value] || value || '未分配角色';
const fmtReadiness = (value) => readinessLabels[value] || fmtStatus(value);
const fmtConnectionCategory = (value) => currentLanguage === 'en'
  ? connectionCategoryEnglishLabels[value] || value || 'Not verified'
  : connectionCategoryLabels[value] || value || '尚未验证';
const fmtConnectionReason = (state) => currentLanguage === 'en'
  ? connectionEnglishCopy[state?.category]?.[0] || 'No verified connection reason is available.'
  : fmtOperationalCopy(state?.reason);
const fmtConnectionNextAction = (state) => currentLanguage === 'en'
  ? connectionEnglishCopy[state?.category]?.[1] || 'Ask a system administrator to inspect the read-only connection.'
  : fmtOperationalCopy(state?.next_action);
const fmtOperationalCopy = (value) => String(value ?? '—')
  .replaceAll(';', '；')
  .replaceAll(',', '，')
  .replaceAll('独立 Gate', '独立安全开关')
  .replaceAll('Gate', '安全开关')
  .replaceAll('安全开关 阻断', '安全开关阻断');
const exchangeAccountCopy = {
  'account policy is eligible; global and task gates still apply':'账户政策允许；全局与任务安全开关仍需逐项通过',
  'account eligibility is blocked because a required connection or runtime fact was lost':'账户交易资格已阻断；必需的连接或运行事实已失效',
  'trading capability is disabled; connection status never enables order sending':'交易能力已关闭；连接状态不会开启下单',
  'add encrypted credentials':'添加加密凭据',
  'run a supported no-side-effect connection verification':'运行无副作用只读连接验证',
  'enable the database-bound continuous read-only sync':'启用数据库凭据绑定的连续只读同步',
  'wait for an implemented trading connector; keep trading disabled':'等待交易写入适配器实现；继续保持交易关闭',
  'verify global, sender, risk, and task gates before LIVE execution':'真实执行前逐项确认全局、发送者、风控与任务安全开关',
  'explicitly enable exact-account trading eligibility when approved':'批准后显式启用当前账户的交易资格',
  'keep trading disabled until risk and live-send gates are explicitly approved':'保持交易关闭，直至风险与真实发送安全开关获得明确批准',
};
const fmtExchangeAccountCopy = (value) => currentLanguage === 'en'
  ? String(value || '—')
  : (exchangeAccountCopy[String(value || '')] || fmtOperationalCopy(value));

function fmtConnectionCapability(key, state) {
  if (state.write_process_enabled) {
    return currentLanguage === 'en'
      ? 'Safety fault: the write-process switch must remain disabled'
      : '安全异常：写入进程开关不应开启';
  }
  if (state.available) {
    if (key === 'NOTILT') {
      return currentLanguage === 'en'
        ? 'Live read-only capital facts; signing and broadcasting disabled'
        : '实时只读资金事实；签名与广播关闭';
    }
    if (key === 'PERPTAPE') {
      return currentLanguage === 'en'
        ? 'Live read-only opportunity data; no trading capability'
        : '实时只读机会数据；不提供交易能力';
    }
    return currentLanguage === 'en'
      ? 'Live read-only account facts; orders and writes disabled'
      : '实时只读账户事实；下单与写入关闭';
  }
  if (state.last_success_at) {
    return key === 'NOTILT'
      ? (currentLanguage === 'en' ? 'Saved snapshot only; live capital facts unavailable; signing and broadcasting disabled' : '仅可查看历史快照；实时资金事实不可用；签名与广播关闭')
      : key === 'PERPTAPE'
        ? (currentLanguage === 'en' ? 'Saved snapshot only; live opportunities unavailable' : '仅可查看历史快照；实时机会不可用')
        : (currentLanguage === 'en' ? 'Saved snapshot only; live account facts unavailable; orders and writes disabled' : '仅可查看历史快照；实时账户事实不可用；下单与写入关闭');
  }
  return key === 'NOTILT'
    ? (currentLanguage === 'en' ? 'No verified capital facts; signing and broadcasting disabled' : '暂无可核验资金事实；签名与广播关闭')
    : key === 'PERPTAPE'
      ? (currentLanguage === 'en' ? 'No verified live opportunity data' : '暂无可核验实时机会数据')
      : (currentLanguage === 'en' ? 'No verified account facts; orders and writes disabled' : '暂无可核验账户事实；下单与写入关闭');
}
function fmtTargetReason(value) {
  const normalized = String(value || '').trim();
  if (!normalized) return '—';
  if (normalized.startsWith('FREQTRADE_EMERGENCY_RECOVERY:')) {
    return localizedText('受控执行恢复：交易所成交与仓位已经核对，目标已降至 0。');
  }
  const labels = {
    KILL_SWITCH:'风险紧急停止已触发，目标降至 0。',
    FROZEN_INVALIDATION_REACHED:'冻结提案的失效价格已触达，目标降至 0。',
  };
  const reasons = normalized.split(',').map(item => item.trim()).filter(Boolean);
  if (reasons.length && reasons.every(item => labels[item])) {
    return localizedText(reasons.map(item => labels[item]).join('；'));
  }
  if (/^[A-Z0-9_:., -]+$/.test(normalized)) {
    return localizedText('系统已根据当前风险条件更新目标。');
  }
  return fmtOperationalCopy(normalized);
}
const fmtCapitalDirection = (value) => capitalDirectionLabels[value] || value || '未知方向';
const fmtCapitalPurpose = (value) => capitalPurposeLabels[value] || value || '未说明用途';
const fmtCapitalTransport = (value) => capitalTransportLabels[value] || value || '未记录执行方式';
const exceptionGuidance = {
  CAMPAIGN_UNKNOWN:{priority:1,title:'交易任务状态不确定',copy:'系统无法确认这笔交易当前处于哪个阶段，因此不会继续增加风险。',next:'先核对订单、成交和仓位，再运行对账。'},
  ORDER_DISPATCH_UNRESOLVED:{priority:1,title:'订单已派发，结果等待确认',copy:'外部写入前的派发快照已经持久化；系统只会查询同一派发，不会再次发送。',next:'等待受控执行进程查询原派发；超时后按结果未知进行对账。'},
  ORDER_INTENT_UNKNOWN:{priority:1,title:'订单结果不确定',copy:'发送结果可能成功也可能失败，不能把超时当作失败后重发。',next:'到交易所核对原订单与成交，然后运行对账。'},
  RISK_RESERVATION_UNKNOWN:{priority:1,title:'风险占用不确定',copy:'这部分风险继续占用总容量，不能提前释放给另一笔交易。',next:'先查清原订单结果；对账一致后再处理风险预留。'},
  POSITION_UNKNOWN:{priority:2,title:'当前仓位未知',copy:'缺少可信仓位事实，系统不能把“没读到”当成“已经平仓”。',next:'从交易所同步当前仓位；不确定时不要把数量填成 0。'},
  POSITION_STALE:{priority:2,title:'仓位事实已过期',copy:'上次仓位观测超过风险政策允许的有效期，不能据此继续管理风险。',next:'重新同步交易所仓位，再判断保护和下一步。'},
  PROTECTION_UNKNOWN:{priority:3,title:'保护状态未知',copy:'系统不能确认止损或原生保护是否真实存在并仍然有效。',next:'核对交易所保护单；无法确认时优先减仓或退出。'},
  PROTECTION_STALE:{priority:3,title:'保护事实已过期',copy:'曾经有效的保护不能证明现在仍有效，必须重新确认。',next:'同步最新保护单及覆盖数量。'},
  PROTECTION_INSUFFICIENT:{priority:3,title:'保护数量不足',copy:'当前保护不能完整覆盖已知仓位，继续持有会暴露超出计划的风险。',next:'先补齐保护；做不到时立即减仓或退出。'},
  RECONCILIATION_UNKNOWN:{priority:4,title:'对账结果未知',copy:'系统与交易所事实尚不能形成可信结论。',next:'补齐缺失事实后重新运行计算型对账。'},
  RECONCILIATION_DIFFERENCE:{priority:4,title:'对账存在差异',copy:'订单、成交、仓位或保护至少有一项与系统预期不一致。',next:'逐项核对差异；不要在差异未解决时新增风险。'},
  RECONCILIATION_MANUAL_REQUIRED:{priority:4,title:'对账需要人工处理',copy:'自动对账无法安全决定如何恢复，当前风险继续受限。',next:'按差异清单核实交易所事实并记录人工结论。'},
  RECONCILIATION_RESOLVED:{priority:4,title:'仍需新的计算型对账',copy:'人工标记已处理不等于交易所与系统已经重新一致。',next:'更新事实后再运行一次计算型对账。'},
  RECONCILIATION_STALE:{priority:4,title:'对账早于最新事实',copy:'最近对账发生后仓位或订单意图又有变化，旧结论已经失效。',next:'以最新仓位和订单事实重新运行对账。'},
};
const explainException = (code) => exceptionGuidance[code] || {priority:9,title:'需要人工核实',copy:'系统发现一项无法自动解释的阻断事实。',next:'进入交易任务查看技术详情并完成对账。'};
const exceptionCategory = (code) => {
  if (['CAMPAIGN_UNKNOWN','ORDER_DISPATCH_UNRESOLVED','ORDER_INTENT_UNKNOWN','RISK_RESERVATION_UNKNOWN'].includes(code)) return '结果未知';
  if (['POSITION_UNKNOWN','PROTECTION_UNKNOWN'].includes(code)) return '事实缺失';
  if (String(code).endsWith('_STALE')) return '数据过期';
  if (code === 'PROTECTION_INSUFFICIENT') return '保护不足';
  if (String(code).startsWith('RECONCILIATION_')) return '对账差异';
  return '运行阻断';
};
function formatExceptionDetail(value) {
  const detail = String(value || '');
  if (detail.startsWith('observed_at=')) return `最近有效事实：${fmtDate(detail.slice(12))}`;
  if (detail.startsWith('max_age_seconds=')) return `最长有效期：${fmtSeconds(detail.slice(16))}`;
  const labels = {
    POSITION_FACT_NEWER:'仓位事实晚于最近对账',
    ORDER_INTENT_NEWER:'订单意图晚于最近对账',
  };
  return labels[detail] || fmtOperationalCopy(detail);
}
const riskReasonGuidance = {
  INVALID_INPUT:{label:'风险输入无效',action:'检查计划数量、最大风险和风险政策后重新运行。'},
  READ_ONLY_SOURCE_UNAVAILABLE:{label:'交易所只读连接当前不可用',action:'等待所属交易所只读探针恢复并完成一次成功检查后重试。'},
  STALE_FACTS:{label:'账户事实已经过期',action:'刷新交易所仓位、权益和受管资金事实后重新检查。'},
  POSITION_UNKNOWN:{label:'仓位状态未知',action:'完成该账户与标的的仓位同步和对账后重新检查。'},
  EQUITY_UNKNOWN:{label:'资金权益未知',action:'刷新交易所权益和受管资金事实后重新检查。'},
  PROTECTION_UNKNOWN:{label:'现有仓位保护不足',action:'确认保护单有效且足额覆盖后重新检查。'},
  KILL_SWITCH:{label:'系统处于紧急停止',action:'当前只能对账、减仓或退出；排障后通过受控流程恢复。'},
  REDUCE_ONLY:{label:'系统仅允许降低风险',action:'当前只能对账、减仓或退出；恢复新增风险需要受控审核。'},
  PYRAMID_DISABLED:{label:'自动加仓已关闭',action:'初仓不受影响；加仓需要新的受控授权。'},
  RISK_CAPACITY_EXHAUSTED:{label:'总风险容量已经用完',action:'等待其他风险释放，或由受控流程调整风险政策。'},
  RISK_CAPACITY_SCALED:{label:'系统缩小了可用仓位',action:'授权只会采用系统批准后的较小数量和风险金额。'},
};
const actionErrorGuidance = {
  INITIAL_INTENT_ALREADY_EXISTS:'这个冻结提案已经创建过初仓意图。请进入原交易任务继续处理，不能重复开仓。',
  ACTIVE_ORDER_INTENT:'当前交易任务还有未完成意图。请先确认原意图结果，不要重复提交。',
  AUTHORIZATION_EXPIRED:'短期授权已经过期。请重新运行风险检查，再签发新授权。',
  AUTHORIZATION_INACTIVE:'短期授权已失效，不能继续新增风险。',
  AUTHORIZATION_RISK_STATE_INVALID:'系统当前不允许新增风险；只能对账、减仓或退出。',
  RISK_DECISION_CONTROL_CHANGED:'风险政策已变化。请重新运行风险检查。',
  PROPOSAL_EXPIRED:'提案已经过期，需要按当前事实创建新提案。',
  CAMPAIGN_POSITION_NOT_CLOSED:'仓位尚未被确认清零，或平仓事实已经过期。请先同步最新仓位。',
  CAMPAIGN_EXIT_NOT_TERMINAL:'退出意图尚未结束。请先确认成交、取消或拒绝结果。',
  RECONCILIATION_REQUIRED:'关闭前需要在最新仓位和退出结果之后重新完成一致对账。',
  RISK_RESERVATION_UNRESOLVED:'风险预留仍处于不确定或待确认状态，必须先完成对账。',
};
const apiErrorGuidance = {
  LOGIN_DENIED:'用户名或密码不正确。',
  LOGIN_RATE_LIMITED:'登录尝试过多，请稍后再试。',
  RISK_POLICY_MISSING:'风险政策尚未配置，因此系统已暂停创建和执行新增风险。请联系系统管理员完成配置。',
  PERPTAPE_NOT_CONFIGURED:'Perptape 尚未配置。人工提案仍可使用，外部机会将在完成配置后恢复。',
  PERPTAPE_UNAVAILABLE:'暂时无法连接 Perptape。人工提案仍可使用，请稍后重新检查外部机会。',
  PERPTAPE_RUNTIME_FEED_MISSING:'Perptape 正在等待首次同步，请稍后重新检查。',
  PERPTAPE_RUNTIME_FEED_STALE:'Perptape 最近数据已经过期，系统不会把旧候选当成实时机会。',
  PERPTAPE_CACHE_INVALID:'Perptape 已保存的数据无法读取，请联系交易运维人员处理。',
  INSTRUMENT_UNAVAILABLE:'该交易合约尚未进入可交易合约目录，暂时不能创建提案。',
  RBAC_DENIED:'当前身份没有查看或执行此操作的权限。',
  TEAM_NOT_OPERATIONAL:'当前团队仍处于安全配置阶段。请先完成团队账户、风险政策和数据范围配置。',
  TEAM_CONTEXT_REQUIRED:'请先选择一个当前团队。',
  WORKSPACE_CONTEXT_REQUIRED:'请先选择一个当前 Workspace。',
  TEAM_ACCESS_DENIED:'你不是该团队的有效成员，系统已拒绝切换。',
  TEAM_SCOPE_DENIED:'该资源不属于当前团队；请切换到正确团队后重试。',
  WORKSPACE_ACCESS_DENIED:'你不是该 Workspace 的有效成员，系统已拒绝切换。',
  CAPABILITY_FORBIDDEN:'当前身份没有查看或执行此操作的权限。',
  LIVE_SCOPE_CONFIGURATION_REQUIRED:'实盘账户或交易所范围尚未配置完整。',
  NOTILT_RELEASE_BUDGET_MISSING:'当前资产没有可用的 NoTilt 实时额度，系统不会生成释放请求。',
  NOTILT_RELEASE_SCOPE_MISMATCH:'NoTilt 实时资金范围与已配置金库不一致，请由系统管理员核对配置。',
  NOTILT_VAULT_UNTRUSTED:'当前金库不在 NoTilt 官方可信部署目录中，系统已阻断。',
  NOTILT_WHITELIST_INACTIVE:'NoTilt 白名单尚未生效或未指向当前金库，系统已阻断。',
  NOTILT_AGENT_OWNER_FORBIDDEN:'当前 Agent 与金库所有者身份冲突，不能使用 Agent 额度路径。',
  NOTILT_PANIC_LOCKED:'NoTilt 金库处于紧急锁定状态，不能构建资金请求。',
  NOTILT_FACT_STALE:'NoTilt 实时额度已过期，请刷新资金事实后重试。',
  NOTILT_RELEASE_LIMIT_EXCEEDED:'金额超过 NoTilt 当前实时可释放上限，请降低金额或等待额度恢复。',
};
const fmtStatus = (value) => statusLabels[value] || value || '未知';
const fmtRisk = (value) => riskLabels[value] || value || '未知';
const riskGuidance = (reason) => riskReasonGuidance[reason] || {label:'风险检查未通过',action:'查看当前风险事实，处理阻塞后重新检查。'};
const friendlyApiError = (error) => {
  const risk = riskReasonGuidance[error?.code] || riskReasonGuidance[error?.message];
  if (risk) return `${risk.label}：${risk.action}`;
  if (actionErrorGuidance[error?.code]) return actionErrorGuidance[error.code];
  if (apiErrorGuidance[error?.code]) return apiErrorGuidance[error.code];
  if (['REQUEST_TIMEOUT','REQUEST_ABORTED','NETWORK_ERROR'].includes(error?.code) && error?.message) return error.message;
  return '系统暂时无法完成请求，请稍后重试；如果问题持续存在，请联系系统管理员。';
};
const fmtSeconds = (value) => {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  return `${Math.round(seconds / 60)} 分钟`;
};
const fmtTimeRemaining = (value) => {
  const remaining = new Date(value).getTime() - Date.now();
  if (!Number.isFinite(remaining) || remaining <= 0) return '已到期';
  const minutes = Math.max(1, Math.ceil(remaining / 60000));
  if (minutes < 60) return `剩余 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return `剩余 ${hours} 小时${remainder ? ` ${remainder} 分钟` : ''}`;
};
const factStatusLabel = (value) => ({KNOWN:'已确认',ACTIVE:'有效',NOT_REQUIRED:'不需要',MISSING:'缺失',UNKNOWN:'未知'}[value] || value || '未知');
const percentageDistance = (from, to) => {
  const base = Number(from); const target = Number(to);
  if (!base || !target) return '—';
  return `${Math.abs((target - base) / base * 100).toFixed(2)}%`;
};
const roleNames = () => (session?.roles || []).map((item) => item.role);
const currentRoleSummary = () => {
  const roles = roleNames();
  if (roles.includes('SYSTEM_ADMIN')) return '系统管理员（最高权限）';
  return roles.map(fmtRole).join('、') || '未分配岗位';
};
const capabilityRoles = {
  'signal.view':['OBSERVER','PROPOSER','REVIEWER','OPERATOR'],
  'opportunity.view':['OBSERVER','PROPOSER'],
  'proposal.view':['OBSERVER','PROPOSER','REVIEWER','OPERATOR'],
  'operations.view':['OBSERVER','OPERATOR'],
  'results.view':['OBSERVER','OPERATOR'],
  'notification.view':['OBSERVER','PROPOSER','REVIEWER','OPERATOR','TREASURY_ADMIN'],
  'system.view':['OBSERVER','REVIEWER','OPERATOR'],
  'venue.view':['OBSERVER','OPERATOR'],
  'venue.sync':['OPERATOR'],
  'capital.view':['TREASURY_ADMIN','SYSTEM_ADMIN'],
  'proposal.create':['PROPOSER'],
  'proposal.review':['REVIEWER'],
  'access.manage':['SYSTEM_ADMIN'],
};
const hasCapability = (capability) => (
  roleNames().includes('SYSTEM_ADMIN')
  || (capabilityRoles[capability] || []).some(role => roleNames().includes(role))
);
const currentWorkspaceMembership = () => (session?.workspaces || []).find(
  workspace => workspace.workspace_id === session?.active_workspace?.workspace_id
);
const routeCapability = (path) => {
  if (path === '/' || path === '/workspaces' || path === '/home') return null;
  if (path === '/capital') return 'capital.view';
  if (path === '/opportunities/defaults') return 'proposal.create';
  if (path === '/opportunities') return 'opportunity.view';
  if (path === '/signals') return 'signal.view';
  if (path === '/proposals/new') return 'proposal.create';
  if (path === '/reviews') return 'proposal.review';
  if (path === '/proposals' || path.startsWith('/proposals/')) return 'proposal.view';
  if (path === '/campaigns' || path.startsWith('/campaigns/') || path === '/orders' || path === '/exceptions') return 'operations.view';
  if (path === '/shadow') return 'venue.view';
  if (path === '/results') return 'results.view';
  if (path === '/notifications') return 'notification.view';
  if (path === '/positions' || path === '/risk') return 'system.view';
  if (path === '/venues' || path.startsWith('/venues/')) return 'venue.view';
  if (path === '/admin/users' || path === '/admin/agents') return 'access.manage';
  return 'operations.view';
};
const capabilityLabel = (capability) => ({'signal.view':'查看信号源','opportunity.view':'查看机会','proposal.view':'查看提案','operations.view':'交易运维','results.view':'查看绩效报表','notification.view':'查看通知中心','system.view':'查看系统状态','venue.view':'查看交易账户','capital.view':'资金管理','proposal.create':'发起提案','proposal.review':'独立审核','access.manage':'成员权限管理'}[capability] || capability);
const accessRoleCatalog = [
  {role:'OBSERVER', label:'只读观察', copy:'查看机会、提案、交易任务、系统状态和交易账户；不能执行动作。'},
  {role:'PROPOSER', label:'发起提案', copy:'查看机会并创建提案；不能审核自己的提案，也不能操作交易任务。'},
  {role:'REVIEWER', label:'独立审核', copy:'独立审核冻结提案与风险恢复申请；不能发起提案、操作交易或查看资金。'},
  {role:'OPERATOR', label:'交易运维', copy:'运行风险、授权、订单、减仓、对账和交易所同步；不自动获得资金权限。'},
  {role:'TREASURY_ADMIN', label:'资金管理', copy:'查看与管理资金数据、划转和资金对账；与交易运维职责分离。'},
  {role:'SYSTEM_ADMIN', label:'超级管理员', copy:'管理所有成员并可访问资金中心；所有资金动作仍受实时校验、最终确认和安全开关约束。'},
];
const loginDestination = () => {
  return '/';
};

async function api(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const mutation = !['GET', 'HEAD'].includes(method);
  const controller = new AbortController();
  let didTimeout = false;
  const timeout = setTimeout(() => {
    didTimeout = true;
    controller.abort();
  }, REQUEST_TIMEOUT_MS);
  const externalSignal = options.signal;
  const abortFromExternalSignal = () => controller.abort(externalSignal.reason);
  if (externalSignal) {
    if (externalSignal.aborted) abortFromExternalSignal();
    else externalSignal.addEventListener('abort', abortFromExternalSignal, {once:true});
  }
  let response;
  let data;
  try {
    response = await fetch(path, {
      credentials: 'same-origin',
      ...options,
      signal: controller.signal,
      headers: {'content-type': 'application/json', ...(options.headers || {})}
    });
    if (response.status === 204) data = null;
    else {
      try { data = await response.json(); }
      catch (error) {
        if (error.name === 'AbortError') throw error;
        data = {};
      }
    }
  } catch (error) {
    if (error.name === 'AbortError') {
      if (!didTimeout) {
        const abortedError = new Error('请求已取消');
        abortedError.code = 'REQUEST_ABORTED';
        abortedError.status = 499;
        throw abortedError;
      }
      const message = mutation
        ? '操作在 15 秒内未收到确认。这是可恢复错误：按钮已恢复；请先刷新当前页面并核对权威状态，确认结果后再决定是否重试。'
        : '读取超过 15 秒，请检查网络或服务状态后重试';
      const timeoutError = new Error(message);
      timeoutError.code = 'REQUEST_TIMEOUT';
      timeoutError.status = 408;
      timeoutError.outcomeUnknown = mutation;
      throw timeoutError;
    }
    const message = mutation
      ? '连接中断，操作结果可能未知。这是可恢复错误：按钮已恢复；请先刷新当前页面并核对权威状态，确认结果后再决定是否重试。'
      : '无法连接控制台服务，请检查网络后重试';
    const networkError = new Error(message);
    networkError.code = 'NETWORK_ERROR';
    networkError.status = 0;
    networkError.outcomeUnknown = mutation;
    throw networkError;
  } finally {
    clearTimeout(timeout);
    externalSignal?.removeEventListener('abort', abortFromExternalSignal);
  }
  if (!response.ok) {
    const detailError = data?.detail?.error;
    const error = new Error(
      data?.error?.message
      || detailError?.message
      || data?.detail?.message
      || data?.detail?.error_code
      || `HTTP ${response.status}`
    );
    error.code = data?.error?.code || detailError?.code || data?.detail?.error_code || `HTTP_${response.status}`;
    error.status = response.status;
    error.handled = response.status === 401 && handleUnauthorizedResponse();
    throw error;
  }
  return data;
}

function showToast(message, kind = 'success') {
  if (toastTimer) clearTimeout(toastTimer);
  toast.textContent = localizedText(message);
  toast.classList.toggle('error', kind === 'error');
  toast.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  toast.setAttribute('aria-live', kind === 'error' ? 'assertive' : 'polite');
  toast.classList.add('show');
  toastTimer = setTimeout(() => toast.classList.remove('show'), kind === 'error' ? 5200 : 3200);
}

function handleUnauthorizedResponse() {
  if (authFailureActive) return true;
  if (!session) return false;
  authFailureActive = true;
  session = null;
  sessionNotice = '会话已失效。请重新验证内部身份，完成后会返回当前页面。';
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = null;
  toast.classList.remove('show', 'error');
  toast.textContent = '';
  toast.setAttribute('role', 'status');
  toast.setAttribute('aria-live', 'polite');
  if (dialog.open) dialog.close();
  if (confirmDialog.open) confirmDialog.close();
  setShell(false);
  renderLogin();
  enhanceRenderedPage();
  return true;
}

function showApiError(error, target = null) {
  if (error?.handled) return;
  const message = friendlyApiError(error);
  if (target) target.textContent = localizedText(message);
  else showToast(message, 'error');
}

function workspaceDefaultTeamId(workspace) {
  if (!workspace) return null;
  return workspace.default_team_id
    || (session?.teams || []).find(team => team.workspace_id === workspace.workspace_id && team.slug === 'default')?.team_id
    || (session?.teams || []).find(team => team.workspace_id === workspace.workspace_id)?.team_id
    || null;
}

function renderWorkspaceSwitcher() {
  if (!session) return;
  const activeWorkspaceId = session.active_workspace?.workspace_id;
  const activeWorkspace = (session.workspaces || []).find(item => item.workspace_id === activeWorkspaceId);
  const nameTarget = scopeSwitcher.querySelector('[data-workspace-name]');
  if (nameTarget) nameTarget.textContent = activeWorkspace?.name || '选择工作区';
  scopeSwitcherMenu.innerHTML = `<div class="workspace-switcher-menu-head"><span>工作区</span><small>${(session.workspaces || []).length} 个可用</small></div>
    <div class="workspace-switcher-options">${(session.workspaces || []).map(workspace => {
      const selected = workspace.workspace_id === activeWorkspaceId;
      const teamId = workspaceDefaultTeamId(workspace);
      return `<button class="workspace-switcher-option ${selected ? 'is-active' : ''}" type="button" role="menuitem" data-switch-workspace="${escapeHtml(workspace.workspace_id)}" data-switch-team="${escapeHtml(teamId || '')}"><span class="workspace-option-avatar" aria-hidden="true">${escapeHtml(workspace.name.slice(0, 1).toUpperCase())}</span><span><b>${escapeHtml(workspace.name)}</b><small>${escapeHtml(`${workspace.member_count ?? 0} 名成员${workspace.agent_count ? ` · ${workspace.agent_count} 个 Agent` : ''}`)}</small></span><strong>${selected ? '当前' : '进入'}</strong></button>`;
    }).join('')}</div>
    <div class="workspace-switcher-menu-actions"><a href="/" data-link role="menuitem">创建新工作区</a><a href="/" data-link role="menuitem">查看所有工作区</a></div>`;
}

function closeWorkspaceSwitcher({restoreFocus = false} = {}) {
  scopeSwitcherMenu.hidden = true;
  scopeSwitcher.setAttribute('aria-expanded', 'false');
  if (restoreFocus && !scopeSwitcher.hidden) scopeSwitcher.focus();
}

function setShell(loggedIn, {workspaceGate = false} = {}) {
  sidebar.hidden = !loggedIn || workspaceGate;
  identityChip.hidden = !loggedIn;
  scopeControl.hidden = !loggedIn || workspaceGate;
  mobileNavToggle.hidden = !loggedIn || workspaceGate;
  if (loggedIn) {
    renderWorkspaceSwitcher();
    const rolePriority = ['SYSTEM_ADMIN','TREASURY_ADMIN','OPERATOR','REVIEWER','PROPOSER','OBSERVER'];
    const primaryRole = rolePriority.find(role => roleNames().includes(role));
    const identity = `${session.username} · ${localizedText(primaryRole ? fmtRole(primaryRole) : '未分配角色')}`;
    const scopeDetail = `${session.active_workspace?.name || '未选择 Workspace'} / ${session.active_team?.name || '未选择团队'}`;
    const identityDetail = `${session.username} · ${scopeDetail} · ${roleNames().map(role => localizedText(fmtRole(role))).join(' / ') || localizedText('未分配角色')}`;
    identityChip.innerHTML = `<strong>${escapeHtml(session.username)}</strong><span>${escapeHtml(localizedText(primaryRole ? fmtRole(primaryRole) : '未分配角色'))}</span>`;
    identityChip.title = identityDetail;
    identityChip.setAttribute('aria-label', identityDetail);
    mobileSessionSummary.textContent = identity;
    mobileSessionSummary.title = identityDetail;
    document.querySelectorAll('[data-nav-capability]').forEach(link => {
      link.hidden = !hasCapability(link.dataset.navCapability);
    });
    document.querySelectorAll('[data-nav-section]').forEach(section => {
      section.hidden = ![...section.querySelectorAll('a')].some(link => !link.hidden);
    });
  }
  closeWorkspaceSwitcher();
  closeMobileNav({restoreFocus:false});
}

function errorView(error, retry = true) {
  const guidance = errorStateGuidance(error);
  return `<section class="error-state" role="alert" aria-live="assertive" aria-labelledby="error-state-title"><div><p class="eyebrow">运行状态 · 已安全阻断</p><h2 id="error-state-title" data-error-heading tabindex="-1">${escapeHtml(guidance.title)}</h2><p class="error-state-reason">${escapeHtml(guidance.reason)}</p><dl class="error-state-guidance"><div><dt>影响</dt><dd>${escapeHtml(guidance.impact)}</dd></div><div><dt>负责角色</dt><dd>${escapeHtml(guidance.owner)}</dd></div><div><dt>下一步</dt><dd>${escapeHtml(guidance.next)}</dd></div><div><dt>技术状态</dt><dd><code>${escapeHtml(error?.code || `HTTP_${error?.status || 'UNKNOWN'}`)}</code></dd></div></dl><div class="error-state-actions">${retry ? '<button class="primary" data-retry>重新检查</button>' : ''}<a class="secondary" href="/home" data-link>返回当前任务</a></div></div></section>`;
}

function errorStateGuidance(error) {
  const reason = friendlyApiError(error);
  if (error?.code === 'NETWORK_ERROR') return {
    title:'连接已中断', reason,
    impact:'本页事实未更新；离线前画面不得视为当前状态，新增风险与写入操作保持阻断。',
    owner:'平台运维或网络管理员',
    next:'确认服务与网络恢复后重新检查；恢复前不要重复提交任何结果未知的操作。',
  };
  if (error?.code === 'REQUEST_TIMEOUT') return {
    title:'读取超时', reason,
    impact:'本页事实未在时限内确认；新增风险与写入操作保持阻断。',
    owner:'平台运维或网络管理员',
    next:'先确认服务负载与网络状态，再重新检查；写入超时必须先核对权威状态。',
  };
  if (error?.status === 403 || error?.code === 'HTTP_403') return {
    title:'当前范围没有访问权限', reason,
    impact:'当前页面不会加载团队或账户数据，也不会开放任何操作。',
    owner:'团队管理员',
    next:'返回当前任务，或由团队管理员核对岗位、账户与交易所范围。',
  };
  if (error?.code === 'RISK_POLICY_MISSING') return {
    title:'风险政策尚未配置', reason,
    impact:'新增风险、影子启用及相关写入保持阻断；既有减仓与退出边界不因页面提示而改变。',
    owner:'系统管理员',
    next:'保存当前团队的版本化风险政策后重新检查。',
  };
  return {
    title:'当前数据无法读取', reason,
    impact:'本页事实不可用；依赖这些事实的操作保持阻断。',
    owner:'当前功能负责人或系统管理员',
    next:'根据上方原因恢复所需事实后重新检查。',
  };
}

function cancelMobileNavFocus() {
  mobileNavFocusToken += 1;
  if (mobileNavFocusFrame !== null) cancelAnimationFrame(mobileNavFocusFrame);
  mobileNavFocusFrame = null;
}

function openMobileNav() {
  if (!session || !matchMedia('(max-width: 980px)').matches) return;
  cancelMobileNavFocus();
  sidebar.classList.add('open');
  sidebar.inert = false;
  sidebar.setAttribute('aria-hidden', 'false');
  navBackdrop.hidden = false;
  mobileNavToggle.setAttribute('aria-expanded', 'true');
  document.body.classList.add('nav-open');
  main.inert = true;
  const focusToken = mobileNavFocusToken;
  mobileNavFocusFrame = requestAnimationFrame(() => {
    if (focusToken !== mobileNavFocusToken) return;
    mobileNavFocusFrame = null;
    const target = sidebar.querySelector('nav a');
    if (
      session &&
      sidebar.classList.contains('open') &&
      !sidebar.hidden &&
      !sidebar.inert &&
      getComputedStyle(sidebar).visibility === 'visible' &&
      target?.isConnected
    ) target.focus();
  });
}

function closeMobileNav({restoreFocus = true} = {}) {
  cancelMobileNavFocus();
  const mobile = matchMedia('(max-width: 980px)').matches;
  sidebar.classList.remove('open');
  navBackdrop.hidden = true;
  mobileNavToggle.setAttribute('aria-expanded', 'false');
  document.body.classList.remove('nav-open');
  main.inert = false;
  sidebar.inert = sidebar.hidden || mobile;
  sidebar.setAttribute('aria-hidden', String(sidebar.hidden || mobile));
  if (restoreFocus && !mobileNavToggle.hidden) mobileNavToggle.focus();
}

function syncNavigationMode() {
  closeMobileNav({restoreFocus:false});
}

function bindLinkedRows() {
  document.querySelectorAll('tr[data-href]').forEach((row) => {
    if (row.dataset.rowBound === 'true') return;
    row.dataset.rowBound = 'true';
    const firstCell = row.querySelector('td');
    if (firstCell && !firstCell.querySelector('.row-link')) {
      const context = firstCell.textContent.trim().replace(/\s+/g, ' ');
      const link = document.createElement('a');
      link.className = 'row-link';
      link.href = row.dataset.href;
      link.dataset.link = '';
      link.textContent = '查看详情';
      link.setAttribute('aria-label', `查看 ${context}`);
      firstCell.append(link);
    }
    row.addEventListener('click', (event) => {
      if (event.target.closest('a, button, input, select, textarea')) return;
      navigate(row.dataset.href);
    });
  });
}

function enhanceTables() {
  document.querySelectorAll('.table-wrap').forEach((wrapper) => {
    if ((wrapper.closest('.risk-condition-details') || wrapper.matches('.connection-status-table')) && matchMedia('(max-width: 780px)').matches) return;
    const heading = wrapper.closest('section')?.querySelector('h2, h1')?.textContent || '数据表格';
    if (wrapper.scrollWidth <= wrapper.clientWidth + 1) return;
    wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region');
    wrapper.setAttribute('aria-label', `${heading}，可横向滚动`);
    wrapper.classList.add('is-scrollable');
    if (wrapper.previousElementSibling?.matches('[data-table-hint]')) return;
    const hint = document.createElement('p');
    hint.className = 'table-scroll-hint';
    hint.dataset.tableHint = '';
    hint.textContent = '横向滑动或使用方向键查看完整表格';
    wrapper.before(hint);
  });
}

function enhanceRenderedPage() {
  bindLinkedRows();
  enhanceTables();
  applyLanguageToDocument();
}

function confirmAction({title, message, confirmLabel}) {
  document.querySelector('#confirm-title').textContent = localizedText(title);
  document.querySelector('#confirm-message').textContent = localizedText(message);
  document.querySelector('#confirm-submit').textContent = localizedText(confirmLabel || '确认并继续');
  confirmDialog.returnValue = '';
  confirmDialog.showModal();
  return new Promise((resolve) => {
    confirmDialog.addEventListener('close', () => resolve(confirmDialog.returnValue === 'confirm'), {once:true});
  });
}

async function withPending(button, pendingLabel, action) {
  if (!button || button.dataset.pending === 'true') return undefined;
  const originalLabel = button.textContent;
  button.dataset.pending = 'true';
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  button.textContent = localizedText(pendingLabel);
  try {
    return await action();
  } finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    delete button.dataset.pending;
    button.textContent = originalLabel;
  }
}

function formNumber(value, fallback = '') {
  if (value === null || value === undefined || value === '') return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? String(parsed) : String(value);
}

async function bootstrap() {
  authStatus = await api('/api/auth/status');
  environmentBadge.textContent = fmtEnvironment(authStatus?.environment);
  environmentBadge.dataset.environment = String(authStatus?.environment || 'unknown').toLowerCase();
  try {
    const result = await api('/api/auth/session');
    session = result.session;
  } catch (error) {
    if (error.status !== 401) console.error(error);
  }
  setShell(Boolean(session), {workspaceGate:Boolean(session) && ['/', '/workspaces'].includes(location.pathname)});
  await route();
}

async function route() {
  if (location.pathname !== '/opportunities') stopOpportunityStream();
  window.scrollTo(0, 0);
  updateActiveNav();
  closeMobileNav({restoreFocus:false});
  if (!session) {
    setShell(false);
    renderLogin();
    enhanceRenderedPage();
    return;
  }
  const path = location.pathname;
  if (path === '/' || path === '/workspaces') {
    setShell(true, {workspaceGate:true});
    renderWorkspaceGateway();
    enhanceRenderedPage();
    return;
  }
  setShell(true);
  const teamSetupPaths = new Set(['/admin/users', '/admin/agents', '/venues', '/signals', '/notifications', '/risk', '/shadow']);
  if (!session.active_workspace || !session.active_team || (!session.active_team.trading_enabled && !teamSetupPaths.has(path))) {
    renderScopeSetup();
    enhanceRenderedPage();
    return;
  }
  const requiredCapability = routeCapability(path);
  if (requiredCapability && !hasCapability(requiredCapability)) {
    main.innerHTML = `<section class="empty-state"><div><p class="eyebrow">权限范围</p><h2>当前职责不包含这个页面</h2><p>此页面需要“${escapeHtml(capabilityLabel(requiredCapability))}”权限。侧栏只展示当前身份可用入口；直接打开链接也不会绕过服务端权限。</p><div class="toolbar empty-actions"><a class="secondary" href="/home" data-link>返回当前任务</a>${hasCapability('capital.view') ? '<a class="primary" href="/capital" data-link>进入资金中心</a>' : ''}</div></div></section>`;
    enhanceRenderedPage();
    return;
  }
  main.innerHTML = '<section class="loading-state"><span class="spinner"></span><p>正在读取当前事实…</p></section>';
  try {
    if (path === '/home') await renderHome();
    else if (path === '/signals') await renderSignalSources();
    else if (path === '/opportunities') await renderOpportunities();
    else if (path === '/opportunities/defaults') await renderOpportunityDefaults();
    else if (path === '/proposals/new') await renderManualProposal();
    else if (path === '/reviews') await renderProposalList('PENDING_REVIEW', '审核队列');
    else if (path === '/proposals') {
      const historyMode = new URLSearchParams(location.search).get('history') === '1';
      await renderProposalList(null, historyMode ? '历史提案' : '当前提案', historyMode);
    }
    else if (path === '/campaigns') await renderCampaignList();
    else if (path === '/shadow') await renderShadowWorkspace();
    else if (path === '/results') await renderActualResults();
    else if (path === '/notifications') await renderNotifications();
    else if (path === '/campaigns/alerts') await renderRuntimeAlerts();
    else if (path === '/positions') await renderSystemStatus();
    else if (path === '/orders') await renderCampaignFacts('orders');
    else if (path === '/risk') await renderCampaignFacts('risk');
    else if (path === '/capital') await renderCapitalCenter();
    else if (path === '/exceptions') { history.replaceState({}, '', '/campaigns/alerts'); await renderRuntimeAlerts(); }
    else if (path === '/venues' || path === '/venues/binance' || path === '/venues/hyperliquid') await renderVenueFacts();
    else if (path === '/admin/users') await renderAccessManagement();
    else if (path === '/admin/agents') await renderAgentManagement();
    else {
      const campaignMatch = path.match(/^\/campaigns\/([0-9a-f-]+)$/i);
      const proposalMatch = path.match(/^\/proposals\/([0-9a-f-]+)$/i);
      if (campaignMatch) await renderCampaignDetail(campaignMatch[1]);
      else if (proposalMatch) await renderProposalDetail(proposalMatch[1]);
      else main.innerHTML = '<section class="empty-state"><div><h2>页面不存在</h2><a class="primary" href="/opportunities" data-link>返回机会页</a></div></section>';
    }
    enhanceRenderedPage();
  } catch (error) {
    if (error.status === 401) {
      if (!error.handled) handleUnauthorizedResponse();
      return;
    }
    main.innerHTML = errorView(error);
    enhanceRenderedPage();
    main.querySelector('[data-error-heading]')?.focus();
  }
}
