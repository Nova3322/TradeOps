const main = document.querySelector('#main');
const sidebar = document.querySelector('#sidebar');
const identityChip = document.querySelector('#identity-chip');
const environmentBadge = document.querySelector('#environment-badge');
const languageToggle = document.querySelector('#language-toggle');
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
let sessionNotice = '';
let toastTimer = null;
let authFailureActive = false;
let mobileNavFocusFrame = null;
let mobileNavFocusToken = 0;
const REQUEST_TIMEOUT_MS = 15000;
const LANGUAGE_STORAGE_KEY = 'trading-language';
let currentLanguage = localStorage.getItem(LANGUAGE_STORAGE_KEY) === 'en' ? 'en' : 'zh-CN';

const ENGLISH_EXACT = new Map(Object.entries({
  '交易控制台':'Trading Console', '生产交易管理':'Production trading operations', '生产环境':'Production',
  '语言':'Language', '切换语言':'Switch language', '切换主题':'Switch theme', '菜单':'Menu',
  '主导航':'Main navigation', '今日':'Today', '机会':'Opportunities', '审核队列':'Review queue',
  '交易任务':'Trades', '系统状态':'System status', '资金':'Capital', '异常':'Exceptions',
  '结果与审计':'Results & audit', '交易账户':'Exchange accounts', '成员权限':'Access control',
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
  '当前职责不包含这个页面':'This page is outside your assigned role', '返回今日':'Back to Today', '进入资金中心':'Open Capital',
  '页面不存在':'Page not found', '返回机会页':'Back to Opportunities', '内部访问':'Internal access',
  '进入交易控制台':'Open Trading Console', '需要统一身份登录':'Identity verification required',
  '内部用户名':'Internal username', '进入控制台':'Open console', '统一身份服务尚未接入。':'The identity service is not connected.',
  '审核工作台':'Review workspace', '当前没有需要你审核的提案':'There are no proposals waiting for your review',
  '进入审核队列':'Open review queue', '提案工作台':'Proposal workspace', '从机会开始形成交易判断':'Start a trading thesis from an opportunity',
  '查看机会':'View opportunities', '当前可见提案':'Visible proposals', '待审核':'Pending review', '资金工作台':'Capital workspace',
  '今日只显示你的资金职责':'Today shows only your capital responsibilities', '尚未分配职责':'No role assigned',
  '当前身份尚未分配业务职责':'No business responsibilities are assigned to this account', '风险提醒':'Risk alert',
  '处理风险异常':'Resolve risk exceptions', '查看风险异常':'View risk exceptions', '新增风险受限':'New risk restricted',
  '查看限制与恢复条件':'View restrictions and recovery conditions', '需要审核':'Review required', '查看审核队列':'View review queue',
  '交易运行中':'Trades are active', '查看运行中交易任务':'View active trades', '当前无待办':'Nothing requires action',
  '查看市场机会':'View market opportunities', '受影响交易任务':'Affected trades', '非本人待审核':'Independent reviews pending',
  '运行中交易任务':'Active trades', '新增风险状态':'New-risk status', '处理顺序':'Priority order',
  '现在按这个顺序处理':'Handle items in this order', '刷新当前数据':'Refresh data', '新的交易判断':'New trading thesis',
  '市场观察':'Market watch', '开始新的判断':'Start a new thesis', '继续观察市场机会':'Continue watching opportunities',
  '查看突破榜单机会':'View breakout opportunities', '创建人工提案':'Create manual proposal', '系统边界':'System controls',
  '当前控制状态':'Current control state', '站点环境':'Runtime', '风险政策':'Risk policy', '自动加仓':'Automatic scaling',
  '安全原则':'Safety rule', '数据缺失即阻断':'Missing data blocks trading', '连接不可用':'Connection unavailable',
  '人工提案':'Manual proposal', '刷新机会':'Refresh opportunities', '突破榜单数据源':'Breakout data source',
  '外部机会当前不可用':'External opportunities are unavailable', '当前候选':'Current candidates', '可创建提案':'Eligible proposals',
  '可交易合约':'Tradable instruments', '数据截止':'Data as of', '数据源状态':'Source status', '不可用':'Unavailable',
  '连接正常':'Connected', '交易所':'Exchange', '全部':'All', '币对':'Symbol', '共振周期':'Aligned timeframe',
  '全部周期':'All timeframes', '方向':'Direction', '做多':'Long', '做空':'Short', '最低成交量':'Minimum volume',
  '最低持仓量':'Minimum open interest', '不限':'No minimum', '清除筛选':'Clear filters', '参考价格':'Reference price',
  '触发时间':'Triggered at', '数据状态':'Data status', '成交量':'Volume', '持仓量':'Open interest',
  '交易所图表 ↗':'Exchange chart ↗', '突破榜单 ↗':'Breakout list ↗', '高级配置':'Advanced settings', '一键创建':'Create now',
  '没有符合条件的机会':'No opportunities match these filters', '等待机会数据恢复':'Waiting for opportunity data',
  '当前没有突破候选':'No breakout candidates right now', '人工创建交易提案':'Manual trading proposal',
  '创建人工提案':'Create manual proposal', '返回机会':'Back to Opportunities', '交易意图':'Trading intent',
  '风险边界':'Risk limits', '交易标的':'Instrument', '触发价格':'Trigger price', '最大持仓数量':'Maximum position size',
  '高级执行参数':'Advanced execution settings', '限价（可选）':'Limit price (optional)', '提案理由':'Rationale',
  '创建并提交审核':'Create and submit for review', '提案预览':'Proposal preview', '提交前摘要':'Summary before submission',
  '选择交易标的':'Select an instrument', '计划名义价值':'Planned notional', '失效距离':'Distance to invalidation',
  '有效期':'Expires in', '补全交易意图':'Complete the trading intent', '补全风险边界':'Complete the risk limits',
  '只创建提案，不直接下单':'Creates a proposal only; no order is placed', '提案审核':'Proposal review', '全部提案':'All proposals',
  '当前列表':'Current list', '等待审核':'Waiting for review', '高风险':'High risk', '30 分钟内到期':'Expires within 30 minutes',
  '待我审核':'Assigned to me', '搜索标的或账户':'Search instrument or account', '全部方向':'All directions',
  '风险':'Risk', '全部档位':'All levels', '提交时间':'Submitted', '到期':'Expires', '状态':'Status',
  '数量':'Quantity', '最多':'Up to', '人工':'Manual', '版本':'Version', '当前没有待你审核的提案':'No proposals are waiting for your review',
  '当前没有匹配提案':'No matching proposals', '没有符合条件的提案':'No proposals match these filters',
  '交易系统状态':'Trading system status', '刷新状态':'Refresh status', '查看风险控制':'View risk controls', '当前结论':'Current conclusion',
  '无需立即动作':'No immediate action', '核心服务':'Core services', '开仓与加仓':'Entry and scaling',
  '减仓与退出':'Reduce and exit', '止损与保护监控':'Stop-loss and protection monitoring', '风险敞口监控':'Exposure monitoring',
  '对账监控':'Reconciliation monitoring', '突破榜单机会源':'Breakout opportunity source', '服务可用':'Available',
  '服务不可用':'Unavailable', '当前无运行中任务':'No active trades', '当前无监控对象':'Nothing to monitor',
  '暂无对账对象':'Nothing to reconcile', '监控正常':'Monitoring normally', '对账一致':'Reconciled',
  '外部数据连接':'External connections', '交易所与机会源':'Exchanges and opportunity source', '数据源':'Source',
  '读取状态':'Read status', '运行范围':'Operating scope', '写入能力':'Write capability', '查看账户数据 →':'View account data →',
  '查看机会 →':'View opportunities →', '当前阻断':'Current blockers', '需要处理的问题类型':'Issues requiring action',
  '查看恢复步骤':'View recovery steps', '交易账户数据 · 只读':'Exchange account data · read only',
  '账户与同步':'Account and sync', '查看账户':'View account', '同步并对账':'Sync and reconcile', '连接状态':'Connection status',
  '运行模式':'Operating mode', '最后同步':'Last sync', '账户数据已保存':'Account data saved', '尚无数据':'No data yet',
  '权益':'Equity', '可用余额':'Available balance', '账户状态':'Account status', '最近对账':'Latest reconciliation',
  '仓位与风险保护':'Positions and protection', '当前委托':'Open orders', '最近成交':'Recent fills', '资金费':'Funding',
  '标的':'Instrument', '标记价':'Mark price', '更新时间':'Updated', '交易所订单':'Exchange order', '关联操作':'Related action',
  '成交编号':'Fill ID', '价格':'Price', '手续费':'Fee', '成交时间':'Filled at', '支付编号':'Payment ID', '金额':'Amount',
  '支付时间':'Paid at', '当前没有已保存的数据。':'No saved data.', '资金中心':'Capital', '总净值':'Total net worth',
  '链上资金库净值':'On-chain vault net worth', '净值状态':'Net-worth status', '资金划转控制':'Transfer control',
  '在途 / 占用':'In transit / reserved', '资金快照':'Capital snapshot', '资金构成':'Capital composition',
  '资金位置':'Capital locations', '资金提案':'Capital proposals', '资金划转':'Capital transfers', '位置':'Location',
  '已确认可用':'Confirmed available', '美元净值':'USD value', '源端预留':'Source reserved', '有效可用':'Effective available',
  '控制 / 充值':'Control / deposit', '提案':'Proposal', '路径':'Route', '动作':'Actions', '划转记录':'Transfer record',
  '划转总额':'Gross amount', '状态 / 对账':'Status / reconciliation', '外部引用':'External reference',
  '交易结果':'Trading results', '结果':'Results', '按结算币种看结果':'Results by settlement currency', '交易任务结果记录':'Trade results',
  '盈亏与成本明细':'P&L and costs', '已关闭交易任务的累计盈亏与绝对回撤':'Cumulative P&L and absolute drawdown for closed trades',
  '权限与操作记录':'Access and activity log', '币种':'Currency', '已实现':'Realized', '未实现':'Unrealized',
  '最终 / 当前':'Final / current', '滑点':'Slippage', '盈亏':'P&L', '费用 / 资金费 / 滑点':'Fees / funding / slippage',
  '操作者':'Actor', '事件 / 对象':'Event / object', '原因':'Reason', '关联编号 / 版本':'Reference / version',
  '异常与恢复':'Exceptions and recovery', '刷新当前数据':'Refresh data', '阻断问题':'Blocking issues', '结果未知':'Unknown outcome',
  '数据过期':'Stale data', '恢复队列':'Recovery queue', '下一步：':'Next: ', '打开交易任务并按顺序处理':'Open trade and follow the steps',
  '当前运行中的交易任务没有阻断异常':'No blocking exceptions in active trades', '成员权限':'Access control', '权限分离原则':'Separation of duties',
  '审核与发起分开':'Proposal and review are separate', '交易与资金分开':'Trading and treasury are separate',
  '身份与权限分开':'Identity and authorization are separate', '新增内部成员':'Add internal member', '展开':'Expand',
  '账户范围':'Account scope', '交易所范围':'Exchange scope', '常用模板':'Role templates', '只审核':'Review only',
  '只发起提案':'Propose only', '交易运维':'Trading operations', '创建成员':'Create member', '当前用户':'Current users',
  '现有成员':'Existing members', '已启用':'Enabled', '已停用':'Disabled', '保存权限':'Save access',
  '允许登录和使用已分配权限':'Allow sign-in and assigned permissions', '低':'Low', '中':'Medium', '高':'High',
  '否':'No', '是':'Yes', '未知':'Unknown', '未配置':'Not configured', '已关闭':'Disabled', '只读':'Read only',
  '可用':'Available', '正常':'Normal', '未运行':'Not run', '未提交':'Not submitted', '生产':'Production', '已结束':'Closed',
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
  '建仓中 / 持仓中':'Opening / open', '运行范围':'Operating scope', '生产交易':'Production trading',
  '提案通过审核和风险检查后，交易运维人员才能发起开仓。':'Trading operations can open a position only after proposal approval and risk checks pass.',
  '这里直接说明系统能否工作、哪些能力受限，以及是否需要处理。绿色表示当前证据正常；黄色表示能力受限；红色表示必须先处理；灰色表示当前没有监控对象。':'This page shows whether the trading system is operational, which capabilities are limited, and what needs attention. Green is healthy, yellow is limited, red requires action, and gray means there is nothing to monitor.',
  '交易管理可用，但突破榜单机会源受限':'Trading operations are available, but the breakout feed is limited',
  '尚未配置。现有交易任务仍可管理，但新的突破榜单机会暂不可用。':'The breakout feed is not configured. Existing trades remain manageable, but new feed-based opportunities are unavailable.',
  '业务数据库和交易服务运行正常。':'The business database and trading services are healthy.',
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
  '生产账户 · 数据读取':'Production accounts · data access',
  '在这里查看币安和链上永续账户的连接、余额、仓位、委托、成交、资金费与对账。日常交易仍从交易任务、系统状态和异常页面进入。':'Use this page to inspect Binance and Hyperliquid connections, balances, positions, orders, fills, funding, and reconciliation. Daily operations still start from Trades, System status, and Exceptions.',
  '刷新账户数据':'Refresh account data', '选择交易所':'Select exchange', '账户数据只读':'Account data read only',
  '统一账户':'Unified account', '账户数据已保存':'Account data saved',
  '切换账户时只读取当前身份获准查看的范围。点击同步后，系统会从交易所获取数据、保存并立即运行对账。':'Changing accounts only reads scopes assigned to the current identity. Sync fetches exchange data, saves it, and immediately runs reconciliation.',
  '生产读取连接尚未配置或已关闭。页面只展示已经保存的数据，不会用其他数据填充。':'The production read connection is not configured or is disabled. This page shows saved data only and never fills gaps with substitute data.',
  '权益状态':'Equity status', '仓位与风险保护':'Positions and risk protection',
  '系统管理 · 权限配置':'System administration · access configuration',
  '按岗位勾选权限，不需要逐个页面配置。一个人可以组合多个岗位，还可以按账户和交易所限制每个用户能够查看和操作的数据。':'Assign permissions by role instead of configuring each page. A user can hold multiple roles, with optional account and exchange scopes limiting visible and actionable data.',
  '审核人不能审核自己的提案；提案发起人不会自动获得执行权限。':'Reviewers cannot review their own proposals, and proposers do not automatically receive execution permission.',
  '交易运维人员看不到资金中心；系统管理员也不会自动获得资金权限。':'Trading operators cannot access Capital, and system administrators do not automatically receive treasury permissions.',
  '生产环境仍由统一身份登录和通行密钥（Passkey）认证；这里仅管理内部授权，不创建密码。':'Production uses centralized identity and passkey authentication. This page manages internal authorization only and does not create passwords.',
  '生产环境仍由统一身份登录和通行密钥认证；这里仅管理内部授权，不创建密码。':'Production uses centralized identity and passkey authentication. This page manages internal authorization only and does not create passwords.',
  '先创建授权记录，再由生产身份服务完成身份绑定':'Create the authorization record first, then bind it through the production identity service',
  '全部交易所':'All exchanges', '等待生产身份源绑定':'Waiting for production identity binding',
  '这是当前账号。为避免误锁死，必须由另一名系统管理员修改。':'This is the current account. Another system administrator must make changes to prevent accidental lockout.',
  '只读观察':'Read-only observer', '查看机会、提案、交易任务、系统状态、交易账户和结果；不能执行动作。':'View opportunities, proposals, trades, system status, exchange accounts, and results without taking actions.',
  '发起提案':'Proposal creator', '查看机会并创建提案；不能审核自己的提案，也不能操作交易任务。':'View opportunities and create proposals. Cannot review own proposals or operate trades.',
  '独立审核':'Independent reviewer', '只处理冻结提案的审核；不能发起、执行或查看资金。':'Review frozen proposals only. Cannot propose, execute, or view capital.',
  '运行风险、授权、订单、减仓、对账和交易所同步；不自动获得资金权限。':'Run risk checks, authorizations, orders, reductions, reconciliation, and exchange sync without treasury access.',
  '资金管理':'Treasury management', '查看与管理资金数据、划转和资金对账；与交易运维职责分离。':'View and manage capital data, transfers, and capital reconciliation separately from trading operations.',
  '系统管理':'System administration', '管理成员与系统控制；不会自动获得资金管理权限。':'Manage users and system controls without automatically receiving treasury permissions.',
  '留空 = 全部账户':'Blank = all accounts',
  '自动资金划转':'Automatic capital transfer', '审核后自动准备，钱包只做最终确认':'Automatically prepared after approval; the wallet only confirms',
  '1. 自动复核':'1. Automatic checks', '重新检查空仓、未决订单、对账、资金余额和链上额度。':'Recheck flat positions, unresolved orders, reconciliation, balances, and on-chain limits.',
  '2. 自动准备':'2. Automatic preparation', '预留资金并生成严格限定目标、资产和金额的链上交易计划。':'Reserve funds and generate an on-chain plan restricted to the approved target, asset, and amount.',
  '3. 自动跟踪':'3. Automatic tracking', '验证链上回执并持续对账；结果未知时立即阻断后续动作。':'Verify on-chain receipts and keep reconciling. Unknown outcomes immediately block the next action.',
  '交易控制台不保存私钥，也不替钱包签名或广播。钱包确认前会明确显示链、目标地址、资产、金额和资金库。':'The console never stores private keys or signs and broadcasts for the wallet. Before confirmation it shows the chain, target address, asset, amount, and vault.',
  '创建生产资金提案':'Create production capital proposal', '创建并进入审核':'Create and send for review',
  '提交后由两名独立审核人确认。审核通过后，一键启动自动划转：系统复核额度、预留资金、生成链上计划并跟踪回执；独立钱包负责最终签名确认。':'Two independent reviewers must approve. After approval, one action starts the automated transfer workflow: the system rechecks limits, reserves funds, prepares the on-chain plan, and tracks receipts; an independent wallet provides the final signature.',
  '开始自动划转':'Start automatic transfer', '继续自动划转':'Continue automatic transfer',
  '新建人工提案':'New manual proposal', '查看提案':'View proposals',
  '新增风险已受限，减仓和退出仍可用':'New risk is restricted; reductions and exits remain available',
  '减仓和退出不受阻断。':'Reductions and exits remain available.', '查看突破榜单机会':'View breakout opportunities',
  '突破榜单 · 连接不可用':'Breakout list · connection unavailable',
  '先查看突破榜单候选；没有合适信号时，也可以点击“人工提案”自行录入。无论哪种方式，都只会创建提案，并且必须经过独立审核。':'Review breakout candidates first. If none fits, use Manual proposal to enter your own thesis. Either path creates a proposal and requires independent review.',
  '突破榜单数据源':'Breakout feed',
  '突破榜单尚未配置。人工提案仍可使用，外部机会将在完成配置后恢复。 系统不会把过期候选当成当前机会。':'The breakout feed is not configured. Manual proposals remain available, and external opportunities will return after configuration. Stale candidates are never presented as current opportunities.',
  '人工提案仍然可用；突破榜单恢复后可以再次刷新。':'Manual proposals remain available. Refresh again after the breakout feed recovers.',
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
}));

const ENGLISH_PATTERNS = [
  [/^(\d+) 个交易任务$/, '$1 trades'], [/^(\d+) 项阻断$/, '$1 blockers'], [/^(\d+) 项需要处理$/, '$1 issues require action'],
  [/^(\d+) 项敞口不确定$/, '$1 exposure issues'], [/^(\d+) 项未一致$/, '$1 reconciliation issues'],
  [/^(\d+) 名启用成员$/, '$1 active users'], [/^(\d+) 个结果$/, '$1 results'], [/^(\d+) 条记录$/, '$1 records'],
  [/^显示 (\d+) \/ (\d+) 个机会$/, 'Showing $1 of $2 opportunities'], [/^(\d+) 分钟$/, '$1 minutes'],
  [/^截止 (.+)$/, 'As of $1'], [/^数据截止 (.+)$/, 'Data as of $1'], [/^完成于 (.+)$/, 'Completed $1'],
  [/^最近数据 (.+)$/, 'Latest data $1'], [/^创建于 (.+)$/, 'Created $1'], [/^版本 (\d+)$/, 'Version $1'],
  [/^提案 (.+)$/, 'Proposal $1'], [/^交易任务 (.+)$/, 'Trade $1'], [/^数量 (.+)$/, 'Quantity $1'],
  [/^最多 (.+)$/, 'Up to $1'], [/^上次 (.+)$/, 'Previous $1'], [/^触发价 (.+)$/, 'Trigger $1'],
  [/^(\d+) 笔成交$/, '$1 fills'], [/^(\d+) 个意图$/, '$1 intents'], [/^(\d+) 笔未签名交易$/, '$1 unsigned transactions'],
  [/^(\d+) 项恢复条件尚未满足。$/, '$1 recovery requirements are not yet satisfied.'],
  [/^(\d+) \/ (\d+) 连接正常$/, '$1 of $2 connections healthy'],
  [/^等待生产身份源绑定 · 创建于 (.+)$/, 'Waiting for production identity binding · created $1'],
  [/^(\d+) 项总阻断$/, '$1 total blockers'],
  [/^(\d+) 个运行中交易任务$/, '$1 active trades'],
  [/^只读 · 最近数据 (.+)$/, 'Read only · latest data $1'],
  [/^(\d+) 项阻塞$/, '$1 blockers'],
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
    .replace(/\bPERPTAPE\b/gi, '突破榜单')
    .replace(/\bHYPERLIQUID\b/g, '链上永续')
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
    translated = translated.replace(/[\u3400-\u9fff]+/g, ' details ');
    translated = translated.replace(/\s+([,.;:!?])/g, '$1').replace(/\s{2,}/g, ' ').trim();
  }
  return source.replace(trimmed, translated);
}

function translateChineseText(value) {
  return String(value ?? '')
    .replaceAll('Trading Console', '交易控制台')
    .replaceAll('PostgreSQL', '业务数据库')
    .replaceAll('HYPERLIQUID', '链上永续')
    .replaceAll('Hyperliquid', '链上永续')
    .replaceAll('BINANCE', '币安')
    .replaceAll('Binance', '币安')
    .replaceAll('PERPTAPE', '突破榜单')
    .replaceAll('Perptape', '突破榜单')
    .replaceAll('NoTilt', '链上资金库')
    .replaceAll('Telegram', '消息通知')
    .replaceAll('Passkey', '通行密钥')
    .replaceAll('Arbitrum', '阿比特鲁姆')
    .replaceAll('Ethereum', '以太坊')
    .replaceAll('BNB Chain', '币安智能链')
    .replace(/([\u3400-\u9fff])\s+突破榜单/g, '$1突破榜单')
    .replace(/突破榜单\s+([\u3400-\u9fff])/g, '突破榜单$1')
    .replace(/\bLIVE\b/g, '实盘')
    .replace(/\bSHADOW\b/g, '模拟')
    .replace(/\bTESTNET\b/g, '测试网');
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
    if (node.parentElement?.closest('script, style')) return;
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
const statusLabels = {DRAFT:'草稿',PENDING_REVIEW:'待审核',APPROVED:'已批准',REJECTED:'已拒绝',EXPIRED:'已过期',ALLOW:'通过',SCALE:'缩小仓位',DENY:'拒绝',PENDING:'等待中',RESERVED:'已预留',READY:'待发送',SENT:'已发送',PARTIALLY_FILLED:'部分成交',FILLED:'已成交',CANCELLED:'已取消',UNKNOWN:'结果未知',KNOWN:'已确认',OPENING:'建仓中',OPEN:'持仓中',REDUCING:'减仓中',CLOSING:'退出中',CLOSED:'已结束',ACTIVE:'有效',DEGRADED:'保护不足',RELEASED:'已释放',MATCH:'对账一致',DIFFERENCE:'存在差异',MANUAL_REQUIRED:'需要人工处理',RESOLVED:'已解决',NORMAL:'正常',URGENT:'紧急',IMMEDIATE:'立即',ENABLED:'已开启',DISABLED:'已关闭',SUCCESS:'连接正常',FAILED:'连接失败',SKIPPED:'未运行',STALE:'数据已过期',WAITING:'等待首次同步',NOT_CONFIGURED:'未配置',ON_DEMAND:'按需读取',MISSING:'缺失',CURRENT:'当前有效',INCOMPLETE:'数据不完整',CONTROLLED:'受控',READ_ONLY:'只读',SOURCE_RESERVED:'源端已预留',SUBMITTED:'已提交',IN_FLIGHT:'划转中',DESTINATION_CONFIRMED:'目的端已确认',SETTLED:'已结算',FAILED_SOURCE_RESTORED:'失败，源端已恢复',DEPOSIT_PLAN_READY:'充值计划待执行',DEPOSIT_CONFIRMED:'充值已确认',RELEASE_REQUEST_PLAN_READY:'释放申请计划待执行',RELEASE_REQUEST_CONFIRMED:'释放申请已确认',RELEASE_EXECUTION_PLAN_READY:'释放执行计划待执行',RELEASE_EXECUTION_CONFIRMED:'释放执行已确认',RELEASE_CANCELLATION_PLAN_READY:'释放取消计划待执行',RELEASE_CANCELLED:'释放已取消'};
const riskLabels = {LOW:'低风险',MEDIUM:'中风险',HIGH:'高风险'};
const intentKindLabels = {INITIAL:'初仓',ADD:'加仓',REDUCE:'减仓',EXIT:'退出'};
const directionLabels = {LONG:'做多',SHORT:'做空'};
const sideLabels = {BUY:'买入',SELL:'卖出'};
const capitalDirectionLabels = {VAULT_TO_VENUE:'资金库转入交易所',VENUE_TO_VAULT:'交易所转回资金库'};
const capitalPurposeLabels = {AUTO_PROFIT_SWEEP:'自动归集利润',AUTO_OPERATING_REFILL:'自动补充运营资金',MANUAL:'人工调配资金'};
const capitalTransportLabels = {MOCK:'模拟执行',NOTILT_UNSIGNED_HANDOFF:'NoTilt 未签名交接'};
const auditEventLabels = {
  AUTHORIZATION_ISSUED:'交易授权已签发',AUTOMATIC_EXIT_PREPARED:'自动退出意图已准备',AUTO_ADD_DISABLED:'全局自动加仓已关闭',CAMPAIGN_AUTO_ADD_DISABLED:'交易任务自动加仓已关闭',CAMPAIGN_CLOSED:'交易任务已关闭',CAMPAIGN_TARGET_UPDATED:'交易目标已更新',CAPITAL_AUTOMATION_POLICY_SET:'资金自动化政策已设置',CAPITAL_BALANCE_RECORDED:'资金余额已记录',CAPITAL_SOURCE_RESERVED:'源端资金已预留',CAPITAL_TRANSFER_OBSERVED:'资金划转状态已观测',CAPITAL_TRANSFER_RECONCILED:'资金划转已对账',CAPITAL_TRANSFER_SUBMITTED_MOCK:'模拟资金划转已提交',INSTRUMENT_REGISTERED:'交易合约已登记',NEW_RISK_PAUSED:'新增风险已暂停',NOTILT_RECEIPT_VERIFIED:'NoTilt 链上回执已验证',NOTILT_UNSIGNED_PLAN_RECORDED:'NoTilt 未签名计划已记录',NOTILT_VAULT_FACT_RECORDED:'NoTilt 资金库数据已记录',ORDER_INTENT_PREPARED:'订单意图已准备',ORDER_INTENT_TERMINATED:'订单意图已终止',ORDER_INTENT_UNKNOWN:'订单结果已标记为未知',PENDING_REVIEW:'等待审核',PERPTAPE_FEED_RECORDED:'Perptape 机会数据已记录',POSITION_RECORDED:'仓位数据已记录',PROPOSAL_CREATED:'提案已创建',PROPOSAL_EXPIRED:'提案已过期',PROPOSAL_REVIEWED:'提案已审核',PROPOSAL_SUBMITTED:'提案已提交',PROTECTION_RECORDED:'保护数据已记录',REDUCTION_INTENT_PREPARED:'减仓意图已准备',RISK_DECIDED:'风险检查已完成',RISK_POLICY_SET:'风险政策已设置',RISK_RESTORE_EXECUTED:'风险恢复已执行',RISK_RESTORE_REQUESTED:'风险恢复已申请',RISK_RESTORE_REVIEWED:'风险恢复已审核',ROLE_ASSIGNED:'权限已分配',SENDER_LEASE_ACQUIRED:'发送租约已取得',SERVICE_PRINCIPAL_CREATED:'服务身份已创建',SHADOW_ORDER_RECORDED:'模拟订单已记录',TELEGRAM_PRIVATE_CHAT_BOUND:'Telegram 私聊已绑定',TRANSFER_AUTHORIZATION_ISSUED:'资金划转授权已签发',TRANSFER_PROPOSAL_CREATED:'资金提案已创建',TRANSFER_PROPOSAL_REVIEWED:'资金提案已审核',TRANSFER_PROPOSAL_SUBMITTED:'资金提案已提交',USER_ACCESS_CREATED:'成员权限已创建',USER_ACCESS_UPDATED:'成员权限已更新',USER_BOOTSTRAPPED:'初始管理员已创建',USER_CREATED:'成员已创建',VENUE_FILL_RECORDED:'交易所成交已记录',
};
const auditObjectLabels = {AccountEquity:'账户权益',Campaign:'交易任务',CapabilityGate:'功能控制',CapitalAutomationPolicy:'资金自动化政策',CapitalTransfer:'资金划转',Instrument:'交易合约',OrderIntent:'订单意图',PerptapeFeed:'Perptape 机会数据',Position:'仓位',Proposal:'提案',ProtectionOrder:'保护订单',RiskControlChangeRequest:'风险控制变更申请',RiskDecision:'风险决策',RiskPolicy:'风险政策',RoleAssignment:'权限分配',SenderLease:'发送租约',TradingAuthorization:'交易授权',TransferAuthorization:'划转授权',TransferProposal:'资金提案',User:'成员',VenueFill:'交易所成交',VenueOrder:'交易所订单'};
const auditReasonLabels = {MANUAL:'人工创建',SYSTEM:'系统机会',INITIAL:'初仓',ALLOW:'允许',DENY:'拒绝',SCALE:'缩小仓位','frozen for review':'已冻结并提交审核','expired before review':'审核前已过期','approved proposal and risk decision':'提案与风险决策均已批准'};
const environmentLabels = {LIVE:'生产环境',SHADOW:'生产环境',TESTNET:'生产环境',production:'生产环境',test:'生产环境',development:'生产环境',local:'生产环境'};
const roleLabels = {OBSERVER:'只读用户',PROPOSER:'提案发起人',REVIEWER:'审核人',OPERATOR:'交易运维人员',TREASURY_ADMIN:'资金管理员',SYSTEM_ADMIN:'系统管理员'};
const readinessLabels = {READY:'可用',INCOMPLETE:'数据不完整',STALE:'数据已过期'};
const venueModeLabels = {USER_DATA_READ_ONLY:'账户数据只读',INFO_READ_ONLY:'账户数据只读',READ_ONLY:'只读'};
const accountModeLabels = {PORTFOLIO_MARGIN:'统一账户',MAIN_ACCOUNT:'主账户',SUBACCOUNT:'子账户'};
const fmtIntentKind = (value) => intentKindLabels[value] || value || '未知意图';
const fmtDirection = (value) => directionLabels[value] || value || '未知方向';
const fmtSide = (value) => sideLabels[value] || value || '未知方向';
const fmtEnvironment = (value, withCode = false) => {
  void value;
  void withCode;
  return localizedText('生产环境');
};
const fmtRole = (value) => roleLabels[value] || value || '未分配角色';
const fmtReadiness = (value) => readinessLabels[value] || fmtStatus(value);
const fmtCapitalDirection = (value) => capitalDirectionLabels[value] || value || '未知方向';
const fmtCapitalPurpose = (value) => capitalPurposeLabels[value] || value || '未说明用途';
const fmtCapitalTransport = (value) => capitalTransportLabels[value] || value || '未记录执行方式';
const fmtAuditEvent = (value) => auditEventLabels[value] || '系统操作';
const fmtAuditObject = (value) => auditObjectLabels[value] || '系统对象';
const fmtAuditReason = (value) => {
  if (!value) return '未说明原因';
  if (auditReasonLabels[value]) return auditReasonLabels[value];
  if (value.startsWith('APPROVE: ')) return `批准：${value.slice(9)}`;
  if (value.startsWith('REJECT: ')) return `拒绝：${value.slice(8)}`;
  return value;
};
const exceptionGuidance = {
  CAMPAIGN_UNKNOWN:{priority:1,title:'交易任务状态不确定',copy:'系统无法确认这笔交易当前处于哪个阶段，因此不会继续增加风险。',next:'先核对订单、成交和仓位，再运行对账。'},
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
const riskReasonGuidance = {
  INVALID_INPUT:{label:'风险输入无效',action:'检查计划数量、最大风险和风险政策后重新运行。'},
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
  RISK_POLICY_MISSING:'风险政策尚未配置，因此系统已暂停创建和执行新增风险。请联系系统管理员完成配置。',
  PERPTAPE_NOT_CONFIGURED:'Perptape 尚未配置。人工提案仍可使用，外部机会将在完成配置后恢复。',
  PERPTAPE_UNAVAILABLE:'暂时无法连接 Perptape。人工提案仍可使用，请稍后重新检查外部机会。',
  PERPTAPE_RUNTIME_FEED_MISSING:'Perptape 正在等待首次同步，请稍后重新检查。',
  PERPTAPE_RUNTIME_FEED_STALE:'Perptape 最近数据已经过期，系统不会把旧候选当成实时机会。',
  PERPTAPE_CACHE_INVALID:'Perptape 已保存的数据无法读取，请联系交易运维人员处理。',
  INSTRUMENT_UNAVAILABLE:'该交易合约尚未进入可交易合约目录，暂时不能创建提案。',
  RBAC_DENIED:'当前身份没有查看或执行此操作的权限。',
  CAPABILITY_FORBIDDEN:'当前身份没有查看或执行此操作的权限。',
  LIVE_SCOPE_CONFIGURATION_REQUIRED:'实盘账户或交易所范围尚未配置完整。',
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
const factStatusLabel = (value) => ({KNOWN:'已确认',ACTIVE:'有效',NOT_REQUIRED:'不需要',MISSING:'缺失',UNKNOWN:'未知'}[value] || value || '未知');
const percentageDistance = (from, to) => {
  const base = Number(from); const target = Number(to);
  if (!base || !target) return '—';
  return `${Math.abs((target - base) / base * 100).toFixed(2)}%`;
};
const roleNames = () => (session?.roles || []).map((item) => item.role);
const capabilityRoles = {
  'opportunity.view':['OBSERVER','PROPOSER'],
  'proposal.view':['OBSERVER','PROPOSER','REVIEWER','OPERATOR'],
  'operations.view':['OBSERVER','OPERATOR'],
  'system.view':['OBSERVER','OPERATOR'],
  'venue.view':['OBSERVER','OPERATOR'],
  'venue.sync':['OPERATOR'],
  'results.view':['OBSERVER','OPERATOR'],
  'capital.view':['TREASURY_ADMIN'],
  'proposal.create':['PROPOSER'],
  'proposal.review':['REVIEWER'],
  'access.manage':[],
};
const hasCapability = (capability) => (
  (roleNames().includes('SYSTEM_ADMIN') && capability !== 'capital.view')
  || (capabilityRoles[capability] || []).some(role => roleNames().includes(role))
);
const routeCapability = (path) => {
  if (path === '/') return null;
  if (path === '/capital') return 'capital.view';
  if (path === '/opportunities') return 'opportunity.view';
  if (path === '/proposals/new') return 'proposal.create';
  if (path === '/reviews') return 'proposal.review';
  if (path === '/proposals' || path.startsWith('/proposals/')) return 'proposal.view';
  if (path === '/campaigns' || path.startsWith('/campaigns/') || path === '/orders' || path === '/exceptions') return 'operations.view';
  if (path === '/positions' || path === '/risk') return 'system.view';
  if (path === '/results') return 'results.view';
  if (path === '/venues' || path.startsWith('/venues/')) return 'venue.view';
  if (path === '/admin/users') return 'access.manage';
  return 'operations.view';
};
const capabilityLabel = (capability) => ({'opportunity.view':'查看机会','proposal.view':'查看提案','operations.view':'交易运维','system.view':'查看系统状态','venue.view':'查看交易账户','results.view':'查看结果与审计','capital.view':'资金管理','proposal.create':'发起提案','proposal.review':'独立审核','access.manage':'成员权限管理'}[capability] || capability);
const accessRoleCatalog = [
  {role:'OBSERVER', label:'只读观察', copy:'查看机会、提案、交易任务、系统状态、交易账户和结果；不能执行动作。'},
  {role:'PROPOSER', label:'发起提案', copy:'查看机会并创建提案；不能审核自己的提案，也不能操作交易任务。'},
  {role:'REVIEWER', label:'独立审核', copy:'只处理冻结提案的审核；不能发起、执行或查看资金。'},
  {role:'OPERATOR', label:'交易运维', copy:'运行风险、授权、订单、减仓、对账和交易所同步；不自动获得资金权限。'},
  {role:'TREASURY_ADMIN', label:'资金管理', copy:'查看与管理资金数据、划转和资金对账；与交易运维职责分离。'},
  {role:'SYSTEM_ADMIN', label:'系统管理', copy:'管理成员与系统控制；不会自动获得资金管理权限。'},
];
const loginDestination = () => {
  const destination = `${location.pathname}${location.search}`;
  return destination;
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
    const error = new Error(data?.error?.message || data?.detail?.error?.code || `HTTP ${response.status}`);
    error.code = data?.error?.code || data?.detail?.error?.code || `HTTP_${response.status}`;
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

function setShell(loggedIn) {
  sidebar.hidden = !loggedIn;
  identityChip.hidden = !loggedIn;
  mobileNavToggle.hidden = !loggedIn;
  if (loggedIn) {
    const identity = `${session.username} · ${roleNames().map(fmtRole).join(' / ') || '未分配角色'}`;
    identityChip.textContent = identity;
    mobileSessionSummary.textContent = identity;
    document.querySelectorAll('[data-nav-capability]').forEach(link => {
      link.hidden = !hasCapability(link.dataset.navCapability);
    });
  }
  closeMobileNav({restoreFocus:false});
}

function errorView(error, retry = true) {
  const isMissingPolicy = error?.code === 'RISK_POLICY_MISSING';
  const title = isMissingPolicy ? '风险政策尚未配置' : '当前数据无法读取';
  return `<section class="error-state"><div><h2>${escapeHtml(title)}</h2><p>${escapeHtml(friendlyApiError(error))}</p>${retry ? '<button class="secondary" data-retry>重新检查</button>' : ''}</div></section>`;
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
  environmentBadge.textContent = localizedText('生产环境');
  try {
    const result = await api('/api/auth/session');
    session = result.session;
  } catch (error) {
    if (error.status !== 401) console.error(error);
  }
  setShell(Boolean(session));
  await route();
}

async function route() {
  window.scrollTo(0, 0);
  updateActiveNav();
  closeMobileNav({restoreFocus:false});
  if (!session) {
    renderLogin();
    enhanceRenderedPage();
    return;
  }
  const path = location.pathname;
  const requiredCapability = routeCapability(path);
  if (requiredCapability && !hasCapability(requiredCapability)) {
    main.innerHTML = `<section class="empty-state"><div><p class="eyebrow">权限范围</p><h2>当前职责不包含这个页面</h2><p>此页面需要“${escapeHtml(capabilityLabel(requiredCapability))}”权限。侧栏只展示当前身份可用入口；直接打开链接也不会绕过服务端权限。</p><div class="toolbar empty-actions"><a class="secondary" href="/" data-link>返回今日</a>${hasCapability('capital.view') ? '<a class="primary" href="/capital" data-link>进入资金中心</a>' : ''}</div></div></section>`;
    enhanceRenderedPage();
    return;
  }
  main.innerHTML = '<section class="loading-state"><span class="spinner"></span><p>正在读取当前事实…</p></section>';
  try {
    if (path === '/') await renderHome();
    else if (path === '/opportunities') await renderOpportunities();
    else if (path === '/proposals/new') await renderManualProposal();
    else if (path === '/reviews') await renderProposalList('PENDING_REVIEW', '审核队列');
    else if (path === '/proposals') await renderProposalList(null, '全部提案');
    else if (path === '/campaigns') await renderCampaignList();
    else if (path === '/positions') await renderSystemStatus();
    else if (path === '/orders') await renderCampaignFacts('orders');
    else if (path === '/risk') await renderCampaignFacts('risk');
    else if (path === '/capital') await renderCapitalCenter();
    else if (path === '/results') await renderActualResults();
    else if (path === '/exceptions') await renderExceptions();
    else if (path === '/venues' || path === '/venues/binance' || path === '/venues/hyperliquid') await renderVenueFacts();
    else if (path === '/admin/users') await renderAccessManagement();
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
  }
}

function renderLogin() {
  main.innerHTML = `<section class="login-page"><div class="login-card">
    <span class="mock-ribbon">${authStatus.mock_identity_available ? '内部身份验证' : '需要统一身份登录'}</span>
    <p class="eyebrow" style="margin-top:18px">内部访问</p><h1>进入交易控制台</h1>
    <p class="lede">系统不开放外部注册。只有已经分配岗位和数据范围的内部成员可以进入。</p>
    ${sessionNotice ? `<div class="callout" role="status">${escapeHtml(sessionNotice)}</div>` : ''}
    ${authStatus.mock_identity_available ? `<form id="login-form"><label>内部用户名<input name="username" autocomplete="username" required placeholder="reviewer-1"></label><button class="primary">进入控制台</button><div class="form-error" role="alert"></div></form>` : '<div class="callout">统一身份服务尚未接入。</div>'}
  </div></section>`;
  document.querySelector('#login-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const result = await api('/api/auth/mock/login', {method:'POST', body: JSON.stringify({username: new FormData(form).get('username')})});
      session = result.session;
      sessionNotice = '';
      authFailureActive = false;
      setShell(true);
      history.replaceState({}, '', loginDestination());
      await route();
    } catch (error) {
      showApiError(error, form.querySelector('.form-error'));
    } finally { button.disabled = false; }
  });
}

async function renderHome() {
  if (!hasCapability('operations.view')) {
    if (hasCapability('proposal.review')) {
      const result = await api('/api/proposals?proposal_status=PENDING_REVIEW');
      const actionable = result.data.filter(item => item.environment === 'LIVE' && item.actionable_for_current_user);
      main.innerHTML = `<section class="page home-page"><article class="home-status tone-${actionable.length ? 'attention' : 'success'}"><div><p class="eyebrow">审核工作台</p><h1>${actionable.length ? `${actionable.length} 笔提案等待你的独立审核` : '当前没有需要你审核的提案'}</h1><p>审核人只查看已经提交的提案和审核依据，不能发起提案、查看资金或操作交易任务。</p></div><a class="primary" href="/reviews" data-link>进入审核队列</a></article></section>`;
      return;
    }
    if (hasCapability('proposal.create')) {
      const result = await api('/api/proposals');
      const liveProposals = result.data.filter(item => item.environment === 'LIVE');
      main.innerHTML = `<section class="page home-page"><article class="home-status tone-success"><div><p class="eyebrow">提案工作台</p><h1>从机会开始形成交易判断</h1><p>你可以查看机会、发起提案并跟踪自己可见的提案；不能审核、查看资金或操作交易任务。</p></div><a class="primary" href="/opportunities" data-link>查看机会</a></article><div class="stats"><div class="stat"><small>当前可见提案</small><b>${liveProposals.length}</b></div><div class="stat"><small>待审核</small><b>${liveProposals.filter(item => item.status === 'PENDING_REVIEW').length}</b></div></div></section>`;
      return;
    }
    if (hasCapability('capital.view')) {
      main.innerHTML = `<section class="page home-page"><article class="home-status tone-success"><div><p class="eyebrow">资金工作台</p><h1>今日只显示你的资金职责</h1><p>你可以查看资金数据、净值完整性、在途占用和资金对账；交易提案、交易任务与交易所排障不在当前角色范围内。</p></div><a class="primary" href="/capital" data-link>进入资金中心</a></article></section>`;
      return;
    }
    main.innerHTML = `<section class="page home-page"><article class="home-status tone-neutral"><div><p class="eyebrow">尚未分配职责</p><h1>当前身份尚未分配业务职责</h1><p>请由系统管理员分配明确岗位与权限范围；系统不会把缺少权限显示成空数据。</p></div></article></section>`;
    return;
  }
  const riskControlRequest = api('/api/risk-controls').catch(error => {
    if (error.status === 403) return null;
    throw error;
  });
  const [proposalResponse, campaignResponse, exceptionResponse, riskControl] = await Promise.all([
    api('/api/proposals?proposal_status=PENDING_REVIEW'),
    api('/api/campaigns'),
    api('/api/campaign-exceptions'),
    riskControlRequest,
  ]);
  const now = Date.now();
  const roles = roleNames();
  const canReview = roles.includes('REVIEWER') || roles.includes('SYSTEM_ADMIN');
  const canOperate = roles.includes('OPERATOR') || roles.includes('SYSTEM_ADMIN');
  const canPropose = roles.includes('PROPOSER') || roles.includes('SYSTEM_ADMIN');
  const pending = proposalResponse.data.filter(item => item.environment === 'LIVE' && new Date(item.expires_at).getTime() > now);
  const actionableReviews = canReview ? pending.filter(item => item.actionable_for_current_user) : [];
  const expiringReviews = actionableReviews.filter(item => new Date(item.expires_at).getTime() - now < 30 * 60 * 1000);
  const nextReview = [...actionableReviews].sort((left, right) => new Date(left.expires_at) - new Date(right.expires_at))[0];
  const activeCampaigns = campaignResponse.data.filter(item => item.environment === 'LIVE' && item.status !== 'CLOSED');
  const activeCampaignIds = new Set(activeCampaigns.map(item => item.campaign_id));
  const exceptions = exceptionResponse.data.filter(item => activeCampaignIds.has(item.campaign_id));
  const exceptionCampaigns = new Set(exceptions.map(item => item.campaign_id));
  const riskLimited = Boolean(riskControl && riskControl.policy.system_state !== 'NORMAL');
  const clearScopeLabel = riskControl ? '当前安全' : '当前作用域无异常';
  const safety = exceptions.length
    ? {
        tone:'danger',
        eyebrow:'风险提醒',
        title:`${exceptionCampaigns.size} 个交易任务需要先处理`,
        copy:`当前有 ${exceptions.length} 项阻断问题。相关新增风险保持关闭；先确认结果未知、仓位、保护和对账数据。`,
        href:'/exceptions',
        action:canOperate ? '处理风险异常' : '查看风险异常',
      }
    : riskLimited
      ? {
          tone:'attention',
          eyebrow:'新增风险受限',
          title:'新增风险已受限，减仓和退出仍可用',
          copy:`当前系统风险状态为“${riskControlStatusLabel(riskControl.policy.system_state)}”。先完成恢复条件，不会自动放开旧授权。`,
          href:'/risk',
          action:'查看限制与恢复条件',
        }
      : actionableReviews.length
        ? {
            tone:'attention',
            eyebrow:'需要审核',
            title:`${clearScopeLabel}；有 ${actionableReviews.length} 笔非本人提案等待审核`,
            copy:'打开队列确认是否需要你投票；批准只进入风险检查，不会直接产生订单。',
            href:'/reviews',
            action:'查看审核队列',
          }
        : activeCampaigns.length
          ? {
              tone:'success',
              eyebrow:'交易运行中',
              title:`${clearScopeLabel}；${activeCampaigns.length} 个交易任务正在运行`,
              copy:'没有派生异常。继续观察仓位、保护、意图和最近对账；需要降险时可随时减仓或退出。',
              href:'/campaigns',
              action:'查看运行中交易任务',
            }
          : {
              tone:'success',
              eyebrow:'当前无待办',
              title:`${clearScopeLabel}，没有必须立即处理的事项`,
              copy:`系统没有发现阻断异常、待你审核的提案或运行中交易任务。${riskControl ? '' : '全局风险恢复仍由管理员控制。'} 可以继续观察机会。`,
              href:'/opportunities',
              action:'查看市场机会',
            };
  const priorityCards = [];
  if (exceptions.length) priorityCards.push(`<a class="home-priority danger" href="/exceptions" data-link><span class="priority-number">1</span><div><small>必须先处理</small><b>${exceptions.length} 项阻断异常</b><p>影响 ${exceptionCampaigns.size} 个交易任务；结果未知和保护问题不会被自动忽略。</p></div><strong>进入异常队列 →</strong></a>`);
  if (riskLimited) priorityCards.push(`<a class="home-priority attention" href="/risk" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>新增风险受限</small><b>${escapeHtml(riskControlStatusLabel(riskControl.policy.system_state))}</b><p>${riskControl.restore_conditions.blockers.length ? `${riskControl.restore_conditions.blockers.length} 项恢复条件尚未满足。` : '恢复条件已满足，仍需完成受控审核与执行。'} 减仓和退出不受阻断。</p></div><strong>查看恢复条件 →</strong></a>`);
  if (actionableReviews.length) priorityCards.push(`<a class="home-priority attention" href="/reviews" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>独立审核队列</small><b>${actionableReviews.length} 笔非本人提案等待审核</b><p>${expiringReviews.length ? `${expiringReviews.length} 笔将在 30 分钟内到期。` : `最早一笔到期于 ${fmtDate(nextReview.expires_at)}。`} 已投票的高风险提案可能仍在等待另一名审核人。</p></div><strong>打开审核队列 →</strong></a>`);
  if (activeCampaigns.length) priorityCards.push(`<a class="home-priority" href="/campaigns" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>持续观察</small><b>${activeCampaigns.length} 个运行中交易任务</b><p>${escapeHtml(activeCampaigns.slice(0, 3).map(item => `${item.venue} · ${fmtDirection(item.direction)} · ${fmtStatus(item.status)}`).join('；'))}</p></div><strong>查看当前仓位 →</strong></a>`);
  if (!priorityCards.length) priorityCards.push(`<a class="home-priority clear" href="/opportunities" data-link><span class="priority-number">✓</span><div><small>当前无待办</small><b>继续观察，不必为了操作而操作</b><p>${canPropose ? '机会只是候选；只有形成清楚交易判断时才创建提案。' : '当前身份可以观察机会，但不能创建提案；如有判断请交由提案发起人保存参数。'}</p></div><strong>查看机会 →</strong></a>`);
  main.innerHTML = `<section class="page home-page"><article class="home-status tone-${safety.tone}"><div><p class="eyebrow">${safety.eyebrow}</p><h1>${escapeHtml(safety.title)}</h1><p>${escapeHtml(safety.copy)}</p></div><a class="primary" href="${safety.href}" data-link>${escapeHtml(safety.action)}</a></article>
    <div class="stats home-stats"><div class="stat"><small>受影响交易任务</small><b class="${exceptions.length ? 'danger-text' : ''}">${exceptionCampaigns.size}</b><span>${exceptions.length ? `${exceptions.length} 项问题` : '没有派生异常'}</span></div><div class="stat"><small>非本人待审核</small><b class="${expiringReviews.length ? 'warning-text' : ''}">${actionableReviews.length}</b><span>${expiringReviews.length ? `${expiringReviews.length} 笔即将到期` : canReview ? '创建者不可审核自己的提案' : '当前身份不是审核人'}</span></div><div class="stat"><small>运行中交易任务</small><b>${activeCampaigns.length}</b><span>${activeCampaigns.length ? '保护与对账需持续有效' : '当前没有活动仓位流程'}</span></div><div class="stat"><small>新增风险状态</small><b class="${riskLimited ? 'warning-text status-copy' : 'status-copy'}">${escapeHtml(riskControl ? riskControlStatusLabel(riskControl.policy.system_state) : '由管理员控制')}</b><span>${riskControl ? `自动加仓${escapeHtml(riskControlStatusLabel(riskControl.auto_add_gate.status))}` : '当前身份无全局恢复权限'}</span></div></div>
    <div class="home-layout"><section><div class="section-heading"><div><p class="eyebrow">处理顺序</p><h2>现在按这个顺序处理</h2></div><button class="secondary" data-refresh>刷新当前数据</button></div><div class="home-priority-list">${priorityCards.join('')}</div></section>
      <aside class="stack"><article class="card home-quick-start"><p class="eyebrow">${canPropose ? '新的交易判断' : '市场观察'}</p><h2>${canPropose ? '开始新的判断' : '继续观察市场机会'}</h2><p class="subtle">${canPropose ? '先看机会或写交易判断；这两条路径都只会创建提案，并进入独立审核。' : '当前身份只查看候选，不保存交易参数，也不会从这里新增风险。'}</p><div class="stacked-actions"><a class="primary" href="/opportunities" data-link>查看 Perptape 机会</a>${canPropose ? '<a class="secondary" href="/proposals/new" data-link>创建人工提案</a>' : ''}</div></article>
        <article class="card home-boundary"><p class="eyebrow">系统边界</p><h2>当前控制状态</h2><dl class="definition-grid">${definition('站点环境', fmtEnvironment(authStatus?.environment, true))}${definition('风险政策', riskControl ? riskControlStatusLabel(riskControl.policy.system_state) : '由管理员控制')}${definition('自动加仓', riskControl ? riskControlStatusLabel(riskControl.auto_add_gate.status) : '由管理员控制')}${definition('安全原则', '数据缺失即阻断')}</dl><p class="safety-note">是否允许真实发送，由具体交易任务、短期授权、交易所配置和服务端控制开关共同决定。</p></article></aside></div></section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
}

async function renderOpportunities() {
  let result = null;
  let sourceError = null;
  try {
    result = await api('/api/opportunities');
  } catch (error) {
    if (error.status === 401 || error.status === 403) throw error;
    sourceError = error;
  }
  opportunities = result?.data || [];
  const items = opportunities;
  const canPropose = hasCapability('proposal.create');
  const options = (key) => [...new Set(items.map(item => item[key]).filter(Boolean))].sort().map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('');
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">PERPTAPE · ${escapeHtml(result?.source_contract_version || '连接不可用')}</p><h1>机会</h1><p class="lede">先查看 Perptape 候选；没有合适信号时，也可以点击“人工提案”自行录入。无论哪种方式，都只会创建提案，并且必须经过独立审核。</p></div><div class="toolbar">${canPropose ? '<a class="primary" href="/proposals/new" data-link>＋ 人工提案</a>' : ''}<button class="secondary" data-refresh>刷新机会</button></div></header>
    ${sourceError ? `<article class="source-status tone-attention"><div><p class="eyebrow">Perptape 数据源</p><h2>外部机会当前不可用</h2><p>${escapeHtml(friendlyApiError(sourceError))} 系统不会把过期候选当成当前机会。</p></div>${canPropose ? '<a class="secondary" href="/proposals/new" data-link>创建人工提案</a>' : ''}</article>` : ''}
    <div class="stats"><div class="stat"><small>当前候选</small><b>${items.length}</b></div><div class="stat"><small>${canPropose ? '可创建提案' : '可交易合约'}</small><b>${items.filter(i => i.readiness === 'READY' && i.proposal_eligible).length}</b></div><div class="stat"><small>数据截止</small><b style="font-size:14px">${fmtDate(result?.as_of)}</b></div><div class="stat"><small>数据源状态</small><b style="font-size:14px">${sourceError ? '不可用' : '连接正常'}</b></div></div>
    ${items.length ? `<form id="opportunity-filters" class="filter-panel"><label>交易所<select name="venue"><option value="">全部</option>${options('venue')}</select></label><label>币对<input name="symbol" type="search" placeholder="例如 BTC、XYZ100"></label><label>共振周期<select name="timeframe"><option value="">全部周期</option>${options('timeframe')}</select></label><label>方向<select name="direction"><option value="">全部</option><option value="LONG">做多</option><option value="SHORT">做空</option></select></label><label>最低成交量<input name="volume" type="number" min="0" placeholder="不限"></label><label>最低持仓量<input name="open_interest" type="number" min="0" placeholder="不限"></label><button type="reset" class="text-button">清除筛选</button></form><div class="result-summary"><span>显示 <b data-filter-count>${items.length}</b> / ${items.length} 个机会</span><span>缺少成交量或持仓量的候选不会通过对应数值筛选</span></div><div id="opportunity-grid" class="card-grid">${items.map(opportunityCard).join('')}</div><section id="opportunity-empty" class="empty-state compact-empty" hidden><div><h2>没有符合条件的机会</h2><p>尝试降低成交量或持仓量门槛，或者清除部分筛选。</p></div></section>` : `<section class="empty-state compact-empty"><div><h2>${sourceError ? '等待机会数据恢复' : '当前没有突破候选'}</h2><p>${sourceError ? '人工提案仍然可用；Perptape 恢复后可以再次刷新。' : '这不代表市场没有风险或行情，只表示当前没有返回候选。'}</p></div></section>`}
  </section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  bindOpportunityActions();
}

function opportunityCard(item) {
  const directionClass = item.direction === 'LONG' ? 'direction-long' : 'direction-short';
  const canPropose = hasCapability('proposal.create');
  const canCreateProposal = canPropose && item.readiness === 'READY' && item.proposal_eligible;
  const catalogStatus = item.proposal_blocker === 'INSTRUMENT_UNAVAILABLE'
    ? '<p class="callout">该合约尚未进入可交易合约目录，暂时不能创建提案。</p>'
    : '';
  return `<article class="card" data-opportunity-card="${escapeHtml(item.candidate_id)}"><div class="card-top"><div><span class="subtle">${escapeHtml(item.venue)} · ${escapeHtml(item.timeframe)}</span><div class="symbol">${escapeHtml(item.symbol)}</div></div><span class="tag ${directionClass}">${escapeHtml(fmtDirection(item.direction))}</span></div>
    <div class="metric-row"><div><small>参考价格</small><b>${fmtNumber(item.reference_price)}</b></div><div><small>触发时间</small><b>${fmtDate(item.triggered_at)}</b></div><div><small>数据状态</small><b>${escapeHtml(fmtReadiness(item.readiness))}</b></div></div>
    <div class="market-facts"><span>成交量 <b>${fmtCompact(item.quote_volume)}</b></span><span>持仓量 <b>${fmtCompact(item.open_interest)}</b></span></div>
    <p class="subtle">${escapeHtml(item.rationale)}</p>${catalogStatus}${canPropose ? '' : '<p class="safety-note">当前角色可观察候选，但不能创建提案。</p>'}<div class="link-row"><a class="text-button" href="${escapeHtml(item.detail_url)}" target="_blank" rel="noreferrer">Perptape 榜单 ↗</a><a class="text-button" href="${escapeHtml(item.chart_url)}" target="_blank" rel="noreferrer">交易所图表 ↗</a></div>${canPropose ? `<div class="card-actions proposal-actions"><button class="secondary" data-advanced-system="${escapeHtml(item.candidate_id)}" ${canCreateProposal ? '' : 'disabled'}>高级配置</button><button class="primary" data-create-system="${escapeHtml(item.candidate_id)}" ${canCreateProposal ? '' : 'disabled'}>一键创建</button></div>` : ''}</article>`;
}

function openSystemDialog(candidateId) {
  const form = document.querySelector('#system-proposal-form');
  const item = opportunities.find(candidate => candidate.candidate_id === candidateId);
  form.reset();
  form.elements.candidate_id.value = candidateId;
  form.elements.account_id.value = 'acct-1';
  const price = Number(item?.reference_price || 1);
  form.elements.quantity.value = Math.max(0.000001, 100 / price).toPrecision(6);
  form.elements.max_risk.value = '1';
  form.elements.invalidation_price.value = (price * (item?.direction === 'SHORT' ? 1.02 : 0.98)).toPrecision(8);
  form.elements.expires_in_minutes.value = '120';
  form.elements.rationale.value = '使用默认风险配置创建 Perptape 候选提案，尚未形成任何订单。';
  document.querySelector('#system-form-error').textContent = '';
  dialog.showModal();
}

function defaultSystemPayload(item) {
  const price = Number(item.reference_price);
  return {
    environment:'LIVE',
    account_id:'acct-1', risk_tier:'MEDIUM',
    quantity:Math.max(0.000001, 100 / price).toPrecision(6),
    initial_quantity:null, max_risk:'1',
    invalidation_price:(price * (item.direction === 'SHORT' ? 1.02 : 0.98)).toPrecision(8),
    allow_auto_add:false, requested_adds:0, add_trigger_price:null,
    expires_in_minutes:120,
    rationale:'使用默认风险配置创建 Perptape 候选提案，尚未形成任何订单。'
  };
}

function bindOpportunityActions() {
  document.querySelectorAll('[data-advanced-system]').forEach(button => button.addEventListener('click', () => openSystemDialog(button.dataset.advancedSystem)));
  document.querySelectorAll('[data-create-system]').forEach(button => button.addEventListener('click', async () => {
    const item = opportunities.find(candidate => candidate.candidate_id === button.dataset.createSystem);
    if (!item) return;
    button.disabled = true; button.textContent = '创建中…';
    try {
      const result = await api(`/api/opportunities/${item.candidate_id}/proposals`, {method:'POST', body:JSON.stringify(defaultSystemPayload(item))});
      showToast(`${item.symbol} 提案已按默认配置创建`);
      navigate(`/proposals/${result.proposal_id}`);
    } catch (error) { showApiError(error); button.disabled = false; button.textContent = '一键创建'; }
  }));
  const filters = document.querySelector('#opportunity-filters');
  if (!filters) return;
  const applyFilters = () => {
    const values = Object.fromEntries(new FormData(filters));
    let visible = 0;
    opportunities.forEach(item => {
      const match = (!values.venue || item.venue === values.venue)
        && (!values.symbol || `${item.symbol} ${item.canonical_symbol}`.toLowerCase().includes(values.symbol.toLowerCase().trim()))
        && (!values.timeframe || item.timeframe === values.timeframe)
        && (!values.direction || item.direction === values.direction)
        && (!values.volume || (item.quote_volume !== null && Number(item.quote_volume) >= Number(values.volume)))
        && (!values.open_interest || (item.open_interest !== null && Number(item.open_interest) >= Number(values.open_interest)));
      document.querySelector(`[data-opportunity-card="${CSS.escape(item.candidate_id)}"]`).hidden = !match;
      if (match) visible += 1;
    });
    document.querySelector('[data-filter-count]').textContent = visible;
    document.querySelector('#opportunity-empty').hidden = visible !== 0;
  };
  filters.addEventListener('input', applyFilters);
  filters.addEventListener('reset', () => requestAnimationFrame(applyFilters));
}

async function renderManualProposal() {
  const result = await api('/api/instruments');
  instruments = result.data;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">人工创建交易提案</p><h1>创建人工提案</h1><p class="lede">先说明交易方向、计划规模和最多可以承受的损失。分批入场和自动加仓是高级选项；提交后只会保存当前判断，仍需独立审核和系统风险检查。</p></div><a class="secondary" href="/opportunities" data-link>返回机会</a></header>
    <div class="compose-layout"><form id="manual-form" class="form-panel proposal-compose">
      <section class="form-section"><div class="section-title"><span>1</span><div><h2>交易意图</h2><p>选标的、定方向，说清从哪个价格开始执行。</p></div></div><div class="field-grid">
        <label>账户<span class="field-help">资金和权限归属</span><input name="account_id" value="acct-1" required></label>
        <label>交易标的<span class="field-help">仅展示已启用的可交易合约</span><select name="instrument_id" required>${instruments.map(i => `<option value="${i.instrument_id}" data-venue="${escapeHtml(i.venue)}">${escapeHtml(i.venue)} · ${escapeHtml(i.symbol)}</option>`).join('')}</select></label>
        <label>方向<span class="field-help">做多或做空</span><select name="direction"><option value="LONG">做多</option><option value="SHORT">做空</option></select></label>
        <label>触发价格<span class="field-help">计划开始执行的位置</span><input name="trigger_price" type="number" step="any" min="0" required></label>
      </div></section>
      <section class="form-section"><div class="section-title"><span>2</span><div><h2>风险边界</h2><p>用数量、最大损失和失效点限制这笔交易。</p></div></div><div class="field-grid">
        <label>风险档位<span class="field-help">决定审核要求和允许的加仓次数</span><select name="risk_tier"><option value="LOW">低</option><option value="MEDIUM" selected>中</option><option value="HIGH">高</option></select></label>
        <label>最大持仓数量<span class="field-help">该交易任务在任何时候都不能超过此数量</span><input name="quantity" type="number" step="any" min="0" required></label>
        <label>最大风险<span class="field-help">以账户结算币计价</span><input name="max_risk" type="number" step="any" min="0" required></label>
        <label>失效价格<span class="field-help">到达后交易逻辑不再成立</span><input name="invalidation_price" type="number" step="any" min="0" required></label>
      </div></section>
      <details class="advanced-form"><summary><span>高级执行参数</span><small>分批入场、限价、自动加仓与有效期</small></summary><div class="field-grid">
        <label>初仓数量<input name="initial_quantity" type="number" step="any" min="0" placeholder="默认等于总数量"></label>
        <label>限价（可选）<input name="limit_price" type="number" step="any" min="0"></label>
        <label>允许自动加仓<select name="allow_auto_add"><option value="false" selected>否</option><option value="true">是</option></select></label>
        <label>可用加仓次数<select name="requested_adds"><option value="0" selected>0</option><option value="1">1</option><option value="2">2</option><option value="3">3</option></select></label>
        <label>加仓触发价格<input name="add_trigger_price" type="number" step="any" min="0"></label>
        <label>有效时间（分钟）<input name="expires_in_minutes" type="number" min="5" max="1440" value="120" required></label>
      </div></details>
      <label class="rationale-field">提案理由<span class="field-help">至少说明触发逻辑和主要风险</span><textarea name="rationale" rows="4" required placeholder="例如：4h 突破确认，成交量扩张；若跌破失效价则退出。"></textarea></label>
      <div class="form-error" role="alert"></div><div class="form-actions"><span class="submit-disclosure">提交后保存当前参数并进入审核队列，不会直接下单。</span><button class="primary">创建并提交审核</button></div>
    </form>
    <aside class="proposal-preview" aria-live="polite"><p class="eyebrow">提案预览</p><h2>提交前摘要</h2><div class="preview-symbol" data-preview-symbol>选择交易标的</div><div class="preview-direction" data-preview-direction>做多</div><dl class="preview-metrics"><div><dt>计划名义价值</dt><dd data-preview-notional>—</dd></div><div><dt>最大风险</dt><dd data-preview-risk>—</dd></div><div><dt>失效距离</dt><dd data-preview-distance>—</dd></div><div><dt>有效期</dt><dd data-preview-expiry>120 分钟</dd></div></dl><div class="preview-checks"><p data-check-intent>○ 补全交易意图</p><p data-check-risk>○ 补全风险边界</p><p>✓ 只创建提案，不直接下单</p></div></aside></div></section>`;
  const form = document.querySelector('#manual-form');
  form.addEventListener('submit', submitManualProposal);
  form.addEventListener('input', updateManualProposalPreview);
  updateManualProposalPreview({currentTarget:form});
}

function updateManualProposalPreview(event) {
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  const selected = instruments.find(item => item.instrument_id === data.instrument_id);
  const trigger = Number(data.trigger_price); const quantity = Number(data.quantity);
  const intentReady = Boolean(selected && trigger > 0 && data.direction);
  const riskReady = Number(data.max_risk) > 0 && Number(data.invalidation_price) > 0 && quantity > 0;
  document.querySelector('[data-preview-symbol]').textContent = selected ? `${selected.symbol} · ${selected.venue}` : '选择交易标的';
  const direction = document.querySelector('[data-preview-direction]');
  direction.textContent = fmtDirection(data.direction);
  direction.className = `preview-direction ${data.direction === 'SHORT' ? 'direction-short' : 'direction-long'}`;
  document.querySelector('[data-preview-notional]').textContent = trigger > 0 && quantity > 0 ? fmtAmount(trigger * quantity, selected?.quote_currency) : '—';
  document.querySelector('[data-preview-risk]').textContent = data.max_risk ? `${fmtAmount(data.max_risk, selected?.collateral_currency)} · ${fmtRisk(data.risk_tier)}` : '—';
  document.querySelector('[data-preview-distance]').textContent = percentageDistance(data.trigger_price, data.invalidation_price);
  document.querySelector('[data-preview-expiry]').textContent = `${data.expires_in_minutes || 120} 分钟`;
  document.querySelector('[data-check-intent]').textContent = `${intentReady ? '✓' : '○'} ${intentReady ? '交易意图完整' : '补全交易意图'}`;
  document.querySelector('[data-check-risk]').textContent = `${riskReady ? '✓' : '○'} ${riskReady ? '风险边界完整' : '补全风险边界'}`;
  applyLanguageToDocument(document.querySelector('.proposal-preview'));
}

async function submitManualProposal(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  const selected = instruments.find(i => i.instrument_id === data.instrument_id);
  const button = form.querySelector('button');
  button.disabled = true;
  data.environment = 'LIVE';
  data.venue = selected.venue;
  data.limit_price = data.limit_price || null;
  data.initial_quantity = data.initial_quantity || null;
  data.add_trigger_price = data.add_trigger_price || null;
  data.allow_auto_add = data.allow_auto_add === 'true';
  data.requested_adds = Number(data.requested_adds);
  data.idempotency_key = crypto.randomUUID();
  for (const field of ['quantity','initial_quantity','max_risk','trigger_price','limit_price','invalidation_price','add_trigger_price']) if (data[field] !== null) data[field] = String(data[field]);
  data.expires_in_minutes = Number(data.expires_in_minutes);
  try {
    const result = await api('/api/proposals/manual', {method:'POST', body: JSON.stringify(data)});
    showToast('人工提案已保存并进入审核');
    navigate(`/proposals/${result.proposal_id}`);
  } catch (error) { showApiError(error, form.querySelector('.form-error')); button.disabled = false; }
}

async function renderProposalList(status, title) {
  const result = await api(`/api/proposals${status ? `?proposal_status=${status}` : ''}`);
  const allItems = result.data.filter(item => item.environment === 'LIVE');
  const items = status ? allItems.filter(item => item.actionable_for_current_user) : allItems;
  const pending = status ? items.length : items.filter(item => item.status === 'PENDING_REVIEW' && item.actionable_for_current_user).length;
  const expiring = items.filter(item => { const remaining = new Date(item.expires_at) - Date.now(); return remaining > 0 && remaining < 30 * 60 * 1000; }).length;
  const canPropose = roleNames().includes('PROPOSER') || roleNames().includes('SYSTEM_ADMIN');
  const createActions = canPropose ? '<div class="toolbar"><a class="secondary" href="/opportunities" data-link>查看机会</a><a class="primary" href="/proposals/new" data-link>新建人工提案</a></div>' : '';
  const emptyState = status
    ? '<section class="empty-state"><div><h2>当前没有待你审核的提案</h2><p>自己的提案、已经投过票、已到期或已结束的提案不会留在这里。</p><div class="toolbar empty-actions"><a class="secondary" href="/" data-link>返回今日</a><a class="primary" href="/proposals" data-link>查看全部提案</a></div></div></section>'
    : `<section class="empty-state"><div><h2>当前没有匹配提案</h2><p>${canPropose ? '可以从机会页一键创建，或提交一份人工提案。' : '当前作用域内还没有提案。'}</p>${createActions}</div></section>`;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">提案审核</p><h1>${escapeHtml(title)}</h1><p class="lede">${status ? '这里只保留真正需要你独立判断的提案；批准不等于下单。' : '集中查看提案从创建、审核到授权的当前状态。'}</p></div>${createActions}</header>
    <div class="stats proposal-stats"><div class="stat"><small>当前列表</small><b>${items.length}</b></div><div class="stat"><small>等待审核</small><b>${pending}</b></div><div class="stat"><small>高风险</small><b>${items.filter(item => item.risk_tier === 'HIGH').length}</b></div><div class="stat"><small>30 分钟内到期</small><b>${expiring}</b></div></div>
    <div class="section-tabs"><a class="${status ? 'active' : ''}" href="/reviews" data-link>待我审核${pending ? `<span>${pending}</span>` : ''}</a><a class="${status ? '' : 'active'}" href="/proposals" data-link>全部提案</a></div>
    ${items.length ? `<div class="proposal-list-tools"><label>搜索标的或账户<input id="proposal-search" type="search" placeholder="BTCUSDT / acct-1"></label><label>方向<select id="proposal-direction"><option value="">全部方向</option><option value="LONG">做多</option><option value="SHORT">做空</option></select></label><label>风险<select id="proposal-risk"><option value="">全部档位</option><option value="LOW">低</option><option value="MEDIUM">中</option><option value="HIGH">高</option></select></label><span><b data-proposal-count>${items.length}</b> 个结果</span></div><div class="table-wrap proposal-table"><table><thead><tr><th>提案</th><th>方向 / 数量</th><th>风险边界</th><th>状态</th><th>提交时间</th><th>到期</th></tr></thead><tbody>${items.map(item => `<tr data-href="/proposals/${item.proposal_id}" data-proposal-row data-search="${escapeHtml(`${item.symbol || ''} ${item.account_id} ${item.venue}`.toLowerCase())}" data-direction="${escapeHtml(item.direction)}" data-risk="${escapeHtml(item.risk_tier)}"><td><b>${escapeHtml(item.symbol || shortId(item.instrument_id))}</b><br><span class="subtle">${escapeHtml(item.venue)} · ${escapeHtml(item.source === 'SYSTEM' ? 'Perptape' : '人工')}</span></td><td><span class="direction-pill ${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(fmtDirection(item.direction))}</span><br><span class="subtle">数量 ${fmtNumber(item.quantity)}</span></td><td><b>${fmtRisk(item.risk_tier)}</b><br><span class="subtle">最多 ${escapeHtml(fmtAmount(item.max_risk, item.collateral_currency))}</span></td><td><span class="status-pill status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</span></td><td>${fmtDate(item.created_at)}<br><span class="subtle">版本 ${item.version}</span></td><td>${fmtDate(item.expires_at)}</td></tr>`).join('')}</tbody></table></div><section id="proposal-filter-empty" class="empty-state compact-empty" hidden><div><h2>没有符合条件的提案</h2><p>请清除搜索或调整筛选。</p></div></section>` : emptyState}</section>`;
  bindLinkedRows();
  const filter = () => {
    const query = document.querySelector('#proposal-search')?.value.toLowerCase().trim() || '';
    const direction = document.querySelector('#proposal-direction')?.value || '';
    const risk = document.querySelector('#proposal-risk')?.value || '';
    let visible = 0;
    document.querySelectorAll('[data-proposal-row]').forEach(row => {
      const matches = (!query || row.dataset.search.includes(query)) && (!direction || row.dataset.direction === direction) && (!risk || row.dataset.risk === risk);
      row.hidden = !matches;
      if (matches) visible += 1;
    });
    if (document.querySelector('[data-proposal-count]')) document.querySelector('[data-proposal-count]').textContent = visible;
    if (document.querySelector('#proposal-filter-empty')) document.querySelector('#proposal-filter-empty').hidden = visible !== 0;
  };
  ['#proposal-search','#proposal-direction','#proposal-risk'].forEach(selector => document.querySelector(selector)?.addEventListener('input', filter));
}

async function renderProposalDetail(id) {
  const item = await api(`/api/proposals/${id}`);
  if (item.environment !== 'LIVE') {
    main.innerHTML = '<section class="page"><section class="empty-state"><div><h1>该提案不属于生产控制台</h1><p>这里仅展示生产交易提案。非生产记录不会出现在当前网站。</p><a class="primary" href="/proposals" data-link>返回提案列表</a></div></section></section>';
    return;
  }
  const reviewedByMe = item.approvals.some(approval => approval.reviewer_id === session.user_id);
  const isExpired = item.status === 'PENDING_REVIEW' && new Date(item.expires_at).getTime() <= Date.now();
  const canReview = Boolean(item.actionable_for_current_user);
  const canOperate = roleNames().includes('OPERATOR') || roleNames().includes('SYSTEM_ADMIN');
  const details = item.frozen_payload?.details || {};
  const candidate = details.candidate || {};
  const triggerPrice = details.trigger_price || candidate.reference_price || candidate.threshold_price;
  const invalidationPrice = details.invalidation_price;
  const notional = triggerPrice ? Number(triggerPrice) * Number(item.quantity) : null;
  const reviewDone = isExpired || !['DRAFT','PENDING_REVIEW'].includes(item.status);
  const riskDone = Boolean(item.risk_decision);
  const riskDenied = item.risk_decision?.result === 'DENY';
  const riskReason = item.risk_decision?.reasons?.[0];
  const riskHelp = riskGuidance(riskReason);
  const riskContext = item.risk_decision?.context || {};
  const authorizationDone = Boolean(item.authorization);
  const authorizationUsable = Boolean(item.authorization?.active && new Date(item.authorization.expires_at).getTime() > Date.now());
  const riskAfterAuthorization = Boolean(item.risk_decision?.created_at && item.authorization?.created_at && new Date(item.risk_decision.created_at) > new Date(item.authorization.created_at));
  const initialEntry = item.initial_entry;
  const needsFreshRisk = Boolean(authorizationDone && !authorizationUsable && !initialEntry && !riskAfterAuthorization);
  const needsAuthorization = Boolean(riskDone && !riskDenied && !initialEntry && (!authorizationDone || (!authorizationUsable && riskAfterAuthorization)));
  const terminal = isExpired || ['REJECTED','EXPIRED'].includes(item.status);
  const rationale = details.rationale || candidate.rationale || '未提供补充理由';
  const sourceLink = item.source_link || candidate.detail_url;
  const chartLink = candidate.chart_url;
  const sourceFacts = item.source === 'SYSTEM'
    ? `<div class="source-facts"><div><small>来源状态</small><b class="${item.source_readiness === 'READY' ? 'direction-long' : 'direction-short'}">${escapeHtml(fmtReadiness(item.source_readiness))}</b></div><div><small>共振周期</small><b>${escapeHtml(candidate.timeframe || '—')}</b></div><div><small>成交量</small><b>${fmtCompact(candidate.quote_volume)}</b></div><div><small>持仓量</small><b>${fmtCompact(candidate.open_interest)}</b></div><div><small>更新时间</small><b>${fmtDate(item.source_observed_at)}</b></div></div>`
    : '<div class="source-facts manual-source"><div><small>来源</small><b>人工输入</b></div><div><small>审核依据</small><b>保存参数与提案理由</b></div></div>';
  const highRiskReviewCopy = item.risk_tier === 'HIGH' ? `高风险提案需要两名不同审核人；当前已记录 ${item.approvals.length} 票。` : '批准后仍需运行系统风险检查。';
  const nextAction = terminal
    ? {title:isExpired ? '提案已到期' : '流程已终止', copy:'该提案不能继续扩大风险。条件改变后需创建新提案。', tone:'danger'}
    : item.status === 'PENDING_REVIEW'
      ? canReview
        ? {title:'需要你的独立判断', copy:`核对方向、触发价、失效位置和最大风险。${highRiskReviewCopy}`, tone:'attention'}
        : reviewedByMe
          ? {title:'你的审核已记录', copy:'这笔提案仍在等待另一名独立审核人；你无需再次操作。', tone:'success'}
          : {title:'等待独立审核', copy:item.proposer_id === session.user_id ? '你是提案创建者，不能审核自己的提案。' : '当前角色没有审核权限。', tone:'neutral'}
      : initialEntry
        ? {title:'初仓意图已经创建', copy:'该提案不能再创建第二个初仓意图；后续执行、保护和异常处理统一进入交易任务。', tone:'success'}
        : item.status === 'APPROVED' && (!riskDone || riskDenied)
          ? riskDenied
            ? {title:riskHelp.label, copy:riskHelp.action, tone:'danger'}
            : {title:'下一步：运行风险检查', copy:'审核已完成，交易运维人员需要基于最新账户数据运行风险检查。', tone:'attention'}
        : needsFreshRisk
          ? {title:'短期授权已经失效', copy:'重新读取当前账户事实并运行风险检查；通过后才能签发新的短期授权。', tone:'danger'}
        : needsAuthorization
          ? {title:'下一步：签发短期授权', copy:'风险检查已通过，可签发限时、限数量、限风险的交易授权。', tone:'attention'}
          : authorizationUsable
            ? {title:'已准备创建初仓意图', copy:'授权仍在有效期内；创建后只记录风险占用和订单意图，后续执行仍受服务端控制开关和发送租约限制。', tone:'success'}
            : {title:'当前没有待办动作', copy:'请核对授权有效期和当前状态。', tone:'neutral'};
  const canRunRisk = item.status === 'APPROVED' && canOperate && (!riskDone || riskDenied || needsFreshRisk);
  const executionAction = initialEntry
    ? `<a class="primary wide-action" href="/campaigns/${initialEntry.campaign_id}" data-link>进入交易任务</a><p class="microcopy">初仓意图 ${shortId(initialEntry.intent_id)} · ${escapeHtml(fmtStatus(initialEntry.intent_status))}</p>`
    : canRunRisk
      ? `<button class="primary wide-action" data-risk>${riskDenied ? '处理后重新检查' : needsFreshRisk ? '重新检查当前风险' : '运行风险检查'}</button>`
      : needsAuthorization && canOperate
        ? '<button class="primary wide-action" data-authorize>签发 30 分钟授权</button>'
        : authorizationUsable && canOperate
          ? '<button class="primary wide-action" data-initial>创建一次性初仓意图</button>'
          : '';
  const riskOutcomeCopy = !riskDone
    ? ''
    : item.risk_decision.result === 'ALLOW'
      ? `当前事实允许计划数量 ${fmtNumber(item.risk_decision.approved_quantity)}，最多占用 ${fmtAmount(item.risk_decision.risk_amount, item.collateral_currency)} 风险。`
      : item.risk_decision.result === 'SCALE'
        ? `系统把请求数量 ${fmtNumber(riskContext.requested_quantity)} 缩小为 ${fmtNumber(item.risk_decision.approved_quantity)}；授权不能超过缩小后的边界。`
        : `${riskHelp.label}。${riskHelp.action}`;
  const riskCapacityCopy = riskContext.managed_capital_known
    ? `${fmtAmount(riskContext.current_risk, item.collateral_currency)} / ${fmtAmount(riskContext.effective_max_total_risk || riskContext.max_total_risk, item.collateral_currency)}`
    : `${fmtAmount(riskContext.current_risk, item.collateral_currency)} / 受管资金未确认`;
  const riskReasons = !riskDone
    ? ''
    : item.risk_decision.reasons.length
      ? `<div class="risk-guidance-list">${item.risk_decision.reasons.map(reason => { const guidance = riskGuidance(reason); return `<div><b>${escapeHtml(guidance.label)}</b><span>${escapeHtml(guidance.action)}</span></div>`; }).join('')}</div>`
      : '<p class="success-note">仓位、权益、受管资金、系统状态和总风险容量均通过。</p>';
  const riskDecisionPanel = riskDone
    ? `<p class="risk-outcome-copy">${escapeHtml(riskOutcomeCopy)}</p><dl class="definition-grid risk-decision-grid">${definition('请求数量', fmtNumber(riskContext.requested_quantity))}${definition('系统批准数量', fmtNumber(item.risk_decision.approved_quantity))}${definition('本次风险占用', fmtAmount(item.risk_decision.risk_amount, item.collateral_currency))}${definition('组合风险容量', riskCapacityCopy)}${definition('事实年龄', `${fmtSeconds(riskContext.fact_age_seconds)} / 上限 ${fmtSeconds(riskContext.max_fact_age_seconds)}`)}${definition('数据截止', fmtDate(item.risk_decision.data_as_of))}</dl><div class="risk-fact-strip"><span>仓位 <b>${escapeHtml(factStatusLabel(riskContext.position_status))}</b></span><span>权益 <b>${escapeHtml(factStatusLabel(riskContext.equity_status))}</b></span><span>受管资金 <b>${riskContext.managed_capital_known ? '已确认' : '缺失'}</b></span><span>保护 <b>${escapeHtml(factStatusLabel(riskContext.protection_status))}</b></span></div>${riskReasons}`
    : '<div class="empty-inline"><b>等待审核通过</b><span>风险检查会读取服务端最新仓位、权益、受管资金、保护和总风险容量。</span></div>';
  const authorizationState = !authorizationDone ? '未签发' : authorizationUsable ? '有效' : item.authorization.active ? '已过期' : '已撤销';
  const authorizationPanel = authorizationDone
    ? `<dl class="definition-grid authorization-grid">${definition('批准数量', fmtNumber(item.authorization.quantity_limit))}${definition('已使用', fmtNumber(item.authorization.used_quantity))}${definition('剩余数量', fmtNumber(item.authorization.remaining_quantity))}${definition('风险上限', fmtAmount(item.authorization.risk_limit, item.collateral_currency))}${definition('可用加仓次数', `${item.authorization.used_adds} / ${item.authorization.allowed_adds}`)}${definition('到期', fmtDate(item.authorization.expires_at))}</dl>${initialEntry ? `<div class="entry-boundary"><b>一次性初仓已使用</b><span>意图 ${shortId(initialEntry.intent_id)} · ${escapeHtml(fmtStatus(initialEntry.intent_status))}</span><a href="/campaigns/${initialEntry.campaign_id}" data-link>查看交易任务 →</a></div>` : '<p class="microcopy">授权仍不是订单；创建初仓意图时还会再次读取数据并预留风险。</p>'}`
    : '<div class="empty-inline"><b>风险通过后可签发</b><span>授权同时限制有效期、数量、风险金额、权限范围和可用加仓次数。</span></div>';
  main.innerHTML = `<section class="page proposal-detail"><header class="page-head"><div><p class="eyebrow">${escapeHtml(fmtEnvironment(item.environment, true))} · ${escapeHtml(item.source === 'SYSTEM' ? 'PERPTAPE 机会' : '人工提案')}</p><div class="proposal-title-row"><h1>${escapeHtml(item.symbol || candidate.symbol || '交易提案')}</h1><span class="direction-pill ${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(fmtDirection(item.direction))}</span><span class="status-pill status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</span></div><p class="lede">${escapeHtml(item.venue)} · ${escapeHtml(item.account_id)} · 提案 ${shortId(item.proposal_id)} · 版本 ${item.version}</p></div><div class="toolbar"><a class="secondary" href="/reviews" data-link>返回审核队列</a>${sourceLink ? `<a class="secondary" href="${escapeHtml(sourceLink)}" target="_blank" rel="noreferrer">Perptape 榜单 ↗</a>` : ''}${chartLink ? `<a class="secondary" href="${escapeHtml(chartLink)}" target="_blank" rel="noreferrer">交易所图表 ↗</a>` : ''}</div></header>
    <ol class="workflow-stepper" aria-label="提案流程"><li class="done"><span>1</span><div><b>提案已保存</b><small>${fmtDate(item.frozen_at)}</small></div></li><li class="${reviewDone ? 'done' : 'current'}"><span>2</span><div><b>独立审核</b><small>${isExpired ? '已到期' : reviewDone ? fmtStatus(item.status) : reviewedByMe ? '你的审核已记录' : '等待判断'}</small></div></li><li class="${riskDenied ? 'blocked' : riskDone ? 'done' : reviewDone && !terminal ? 'current' : ''}"><span>3</span><div><b>风险检查</b><small>${riskDone ? fmtStatus(item.risk_decision.result) : '尚未运行'}</small></div></li><li class="${initialEntry || authorizationUsable ? 'done' : needsAuthorization ? 'current' : ''}"><span>4</span><div><b>短期授权</b><small>${initialEntry ? '已生成初仓意图' : authorizationDone ? (authorizationUsable ? '有效' : '已失效') : '尚未签发'}</small></div></li></ol>
    <div class="proposal-detail-layout"><div class="stack">
      <article class="card decision-brief"><div class="card-heading"><div><p class="eyebrow">交易判断摘要</p><h2>这笔交易要做什么</h2></div><span class="risk-badge risk-${escapeHtml(item.risk_tier)}">${escapeHtml(fmtRisk(item.risk_tier))}</span></div><p class="proposal-rationale">${escapeHtml(rationale)}</p><div class="decision-metrics"><div><small>计划数量</small><b>${fmtNumber(item.quantity)}</b><span>初仓 ${fmtNumber(details.initial_quantity || item.quantity)}</span></div><div><small>估算名义价值</small><b>${notional === null ? '—' : escapeHtml(fmtAmount(notional, item.quote_currency))}</b><span>触发价 ${fmtNumber(triggerPrice)}</span></div><div><small>最大风险</small><b>${escapeHtml(fmtAmount(item.max_risk, item.collateral_currency))}</b><span>${fmtRisk(item.risk_tier)}</span></div><div><small>失效位置</small><b>${fmtNumber(invalidationPrice)}</b><span>距触发 ${percentageDistance(triggerPrice, invalidationPrice)}</span></div></div>${sourceFacts}</article>
      <article class="card frozen-scope"><div class="card-heading"><div><p class="eyebrow">已保存参数</p><h2>提案范围</h2></div><span class="status-pill">不可编辑</span></div><dl class="definition-grid spacious">${definition('账户', item.account_id)}${definition('交易所', item.venue)}${definition('方向', fmtDirection(item.direction))}${definition('风险档位', fmtRisk(item.risk_tier))}${definition('限价', fmtNumber(details.limit_price))}${definition('有效至', fmtDate(item.expires_at))}${definition('自动加仓', details.allow_auto_add ? `允许 · ${details.requested_adds} 次` : '关闭')}${definition('加仓触发价', fmtNumber(details.add_trigger_price))}${definition('来源候选', item.source_candidate_id || '人工创建')}${definition('来源更新时间', fmtDate(item.source_observed_at))}</dl></article>
      <article class="card review-trail"><div class="card-heading"><div><p class="eyebrow">审核记录</p><h2>审核记录</h2></div><span class="subtle">${item.approvals.length} 条记录</span></div>${item.approvals.length ? `<div class="review-timeline">${item.approvals.map(a => `<div class="review-event"><span class="${a.decision === 'APPROVE' ? 'approve-dot' : 'reject-dot'}"></span><div><b>${a.decision === 'APPROVE' ? '批准提案' : '拒绝提案'}</b><p>${escapeHtml(a.reason)}</p><small>${shortId(a.reviewer_id)} · ${fmtDate(a.created_at)}</small></div></div>`).join('')}</div>` : '<div class="empty-inline"><b>尚无审核记录</b><span>审核人的独立判断会按时间出现在这里。</span></div>'}</article>
    </div><aside class="stack proposal-actions-column">
      <article class="card next-action tone-${nextAction.tone}"><p class="eyebrow">下一步</p><h2>${escapeHtml(nextAction.title)}</h2><p>${escapeHtml(nextAction.copy)}</p>${item.status === 'PENDING_REVIEW' && canReview ? `<label>审核意见<span class="field-help">说明你核对了什么，以及判断依据</span><textarea id="review-reason" rows="4">已核对交易逻辑、保存参数与最大风险边界</textarea></label><div class="review-actions"><button class="primary" data-approve>批准提案</button><button class="danger" data-reject>拒绝提案</button></div><p class="microcopy">批准时需要进行一次二次强验证；不会直接下单。</p><div class="form-error" id="review-error"></div>` : ''}${executionAction}<div class="form-error" id="execution-error"></div></article>
      <article class="card risk-engine-card"><div class="card-heading"><div><p class="eyebrow">风险检查</p><h2>系统允许开多少</h2></div>${item.risk_decision ? `<span class="status-pill status-${escapeHtml(item.risk_decision.result)}">${escapeHtml(fmtStatus(item.risk_decision.result))}</span>` : '<span class="status-pill">未运行</span>'}</div>${riskDecisionPanel}</article>
      <article class="card authorization-card"><div class="card-heading"><div><p class="eyebrow">限时授权</p><h2>这份许可还能做什么</h2></div><span class="status-pill ${authorizationUsable ? 'status-APPROVED' : authorizationDone ? 'status-EXPIRED' : ''}">${authorizationState}</span></div>${authorizationPanel}</article>
    </aside></div></section>`;
  document.querySelector('[data-approve]')?.addEventListener('click', () => approveProposal(item));
  document.querySelector('[data-reject]')?.addEventListener('click', () => rejectProposal(item));
  document.querySelector('[data-risk]')?.addEventListener('click', (event) => runRisk(item, event.currentTarget));
  document.querySelector('[data-authorize]')?.addEventListener('click', (event) => authorize(item, event.currentTarget));
  document.querySelector('[data-initial]')?.addEventListener('click', (event) => createInitialIntent(item, event.currentTarget));
}

const definition = (label, value) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? '—')}</dd></div>`;

async function approveProposal(item) {
  const errorBox = document.querySelector('#review-error');
  try {
    const grant = await api('/api/auth/mock/step-up', {method:'POST', body:JSON.stringify({action:'proposal.approve', object_id:item.proposal_id, object_version:item.version})});
    await api(`/api/proposals/${item.proposal_id}/reviews`, {method:'POST', body:JSON.stringify({decision:'APPROVE', reason:document.querySelector('#review-reason').value, expected_version:item.version, action_grant:grant.action_grant})});
    showToast('审核结果已记录'); await route();
  } catch (error) { showApiError(error, errorBox); }
}

async function rejectProposal(item) {
  try {
    await api(`/api/proposals/${item.proposal_id}/reviews`, {method:'POST', body:JSON.stringify({decision:'REJECT', reason:document.querySelector('#review-reason').value, expected_version:item.version})});
    showToast('提案已拒绝'); await route();
  } catch (error) { showApiError(error, document.querySelector('#review-error')); }
}

async function runRisk(item, button) {
  await withPending(button, '检查中…', async () => {
    try { await api(`/api/proposals/${item.proposal_id}/risk-decisions`, {method:'POST', body:JSON.stringify({idempotency_key:crypto.randomUUID()})}); showToast('风险检查已完成'); await route(); }
    catch (error) { showApiError(error, document.querySelector('#execution-error')); }
  });
}

async function authorize(item, button) {
  const allowedAdds = item.frozen_payload?.details?.allow_auto_add ? Number(item.frozen_payload.details.requested_adds || 0) : 0;
  await withPending(button, '签发中…', async () => {
    try { await api(`/api/proposals/${item.proposal_id}/authorizations`, {method:'POST', body:JSON.stringify({idempotency_key:crypto.randomUUID(), expires_in_minutes:30, allowed_adds:allowedAdds})}); showToast('短期授权已签发'); await route(); }
    catch (error) { showApiError(error, document.querySelector('#execution-error')); }
  });
}

async function createInitialIntent(item, button) {
  await withPending(button, '创建中…', async () => {
    try {
      const initialQuantity = item.frozen_payload?.details?.initial_quantity || item.authorization.quantity_limit;
      const result = await api(`/api/authorizations/${item.authorization.authorization_id}/intents`, {method:'POST', body:JSON.stringify({kind:'INITIAL', account_id:item.account_id, venue:item.venue, instrument_id:item.instrument_id, direction:item.direction, quantity:initialQuantity, idempotency_key:crypto.randomUUID()})});
      showToast('风险已原子预留，初仓意图已创建'); navigate(`/campaigns/${result.campaign_id}`);
    } catch (error) { showApiError(error, document.querySelector('#execution-error')); }
  });
}

async function loadCampaignDetails() {
  const result = await api('/api/campaigns');
  const visible = result.data.filter(item => item.environment === 'LIVE');
  return Promise.all(visible.map((item) => api(`/api/campaigns/${item.campaign_id}`)));
}

const riskControlStatusLabel = (value) => ({
  NORMAL:'正常开放', NO_PYRAMID:'禁止加仓', REDUCE_ONLY:'仅允许减仓', KILL_SWITCH:'紧急停止',
  ENABLED:'已启用', DISABLED:'已关闭', PENDING_REVIEW:'等待双人审核', APPROVED:'审核完成待执行',
  REJECTED:'已拒绝', EXPIRED:'已过期', EXECUTED:'已执行',
}[value] || value);

function formatControlBlocker(value) {
  if (apiErrorGuidance[value]) return apiErrorGuidance[value];
  if (exceptionGuidance[value]) return exceptionGuidance[value].title;
  return ({
    ACTIVE_ORDER_INTENT:'仍有订单正在处理',
    PENDING_RECONCILIATION:'最新数据尚未完成对账',
    COOLDOWN_ACTIVE:'安全冷却时间尚未结束',
    UNKNOWN_OUTCOME:'仍有结果未知的操作',
  }[value] || '一项安全条件尚未满足');
}

function formatControlReason(value) {
  const normalized = String(value || '').trim();
  return ({
    'administrator paused new risk from Web':'管理员暂停了所有新增风险',
    'administrator disabled AUTO_ADD from Web':'管理员关闭了全局自动加仓',
  }[normalized] || (/[\u3400-\u9fff]/.test(normalized) ? normalized : '管理员执行了风险控制变更'));
}

function renderRiskControlPanel(control) {
  if (!control) return `<article class="card"><div class="card-heading"><div><p class="eyebrow">全局风险控制</p><h2>全局风险恢复由管理员控制</h2></div><span class="status-pill">当前权限范围</span></div><p class="subtle">当前身份只能查看获准访问的账户和交易所风险，不能读取或执行全局风险政策、自动加仓控制和恢复申请。</p><p class="safety-note">这不代表全局风险状态正常。新增风险仍会由系统强制检查；你仍可使用下表查看风险占用、唯一减仓目标和最近对账。</p><div class="toolbar"><a class="secondary" href="/" data-link>返回今日</a><a class="primary" href="/exceptions" data-link>查看当前异常</a></div></article>`;
  const policy = control.policy;
  const gate = control.auto_add_gate;
  const conditions = control.restore_conditions;
  const hasLiveScope = conditions.required_scopes.some(scope => scope.environment === 'LIVE');
  const restoreGateLabel = conditions.ready
    ? (hasLiveScope ? '生产账户条件满足' : '生产账户范围未配置')
    : `${conditions.blockers.length} 项阻塞`;
  const isAdmin = roleNames().includes('SYSTEM_ADMIN');
  const canReview = roleNames().includes('REVIEWER') || isAdmin;
  const activeRequest = control.requests.find(item => ['PENDING_REVIEW','APPROVED'].includes(item.status));
  const requestForm = isAdmin && !activeRequest && (policy.system_state !== 'NORMAL' || gate.status !== 'ENABLED')
    ? `<form id="risk-restore-form" class="form-panel compact-form"><h2>申请受审核恢复</h2><p class="danger-note"><b>这不是反向开关。</b>申请只会保存当前风险政策、控制开关和受控范围；两名独立审核人分别完成强身份验证后，执行时还会重新检查数据、对账、未决订单、冷却期和版本变化。旧提案、旧授权和旧的可用加仓次数永远不会恢复。</p><label>恢复理由<textarea name="reason" rows="4" minlength="10" required>已完成异常处置，并准备由两名独立审核人复核全部恢复条件</textarea></label><label class="checkbox-row"><input name="restore_auto_add" type="checkbox">同时申请恢复全局自动加仓（旧的可用加仓次数仍保持撤销）</label><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">创建恢复申请</button></div></form>`
    : '';
  const requestCards = control.requests.length ? control.requests.map(item => {
    const isRequester = item.requester_id === session.user_id;
    const reviewedByMe = item.reviews.some(review => review.reviewer_id === session.user_id);
    const reviewUi = item.status === 'PENDING_REVIEW' && canReview && !isRequester && !reviewedByMe
      ? `<label>独立审核理由<textarea id="risk-review-${item.request_id}" rows="3">已核对冻结版本、恢复影响和当前阻塞条件</textarea></label><div class="toolbar"><button class="primary" data-risk-review="${item.request_id}" data-decision="APPROVE" data-version="${item.version}">强验证并批准</button><button class="danger" data-risk-review="${item.request_id}" data-decision="REJECT" data-version="${item.version}">拒绝申请</button></div>`
      : '<p class="subtle">当前身份或申请状态没有可用审核动作。</p>';
    const executeUi = item.status === 'APPROVED' && isAdmin
      ? `<button class="danger" data-risk-execute="${item.request_id}" data-version="${item.version}">强验证并执行恢复</button><p class="safety-note">执行时会再次进行安全检查；任何数据、权限范围、风险政策或控制开关发生变化，都会拒绝恢复。</p>`
      : '';
    return `<article class="card"><div class="card-head"><div><p class="eyebrow">恢复申请</p><h2>${riskControlStatusLabel(item.status)}</h2></div><span class="tag">${shortId(item.request_id)}</span></div><dl class="definition-grid">${definition('申请人', shortId(item.requester_id))}${definition('恢复自动加仓', item.restore_auto_add ? '是' : '否')}${definition('最早执行', fmtDate(item.execute_after))}${definition('到期', fmtDate(item.expires_at))}${definition('原控制状态', fmtStatus(item.source_auto_add_status))}</dl><p>${escapeHtml(item.reason)}</p><h3>审核记录</h3>${item.reviews.length ? item.reviews.map(review => `<div class="callout"><b>${escapeHtml(review.decision === 'APPROVE' ? '批准' : '拒绝')}</b> · ${escapeHtml(review.reason)}<br><span class="subtle">${shortId(review.reviewer_id)} · ${fmtDate(review.created_at)}</span></div>`).join('') : '<p class="subtle">尚无审核票。</p>'}<div class="review-action-panel">${reviewUi}${executeUi}</div></article>`;
  }).join('') : '<section class="empty-state"><div><h2>尚无恢复申请</h2><p>收紧控制不会自动恢复；需要创建冻结申请并完成双人独立审核。</p></div></section>';
  return `<section class="risk-control-overview"><div class="stats"><div class="stat"><small>风险政策</small><b>${riskControlStatusLabel(policy.system_state)}</b></div><div class="stat"><small>政策更新时间</small><b>${fmtDate(policy.updated_at)}</b></div><div class="stat"><small>自动加仓</small><b>${riskControlStatusLabel(gate.status)}</b></div><div class="stat"><small>恢复条件</small><b>${restoreGateLabel}</b></div></div><div class="detail-layout"><article class="card"><h2>当前控制状态</h2><dl class="definition-grid">${definition('政策原因', formatControlReason(policy.reason))}${definition('政策更新人', shortId(policy.updated_by))}${definition('政策更新时间', fmtDate(policy.updated_at))}${definition('控制原因', formatControlReason(gate.reason))}${definition('控制操作人', shortId(gate.operator_id))}${definition('控制更新时间', fmtDate(gate.updated_at))}</dl><p class="safety-note">“仅允许减仓”仍允许减仓与退出；暂停新增风险后，当时所有未过期的新风险授权都会永久失效。</p></article><article class="card"><h2>实时恢复条件</h2>${conditions.blockers.length ? `<ul class="exception-list">${conditions.blockers.map(item => `<li>${escapeHtml(formatControlBlocker(item))}</li>`).join('')}</ul>` : hasLiveScope ? '<p class="success-note">生产账户恢复条件当前无阻塞；执行时仍会重新验证。</p>' : '<p class="danger-note">尚未配置生产账户范围，不能判断恢复条件。</p>'}<h3>受控账户</h3>${conditions.required_scopes.filter(scope => scope.environment === 'LIVE').length ? conditions.required_scopes.filter(scope => scope.environment === 'LIVE').map(scope => `<div class="callout"><b>${escapeHtml(scope.account_id)}</b> · ${escapeHtml(scope.venue)}</div>`).join('') : '<p class="danger-note">尚未配置生产账户范围，因此恢复执行保持关闭。</p>'}</article></div>${requestForm}<div class="section-head"><div><p class="eyebrow">双人复核流程</p><h2>恢复申请与独立审核</h2></div></div><div class="stack">${requestCards}</div></section>`;
}

async function bindRiskControlActions() {
  document.querySelector('#risk-restore-form')?.addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const button = event.submitter || form.querySelector('button');
    await withPending(button, '冻结中…', async () => { try { await api('/api/risk-controls/restores', {method:'POST', body:JSON.stringify({reason:data.get('reason'), restore_auto_add:data.get('restore_auto_add') === 'on', idempotency_key:crypto.randomUUID()})}); showToast('恢复申请已冻结，等待两名独立审核人'); await route(); } catch (error) { showApiError(error, form.querySelector('.form-error')); } });
  });
  document.querySelectorAll('[data-risk-review]').forEach(button => button.addEventListener('click', async () => {
    const requestId = button.dataset.riskReview; const version = Number(button.dataset.version); const decision = button.dataset.decision; const reason = document.querySelector(`#risk-review-${requestId}`)?.value || '独立审核拒绝';
    await withPending(button, '提交中…', async () => { try { let action_grant = null; if (decision === 'APPROVE') { const grant = await api('/api/auth/mock/step-up', {method:'POST', body:JSON.stringify({action:'risk.restore.review', object_id:requestId, object_version:version})}); action_grant = grant.action_grant; } await api(`/api/risk-controls/restores/${requestId}/reviews`, {method:'POST', body:JSON.stringify({decision, reason, expected_version:version, idempotency_key:crypto.randomUUID(), action_grant})}); showToast(decision === 'APPROVE' ? '独立审核票已记录' : '恢复申请已拒绝'); await route(); } catch (error) { showApiError(error); } });
  }));
  document.querySelectorAll('[data-risk-execute]').forEach(button => button.addEventListener('click', async () => {
    const requestId = button.dataset.riskExecute; const version = Number(button.dataset.version);
    const confirmed = await confirmAction({title:'执行受审核恢复？', message:'系统将重新验证所有受控范围、当前数据、对账结果、未决订单、冷却期和控制版本。只会创建新的正常风险政策；旧授权和旧的可用加仓次数不会恢复。', confirmLabel:'重新验证并执行'}); if (!confirmed) return;
    await withPending(button, '验证中…', async () => { try { const grant = await api('/api/auth/mock/step-up', {method:'POST', body:JSON.stringify({action:'risk.restore.execute', object_id:requestId, object_version:version})}); await api(`/api/risk-controls/restores/${requestId}/execute`, {method:'POST', body:JSON.stringify({expected_version:version, idempotency_key:crypto.randomUUID(), action_grant:grant.action_grant})}); showToast('新的正常风险政策已创建；旧授权保持失效'); await route(); } catch (error) { showApiError(error); } });
  }));
}

async function renderCampaignList() {
  const result = await api('/api/campaigns');
  const items = result.data.filter(item => item.environment === 'LIVE');
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">交易任务</p><h1>交易任务</h1><p class="lede">每个交易任务覆盖一笔交易从授权、风险占用和下单意图，到成交、保护、减仓、对账与最终结果的完整生命周期。</p></div><div class="toolbar"><a class="secondary" href="/proposals" data-link>查看提案</a></div></header>
    <div class="stats"><div class="stat"><small>全部交易任务</small><b>${items.length}</b></div><div class="stat"><small>建仓中 / 持仓中</small><b>${items.filter(i => ['OPEN','OPENING'].includes(i.status)).length}</b></div><div class="stat"><small>结果未知</small><b>${items.filter(i => i.status === 'UNKNOWN').length}</b></div><div class="stat"><small>运行范围</small><b style="font-size:14px">生产交易</b></div></div>
    ${items.length ? `<div class="table-wrap"><table><thead><tr><th>交易任务</th><th>账户范围</th><th>方向 / 目标</th><th>状态</th><th>盈亏</th><th>更新时间</th></tr></thead><tbody>${items.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td><b>${shortId(item.campaign_id)}</b><br><span class="subtle">提案 ${shortId(item.proposal_id)}</span></td><td>${escapeHtml(item.account_id)}<br><span class="subtle">${escapeHtml(item.venue)}</span></td><td class="${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(fmtDirection(item.direction))} · ${fmtNumber(item.current_target_quantity)}</td><td><b class="status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</b></td><td>${fmtNumber(item.final_pnl)}</td><td>${fmtDate(item.updated_at)}</td></tr>`).join('')}</tbody></table></div>` : `<section class="empty-state"><div><h2>当前没有交易任务</h2><p>提案通过审核和风险检查后，交易运维人员才能发起开仓。</p></div></section>`}</section>`;
  bindLinkedRows();
}

function systemHealthCard({title, status, copy, tone = 'success', meta = ''}) {
  return `<article class="system-health-card tone-${tone}"><div class="system-health-head"><span class="health-indicator" aria-hidden="true"></span><div><small>${escapeHtml(title)}</small><h2>${escapeHtml(status)}</h2></div></div><p>${escapeHtml(copy)}</p>${meta ? `<span class="system-health-meta">${escapeHtml(meta)}</span>` : ''}</article>`;
}

async function renderSystemStatus() {
  const healthRequest = api('/health/ready').then(() => ({ready:true})).catch(error => ({ready:false, error}));
  const [health, control, campaignsResponse, exceptionsResponse, binance, hyperliquid, runtime] = await Promise.all([
    healthRequest,
    api('/api/risk-controls').catch(error => ({error})),
    api('/api/campaigns'),
    api('/api/campaign-exceptions'),
    api('/api/venues/binance/status'),
    api('/api/venues/hyperliquid/status'),
    api('/api/runtime/status').catch(error => ({error})),
  ]);
  const campaigns = campaignsResponse.data.filter(item => item.status !== 'CLOSED' && item.environment === 'LIVE');
  const details = await Promise.all(campaigns.map(item => api(`/api/campaigns/${item.campaign_id}`)));
  const liveCampaignIds = new Set(campaigns.map(item => item.campaign_id));
  const exceptions = exceptionsResponse.data.filter(item => liveCampaignIds.has(item.campaign_id));
  const codes = new Set(exceptions.map(item => item.code));
  const unknownIntents = details.flatMap(item => item.intents).filter(item => item.status === 'UNKNOWN').length;
  const protectionIssues = exceptions.filter(item => item.code.startsWith('PROTECTION_'));
  const exposureIssues = exceptions.filter(item => ['POSITION_UNKNOWN','POSITION_STALE','RISK_RESERVATION_UNKNOWN'].includes(item.code));
  const reconciliationIssues = exceptions.filter(item => item.code.startsWith('RECONCILIATION_'));
  const controlAvailable = !control.error;
  const policy = control.policy || {system_state:'UNKNOWN', version:'—'};
  const gate = control.auto_add_gate || {status:'UNKNOWN', version:'—'};
  const entryOpen = controlAvailable && policy.system_state === 'NORMAL';
  const addOpen = entryOpen && gate.status === 'ENABLED';
  const venueConnections = [binance, hyperliquid];
  const connectedVenues = venueConnections.filter(item => item.enabled && item.configured);
  const perptape = runtime?.data?.external_boundaries?.perptape || {configured:false,status:'NOT_CONFIGURED',candidate_count:0,last_fetched_at:null,contract_version:'—'};
  const perptapeConnected = perptape.status === 'SUCCESS';
  const perptapeStatus = perptapeConnected
    ? '连接正常'
    : perptape.status === 'STALE'
      ? '数据已过期'
      : perptape.status === 'WAITING'
        ? '等待首次同步'
        : perptape.status === 'ON_DEMAND'
          ? '按需读取，尚未验证'
          : perptape.configured
            ? '连接状态未知'
            : '尚未配置';
  const perptapeTone = perptapeConnected ? 'success' : perptape.configured ? 'attention' : 'danger';
  const activeMonitoring = campaigns.length > 0;
  const overallTone = !health.ready || !controlAvailable ? 'danger' : exceptions.length || !perptapeConnected ? 'attention' : activeMonitoring ? 'success' : 'neutral';
  const cards = [
    systemHealthCard({title:'核心服务', status:health.ready ? '服务可用' : '服务不可用', tone:health.ready ? 'success' : 'danger', copy:health.ready ? '业务数据库和交易服务运行正常。' : '核心服务检查失败；不能把缺失响应当成正常。', meta:'数据缺失时自动阻止交易'}),
    systemHealthCard({title:'开仓与加仓', status:controlAvailable ? (addOpen ? '允许新增风险' : entryOpen ? '自动加仓已关闭' : riskControlStatusLabel(policy.system_state)) : '风险政策未配置', tone:addOpen ? 'success' : controlAvailable ? 'attention' : 'danger', copy:controlAvailable ? `风险政策：${riskControlStatusLabel(policy.system_state)}；自动加仓：${riskControlStatusLabel(gate.status)}。` : '缺少当前风险政策或自动加仓控制，系统会阻止新增风险。', meta:'政策变化会立即重新检查所有新增风险'}),
    systemHealthCard({title:'减仓与退出', status:!activeMonitoring ? '当前无运行中任务' : unknownIntents ? '部分交易任务需要先对账' : '路径可用', tone:!activeMonitoring ? 'neutral' : unknownIntents ? 'attention' : 'success', copy:!activeMonitoring ? '当前没有需要减仓或退出的交易任务。' : unknownIntents ? `${unknownIntents} 个订单结果未知，相关交易任务禁止重复动作；其他已知仓位仍可减仓或退出。` : '即使新增风险受限，受控减仓与退出仍然可用。', meta:`${campaigns.length} 个运行中交易任务`}),
    systemHealthCard({title:'止损与保护监控', status:!activeMonitoring ? '当前无监控对象' : protectionIssues.length ? `${protectionIssues.length} 项需要处理` : '监控正常', tone:!activeMonitoring ? 'neutral' : protectionIssues.length ? 'danger' : 'success', copy:!activeMonitoring ? '有交易任务进入持仓后，系统会持续检查止损和保护覆盖。' : protectionIssues.length ? '检测到保护缺失、过期、未知或覆盖不足。' : '运行中的交易任务没有保护异常。', meta:`数据截止 ${fmtDate(exceptionsResponse.as_of)}`}),
    systemHealthCard({title:'风险敞口监控', status:!activeMonitoring ? '当前无监控对象' : exposureIssues.length ? `${exposureIssues.length} 项敞口不确定` : '监控正常', tone:!activeMonitoring ? 'neutral' : exposureIssues.length ? 'danger' : 'success', copy:!activeMonitoring ? '有交易任务进入运行后，系统会检查仓位和风险占用。' : exposureIssues.length ? '仓位或风险占用存在未知或过期数据，系统会阻止新增风险。' : '当前没有仓位未知、仓位过期或风险占用未知。', meta:`${exceptions.length} 项总阻断`}),
    systemHealthCard({title:'对账监控', status:!activeMonitoring ? '暂无对账对象' : reconciliationIssues.length ? `${reconciliationIssues.length} 项未一致` : '对账一致', tone:!activeMonitoring ? 'neutral' : reconciliationIssues.length ? 'attention' : 'success', copy:!activeMonitoring ? '当前没有运行中的交易任务需要对账。' : reconciliationIssues.length ? '至少一个权限范围存在差异、未知、过期或需要人工处理。' : '运行中的交易任务没有派生对账异常。', meta:'只有计算结果为“对账一致”才可作为恢复依据'}),
    systemHealthCard({title:'Perptape 机会源', status:perptapeStatus, tone:perptapeTone, copy:perptapeConnected ? `已读取 ${Number(perptape.candidate_count || 0)} 个候选，可用于机会筛选和提案。` : perptape.configured ? 'Perptape 已配置，但最近数据尚未形成可用连接结论。现有交易任务不受影响，新的外部机会不可用。' : 'Perptape 尚未配置；人工提案仍可使用。', meta:`只读 · 最近数据 ${fmtDate(perptape.last_fetched_at)}`}),
  ].join('');
  const venueRows = venueConnections.map(item => `<tr><td><b>${escapeHtml(item.venue)}</b></td><td>${item.enabled && item.configured ? '<span class="status-pill status-APPROVED">连接正常</span>' : `<span class="status-pill">${item.enabled ? '尚未配置' : '已关闭'}</span>`}</td><td>生产账户</td><td>${item.order_send_available ? '下单通道已就绪' : '只读连接'}</td><td><a class="text-button" href="/venues?venue=${encodeURIComponent(item.venue)}" data-link>查看账户数据 →</a></td></tr>`).join('');
  const perptapeRow = `<tr><td><b>Perptape</b></td><td><span class="status-pill ${perptapeConnected ? 'status-APPROVED' : ''}">${escapeHtml(perptapeStatus)}</span></td><td>市场机会</td><td>只读连接</td><td><a class="text-button" href="/opportunities" data-link>查看机会 →</a></td></tr>`;
  const connectedSources = connectedVenues.length + (perptapeConnected ? 1 : 0);
  const verdictTitle = !health.ready ? '核心服务未通过就绪检查' : !controlAvailable ? '核心服务可用，但风险政策未配置' : exceptions.length ? '核心服务可用，但存在风险阻断' : !perptapeConnected ? '交易管理可用，但 Perptape 机会源受限' : activeMonitoring ? '交易系统正在正常监控' : '核心服务可用，当前无运行中交易任务';
  const verdictCopy = !health.ready ? '请先恢复数据库与服务状态，不要继续依赖旧数据。' : !controlAvailable ? `${friendlyApiError(control.error)} 新增风险保持关闭。` : exceptions.length ? `发现 ${exceptions.length} 项安全异常；受影响的新增风险会保持关闭。` : !perptapeConnected ? `${perptapeStatus}。现有交易任务仍可管理，但新的 Perptape 机会暂不可用。` : activeMonitoring ? '运行中的交易任务没有检测到保护、敞口或对账阻断。' : '当前没有需要监控的交易任务；系统不会把“无监控对象”误报为“监控正常”。';
  main.innerHTML = `<section class="page system-status-page"><header class="page-head"><div><p class="eyebrow">交易系统状态</p><h1>系统状态</h1><p class="lede">这里直接说明系统能否工作、哪些能力受限，以及是否需要处理。绿色表示当前证据正常；黄色表示能力受限；红色表示必须先处理；灰色表示当前没有监控对象。</p></div><div class="toolbar"><button class="secondary" data-refresh>刷新状态</button><a class="secondary" href="/risk" data-link>查看风险控制</a></div></header>
    <article class="home-status tone-${overallTone}"><div><p class="eyebrow">当前结论</p><h2>${escapeHtml(verdictTitle)}</h2><p>${escapeHtml(verdictCopy)}</p></div>${exceptions.length ? '<a class="primary" href="/exceptions" data-link>处理异常</a>' : !controlAvailable ? '<a class="secondary" href="/risk" data-link>查看风险控制</a>' : !perptapeConnected ? '<a class="secondary" href="/opportunities" data-link>查看 Perptape</a>' : '<span class="status-pill status-APPROVED">无需立即动作</span>'}</article>
    <div class="system-health-grid">${cards}</div>
    <section><div class="section-heading"><div><p class="eyebrow">外部数据连接</p><h2>交易所与机会源</h2></div><span class="status-pill">${connectedSources} / ${venueConnections.length + 1} 连接正常</span></div><div class="table-wrap"><table><thead><tr><th>数据源</th><th>读取状态</th><th>运行范围</th><th>可用能力</th><th></th></tr></thead><tbody>${venueRows}${perptapeRow}</tbody></table></div></section>
    ${codes.size ? `<section><div class="section-heading"><div><p class="eyebrow">当前阻断</p><h2>需要处理的问题类型</h2></div><a class="secondary" href="/exceptions" data-link>查看恢复步骤</a></div><div class="exception-code-list">${[...codes].sort().map(code => `<span>${escapeHtml(explainException(code).title)}</span>`).join('')}</div></section>` : ''}
  </section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
}

async function renderCampaignFacts(mode) {
  const details = await loadCampaignDetails();
  let riskControls = null;
  if (mode === 'risk') {
    try {
      riskControls = await api('/api/risk-controls');
    } catch (error) {
      if (error.status !== 403) throw error;
    }
  }
  const titles = {orders:'订单与成交', risk:'风险与目标'};
  let rows = '';
  if (mode === 'orders') rows = details.flatMap(item => item.intents.map(intent => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(fmtIntentKind(intent.kind))}${intent.reduce_only ? ' · 只减仓' : ''}</td><td>${escapeHtml(fmtSide(intent.side))} ${fmtNumber(intent.quantity)}</td><td>${escapeHtml(fmtStatus(intent.status))}</td><td>${intent.order ? `${escapeHtml(intent.order.venue_order_id)} · ${escapeHtml(fmtStatus(intent.order.status))}` : '尚未记录交易所回执'}</td><td>${fmtDate(intent.updated_at)}</td></tr>`)).join('');
  if (mode === 'risk') rows = details.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(fmtStatus(item.status))}</td><td>${item.reservations.map(r => `${escapeHtml(fmtStatus(r.status))} ${fmtNumber(r.amount)}`).join(' · ') || '无预留'}</td><td>${fmtNumber(item.current_target_quantity)} · v${item.target_version}</td><td>${escapeHtml(fmtStatus(item.target_urgency || '—'))}</td><td>${escapeHtml(item.reconciliation ? fmtStatus(item.reconciliation.status) : '未对账')}</td></tr>`).join('');
  const headers = mode === 'orders' ? '<th>交易任务</th><th>意图</th><th>方向 / 数量</th><th>状态</th><th>交易所订单</th><th>更新时间</th>' : '<th>交易任务</th><th>状态</th><th>风险预留</th><th>目标</th><th>紧迫度</th><th>对账</th>';
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">生产交易数据</p><h1>${titles[mode]}</h1><p class="lede">这里显示当前确认的数据；能够重新计算的汇总会按最新数据生成。</p></div></header>
    ${mode === 'risk' ? renderRiskControlPanel(riskControls) : ''}
    ${mode === 'risk' && roleNames().includes('SYSTEM_ADMIN') ? '<div class="form-panel compact-form"><h2>只允许收紧风险</h2><p class="safety-note">这些入口只能关闭自动加仓，或把系统切换为“仅允许减仓”；不能从这里恢复新增风险。</p><div class="toolbar"><button class="danger" data-disable-global-add>关闭全局自动加仓</button><button class="danger" data-pause-new-risk>暂停所有新增风险</button></div></div><div style="height:16px"></div>' : ''}
    ${rows ? `<div class="table-wrap"><table><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>` : '<section class="empty-state"><div><h2>当前没有可展示的数据</h2></div></section>'}</section>`;
  bindLinkedRows();
  if (mode === 'risk') await bindRiskControlActions();
  document.querySelector('[data-disable-global-add]')?.addEventListener('click', (event) => campaignAction('/api/operations/auto-add/disable', {reason:'administrator disabled AUTO_ADD from Web', idempotency_key:crypto.randomUUID()}, {
    button:event.currentTarget,
    successMessage:'全局自动加仓已关闭；现有仓位与退出能力不受影响',
    confirm:{title:'关闭全局自动加仓？', message:'确认后，所有交易任务都不能继续使用剩余加仓次数。该入口只会收紧风险，无法在此页重新开启。', confirmLabel:'关闭自动加仓'},
  }));
  document.querySelector('[data-pause-new-risk]')?.addEventListener('click', (event) => campaignAction('/api/operations/pause-new-risk', {reason:'administrator paused new risk from Web', idempotency_key:crypto.randomUUID()}, {
    button:event.currentTarget,
    successMessage:'系统已切换为“仅允许减仓”；只能收紧风险和退出',
    confirm:{title:'暂停所有新增风险？', message:'确认后，系统将只允许减仓。已有仓位仍可减仓或退出，但新的初仓和加仓会被拒绝。', confirmLabel:'切换为仅允许减仓'},
  }));
}

const LIVE_CAPITAL_SOURCES = [
  {key:'BINANCE', location_type:'VENUE', label:'币安'},
  {key:'HYPERLIQUID', location_type:'VENUE', label:'链上永续'},
  {key:'VAULT', location_type:'VAULT', label:'链上资金库'},
];
const OCCUPIED_CAPITAL_TRANSFER_STATUSES = new Set([
  'SOURCE_RESERVED', 'SUBMITTED', 'IN_FLIGHT', 'DESTINATION_CONFIRMED',
  'UNKNOWN', 'MANUAL_REQUIRED',
]);

function partitionCapitalRecords(records) {
  return {
    live: records.filter(item => item.environment === 'LIVE'),
    simulated: records.filter(item => ['SHADOW', 'TESTNET'].includes(item.environment)),
  };
}

function sumCapitalAmounts(values) {
  const parts = values.map(value => {
    const [whole, fraction = ''] = String(value).split('.');
    return {whole, fraction};
  });
  const scale = Math.max(0, ...parts.map(part => part.fraction.length));
  const total = parts.reduce((sum, part) => (
    sum + BigInt(`${part.whole}${part.fraction.padEnd(scale, '0')}`)
  ), 0n);
  if (!scale) return String(total);
  const digits = String(total).padStart(scale + 1, '0');
  return `${digits.slice(0, -scale)}.${digits.slice(-scale)}`;
}

function liveCapitalInTransit(transfers) {
  return sumCapitalAmounts(transfers
    .filter(transfer => transfer.environment === 'LIVE' && OCCUPIED_CAPITAL_TRANSFER_STATUSES.has(transfer.status))
    .map(transfer => transfer.reserved_amount));
}

function formatCapitalIssue(value) {
  const [code, source = ''] = String(value || '').split(':');
  const sourceLabel = ({BINANCE:'币安',HYPERLIQUID:'链上永续',VAULT:'链上资金库'}[source] || source);
  return ({
    MISSING_LIVE_SOURCE:`${sourceLabel || '资金来源'}尚未同步`,
    STALE_LIVE_SOURCE:`${sourceLabel || '资金来源'}数据已过期`,
    UNKNOWN_USD_VALUE:`${sourceLabel || '资金来源'}缺少美元估值`,
  }[code] || '资金数据尚未完整');
}

function renderUnsignedPlanSummary(transfer) {
  const plans = transfer.planned_transactions || [];
  if (!plans.length) return '';
  const actionLabels = {
    approve:'批准资产额度', deposit:'存入资金库', requestWhitelistRelease:'申请释放资金',
    executeWhitelistRelease:'执行资金释放', cancelWhitelistRelease:'取消资金释放',
  };
  const rows = plans.map(plan => `<li><b>${escapeHtml(actionLabels[plan.function_name] || '链上资金操作')}</b><span>链 ${escapeHtml(plan.chain_id)} · 目标 ${escapeHtml(plan.to)} · 原生币金额 ${escapeHtml(plan.value || '0')}</span></li>`).join('');
  return `<details class="wallet-handoff"><summary>${plans.length} 笔交易等待独立钱包确认</summary><ul>${rows}</ul><p class="subtle">资产 ${escapeHtml(transfer.asset)} · 划转 ${fmtNumber(transfer.gross_amount)} · 资金库 ${escapeHtml(transfer.source_id)}</p><button class="secondary" type="button" data-copy-plan="${escapeHtml(transfer.capital_transfer_id)}">复制给独立钱包</button></details>`;
}

function capitalSourceSlots(balances, notiltStatus) {
  const liveBalances = partitionCapitalRecords(balances).live;
  const vaultConfigured = (notiltStatus?.chains || []).some(chain => chain.vault_configured);
  return LIVE_CAPITAL_SOURCES.flatMap(source => {
    const facts = liveBalances.filter(balance => (
      source.key === 'VAULT'
        ? balance.location_type === 'VAULT'
        : balance.location_type === 'VENUE' && balance.venue === source.key
    ));
    if (facts.length) return facts;
    return [{
      environment:'LIVE', location_type:source.location_type, location_id:'—', venue:source.key,
      asset:'USD', confirmed_available:'0', source_reserved:'0', effective_available:'0',
      usd_equity:'0', control_status:'MISSING', deposit_status:'UNKNOWN', observed_at:null,
      fact_status:'MISSING', missing_detail:source.key === 'VAULT'
        ? (vaultConfigured ? '已配置但未同步' : '未配置或未同步')
        : '未同步',
      source_label:source.label,
    }];
  });
}

function capitalBalanceRows(balances) {
  return balances.map(balance => {
    const missing = balance.fact_status === 'MISSING';
    const sourceLabel = balance.source_label || (balance.location_type === 'VAULT' ? '链上资金库' : balance.location_type === 'VENUE' ? '交易所' : balance.location_type);
    const state = missing
      ? `<b class="capital-status-missing">数据缺失</b><br><span class="subtle">${escapeHtml(balance.missing_detail)}</span>`
      : `${escapeHtml(fmtStatus(balance.control_status))} / ${escapeHtml(fmtStatus(balance.deposit_status))}`;
    return `<tr${missing ? ' class="capital-missing-row"' : ''}><td>${escapeHtml(sourceLabel)}<br><span class="subtle">${escapeHtml(balance.location_id)}</span></td><td>${escapeHtml(balance.location_type === 'VAULT' ? '链上资金库' : balance.venue)}</td><td>${fmtNumber(balance.confirmed_available)} ${escapeHtml(balance.asset)}</td><td>${balance.usd_equity === null ? '未知' : `${fmtNumber(balance.usd_equity)} USD`}</td><td>${fmtNumber(balance.source_reserved)}</td><td><b>${fmtNumber(balance.effective_available)}</b></td><td>${state}</td><td>${missing ? '—' : fmtDate(balance.observed_at)}</td></tr>`;
  }).join('');
}

function capitalBalanceTable(rows, emptyMessage) {
  return rows
    ? `<div class="table-scroll-hint">左右滑动查看完整资金数据</div><div class="table-wrap is-scrollable"><table><thead><tr><th>资金位置</th><th>账户来源</th><th>已确认可用</th><th>美元净值</th><th>源端预留</th><th>有效可用</th><th>控制 / 充值</th><th>更新时间</th></tr></thead><tbody>${rows}</tbody></table></div>`
    : `<div class="callout">${escapeHtml(emptyMessage)}</div>`;
}

async function renderCapitalCenter() {
  const [result, notiltStatus] = await Promise.all([
    api('/api/capital'),
    api('/api/notilt/status').catch(() => null),
  ]);
  const item = result.data;
  const canTreasury = roleNames().includes('TREASURY_ADMIN');
  const netWorth = item.net_worth || {currency:'USD', venues:{}, vault:'0', total:'0', complete:false, issues:[]};
  const balances = partitionCapitalRecords(item.balances);
  const proposals = partitionCapitalRecords(item.proposals);
  const transfers = partitionCapitalRecords(item.transfers);
  const liveInTransit = liveCapitalInTransit(transfers.live);
  const venueNetWorth = Object.entries(netWorth.venues).map(([venue, value]) => `<div class="stat"><small>${escapeHtml(venue)} 净值</small><b>${fmtNumber(value)} ${escapeHtml(netWorth.currency)}</b></div>`).join('');
  const chartBalances = balances.live.filter(balance => balance.usd_equity !== null).sort((a, b) => new Date(a.observed_at) - new Date(b.observed_at));
  const chartLegend = chartBalances.map((balance, index) => `<span><i style="--legend-index:${index}"></i>${escapeHtml(balance.location_type === 'VAULT' ? '资金库' : balance.venue)} <b>${fmtCompact(balance.usd_equity)} USD</b></span>`).join('');
  const liveBalanceRows = capitalBalanceRows(capitalSourceSlots(balances.live, notiltStatus));
  const renderProposalRows = records => records.map(proposal => {
    const actions = [];
    if (canTreasury && proposal.status === 'DRAFT' && proposal.proposer_id === session.user_id) actions.push(`<button class="secondary" data-cap-submit="${proposal.transfer_proposal_id}">提交</button>`);
    if (canTreasury && proposal.status === 'PENDING_REVIEW' && proposal.proposer_id !== session.user_id) actions.push(`<button class="secondary" data-cap-review="${proposal.transfer_proposal_id}" data-version="${proposal.version}">批准</button>`);
    if (canTreasury && proposal.status === 'APPROVED' && !proposal.authorization && proposal.proposer_id !== session.user_id) actions.push(`<button class="primary" data-cap-auto-transfer="${proposal.transfer_proposal_id}">开始自动划转</button>`);
    if (canTreasury && proposal.authorization?.active && proposal.proposer_id !== session.user_id) actions.push(`<button class="primary" data-cap-auto-transfer="${proposal.transfer_proposal_id}" data-authorization="${proposal.authorization.transfer_authorization_id}">继续自动划转</button>`);
    return `<tr><td>${shortId(proposal.transfer_proposal_id)}<br><span class="subtle">版本 ${proposal.version}</span></td><td>${escapeHtml(fmtCapitalDirection(proposal.direction))}<br><span class="subtle">${escapeHtml(fmtCapitalPurpose(proposal.purpose))}</span></td><td>${escapeHtml(proposal.source_id)} → ${escapeHtml(proposal.destination_id)}</td><td>${fmtNumber(proposal.amount)} ${escapeHtml(proposal.asset)}</td><td><b>${escapeHtml(fmtStatus(proposal.status))}</b></td><td><div class="toolbar">${actions.join('')}</div></td></tr>`;
  }).join('');
  const renderTransferRows = records => records.map(transfer => {
    const actions = [];
    if (canTreasury && ['DEPOSIT_PLAN_READY','RELEASE_REQUEST_PLAN_READY','RELEASE_EXECUTION_PLAN_READY','RELEASE_CANCELLATION_PLAN_READY'].includes(transfer.transport_state)) actions.push(`<button class="primary" data-notilt-receipt="${transfer.capital_transfer_id}">验证链上回执</button>`);
    if (canTreasury && transfer.transport_state === 'RELEASE_REQUEST_CONFIRMED' && transfer.status === 'IN_FLIGHT') actions.push(`<button class="primary" data-notilt-execute="${transfer.capital_transfer_id}">生成释放执行计划</button>`);
    if (canTreasury && transfer.transport_state === 'RELEASE_REQUEST_CONFIRMED' && ['IN_FLIGHT','MANUAL_REQUIRED'].includes(transfer.status)) actions.push(`<button class="danger" data-notilt-cancel="${transfer.capital_transfer_id}">生成释放取消计划</button>`);
    if (canTreasury) actions.push(`<button class="secondary" data-cap-reconcile="${transfer.capital_transfer_id}">对账</button>`);
    const plan = renderUnsignedPlanSummary(transfer);
    return `<tr><td>${shortId(transfer.capital_transfer_id)}<br><span class="subtle">${escapeHtml(fmtCapitalTransport(transfer.transport || 'MOCK'))}</span></td><td>${escapeHtml(fmtCapitalDirection(transfer.direction))}</td><td>${fmtNumber(transfer.gross_amount)} ${escapeHtml(transfer.asset)}</td><td><b>${escapeHtml(fmtStatus(transfer.status))}</b><br><span class="subtle">${escapeHtml(fmtStatus(transfer.transport_state || transfer.reconciliation_status))}</span></td><td>${escapeHtml(transfer.external_transfer_id || '未提交')}${plan}</td><td><div class="toolbar">${actions.join('')}</div></td></tr>`;
  }).join('');
  const liveProposalRows = renderProposalRows(proposals.live);
  const liveTransferRows = renderTransferRows(transfers.live);
  const capitalProposalForm = canTreasury ? `<form id="capital-proposal-form" class="form-panel compact-form"><input name="environment" type="hidden" value="LIVE"><h2>创建生产资金提案</h2><p class="safety-note">提交后由两名独立审核人确认。审核通过后，一键启动自动划转：系统复核额度、预留资金、生成链上计划并跟踪回执；独立钱包负责最终签名确认。</p><div class="field-grid"><label>方向<select name="direction"><option value="VAULT_TO_VENUE">资金库转入交易所</option><option value="VENUE_TO_VAULT">交易所转回资金库</option></select></label><label>交易账户<input name="account_id" value="acct-1" required></label><label>交易所<select name="venue"><option value="BINANCE">币安</option><option value="HYPERLIQUID">链上永续</option></select></label><label>资金库编号<input name="vault_id" placeholder="已批准的生产资金库" required></label><label>资产<input name="asset" value="USDC" required></label><label>网络<select name="network"><option value="ARBITRUM">Arbitrum</option><option value="ETHEREUM">Ethereum</option><option value="BSC">BNB Chain</option></select></label><label>收款账户<input name="destination_reference" placeholder="已批准的收款地址或账户" required></label><label>划转总额<input name="amount" type="number" step="any" min="0" required></label><label>最大费用<input name="max_fee" type="number" step="any" min="0" required></label><label>最小到账<input name="min_received" type="number" step="any" min="0" required></label><label>有效期（分钟）<input name="expires_in_minutes" type="number" min="5" value="120" required></label></div><label>理由<textarea name="reason" rows="2" required>补充生产账户运营资金</textarea></label><button class="primary">创建并进入审核</button></form>` : '';
  const automaticTransferPanel = `<section class="card"><div class="card-heading"><div><p class="eyebrow">自动资金划转</p><h2>审核后自动准备，钱包只做最终确认</h2></div><span class="status-pill status-APPROVED">生产流程</span></div><div class="access-principle-grid"><p><b>1. 自动复核</b><span>重新检查空仓、未决订单、对账、资金余额和链上额度。</span></p><p><b>2. 自动准备</b><span>预留资金并生成严格限定目标、资产和金额的链上交易计划。</span></p><p><b>3. 自动跟踪</b><span>验证链上回执并持续对账；结果未知时立即阻断后续动作。</span></p></div><p class="safety-note">交易控制台不保存私钥，也不替钱包签名或广播。钱包确认前会明确显示链、目标地址、资产、金额和资金库。</p></section>`;
  const proposalTable = (rows, emptyMessage) => rows ? `<div class="table-scroll-hint">左右滑动查看完整提案</div><div class="table-wrap is-scrollable"><table><thead><tr><th>提案</th><th>方向 / 用途</th><th>路径</th><th>金额</th><th>状态</th><th>动作</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="callout">${escapeHtml(emptyMessage)}</div>`;
  const transferTable = (rows, emptyMessage) => rows ? `<div class="table-scroll-hint">左右滑动查看完整划转记录</div><div class="table-wrap is-scrollable"><table><thead><tr><th>划转记录</th><th>方向</th><th>划转总额</th><th>状态 / 对账</th><th>外部引用</th><th>动作</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="callout">${escapeHtml(emptyMessage)}</div>`;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">生产资金 · 数据缺失即阻断</p><h1>资金中心</h1><p class="lede">这里只展示币安、链上永续和链上资金库的生产数据。未知或过期估值会把净值标记为不完整；资金动作必须经过独立审核、限时授权和链上预算检查。</p></div></header><div class="stats"><div class="stat"><small>总净值</small><b>${fmtNumber(netWorth.total)} ${escapeHtml(netWorth.currency)}</b></div><div class="stat"><small>链上资金库净值</small><b>${fmtNumber(netWorth.vault)} ${escapeHtml(netWorth.currency)}</b></div>${venueNetWorth}<div class="stat"><small>净值状态</small><b style="font-size:14px">${escapeHtml(fmtStatus(netWorth.complete ? 'CURRENT' : 'INCOMPLETE'))}</b></div><div class="stat"><small>资金划转控制</small><b style="font-size:14px">${escapeHtml(fmtStatus(item.real_transfer_gate || 'DISABLED'))}</b></div><div class="stat"><small>在途 / 占用</small><b>${fmtNumber(liveInTransit)}</b></div></div><section class="capital-chart-panel"><div class="chart-head"><div><p class="eyebrow">资金快照</p><h2>资金构成</h2><p class="subtle">按各资金位置最新有效的美元估值累计；这是当前快照，不是历史净值曲线。</p></div><b>${fmtNumber(netWorth.total)} <small>${escapeHtml(netWorth.currency)}</small></b></div>${chartBalances.length ? `<canvas id="capital-chart" height="210" aria-label="当前资金构成"></canvas><div class="chart-legend">${chartLegend}</div>` : '<div class="chart-empty">有效资金估值就绪后，将在这里显示构成。</div>'}</section>${netWorth.complete ? '' : `<div class="callout"><b>净值不完整：</b>${escapeHtml((netWorth.issues || []).map(formatCapitalIssue).join('；') || '尚无资金数据')}</div>`}<section><h2>资金位置</h2><p class="subtle">固定展示币安、链上永续与链上资金库；金额为 0 且状态为“数据缺失”表示尚未获得可信数据。</p>${capitalBalanceTable(liveBalanceRows, '尚无生产资金数据。')}</section>${automaticTransferPanel}${capitalProposalForm}<section><h2>资金提案</h2>${proposalTable(liveProposalRows, '尚无生产资金提案。')}</section><section><h2>资金划转</h2>${transferTable(liveTransferRows, '尚无生产资金划转。')}</section></section>`;
  drawCapitalChart(chartBalances);
  bindCapitalActions();
}

function drawCapitalChart(balances) {
  const canvas = document.querySelector('#capital-chart');
  if (!canvas || !balances.length) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = 210;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const context = canvas.getContext('2d'); context.scale(ratio, ratio);
  const styles = getComputedStyle(document.documentElement);
  const accent = styles.getPropertyValue('--accent').trim();
  const line = styles.getPropertyValue('--line').trim();
  const panel = styles.getPropertyValue('--panel').trim();
  const values = []; let cumulative = 0;
  balances.forEach(balance => { cumulative += Number(balance.usd_equity); values.push(cumulative); });
  if (values.length === 1) values.unshift(0);
  const max = Math.max(...values, 1);
  const left = 8, right = width - 8, top = 16, bottom = height - 24;
  context.strokeStyle = line; context.lineWidth = 1;
  for (let index = 0; index < 4; index += 1) {
    const y = top + ((bottom - top) * index / 3);
    context.beginPath(); context.moveTo(left, y); context.lineTo(right, y); context.stroke();
  }
  const points = values.map((value, index) => ({x:left + ((right - left) * index / Math.max(1, values.length - 1)), y:bottom - ((bottom - top) * value / max)}));
  context.beginPath(); context.moveTo(points[0].x, bottom); points.forEach(point => context.lineTo(point.x, point.y)); context.lineTo(points.at(-1).x, bottom); context.closePath();
  context.fillStyle = `${accent}1f`; context.fill();
  context.beginPath(); points.forEach((point, index) => index ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y));
  context.strokeStyle = accent; context.lineWidth = 3; context.lineJoin = 'round'; context.lineCap = 'round'; context.stroke();
  points.slice(1).forEach(point => { context.beginPath(); context.arc(point.x, point.y, 4, 0, Math.PI * 2); context.fillStyle = panel; context.fill(); context.strokeStyle = accent; context.lineWidth = 2; context.stroke(); });
}

function bindCapitalActions() {
  document.querySelectorAll('[data-copy-plan]').forEach(button => button.addEventListener('click', async () => {
    const transferId = button.dataset.copyPlan;
    const result = await api('/api/capital');
    const transfer = result.data.transfers.find(item => item.capital_transfer_id === transferId);
    if (!transfer?.planned_transactions?.length) return showToast('当前没有可交给钱包的交易计划', 'error');
    await navigator.clipboard.writeText(JSON.stringify({
      transfer_id:transferId,
      network:transfer.network,
      asset:transfer.asset,
      gross_amount:transfer.gross_amount,
      transactions:transfer.planned_transactions,
    }, null, 2));
    showToast('链上执行计划已复制，请在独立钱包核对并确认');
  }));
  document.querySelector('#capital-proposal-form')?.addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); data.expires_in_minutes = Number(data.expires_in_minutes); data.idempotency_key = crypto.randomUUID(); try { await api('/api/capital/proposals', {method:'POST', body:JSON.stringify(data)}); showToast('资金提案草稿已创建'); await route(); } catch (error) { showApiError(error); } });
  document.querySelectorAll('[data-cap-submit]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/proposals/${button.dataset.capSubmit}/submit`, {})));
  document.querySelectorAll('[data-cap-review]').forEach(button => button.addEventListener('click', async () => { try { const proposalId = button.dataset.capReview; const version = Number(button.dataset.version); const grant = await api('/api/auth/mock/step-up', {method:'POST', body:JSON.stringify({action:'capital.approve', object_id:proposalId, object_version:version})}); await api(`/api/capital/proposals/${proposalId}/reviews`, {method:'POST', body:JSON.stringify({decision:'APPROVE', reason:'independent Treasury review', expected_version:version, action_grant:grant.action_grant})}); showToast('资金审核已记录'); await route(); } catch (error) { showApiError(error); } }));
  document.querySelectorAll('[data-cap-auto-transfer]').forEach(button => button.addEventListener('click', () => startAutomaticCapitalTransfer(button)));
  document.querySelectorAll('[data-notilt-execute]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/transfers/${button.dataset.notiltExecute}/notilt-release-execution-plan`, {})));
  document.querySelectorAll('[data-notilt-cancel]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/transfers/${button.dataset.notiltCancel}/notilt-release-cancellation-plan`, {})));
  document.querySelectorAll('[data-notilt-receipt]').forEach(button => button.addEventListener('click', () => { const transactionHash = prompt('输入独立钱包已广播交易的 tx hash'); if (transactionHash) capitalAction(`/api/capital/transfers/${button.dataset.notiltReceipt}/notilt-receipt`, {transaction_hash:transactionHash.trim()}); }));
  document.querySelectorAll('[data-cap-reconcile]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/transfers/${button.dataset.capReconcile}/reconcile`, {})));
}

async function startAutomaticCapitalTransfer(button) {
  await withPending(button, '自动准备中…', async () => {
    try {
      let authorizationId = button.dataset.authorization;
      if (!authorizationId) {
        const authorization = await api(`/api/capital/proposals/${button.dataset.capAutoTransfer}/authorizations`, {
          method:'POST',
          body:JSON.stringify({idempotency_key:crypto.randomUUID(), expires_in_minutes:30}),
        });
        authorizationId = authorization.transfer_authorization_id;
      }
      await api(`/api/capital/authorizations/${authorizationId}/transfers/notilt-plan`, {
        method:'POST',
        body:JSON.stringify({idempotency_key:crypto.randomUUID()}),
      });
      showToast('自动划转已完成额度复核、资金预留和链上计划生成；请在独立钱包核对并确认');
      await route();
    } catch (error) { showApiError(error); }
  });
}

async function capitalAction(path, body) { try { await api(path, {method:'POST', body:JSON.stringify(body)}); showToast('资金状态已更新'); await route(); } catch (error) { showApiError(error); } }

function signedResult(value) {
  const number = Number(value || 0);
  return `${number > 0 ? '+' : ''}${fmtNumber(value || 0)}`;
}

function resultValueClass(value) {
  const number = Number(value || 0);
  return number > 0 ? 'result-positive' : number < 0 ? 'result-negative' : '';
}

function actualResultsVerdict(campaigns, exceptions) {
  const activeCount = campaigns.filter(item => item.status !== 'CLOSED').length;
  const closedCount = campaigns.length - activeCount;
  const affectedCount = new Set(exceptions.map(item => item.campaign_id)).size;
  if (affectedCount) return {
    tone:'danger',
    title:`${affectedCount} 个交易任务存在数据或对账问题`,
    copy:'这些数字不能直接当作最终结果。请先处理结果未知、数据过期、保护不足或对账差异。',
    href:'/exceptions',
    action:'先处理异常',
  };
  if (activeCount) return {
    tone:'attention',
    title:`${activeCount} 个交易任务仍在运行`,
    copy:'当前盈亏会随仓位和成交事实继续变化；只有仓位归零、退出终结且对账一致后，结果才会固定。',
    href:'/campaigns',
    action:'查看进行中交易任务',
  };
  if (closedCount) return {
    tone:'success',
    title:`已结算 ${closedCount} 个交易任务，当前没有待处理异常`,
    copy:'已关闭的交易任务已经满足退出终结、仓位归零与对账一致；下方保留盈亏、成本和完整审计记录。',
    href:'/campaigns',
    action:'查看交易任务记录',
  };
  return {
    tone:'clear',
    title:'该环境尚未形成可结算结果',
    copy:'没有已保存的交易任务数据，因此这里不会推测盈亏。请先从机会和提案流程形成可审计的交易记录。',
    href:'/opportunities',
    action:'查看市场机会',
  };
}

async function renderActualResults() {
  const environment = 'LIVE';
  const [resultResponse, auditResponse, exceptionResponse] = await Promise.all([
    api(`/api/results?environment=${encodeURIComponent(environment)}`),
    api(`/api/audit?environment=${encodeURIComponent(environment)}&limit=200`),
    api('/api/campaign-exceptions'),
  ]);
  const results = resultResponse.data;
  const resultCampaignIds = new Set(results.campaigns.map(item => item.campaign_id));
  const exceptions = exceptionResponse.data.filter(item => resultCampaignIds.has(item.campaign_id));
  const verdict = actualResultsVerdict(results.campaigns, exceptions);
  const closedCount = results.campaigns.filter(item => item.status === 'CLOSED').length;
  const affectedCount = new Set(exceptions.map(item => item.campaign_id)).size;
  const outcomeCards = Object.entries(results.totals_by_currency).map(([currency, item]) => {
    const curve = results.curves_by_currency[currency];
    return `<article class="result-outcome-card"><small>${escapeHtml(currency)} · 最终 / 当前</small><strong class="${resultValueClass(item.final_pnl)}">${signedResult(item.final_pnl)}</strong><div><span>已实现 <b>${signedResult(item.realized_pnl)}</b></span><span>未实现 <b>${signedResult(item.unrealized_pnl)}</b></span></div><p>手续费 ${fmtNumber(item.fees)} · 资金费 ${fmtNumber(item.funding)} · 滑点 ${fmtNumber(item.slippage)}</p><p>最大绝对回撤 ${fmtNumber(curve?.maximum_drawdown || 0)} ${escapeHtml(currency)}</p></article>`;
  }).join('');
  const totals = Object.entries(results.totals_by_currency).map(([currency, item]) => `<tr><td><b>${escapeHtml(currency)}</b></td><td>${fmtNumber(item.realized_pnl)}</td><td>${fmtNumber(item.unrealized_pnl)}</td><td>${fmtNumber(item.final_pnl)}</td><td>${fmtNumber(item.fees)}</td><td>${fmtNumber(item.funding)}</td><td>${fmtNumber(item.slippage)}</td></tr>`).join('');
  const campaigns = results.campaigns.map(item => `<tr><td><a class="table-primary-link" href="/campaigns/${item.campaign_id}" data-link><b>${escapeHtml(item.symbol || '交易任务')}</b><span>${shortId(item.campaign_id)} · 打开明细 →</span></a><span class="subtle">${escapeHtml(item.actuality === 'FINAL' ? '最终结果' : '当前结果')}</span></td><td>${escapeHtml(item.source === 'SYSTEM' ? '系统机会' : item.source === 'MANUAL' ? '人工提案' : '未知来源')} · ${escapeHtml(item.source_type === 'PERPTAPE_BREAKOUT' ? 'Perptape 突破' : item.source_type === 'MANUAL' ? '人工输入' : '未知类型')}<br><span class="subtle">${escapeHtml(item.source_candidate_id || '人工创建')} · ${escapeHtml(item.source_version || '无版本')}</span></td><td>${escapeHtml(item.venue)} · ${escapeHtml(item.symbol || item.instrument_id)}<br><span class="subtle">${escapeHtml(item.account_id)} · ${escapeHtml(fmtDirection(item.direction))} · ${escapeHtml(fmtRisk(item.risk_tier))}</span></td><td><b>${escapeHtml(fmtStatus(item.status))}</b><br><span class="subtle">${item.fill_count} 笔成交</span></td><td><b class="${resultValueClass(item.final_pnl)}">${signedResult(item.final_pnl)}</b> ${escapeHtml(item.currency)}</td><td>${fmtNumber(item.fees)} / ${fmtNumber(item.funding)} / ${fmtNumber(item.slippage)}</td><td>${fmtDate(item.updated_at)}</td></tr>`).join('');
  const curves = Object.entries(results.curves_by_currency).flatMap(([currency, curve]) => curve.points.map(point => `<tr><td>${escapeHtml(currency)}</td><td><a class="table-primary-link compact" href="/campaigns/${point.campaign_id}" data-link>${shortId(point.campaign_id)} →</a></td><td>${signedResult(point.cumulative_pnl)}</td><td>${signedResult(point.running_peak)}</td><td>${fmtNumber(point.drawdown)}</td><td>${fmtDate(point.at)}</td></tr>`)).join('');
  const audits = auditResponse.data.map(item => {
    const href = item.object_type === 'Campaign' ? `/campaigns/${item.object_id}` : item.object_type === 'Proposal' ? `/proposals/${item.object_id}` : null;
    const object = `<b>${escapeHtml(fmtAuditEvent(item.event_type))}</b><br><span class="subtle">${escapeHtml(fmtAuditObject(item.object_type))} · ${shortId(item.object_id)}${href ? ' · 打开 →' : ''}</span>`;
    return `<tr><td>${fmtDate(item.created_at)}</td><td>${escapeHtml(item.actor)}</td><td>${href ? `<a class="table-primary-link compact" href="${href}" data-link>${object}</a>` : object}</td><td>${escapeHtml(fmtAuditReason(item.reason))}</td><td>${shortId(item.correlation_id)}<br><span class="subtle">版本 ${item.object_version}</span></td></tr>`;
  }).join('');
  main.innerHTML = `<section class="page results-page"><header class="page-head"><div><p class="eyebrow">生产交易记录</p><h1>交易结果</h1><p class="lede">先看盈亏和当前结论，再查看每个交易任务的成交、成本、对账与操作记录。这里只显示生产数据。</p></div></header>
    <article class="results-verdict tone-${verdict.tone}"><div><p class="eyebrow">当前结论</p><h2>${escapeHtml(verdict.title)}</h2><p>${escapeHtml(verdict.copy)}</p></div><a class="${verdict.tone === 'danger' ? 'danger' : 'secondary'}" href="${verdict.href}" data-link>${escapeHtml(verdict.action)}</a></article>
    <section aria-labelledby="results-outcome-heading"><div class="section-heading"><div><p class="eyebrow">结果</p><h2 id="results-outcome-heading">按结算币种看结果</h2></div><p class="subtle">进行中显示当前值，交易任务关闭后才显示最终值</p></div>${outcomeCards ? `<div class="result-outcome-grid">${outcomeCards}</div>` : '<div class="empty-state compact-empty"><div><h2>暂无结果</h2><p>系统没有收到可追溯到交易任务的数据，因此不会展示推测数字。</p></div></div>'}</section>
    <div class="stats results-stats"><div class="stat"><small>交易任务</small><b>${results.campaigns.length}</b></div><div class="stat"><small>已结束</small><b>${closedCount}</b></div><div class="stat"><small>待处理交易任务</small><b class="${affectedCount ? 'danger-text' : ''}">${affectedCount}</b></div><div class="stat"><small>审计事件</small><b>${auditResponse.data.length}</b></div></div>
    <section><h2>盈亏与成本明细</h2>${totals ? `<div class="table-wrap"><table><thead><tr><th>币种</th><th>已实现</th><th>未实现</th><th>最终 / 当前</th><th>手续费</th><th>资金费</th><th>滑点</th></tr></thead><tbody>${totals}</tbody></table></div>` : '<div class="callout">该环境尚无可追溯的交易任务。</div>'}</section>
    <section><h2>交易任务结果记录</h2>${campaigns ? `<div class="table-wrap"><table><thead><tr><th>交易任务 / 结果类型</th><th>来源</th><th>账户范围</th><th>状态</th><th>盈亏</th><th>费用 / 资金费 / 滑点</th><th>更新时间</th></tr></thead><tbody>${campaigns}</tbody></table></div>` : '<div class="callout">当前环境没有交易任务。</div>'}</section>
    <section><h2>已关闭交易任务的累计盈亏与绝对回撤</h2><p class="safety-note">没有可靠期初资本时只展示结算币种绝对值，不伪造百分比收益率或回撤。</p>${curves ? `<div class="table-wrap"><table><thead><tr><th>币种</th><th>交易任务</th><th>累计盈亏</th><th>历史峰值</th><th>回撤</th><th>时间</th></tr></thead><tbody>${curves}</tbody></table></div>` : '<div class="callout">没有已关闭交易任务的曲线数据。</div>'}</section>
    <section><h2>权限与操作记录</h2><p class="subtle">可以打开提案或交易任务继续追查；关联编号用于定位同一条操作记录。</p>${audits ? `<div class="table-wrap"><table><thead><tr><th>时间</th><th>操作者</th><th>事件 / 对象</th><th>原因</th><th>关联编号 / 版本</th></tr></thead><tbody>${audits}</tbody></table></div>` : '<div class="callout">当前身份下没有可见操作记录。</div>'}</section>
  </section>`;
}

async function renderExceptions() {
  const [result, campaignResponse] = await Promise.all([api('/api/campaign-exceptions'), api('/api/campaigns')]);
  const liveCampaignIds = new Set(campaignResponse.data.filter(item => item.environment === 'LIVE').map(item => item.campaign_id));
  const items = result.data.filter(item => liveCampaignIds.has(item.campaign_id));
  const groups = [...items.reduce((result, item) => {
    if (!result.has(item.campaign_id)) result.set(item.campaign_id, []);
    result.get(item.campaign_id).push(item);
    return result;
  }, new Map()).entries()].map(([campaignId, groupItems]) => ({campaignId, items:groupItems}));
  groups.sort((left, right) => Math.min(...left.items.map(item => explainException(item.code).priority)) - Math.min(...right.items.map(item => explainException(item.code).priority)));
  const unknownCount = items.filter(item => item.code.includes('UNKNOWN')).length;
  const staleCount = items.filter(item => item.code.endsWith('_STALE')).length;
  const cards = groups.map(group => {
    const issues = [...group.items.reduce((result, item) => {
      if (!result.has(item.code)) result.set(item.code, []);
      result.get(item.code).push(item);
      return result;
    }, new Map()).entries()].map(([code, matching]) => ({code, matching, guidance:explainException(code)})).sort((left, right) => left.guidance.priority - right.guidance.priority || left.code.localeCompare(right.code));
    return `<article class="card exception-card"><div class="exception-card-head"><div><p class="eyebrow">恢复队列</p><h2>交易任务 ${shortId(group.campaignId)}</h2></div><span class="status-pill status-DENY">${group.items.length} 项阻断</span></div><ol class="exception-steps">${issues.map((issue, index) => `<li><span class="exception-order">${index + 1}</span><div><h3>${escapeHtml(issue.guidance.title)}${issue.matching.length > 1 ? ` × ${issue.matching.length}` : ''}</h3><p>${escapeHtml(issue.guidance.copy)}</p><strong>下一步：${escapeHtml(issue.guidance.next)}</strong></div></li>`).join('')}</ol><a class="primary" href="/campaigns/${group.campaignId}" data-link>打开交易任务并按顺序处理</a></article>`;
  }).join('');
  main.innerHTML = `<section class="page exceptions-page"><header class="page-head"><div><p class="eyebrow">数据缺失即阻断</p><h1>异常与恢复</h1><p class="lede">这里只显示运行中交易任务的阻断问题，并按安全顺序说明发生了什么、是否影响交易，以及下一步该做什么。</p></div><button class="secondary" data-refresh>刷新当前数据</button></header>
    <div class="stats exception-stats"><div class="stat"><small>受影响交易任务</small><b class="${groups.length ? 'danger-text' : ''}">${groups.length}</b></div><div class="stat"><small>阻断问题</small><b>${items.length}</b></div><div class="stat"><small>结果未知</small><b class="${unknownCount ? 'danger-text' : ''}">${unknownCount}</b></div><div class="stat"><small>数据过期</small><b class="${staleCount ? 'warning-text' : ''}">${staleCount}</b><span>截止 ${fmtDate(result.as_of)}</span></div></div>
    ${items.length ? `<div class="exception-grid">${cards}</div>` : '<section class="empty-state"><div><h2>当前运行中的交易任务没有阻断异常</h2><p>没有发现结果未知、数据过期、保护不足或对账差异。已关闭的历史记录不会因为数据变旧而重新报警。</p><div class="toolbar empty-actions"><a class="secondary" href="/" data-link>返回今日</a><a class="primary" href="/campaigns" data-link>查看交易任务</a></div></div></section>'}</section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
}

function latestVenueObservation(facts) {
  const values = [facts.equity?.observed_at, facts.reconciliation?.completed_at]
    .concat(facts.positions.map(item => item.observed_at))
    .concat(facts.orders.map(item => item.observed_at))
    .concat(facts.fills.map(item => item.executed_at))
    .concat(facts.funding.map(item => item.paid_at))
    .filter(Boolean)
    .map(value => new Date(value).getTime())
    .filter(Number.isFinite);
  return values.length ? new Date(Math.max(...values)).toISOString() : null;
}

async function renderVenueFacts() {
  const params = new URLSearchParams(location.search);
  const selectedVenue = (params.get('venue') || (location.pathname.includes('hyperliquid') ? 'HYPERLIQUID' : 'BINANCE')).toUpperCase();
  const venue = selectedVenue === 'HYPERLIQUID' ? 'HYPERLIQUID' : 'BINANCE';
  const role = session.roles.find(item => item.venue_scope === venue && item.account_scope);
  const accountId = params.get('account_id') || role?.account_scope || 'acct-1';
  const endpoint = venue.toLowerCase();
  const [status, response] = await Promise.all([
    api(`/api/venues/${endpoint}/status`),
    api(`/api/venues/${endpoint}/facts?account_id=${encodeURIComponent(accountId)}`),
  ]);
  const facts = response.data;
  const connected = status.enabled && status.configured;
  const canSync = connected && hasCapability('venue.sync');
  const lastSync = latestVenueObservation(facts);
  const venueDetail = venue === 'BINANCE'
    ? (accountModeLabels[status.account_mode] || '账户模式未知')
    : `核心市场${status.dex ? ` · ${status.dex}` : ''}`;
  const symbolDefault = venue === 'BINANCE' ? 'BTCUSDT' : 'BTC';
  const symbolPattern = venue === 'BINANCE' ? '[A-Z0-9_]+' : '[A-Z0-9]+';
  main.innerHTML = `<section class="page venue-facts-page"><header class="page-head"><div><p class="eyebrow">生产账户 · 数据读取</p><h1>交易账户</h1><p class="lede">在这里查看币安和链上永续账户的连接、余额、仓位、委托、成交、资金费与对账。日常交易仍从交易任务、系统状态和异常页面进入。</p></div><button class="secondary" data-refresh>刷新账户数据</button></header>
    <nav class="venue-switch" aria-label="选择交易所"><a class="${venue === 'BINANCE' ? 'active' : ''}" href="/venues?venue=BINANCE&account_id=${encodeURIComponent(accountId)}" data-link>Binance</a><a class="${venue === 'HYPERLIQUID' ? 'active' : ''}" href="/venues?venue=HYPERLIQUID&account_id=${encodeURIComponent(accountId)}" data-link>Hyperliquid</a></nav>
    <div class="stats venue-status-stats"><div class="stat"><small>连接状态</small><b class="${connected ? 'direction-long' : 'warning-text'}">${connected ? '连接正常' : status.enabled ? '尚未配置' : '已关闭'}</b><span>${escapeHtml(venueModeLabels[status.mode] || '只读')}</span></div><div class="stat"><small>运行模式</small><b>生产账户</b><span>${escapeHtml(venueDetail)}</span></div><div class="stat"><small>交易账户</small><b>${escapeHtml(accountId)}</b><span>${escapeHtml(venue)}</span></div><div class="stat"><small>最后同步</small><b>${fmtDate(lastSync)}</b><span>${lastSync ? '账户数据已保存' : '尚无数据'}</span></div></div>
    <article class="card venue-scope-card"><div><h2>账户与同步</h2><p class="subtle">切换账户时只读取当前身份获准查看的范围。点击同步后，系统会从交易所获取数据、保存并立即运行对账。</p></div><form id="venue-account-form" class="inline-form"><input name="venue" type="hidden" value="${venue}"><label>交易账户<input name="account_id" value="${escapeHtml(accountId)}" required></label><button class="secondary">查看账户</button></form>${canSync ? `<form id="venue-sync-form" class="inline-form"><input name="account_id" type="hidden" value="${escapeHtml(accountId)}"><label>${escapeHtml(venue)} 标的<input name="symbol" value="${symbolDefault}" pattern="${symbolPattern}" required></label><button class="primary">同步并对账</button><span class="form-error" role="alert"></span></form>` : `<p class="safety-note">${connected ? '当前身份只有读取权限，不能触发交易所同步。' : '生产读取连接尚未配置或已关闭。页面只展示已经保存的数据，不会用其他数据填充。'}</p>`}</article>
    ${venueFactSections(facts)}
  </section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  document.querySelector('#venue-account-form')?.addEventListener('submit', (event) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    navigate(`/venues?venue=${encodeURIComponent(data.get('venue'))}&account_id=${encodeURIComponent(data.get('account_id'))}`);
  });
  document.querySelector('#venue-sync-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button');
    await withPending(button, '同步中…', async () => {
      try {
        const result = await api(`/api/venues/${endpoint}/sync`, {method:'POST', body:JSON.stringify(Object.fromEntries(new FormData(form)))});
        showToast(`${venue} 同步完成；对账 ${fmtStatus(result.reconciliation.status)}`);
        await route();
      } catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
}

function venueFactSections(facts) {
  const positions = facts.positions.map(item => `<tr><td>${escapeHtml(item.symbol)}</td><td>${fmtNumber(item.quantity)} @ ${fmtNumber(item.average_entry_price)}</td><td>${fmtNumber(item.mark_price)}</td><td>${escapeHtml(factStatusLabel(item.fact_status))}</td><td>${item.protection ? `${escapeHtml(fmtStatus(item.protection.status))} · ${item.protection.fully_covered ? '足额' : '不足'}` : '无保护数据'}</td><td>${fmtDate(item.observed_at)}</td></tr>`).join('');
  const orders = facts.orders.map(item => `<tr><td>${escapeHtml(item.venue_order_id)}</td><td>${escapeHtml(item.symbol)}</td><td>${escapeHtml(fmtStatus(item.status))}</td><td>${fmtNumber(item.filled_quantity)} / ${fmtNumber(item.ordered_quantity)}</td><td>${item.intent_id ? shortId(item.intent_id) : '外部未关联'}</td><td>${fmtDate(item.observed_at)}</td></tr>`).join('');
  const fills = facts.fills.map(item => `<tr><td>${escapeHtml(item.venue_fill_id)}</td><td>${escapeHtml(item.symbol)}</td><td>${escapeHtml(fmtSide(item.side))} ${fmtNumber(item.quantity)}</td><td>${fmtNumber(item.price)}</td><td>${fmtNumber(item.fee)} ${escapeHtml(item.fee_currency)}</td><td>${fmtDate(item.executed_at)}</td></tr>`).join('');
  const funding = facts.funding.map(item => `<tr><td>${escapeHtml(item.venue_payment_id)}</td><td>${escapeHtml(item.symbol)}</td><td>${fmtNumber(item.amount)} ${escapeHtml(item.currency)}</td><td>${fmtDate(item.paid_at)}</td></tr>`).join('');
  const reconciliation = facts.reconciliation;
  return `<div class="stats"><div class="stat"><small>权益</small><b>${fmtNumber(facts.equity?.equity)} ${escapeHtml(facts.equity?.currency || '')}</b></div><div class="stat"><small>可用余额</small><b>${fmtNumber(facts.equity?.available_balance)}</b></div><div class="stat"><small>权益状态</small><b style="font-size:14px">${escapeHtml(factStatusLabel(facts.equity?.fact_status))}</b></div><div class="stat"><small>最近对账</small><b style="font-size:14px" class="${reconciliation?.status === 'MATCH' ? 'direction-long' : reconciliation ? 'warning-text' : ''}">${escapeHtml(reconciliation ? fmtStatus(reconciliation.status) : '未运行')}</b><span>${fmtDate(reconciliation?.completed_at)}</span></div></div>
    ${reconciliation?.differences?.length ? `<article class="danger-note"><b>对账差异</b><ul>${reconciliation.differences.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul></article>` : ''}
    ${factTable('仓位与风险保护', '<th>标的</th><th>数量 / 入场</th><th>标记价</th><th>数据状态</th><th>保护</th><th>更新时间</th>', positions)}
    ${factTable('当前委托', '<th>交易所订单</th><th>标的</th><th>状态</th><th>成交 / 委托</th><th>关联操作</th><th>更新时间</th>', orders)}
    ${factTable('最近成交', '<th>成交编号</th><th>标的</th><th>方向 / 数量</th><th>价格</th><th>手续费</th><th>成交时间</th>', fills)}
    ${factTable('资金费', '<th>支付编号</th><th>标的</th><th>金额</th><th>支付时间</th>', funding)}`;
}

function factTable(title, headers, rows) {
  return `<section><h2>${escapeHtml(title)}</h2>${rows ? `<div class="table-wrap"><table><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="callout">当前没有已保存的数据。</div>'}</section>`;
}

function accessRoleOptions(selectedRoles, prefix, disabled = false) {
  const selected = new Set(selectedRoles);
  return accessRoleCatalog.map(item => `<label class="permission-option" for="${prefix}-${item.role}"><input id="${prefix}-${item.role}" name="roles" type="checkbox" value="${item.role}" ${selected.has(item.role) ? 'checked' : ''} ${disabled ? 'disabled' : ''}><span><b>${escapeHtml(item.label)}</b><small>${escapeHtml(item.copy)}</small></span></label>`).join('');
}

function memberScope(member) {
  const accountScopes = [...new Set(member.roles.map(item => item.account_scope).filter(Boolean))];
  const venueScopes = [...new Set(member.roles.map(item => item.venue_scope).filter(Boolean))];
  return {
    account: accountScopes.length === 1 ? accountScopes[0] : '',
    venue: venueScopes.length === 1 ? venueScopes[0] : '',
    mixed: accountScopes.length > 1 || venueScopes.length > 1,
  };
}

function venueScopeOptions(selected = '') {
  return `<option value="" ${selected ? '' : 'selected'}>全部交易所</option><option value="BINANCE" ${selected === 'BINANCE' ? 'selected' : ''}>币安</option><option value="HYPERLIQUID" ${selected === 'HYPERLIQUID' ? 'selected' : ''}>链上永续</option>`;
}

async function renderAccessManagement() {
  const result = await api('/api/admin/users');
  const members = result.data;
  const cards = members.map(member => {
    const roles = member.roles.map(item => item.role);
    const scope = memberScope(member);
    return `<form class="member-access-card ${member.active ? '' : 'is-inactive'}" data-user-access="${member.user_id}"><div class="member-access-head"><div><h2>${escapeHtml(member.username)}</h2><p>${member.identity_bound ? '已绑定生产身份源' : '等待生产身份源绑定'} · 创建于 ${fmtDate(member.created_at)}</p></div><span class="status-pill ${member.active ? 'status-APPROVED' : ''}">${member.active ? '已启用' : '已停用'}</span></div>
      ${member.is_current_user ? '<p class="safety-note">这是当前账号。为避免误锁死，必须由另一名系统管理员修改。</p>' : ''}
      <div class="permission-grid">${accessRoleOptions(roles, `member-${member.user_id}`, member.is_current_user)}</div>
      <div class="scope-grid"><label>账户范围<input name="account_scope" value="${escapeHtml(scope.account)}" placeholder="留空 = 全部账户" ${member.is_current_user ? 'disabled' : ''}></label><label>交易所范围<select name="venue_scope" ${member.is_current_user ? 'disabled' : ''}>${venueScopeOptions(scope.venue)}</select></label><label class="active-toggle"><input name="active" type="checkbox" ${member.active ? 'checked' : ''} ${member.is_current_user ? 'disabled' : ''}>允许登录和使用已分配权限</label></div>
      ${scope.mixed ? '<p class="danger-note">该成员当前有多个不同的数据范围。保存后，所选岗位会统一使用上面的账户和交易所范围，请先确认。</p>' : ''}
      <div class="form-error" role="alert"></div>${member.is_current_user ? '' : '<div class="form-actions"><button class="secondary">保存权限</button></div>'}</form>`;
  }).join('');
  main.innerHTML = `<section class="page access-page"><header class="page-head"><div><p class="eyebrow">系统管理 · 权限配置</p><h1>成员权限</h1><p class="lede">按岗位勾选权限，不需要逐个页面配置。一个人可以组合多个岗位，还可以按账户和交易所限制每个用户能够查看和操作的数据。</p></div><span class="status-pill">${members.filter(item => item.active).length} 名启用成员</span></header>
    <article class="card access-principles"><h2>权限分离原则</h2><div class="access-principle-grid"><p><b>审核与发起分开</b><span>审核人不能审核自己的提案；提案发起人不会自动获得执行权限。</span></p><p><b>交易与资金分开</b><span>交易运维人员看不到资金中心；系统管理员也不会自动获得资金权限。</span></p><p><b>身份与权限分开</b><span>生产环境仍由统一身份登录和通行密钥认证；这里仅管理内部授权，不创建密码。</span></p></div></article>
    <details class="card create-member-panel"><summary><span><b>新增内部成员</b><small>先创建授权记录，再由生产身份服务完成身份绑定</small></span><strong>展开</strong></summary><form id="create-member-form" class="toolbox-content"><div class="field-grid"><label>内部用户名<input name="username" pattern="[A-Za-z0-9._-]+" placeholder="例如 reviewer-li" required></label><label>账户范围<input name="account_scope" placeholder="留空 = 全部账户"></label><label>交易所范围<select name="venue_scope">${venueScopeOptions()}</select></label></div><div class="preset-row"><span>常用模板</span><button type="button" class="text-button" data-role-preset="REVIEWER">只审核</button><button type="button" class="text-button" data-role-preset="PROPOSER">只发起提案</button><button type="button" class="text-button" data-role-preset="OPERATOR">交易运维</button></div><div class="permission-grid">${accessRoleOptions([], 'create')}</div><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">创建成员</button></div></form></details>
    <div class="section-heading"><div><p class="eyebrow">当前用户</p><h2>现有成员</h2></div><span class="subtle">截止 ${fmtDate(result.as_of)}</span></div><div class="member-access-list">${cards}</div>
  </section>`;
  document.querySelectorAll('[data-role-preset]').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('#create-member-form input[name="roles"]').forEach(input => { input.checked = input.value === button.dataset.rolePreset; });
  }));
  document.querySelector('#create-member-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const payload = {username:data.get('username'), roles:data.getAll('roles'), account_scope:data.get('account_scope') || null, venue_scope:(data.get('venue_scope') || '').toUpperCase() || null};
    if (!payload.roles.length) { form.querySelector('.form-error').textContent = '至少选择一个岗位。'; return; }
    await withPending(event.submitter, '创建中…', async () => {
      try { await api('/api/admin/users', {method:'POST', body:JSON.stringify(payload)}); showToast(`${payload.username} 已创建，等待身份源绑定`); await route(); }
      catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
  document.querySelectorAll('[data-user-access]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const data = new FormData(form);
    const payload = {roles:data.getAll('roles'), active:data.get('active') === 'on', account_scope:data.get('account_scope') || null, venue_scope:(data.get('venue_scope') || '').toUpperCase() || null};
    if (!payload.roles.length) { form.querySelector('.form-error').textContent = '启用或停用成员时都要保留至少一个岗位记录。'; return; }
    await withPending(event.submitter, '保存中…', async () => {
      try { await api(`/api/admin/users/${form.dataset.userAccess}/access`, {method:'PUT', body:JSON.stringify(payload)}); showToast('成员权限已保存并写入审计'); await route(); }
      catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  }));
}

async function renderCampaignDetail(id) {
  const item = await api(`/api/campaigns/${id}`);
  if (item.environment !== 'LIVE') {
    main.innerHTML = '<section class="page"><section class="empty-state"><div><h1>该交易任务不属于生产控制台</h1><p>这里仅展示生产交易任务。非生产记录不会出现在当前网站。</p><a class="primary" href="/campaigns" data-link>返回交易任务</a></div></section></section>';
    return;
  }
  const canOperate = roleNames().includes('OPERATOR') || roleNames().includes('SYSTEM_ADMIN');
  const canRecordSyntheticFacts = canOperate && authStatus?.environment !== 'production' && item.environment !== 'LIVE';
  const active = item.intents.find(intent => ['READY','SENT','PARTIALLY_FILLED','UNKNOWN'].includes(intent.status));
  const positionKnown = item.position?.fact_status === 'KNOWN';
  const latestFilledIntent = item.intents.filter(intent => intent.status === 'FILLED').at(-1);
  const positionCurrent = positionKnown && (!latestFilledIntent || new Date(item.position.observed_at) >= new Date(latestFilledIntent.updated_at));
  const positionQuantity = positionCurrent ? Math.abs(Number(item.position.quantity)) : 0;
  const hasPosition = positionCurrent && positionQuantity > 0;
  const flatKnown = positionCurrent && positionQuantity === 0;
  const protectionReady = hasPosition && item.protection?.status === 'ACTIVE' && item.protection?.fully_covered && new Date(item.protection.observed_at) >= new Date(item.position.observed_at);
  const latestIntent = item.intents.at(-1);
  const reconciliationMatched = item.reconciliation?.status === 'MATCH' && (!item.position || new Date(item.reconciliation.completed_at) >= new Date(item.position.observed_at)) && (!latestIntent || new Date(item.reconciliation.completed_at) >= new Date(latestIntent.updated_at));
  const latestExit = item.intents.filter(intent => intent.kind === 'EXIT').at(-1);
  const exitTerminal = Boolean(latestExit && ['FILLED','CANCELLED','REJECTED'].includes(latestExit.status));
  const riskClosable = item.reservations.every(reservation => !['UNKNOWN','RESERVED'].includes(reservation.status));
  const canCreatePositionAction = canOperate && hasPosition && !active;
  const canAddNow = canCreatePositionAction && protectionReady && reconciliationMatched && item.management?.auto_add_gate === 'ENABLED';
  let addCandidates = []; let addCandidateError = null;
  if (item.management?.allow_auto_add && Number(item.management.remaining_adds) > 0 && canAddNow) {
    try { addCandidates = (await api(`/api/campaigns/${id}/add-candidates`)).data; }
    catch (error) {
      if (error.handled) return;
      addCandidateError = friendlyApiError(error);
    }
  }
  const nextStep = campaignNextStep(item, active, canRecordSyntheticFacts, {positionCurrent, hasPosition, flatKnown, protectionReady, reconciliationMatched, exitTerminal, riskClosable});
  const positionTruth = !item.position ? '未同步' : !positionCurrent ? '需要重新同步' : `${fmtStatus(item.position.fact_status)} · ${fmtNumber(item.position.quantity)}`;
  const protectionTruth = !positionCurrent ? '等待仓位同步' : !hasPosition ? '当前无仓位' : protectionReady ? `完整覆盖 · ${fmtNumber(item.protection.quantity)}` : item.protection ? fmtStatus(item.protection.status) : '尚无保护';
  const activeTruth = active ? `${fmtIntentKind(active.kind)} · ${fmtStatus(active.status)}` : '无进行中意图';
  const reconciliationTruth = item.reconciliation ? `${fmtStatus(item.reconciliation.status)} · ${fmtDate(item.reconciliation.completed_at)}` : '尚未运行';
  const shadowTools = canRecordSyntheticFacts
    ? `<details class="card operation-toolbox"><summary><span><b>模拟数据与维护工具</b><small>仅用于合成数据、盈亏与对账</small></span></summary><div class="toolbox-content">${nextStep.key === 'position' ? '' : positionFactForm(item)}${hasPosition && nextStep.key !== 'protection' ? protectionFactForm(item) : ''}<div class="toolbar"><button class="secondary" data-pnl>按当前数据刷新盈亏</button><button class="secondary" data-reconcile>重新运行对账</button></div><p class="safety-note">这些动作只写入本地模拟数据；不会连接交易所或发送真实订单。</p></div></details>`
    : '';
  main.innerHTML = `<section class="page campaign-detail"><header class="page-head"><div><p class="eyebrow">${escapeHtml(fmtEnvironment(item.environment, true))} · ${escapeHtml(item.venue)}</p><h1>${escapeHtml(item.instrument?.symbol || '交易任务')} ${shortId(item.campaign_id)}</h1><p class="lede"><b class="status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</b> · ${escapeHtml(fmtDirection(item.direction))} · 当前目标 ${fmtNumber(item.current_target_quantity)}</p></div><a class="secondary" href="/campaigns" data-link>返回交易任务</a></header>
    <article class="campaign-command tone-${nextStep.tone}"><div><p class="eyebrow">当前唯一推荐动作</p><h2>${escapeHtml(nextStep.title)}</h2><p>${escapeHtml(nextStep.copy)}</p></div><div class="campaign-command-action">${nextStep.action}<div class="form-error" id="campaign-action-error"></div></div></article>
    <div class="campaign-truth-grid"><div class="${item.position && !positionCurrent ? 'truth-danger' : ''}"><small>当前仓位</small><b>${escapeHtml(positionTruth)}</b><span>${item.position ? `上次 ${fmtNumber(item.position.quantity)} · ${fmtDate(item.position.observed_at)}` : '等待交易所仓位数据'}</span></div><div class="${positionCurrent && hasPosition && !protectionReady ? 'truth-danger' : ''}"><small>原生保护</small><b>${escapeHtml(protectionTruth)}</b><span>${item.protection ? `触发价 ${fmtNumber(item.protection.trigger_price)} · ${fmtDate(item.protection.observed_at)}` : '有仓位时必须确认足额覆盖'}</span></div><div><small>进行中操作</small><b>${escapeHtml(activeTruth)}</b><span>${active ? `${fmtSide(active.side)} ${fmtNumber(active.quantity)} · ${shortId(active.intent_id)}` : '不会与新动作冲突'}</span></div><div class="${item.reconciliation && !reconciliationMatched ? 'truth-danger' : ''}"><small>最近对账</small><b>${escapeHtml(reconciliationTruth)}</b><span>${item.reconciliation?.differences?.length ? `${item.reconciliation.differences.length} 项差异待处理` : reconciliationMatched ? '晚于当前仓位与操作记录' : '需要在最新数据后重跑'}</span></div></div>
    <div class="stats"><div class="stat"><small>已实现盈亏</small><b>${fmtNumber(item.realized_pnl)}</b></div><div class="stat"><small>未实现盈亏</small><b>${fmtNumber(item.unrealized_pnl)}</b></div><div class="stat"><small>最终 / 当前盈亏</small><b>${fmtNumber(item.final_pnl)}</b></div><div class="stat"><small>风险目标</small><b style="font-size:14px">${fmtNumber(item.current_target_quantity)} · ${escapeHtml(item.target_urgency ? fmtStatus(item.target_urgency) : '尚未设置')}</b></div></div>
    <div class="campaign-command-layout"><div class="stack"><article class="card"><div class="card-heading"><div><p class="eyebrow">执行记录</p><h2>订单操作与成交记录</h2></div><span class="status-pill">${item.intents.length} 个操作 · ${item.fills.length} 笔成交</span></div>${item.intents.length ? item.intents.map(intent => intentCard(intent, item.environment)).join('') : '<p class="subtle">尚无订单操作。</p>'}</article><article class="card"><div class="card-heading"><div><p class="eyebrow">仓位数据</p><h2>仓位与风险保护</h2></div><span class="status-pill ${protectionReady ? 'status-APPROVED' : positionCurrent && hasPosition ? 'status-DENY' : ''}">${!positionCurrent ? '仓位待同步' : hasPosition ? (protectionReady ? '保护完整' : '需要保护') : '当前无仓位'}</span></div><dl class="definition-grid spacious">${definition('仓位数量', item.position ? fmtNumber(item.position.quantity) : '未知')}${definition('平均入场', item.position ? fmtNumber(item.position.average_entry_price) : '—')}${definition('标记价', item.position ? fmtNumber(item.position.mark_price) : '—')}${definition('仓位更新时间', fmtDate(item.position?.observed_at))}${definition('保护状态', item.protection ? fmtStatus(item.protection.status) : '尚无数据')}${definition('保护数量', item.protection ? fmtNumber(item.protection.quantity) : '—')}${definition('保护触发价', item.protection ? fmtNumber(item.protection.trigger_price) : '—')}${definition('保护更新时间', fmtDate(item.protection?.observed_at))}</dl></article>${canCreatePositionAction ? `<article class="card risk-reduction-card"><div class="card-heading"><div><p class="eyebrow">降低风险</p><h2>减仓与退出随时可用</h2></div><span class="status-pill">只减险</span></div><p class="subtle">无论新增风险是否暂停，都可以把目标降到更小数量或 0；系统只生成只减仓操作。</p>${targetForm(item)}</article>` : ''}${shadowTools}</div>
      <aside class="stack"><article class="card"><div class="card-heading"><div><p class="eyebrow">风险目标</p><h2>风险预留与唯一目标</h2></div><span class="status-pill">版本 ${item.target_version}</span></div>${item.reservations.map(r => `<div class="callout"><b>${escapeHtml(fmtStatus(r.status))}</b> · ${fmtNumber(r.amount)} ${escapeHtml(item.instrument?.collateral_currency || '')}</div>`).join('') || '<p class="subtle">无风险预留。</p>'}<dl class="definition-grid">${definition('目标数量', fmtNumber(item.current_target_quantity))}${definition('紧迫度', item.target_urgency ? fmtStatus(item.target_urgency) : '尚未设置')}${definition('目标原因', item.target_reason || '—')}</dl></article>${managementPanel(item, addCandidates, addCandidateError, canOperate, canAddNow, active, protectionReady, reconciliationMatched)}<article class="card"><div class="card-heading"><div><p class="eyebrow">对账</p><h2>对账结论</h2></div><span class="status-pill ${reconciliationMatched ? 'status-APPROVED' : item.reconciliation ? 'status-DENY' : ''}">${escapeHtml(item.reconciliation ? fmtStatus(item.reconciliation.status) : '未运行')}</span></div>${item.reconciliation ? `<p class="subtle">完成于 ${fmtDate(item.reconciliation.completed_at)}</p>${item.reconciliation.differences.length ? `<ul class="exception-list">${item.reconciliation.differences.map(value => `<li>${escapeHtml(value)}</li>`).join('')}</ul>` : '<p class="success-note">订单、成交、仓位和风险保护当前一致。</p>'}` : '<p class="subtle">尚未运行对账；任何不确定结果都必须先对账。</p>'}</article></aside></div></section>`;
  bindCampaignActions(item, active);
}

function campaignNextStep(item, active, canOperate, truth) {
  const filledIntent = item.intents.some(intent => intent.status === 'FILLED');
  if (item.status === 'CLOSED') return {key:'done', tone:'success', title:'交易任务已完成并关闭', copy:'风险预留已释放，结果保留在审计与交易结果中。', action:'<a class="secondary" href="/results" data-link>查看交易结果</a>'};
  if (active?.status === 'UNKNOWN') return {key:'reconcile', tone:'danger', title:'结果不确定，先对账', copy:'风险继续占用，禁止重发、加仓或释放；先核对交易所订单、成交、仓位和保护。', action:canOperate ? '<button class="danger" data-reconcile>立即运行对账</button>' : '<p class="microcopy">等待交易运维人员运行对账。</p>'};
  if (active?.status === 'READY') return item.environment === 'LIVE'
    ? {key:'intent', tone:'attention', title:`等待${fmtIntentKind(active.kind)}发送`, copy:'实盘意图只能由受控发送进程在控制开关、短期授权和有效租约内推进；页面不会合成交易所回执。', action:'<p class="microcopy">等待受控发送进程或前往异常页排查阻断。</p>'}
    : {key:'intent', tone:'attention', title:`记录${fmtIntentKind(active.kind)}发送结果`, copy:'当前只有这个意图可以推进；获取发送租约后记录模拟订单，不会连接交易所。', action:canOperate ? operationForm(active, item) : '<p class="microcopy">等待交易运维人员处理待发送意图。</p>'};
  if (active && ['SENT','PARTIALLY_FILLED'].includes(active.status)) return {key:'intent', tone:'attention', title:`确认${fmtIntentKind(active.kind)}成交结果`, copy:'先记录已确认成交，或在确实无法判断时标记为“结果未知”；不要创建第二个意图。', action:canOperate ? operationForm(active, item) : '<p class="microcopy">等待交易运维人员记录成交结果。</p>'};
  if (!truth.positionCurrent && filledIntent) return {key:'position', tone:'attention', title:'同步成交后的当前仓位', copy:'成交已经记录，但仓位数据早于最新成交或尚未确认；在此之前不能判断保护和下一步。', action:canOperate ? positionFactForm(item) : '<p class="microcopy">等待交易运维人员同步仓位。</p>'};
  if (truth.hasPosition && !truth.protectionReady) return {key:'protection', tone:'danger', title:'先补齐足额原生保护', copy:'当前有仓位但保护缺失、未知或不足。优先确认保护；若无法保护，使用下方减仓或退出。', action:canOperate ? protectionFactForm(item) : '<p class="microcopy">等待交易运维人员确认保护或减仓退出。</p>'};
  if (!truth.reconciliationMatched) return {key:'reconcile', tone:'attention', title:'运行对账确认当前数据', copy:'只有意图、订单、成交、仓位和保护一致后，才适合继续管理或关闭交易任务。', action:canOperate ? '<button class="primary" data-reconcile>运行当前范围对账</button>' : '<p class="microcopy">等待交易运维人员运行对账。</p>'};
  if (truth.flatKnown && truth.exitTerminal && truth.riskClosable) return {key:'close', tone:'success', title:'仓位已清零，可以关闭交易任务', copy:'退出结果终结且对账一致；关闭后会释放剩余风险预留并把结果固定到审计记录。', action:canOperate ? '<button class="primary" data-close-campaign>关闭交易任务</button>' : '<p class="microcopy">等待交易运维人员关闭交易任务。</p>'};
  if (truth.flatKnown) return {key:'close-blocked', tone:'danger', title:'平仓事实仍缺少关闭证据', copy:'仓位虽然为 0，但退出意图或风险预留尚未终结。不要直接释放风险；先到异常页确认原因。', action:'<a class="secondary" href="/exceptions" data-link>查看异常与恢复</a>'};
  if (truth.hasPosition) return {key:'hold', tone:'success', title:'仓位已确认且保护完整', copy:'当前没有必须处理的异常。继续观察；需要时可使用下方减仓或退出，加仓仍需通过全部门控。', action:'<span class="status-pill status-APPROVED">当前无需动作</span>'};
  return {key:'reconcile', tone:'attention', title:'确认当前范围数据', copy:'当前没有可确认仓位；先运行对账，避免把缺失数据误认为已经平仓。', action:canOperate ? '<button class="primary" data-reconcile>运行当前范围对账</button>' : '<p class="microcopy">等待交易运维人员运行对账。</p>'};
}

function intentCard(intent, environment = 'SHADOW') { return `<div class="intent-row"><div><b>${escapeHtml(fmtIntentKind(intent.kind))} · ${escapeHtml(fmtSide(intent.side))} ${fmtNumber(intent.quantity)}</b><br><span class="subtle">${shortId(intent.intent_id)} · ${intent.reduce_only ? '只减仓' : '会增加风险'} · ${fmtDate(intent.updated_at)}</span></div><b class="status-${escapeHtml(intent.status)}">${escapeHtml(fmtStatus(intent.status))}</b></div>${intent.order ? `<p class="subtle">${escapeHtml(fmtEnvironment(environment, true))}订单 ${escapeHtml(intent.order.venue_order_id)} · 已成交 ${fmtNumber(intent.order.filled_quantity)} / ${fmtNumber(intent.order.ordered_quantity)}</p>` : ''}`; }

function operationForm(intent, item) { if (intent.status === 'UNKNOWN') return '<p class="safety-note">结果不确定：风险保持占用，不提供重发或释放按钮。必须先人工对账。</p>'; if (intent.status === 'READY') return `<div class="action-panel"><h3>记录已发送的模拟订单</h3><label>交易所订单编号（合成）<input id="venue-order-id" value="shadow-${intent.intent_id.slice(0,8)}"></label><button class="primary" data-shadow-send>确认已发送</button><button class="danger" data-unknown>结果无法确认</button></div>`; return `<form id="fill-form" class="action-panel"><h3>记录已确认的模拟成交</h3><div class="field-grid"><label>成交编号<input name="venue_fill_id" value="fill-${crypto.randomUUID().slice(0,8)}" required></label><label>成交方向<select name="side"><option value="BUY" ${intent.side === 'BUY' ? 'selected' : ''}>买入</option><option value="SELL" ${intent.side === 'SELL' ? 'selected' : ''}>卖出</option></select></label><label>成交数量<input name="quantity" type="number" step="any" value="${escapeHtml(intent.quantity)}" required></label><label>成交价格<input name="price" type="number" step="any" required></label><label>手续费<input name="fee" type="number" step="any" value="0"></label><label>币种<input name="fee_currency" value="${escapeHtml(item.instrument?.collateral_currency || 'USDT')}"></label><label>滑点成本<input name="slippage_cost" type="number" step="any" value="0"></label></div><div class="toolbar" style="margin-top:12px"><button class="primary">确认并记录成交</button><button type="button" class="danger" data-unknown>结果无法确认</button></div></form>`; }

function positionFactForm(item) { return `<form id="position-form" class="action-panel"><h3>同步当前模拟仓位</h3><p class="microcopy">只录入已经确认的交易所数据；不确定时不要把数量填成 0。</p><div class="field-grid"><label>数量<input name="quantity" type="number" step="any" value="${escapeHtml(formNumber(item.position?.quantity, '0'))}" required></label><label>平均入场价<input name="average_entry_price" type="number" step="any" value="${escapeHtml(formNumber(item.position?.average_entry_price, '0'))}" required></label><label>标记价<input name="mark_price" type="number" step="any" value="${escapeHtml(formNumber(item.position?.mark_price))}" required></label></div><button class="secondary">确认并记录仓位</button></form>`; }

function protectionFactForm(item) { return `<form id="protection-form" class="action-panel"><h3>确认当前模拟保护</h3><div class="field-grid"><label>保护订单编号<input name="venue_order_id" value="${escapeHtml(item.protection?.venue_order_id || 'shadow-stop')}" required></label><label>保护数量<input name="quantity" type="number" step="any" value="${escapeHtml(formNumber(Math.abs(Number(item.position.quantity))))}" required></label><label>触发价<input name="trigger_price" type="number" step="any" value="${escapeHtml(formNumber(item.protection?.trigger_price))}" required></label><label>覆盖状态<select name="coverage"><option value="full">已知且完整</option><option value="degraded">已知但不足</option><option value="unknown">结果未知</option></select></label></div><button class="primary">确认保护数据</button></form>`; }

function targetForm(item) { return `<form id="target-form" class="action-panel"><h3>设定唯一减仓目标</h3><label>减仓后剩余数量<input name="target_quantity" type="number" step="any" min="0" max="${escapeHtml(Math.abs(Number(item.position.quantity)))}" required></label><label>处理速度<select name="urgency"><option value="NORMAL">常规</option><option value="URGENT" selected>紧急</option><option value="IMMEDIATE">立即</option></select></label><label>原因<input name="reason" value="人工降低当前风险" required></label><label>执行限价（链上永续必填）<input name="limit_price" type="number" step="any" min="0"></label><button class="primary">创建只减仓操作</button><button type="button" class="danger" data-auto-exit>评估失效价并退出</button></form>`; }

function managementPanel(item, candidates, candidateError, canOperate, canAddNow, active, protectionReady, reconciliationMatched) {
  const management = item.management || {};
  const candidateOptions = candidates.map(candidate => `<option value="${escapeHtml(candidate.candidate_id)}">${escapeHtml(candidate.timeframe)} · ${fmtNumber(candidate.reference_price)} · ${fmtDate(candidate.observed_at)}</option>`).join('');
  const addBlockedReason = active
    ? '先完成或对账当前订单意图，不能并行新增风险。'
    : !protectionReady
      ? '先确认现有仓位已被足额保护。'
      : !reconciliationMatched
        ? '先完成一致对账。'
        : management.auto_add_gate !== 'ENABLED'
          ? '全局自动加仓当前关闭。'
          : '';
  const addForm = canOperate && management.allow_auto_add && Number(management.remaining_adds) > 0 && canAddNow
    ? `<form id="auto-add-form" class="action-panel"><h3>Perptape 加仓候选</h3>${candidateError ? `<p class="safety-note">${escapeHtml(candidateError)}</p>` : candidateOptions ? `<label>后续候选<select name="candidate_id">${candidateOptions}</select></label><label>加仓数量<input name="quantity" type="number" step="any" min="0" max="${escapeHtml(management.remaining_quantity)}" required></label><button class="primary" ${management.auto_add_gate !== 'ENABLED' ? 'disabled' : ''}>完成最终风控并创建加仓意图</button>` : '<p class="safety-note">当前没有同交易所、同标的、同方向的后续 Perptape 候选。</p>'}</form>`
    : `<p class="safety-note">${escapeHtml(addBlockedReason || '该交易任务没有剩余的可用加仓次数，或者原提案没有允许自动加仓。')}</p>`;
  const canDisableAdd = canOperate && management.allow_auto_add && Number(management.remaining_adds) > 0;
  return `<article class="card"><div class="card-heading"><div><p class="eyebrow">高级风险选项</p><h2>自动加仓管理</h2></div><span class="status-pill ${management.auto_add_gate === 'ENABLED' ? 'status-APPROVED' : 'status-EXPIRED'}">${escapeHtml(management.auto_add_gate === 'ENABLED' ? '全局已开启' : '全局已关闭')}</span></div><dl class="definition-grid">${definition('已用 / 可用加仓次数', `${item.authorization?.used_adds || 0} / ${item.authorization?.allowed_adds || 0}`)}${definition('剩余数量', fmtNumber(management.remaining_quantity))}${definition('提案触发价', fmtNumber(management.add_trigger_price))}</dl>${addForm}${canDisableAdd ? '<button class="danger" data-disable-campaign-add>永久关闭本交易任务的后续加仓</button>' : ''}<p class="safety-note">加仓是高级可选动作，必须在确认成交、保护足额且对账一致后进行。只有第一笔实际成交会消耗一次加仓次数；结果未知时系统会阻止新增风险。</p></article>`;
}

function bindCampaignActions(item, active) {
  document.querySelectorAll('[data-pnl]').forEach(button => button.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/pnl`, {}, {button:event.currentTarget, pendingLabel:'刷新中…', successMessage:'盈亏已按当前模拟数据重新计算'})));
  document.querySelectorAll('[data-reconcile]').forEach(button => button.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/reconcile`, {execution_scope:`${item.account_id}:${item.venue}`}, {button:event.currentTarget, pendingLabel:'对账中…', successMessage:'对账已完成；结果已写入审计事实'})));
  document.querySelector('[data-close-campaign]')?.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/close`, {}, {
    button:event.currentTarget,
    pendingLabel:'关闭中…',
    successMessage:'交易任务已关闭，剩余风险预留已释放',
    confirm:{title:'关闭这个交易任务？', message:'系统会再次确认仓位已清零、没有进行中意图且最近对账一致。关闭后会释放剩余风险预留，历史记录仍可审计。', confirmLabel:'确认关闭'},
  }));
  document.querySelector('[data-shadow-send]')?.addEventListener('click', async (event) => withPending(event.currentTarget, '记录中…', async () => {
    const owner = `web-${session.user_id.slice(0,8)}`;
    try {
      const lease = await api('/api/sender-leases', {method:'POST', body:JSON.stringify({execution_scope:`${item.account_id}:${item.venue}`, owner_id:owner, lease_seconds:60})});
      await api(`/api/intents/${active.intent_id}/shadow-send`, {method:'POST', body:JSON.stringify({execution_scope:`${item.account_id}:${item.venue}`, owner_id:owner, fencing_token:lease.fencing_token, venue_order_id:document.querySelector('#venue-order-id').value})});
      showToast('已记录模拟发送结果；没有连接交易所'); await route();
    } catch (error) { showApiError(error); }
  }));
  document.querySelectorAll('[data-unknown]').forEach(button => button.addEventListener('click', () => campaignAction(`/api/intents/${active.intent_id}/unknown`, {reason:'operator marked uncertain SHADOW outcome'}, {
    button,
    successMessage:'意图已标记为结果未知；风险保持占用并等待人工对账',
    confirm:{title:'标记为结果未知？', message:'这会阻止与该意图相关的新增风险，并隐藏重发和释放入口。请只在模拟结果确实无法确认时继续，随后必须人工对账。', confirmLabel:'标记为结果未知'},
  })));
  document.querySelector('#fill-form')?.addEventListener('submit', event => submitNamedForm(event, `/api/intents/${active.intent_id}/fills`));
  document.querySelector('#position-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction('/api/facts/positions', {...data, account_id:item.account_id, venue:item.venue, instrument_id:item.instrument_id, known:true}, {button:event.submitter, successMessage:'模拟仓位数据已更新'}); });
  document.querySelector('#protection-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction(`/api/campaigns/${item.campaign_id}/protection`, {position_id:item.position.position_id, venue_order_id:data.venue_order_id, quantity:data.quantity, trigger_price:data.trigger_price, fully_covered:data.coverage === 'full', known:data.coverage !== 'unknown'}, {button:event.submitter, successMessage:'保护数据已更新；覆盖状态已重新计算'}); });
  document.querySelector('#target-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction(`/api/campaigns/${item.campaign_id}/managed-reductions`, {target_quantity:data.target_quantity, urgency:data.urgency, reason:data.reason, limit_price:data.limit_price || null, idempotency_key:crypto.randomUUID()}, {button:event.submitter, successMessage:'唯一只减仓目标已生成'}); });
  document.querySelector('[data-auto-exit]')?.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/automatic-exit`, {idempotency_key:crypto.randomUUID(), limit_price:document.querySelector('#target-form')?.elements.limit_price.value || null}, {
    button:event.currentTarget,
    successMessage:'自动退出评估已完成；模拟退出意图已按提案失效价生成',
    confirm:{title:'评估并自动退出？', message:'确认后会按提案失效价评估退出条件，并可能生成新的只减仓模拟意图。不会连接交易所或发送真实订单。', confirmLabel:'评估并生成退出意图'},
  }));
  document.querySelector('#auto-add-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction(`/api/campaigns/${item.campaign_id}/auto-add`, {candidate_id:data.candidate_id, quantity:data.quantity, idempotency_key:crypto.randomUUID()}, {button:event.submitter, successMessage:'加仓候选已完成最终风控；结果已记录'}); });
  document.querySelector('[data-disable-campaign-add]')?.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/auto-add/disable`, {reason:'operator disabled further Campaign AddUnits', idempotency_key:crypto.randomUUID()}, {
    button:event.currentTarget,
    successMessage:'本交易任务的后续加仓已关闭',
    confirm:{title:'关闭本交易任务的后续加仓？', message:'确认后，该交易任务剩余的可用加仓次数将不能继续使用。已有仓位仍可减仓或退出。', confirmLabel:'关闭后续加仓'},
  }));
}

async function campaignAction(path, body, {button = null, pendingLabel = '处理中…', successMessage = '模拟状态已更新', confirm = null} = {}) {
  if (confirm && !await confirmAction(confirm)) return;
  const run = async () => {
    try {
      await api(path, {method:'POST', body:JSON.stringify(body)});
      showToast(successMessage);
      await route();
    } catch (error) { showApiError(error, document.querySelector('#campaign-action-error')); }
  };
  return button ? withPending(button, pendingLabel, run) : run();
}
async function submitNamedForm(event, path) { event.preventDefault(); await campaignAction(path, Object.fromEntries(new FormData(event.currentTarget)), {button:event.submitter}); }

function navigate(path) { history.pushState({}, '', path); route(); }
function updateActiveNav() {
  document.querySelectorAll('nav a').forEach((link) => {
    const href = link.getAttribute('href');
    const active = location.pathname === href || (href === '/venues' && location.pathname.startsWith('/venues/'));
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

document.addEventListener('click', (event) => {
  const link = event.target.closest('[data-link]');
  if (link) { event.preventDefault(); navigate(link.getAttribute('href')); }
  if (event.target.closest('[data-retry]')) route();
});
window.addEventListener('popstate', route);
window.addEventListener('resize', syncNavigationMode);
mobileNavToggle.addEventListener('mousedown', (event) => event.preventDefault());
mobileNavToggle.addEventListener('click', () => sidebar.classList.contains('open') ? closeMobileNav() : openMobileNav());
mobileNavToggle.addEventListener('keydown', (event) => {
  if (!['Enter', ' '].includes(event.key)) return;
  event.preventDefault();
  if (sidebar.classList.contains('open')) closeMobileNav();
  else openMobileNav();
});
navBackdrop.addEventListener('click', () => closeMobileNav());
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && sidebar.classList.contains('open')) closeMobileNav();
});
document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => dialog.close()));
document.querySelector('#system-proposal-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form)); const candidateId = data.candidate_id; delete data.candidate_id; data.environment = 'LIVE'; data.expires_in_minutes = Number(data.expires_in_minutes); data.initial_quantity = data.initial_quantity || null; data.add_trigger_price = data.add_trigger_price || null; data.allow_auto_add = data.allow_auto_add === 'true'; data.requested_adds = Number(data.requested_adds);
  try { const result = await api(`/api/opportunities/${candidateId}/proposals`, {method:'POST', body:JSON.stringify(data)}); dialog.close(); showToast('系统机会提案已冻结并进入审核'); navigate(`/proposals/${result.proposal_id}`); }
  catch (error) { showApiError(error, form.querySelector('#system-form-error')); }
});
document.querySelector('#logout-button').addEventListener('click', async (event) => withPending(event.currentTarget, '退出中…', async () => {
  cancelMobileNavFocus();
  try {
    await api('/api/auth/logout', {method:'POST'});
    session = null;
    setShell(false);
    history.replaceState({}, '', '/');
    await route();
  } catch (error) { showApiError(error); }
}));
document.querySelector('#theme-toggle').addEventListener('click', () => { const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = next; localStorage.setItem('trading-theme', next); });
languageToggle.addEventListener('click', () => {
  currentLanguage = currentLanguage === 'en' ? 'zh-CN' : 'en';
  localStorage.setItem(LANGUAGE_STORAGE_KEY, currentLanguage);
  location.reload();
});
document.documentElement.dataset.theme = localStorage.getItem('trading-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
applyLanguageToDocument();
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
syncNavigationMode();
bootstrap().catch((error) => { main.innerHTML = errorView(error, false); });
