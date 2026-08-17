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
const statusLabels = {DRAFT:'草稿',PENDING_REVIEW:'待审核',APPROVED:'已批准',REJECTED:'已拒绝',EXPIRED:'已过期',ALLOW:'通过',SCALE:'缩小仓位',DENY:'拒绝',PENDING:'等待中',RETRY_WAIT:'等待重试',SENDING:'发送中',DEAD_LETTER:'投递失败',OUTCOME_UNKNOWN:'发送结果未知',RESERVED:'已预留',READY:'待发送',DISPATCHING:'已派发，等待确认',SENT:'已发送',PARTIALLY_FILLED:'部分成交',FILLED:'已成交',CANCELLED:'已取消',UNKNOWN:'结果未知',KNOWN:'已确认',OPENING:'建仓中',OPEN:'持仓中',REDUCING:'减仓中',CLOSING:'退出中',CLOSED:'已结束',ACTIVE:'有效',DEGRADED:'保护不足',RELEASED:'已释放',MATCH:'对账一致',DIFFERENCE:'存在差异',MANUAL_REQUIRED:'需要人工处理',RESOLVED:'已解决',NORMAL:'正常',URGENT:'紧急',IMMEDIATE:'立即',ENABLED:'已开启',DISABLED:'已关闭',SUCCESS:'连接正常',FAILED:'连接失败',SKIPPED:'未运行',STALE:'数据已过期',WAITING:'等待首次同步',UNCONFIGURED:'未配置',NOT_CONFIGURED:'未配置',NOT_VERIFIED:'待验证',VERIFIED:'已验证',ON_DEMAND:'按需读取',MISSING:'缺失',CURRENT:'当前有效',INCOMPLETE:'数据不完整',EMPTY:'暂无数据',AVAILABLE:'可用',CONTROLLED:'受控',READ_ONLY:'只读',BLOCKED:'已安全阻断',NOT_SUBMITTED:'未提交',SOURCE_RESERVED:'源端已预留',SUBMITTED:'已提交',IN_FLIGHT:'划转中',DESTINATION_CONFIRMED:'目的端已确认',SETTLED:'已结算',FAILED_SOURCE_RESTORED:'失败，源端已恢复',DEPOSIT_PLAN_READY:'充值计划待执行',DEPOSIT_CONFIRMED:'充值已确认',RELEASE_REQUEST_PLAN_READY:'释放申请计划待执行',RELEASE_REQUEST_CONFIRMED:'释放申请已确认',RELEASE_EXECUTION_PLAN_READY:'释放执行计划待执行',RELEASE_EXECUTION_CONFIRMED:'释放执行已确认',RELEASE_CANCELLATION_PLAN_READY:'释放取消计划待执行',RELEASE_CANCELLED:'释放已取消'};
const riskLabels = {LOW:'低风险',MEDIUM:'中风险',HIGH:'高风险'};
const intentKindLabels = {INITIAL:'初仓',ADD:'加仓',REDUCE:'减仓',EXIT:'退出'};
const directionLabels = {LONG:'做多',SHORT:'做空'};
const sideLabels = {BUY:'买入',SELL:'卖出'};
const sideEnglishLabels = {BUY:'Buy',SELL:'Sell'};
const capitalDirectionLabels = {VAULT_TO_VENUE:'资金库转入交易所',VENUE_TO_VAULT:'交易所转回资金库'};
const currentWorkflowEnvironment = () => {
  const persistedMode = session?.active_team?.execution_mode;
  return ['LIVE','TESTNET'].includes(persistedMode) ? persistedMode : 'SETUP';
};
const roleLabels = {OBSERVER:'只读用户',PROPOSER:'提案发起人',REVIEWER:'审核人',OPERATOR:'风险管理',TREASURY_ADMIN:'资金管理员',SYSTEM_ADMIN:'系统管理员',SYSTEM:'系统'};
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
const fmtIntentKind = (value) => localizedText(intentKindLabels[value] || value || '未知意图');
const fmtDirection = (value) => localizedText(directionLabels[value] || value || '未知方向');
const fmtSide = (value) => currentLanguage === 'en'
  ? sideEnglishLabels[value] || value || 'Unknown side'
  : sideLabels[value] || value || '未知方向';
const deploymentEnvironmentLabels = {
  LOCAL:'本地运行', TEST:'测试运行', PRODUCTION:'生产运行',
  LIVE:'生产模式', TESTNET:'测试模式',
};
const deploymentEnvironmentEnglishLabels = {
  LOCAL:'Local runtime', TEST:'Test runtime', PRODUCTION:'Production runtime',
  LIVE:'Production mode', TESTNET:'Test mode',
};
const fmtEnvironment = (value, withCode = false) => {
  const code = String(value || '').trim().toUpperCase();
  const labels = currentLanguage === 'en'
    ? deploymentEnvironmentEnglishLabels
    : deploymentEnvironmentLabels;
  const label = labels[code] || (currentLanguage === 'en' ? 'Unknown environment' : '环境未确认');
  return withCode && code ? `${label} · ${code}` : label;
};
const fmtExecutionMode = mode => mode === 'LIVE' ? '生产模式' : mode === 'TESTNET' ? '测试模式' : '待配置';
function updateEnvironmentIndicators() {
  const deploymentLabel = fmtEnvironment(authStatus?.environment);
  const teamMode = session?.active_team?.execution_mode;
  const modeLabel = ['LIVE','TESTNET'].includes(teamMode)
    ? fmtEnvironment(teamMode)
    : localizedText('待配置');
  const labelSeparator = currentLanguage === 'en' ? ': ' : '：';
  environmentModeValue.textContent = modeLabel;
  environmentBadge.dataset.environment = String(teamMode || 'setup').toLowerCase();
  environmentBadge.setAttribute('aria-label', `${localizedText('当前模式')}${labelSeparator}${modeLabel}`);
  environmentBadge.title = `${localizedText('当前模式')}${labelSeparator}${modeLabel} · ${localizedText('当前环境')}${labelSeparator}${deploymentLabel}`;
  const canOpenModeSwitcher = Boolean(session && hasCapability('venue.view'));
  environmentBadge.disabled = !canOpenModeSwitcher;
  if (!canOpenModeSwitcher) environmentBadge.setAttribute('aria-expanded', 'false');
}
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
  CURRENT_PASSWORD_INVALID:'当前密码不正确。',
  PASSWORD_UNCHANGED:'新密码必须与当前密码不同。',
  AUTH_VERSION_CONFLICT:'登录身份已变化，请刷新页面后重新验证。',
  PASSWORD_AUTH_REQUIRED:'当前会话不是密码登录，请使用密码重新登录后修改。',
  AGENT_TOKEN_INVALID:'该 API Key 已失效、已轮换或不匹配。请使用当前凭证。',
  AGENT_TOKEN_EXPIRED:'该 API Key 已到期。请在网页中轮换凭证。',
  API_CLIENT_RATE_LIMITED:'该 API Key 请求过于频繁，请稍后重试。',
  API_CLIENT_REVOKED:'该 API Key 已永久撤销；需要接入时请创建新的 API Key。',
  RISK_POLICY_MISSING:'风险政策尚未配置，因此系统已暂停创建和执行新增风险。请联系系统管理员完成配置。',
  PERPTAPE_NOT_CONFIGURED:'Perptape 尚未配置。人工提案仍可使用，外部机会将在完成配置后恢复。',
  PERPTAPE_UNAVAILABLE:'暂时无法连接 Perptape。人工提案仍可使用，请稍后重新检查外部机会。',
  PERPTAPE_RUNTIME_FEED_MISSING:'Perptape 正在等待首次同步，请稍后重新检查。',
  PERPTAPE_RUNTIME_FEED_STALE:'Perptape 最近数据已经过期，系统不会把旧候选当成实时机会。',
  PERPTAPE_CACHE_INVALID:'Perptape 已保存的数据无法读取，请联系风险管理人员处理。',
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
  EXCHANGE_ACCOUNT_NOT_FOUND:'账户已删除、已停用或不属于当前团队，请刷新账户列表后重试。',
  DEFAULT_ACCOUNT_REQUIRED:'请填写与服务端生产运行绑定完全一致的账户 ID。系统校验不可变 account_id，不使用可编辑的账户名称。',
  CAPITAL_CONFIGURATION_FEE_LIMIT_INVALID:'最大费用上限必须低于单次金额上限，请调整后重新保存。',
  CAPITAL_BINANCE_WITHDRAWAL_ADDRESS_SCOPE_MISMATCH:'币安受限提现地址必须与当前使用的链上金库地址完全一致。使用 Safe 时请填写 Safe Smart Account；使用 NoTilt 时请填写 NoTilt 金库地址。',
  BINANCE_CAPITAL_CREDENTIALS_MISSING:'所选生产币安账户缺少已验证的资金 API 凭据。请到账户管理补齐 API Key 与 Secret，并重新执行连接测试。',
  BINANCE_CAPITAL_TIME_SYNC_FAILED:'服务端未能读取币安官方时间，签名请求保持阻断。请检查服务器到币安 API 的网络后重试。',
  BINANCE_CAPITAL_AUTHORIZATION_REJECTED:'币安拒绝了资金 API 的密钥、来源 IP 或接口权限。请确认该 Key 已开启读取与提现权限，并把服务端固定出口 IP 加入币安 IP 白名单。',
  BINANCE_CAPITAL_TIMESTAMP_REJECTED:'币安拒绝了签名请求时间。系统已使用官方时间；请重试，持续失败时检查服务端时间与网络代理。',
  BINANCE_CAPITAL_RATE_LIMITED:'币安资金 API 当前触发限流或临时 IP 封禁，请按 Retry-After 等待后重试。',
  BINANCE_CAPITAL_IP_RESTRICTION_REQUIRED:'当前币安 API Key 未启用受限 IP。请在币安 API 管理中绑定本服务的固定出口 IP 后重试。',
  BINANCE_CAPITAL_WITHDRAW_PERMISSION_DISABLED:'当前币安 API Key 没有提现权限。请在币安 API 管理中核对权限后重试。',
  BINANCE_CAPITAL_DESTINATION_NOT_ALLOWLISTED:'当前链上金库地址未出现在币安 USDC / Arbitrum 提现地址白名单。请在币安地址管理中添加当前 Safe 或 NoTilt 金库地址，等待生效后重试。',
  BINANCE_CAPITAL_DEPOSIT_ADDRESS_MISMATCH:'币安实时返回的 USDC / Arbitrum 充值地址与资金配置不一致。请核对币安充值地址后保存新的资金配置版本。',
  BINANCE_CAPITAL_NETWORK_BUSY:'币安当前将 USDC / Arbitrum 网络标记为繁忙，资金操作保持阻断。请等待网络恢复后重试。',
  BINANCE_CAPITAL_DEPOSIT_DISABLED:'币安当前关闭了 USDC / Arbitrum 充值，资金操作保持阻断。请等待充值恢复后重试。',
  BINANCE_CAPITAL_WITHDRAW_DISABLED:'币安当前关闭了 USDC / Arbitrum 提现，资金操作保持阻断。请等待提现恢复后重试。',
  BINANCE_CAPITAL_AMOUNT_OUT_OF_RANGE:'划转金额不在币安当前 USDC / Arbitrum 提现范围内。请按实时最小值和最大值调整金额。',
  BINANCE_CAPITAL_FEE_LIMIT_EXCEEDED:'币安当前提现手续费超过资金配置冻结的最大费用上限。请核对实时费用后提高上限或稍后重试。',
  BINANCE_CAPITAL_BALANCE_INSUFFICIENT:'币安生产账户的可用 USDC 余额不足以覆盖本次提现金额和手续费。',
  BINANCE_CAPITAL_QUOTA_EXCEEDED:'币安生产账户当前可用提现额度不足。请等待额度恢复或降低金额。',
  BINANCE_CAPITAL_TRAVEL_RULE_REQUIRED:'币安要求本次提现完成 Travel Rule 信息。当前固定资金路径保持阻断，请先在币安完成所需信息。',
  BINANCE_CAPITAL_SUBMISSION_DISABLED:'币安真实提现提交开关当前关闭。请由系统管理员核对生产运行配置。',
  BINANCE_CAPITAL_API_REJECTED:'币安拒绝了资金 API 请求。请核对 API 权限、固定出口 IP、账户状态和请求时间。',
  BINANCE_CAPITAL_API_UNAVAILABLE:'币安资金 API 当前不可达，资金操作保持原位。请稍后重试。',
  SAFE_ACCOUNT_NOT_DEPLOYED:'当前 Safe Smart Account 尚未部署在 Arbitrum。请核对链与 Safe 地址。',
  SAFE_ALLOWANCE_MODULE_DISABLED:'当前 Safe 尚未启用官方 Allowance Module。请由 Safe 多签所有者启用模块后重试。',
  SAFE_ALLOWANCE_OR_BALANCE_INSUFFICIENT:'Safe 当前可用 Spending Limit 或 USDC 余额不足。请降低金额，或由 Safe 所有者提高 delegate 额度并确保余额充足。',
  SAFE_PREFLIGHT_REJECTED:'Safe 官方 Allowance Module 实时预检未通过。请核对 Safe、delegate、模块、额度与余额。',
  HYPERLIQUID_DEPOSIT_ACCOUNT_MISMATCH:'当前自有 Arbitrum 地址与 Hyperliquid API Wallet 所属主账户不一致。请把资金路径地址改为该主账户，或换用属于当前自有地址的 Hyperliquid API Wallet。',
  HYPERLIQUID_AGENT_SCOPE_MISMATCH:'配置的 Hyperliquid API Wallet 属于另一主账户。请修正 API Wallet 与生产账户绑定后重试。',
  HYPERLIQUID_AGENT_NOT_AUTHORIZED:'配置的 Hyperliquid API Wallet 当前不是有效 Agent。请在 Hyperliquid 主账户重新授权后重试。',
  HYPERLIQUID_DEPOSIT_BELOW_MINIMUM:'Hyperliquid Arbitrum 入金必须至少为 5 USDC；请同时为最大费用预留空间。',
  HYPERLIQUID_CAPITAL_PREFLIGHT_UNAVAILABLE:'Hyperliquid 官方资金预检当前不可达，资金操作保持原位。请稍后重试。',
  NOTILT_VAULT_SCOPE_MISMATCH:'NoTilt 金库地址与当前可信生产范围不一致，请核对金库地址后重试。',
  SECOND_CONFIRMATION_REQUIRED:'二次确认内容与当前账户不一致，请刷新页面后重新确认。',
  VERSION_CONFLICT:'页面数据已更新，请刷新后再执行操作。',
  NOTILT_RELEASE_BUDGET_MISSING:'当前资产没有可用的 NoTilt 实时额度，系统不会生成释放请求。',
  NOTILT_RELEASE_SCOPE_MISMATCH:'NoTilt 实时资金范围与已配置金库不一致，请由系统管理员核对配置。',
  NOTILT_VAULT_UNTRUSTED:'当前金库不在 NoTilt 官方可信部署目录中，系统已阻断。',
  NOTILT_WHITELIST_INACTIVE:'NoTilt 白名单尚未生效或未指向当前金库，系统已阻断。',
  NOTILT_AGENT_OWNER_FORBIDDEN:'当前 Agent 与金库所有者身份冲突，不能使用 Agent 额度路径。',
  NOTILT_PANIC_LOCKED:'NoTilt 金库处于紧急锁定状态，不能构建资金请求。',
  NOTILT_FACT_STALE:'NoTilt 实时额度已过期，请刷新资金事实后重试。',
  NOTILT_RELEASE_LIMIT_EXCEEDED:'金额超过 NoTilt 当前实时可释放上限，请降低金额或等待额度恢复。',
};
const fmtStatus = (value) => localizedText(statusLabels[value] || value || '未知');
const exchangeConnectionErrorLabels = {
  BINANCE_AUTHENTICATION_FAILED:'币安拒绝了凭据、只读权限或出口 IP 范围',
  BINANCE_TIMESTAMP_REJECTED:'币安拒绝了请求时间；系统会使用官方服务器时间重试',
  BINANCE_RATE_LIMITED:'币安只读接口正在限流，请稍后重试',
  BINANCE_READ_ONLY_UNAVAILABLE:'币安官方只读接口暂时不可达',
  BINANCE_RESPONSE_INVALID:'币安返回了当前适配器无法采信的响应',
  READ_ONLY_PROBE_FAILED:'只读连接检查失败',
};
const exchangeConnectionErrorEnglishLabels = {
  BINANCE_AUTHENTICATION_FAILED:'Binance rejected the credential, read permission, or egress IP scope',
  BINANCE_TIMESTAMP_REJECTED:'Binance rejected the request time; the system will retry with official server time',
  BINANCE_RATE_LIMITED:'The Binance read-only API is rate-limiting requests; retry later',
  BINANCE_READ_ONLY_UNAVAILABLE:'The official Binance read-only API is currently unreachable',
  BINANCE_RESPONSE_INVALID:'Binance returned a response the current adapter cannot trust',
  READ_ONLY_PROBE_FAILED:'The read-only connection probe failed',
};
const fmtExchangeConnectionError = value => currentLanguage === 'en'
  ? exchangeConnectionErrorEnglishLabels[value] || value || 'No diagnostic is available'
  : exchangeConnectionErrorLabels[value] || value || '暂无可用诊断';
const binanceLimitCategoryLabels = {
  ORDINARY_RATE_LIMIT:'普通请求限流',
  REQUEST_WEIGHT_EXCEEDED:'请求权重超限',
  IP_TEMPORARILY_BANNED:'IP 临时封禁',
};
function fmtBinanceConnectionDiagnostic(connection) {
  const diagnostics = connection?.diagnostics;
  if (!diagnostics) return fmtExchangeConnectionError(connection?.error_code);
  const category = binanceLimitCategoryLabels[diagnostics.category] || diagnostics.category;
  const retryAt = diagnostics.next_retry_at ? fmtDate(diagnostics.next_retry_at) : '稍后';
  const status = diagnostics.http_status ? `HTTP ${diagnostics.http_status}` : 'HTTP 状态未知';
  const code = diagnostics.binance_error_code === null || diagnostics.binance_error_code === undefined
    ? 'Binance code 未返回' : `Binance ${diagnostics.binance_error_code}`;
  return `${category}（${status}，${code}）；建议 ${retryAt} 后重试`;
}
const fmtRisk = (value) => localizedText(riskLabels[value] || value || '未知');
const riskGuidance = (reason) => riskReasonGuidance[reason] || {label:'风险检查未通过',action:'查看当前风险事实，处理阻塞后重新检查。'};
const friendlyApiError = (error) => {
  if (error?.code === 'EXCHANGE_ACCOUNT_DELETE_BLOCKED') {
    const labels = {
      UNFINISHED_PROPOSALS:'未完成提案', ACTIVE_TRADING_AUTHORIZATIONS:'有效交易授权',
      UNFINISHED_ORDER_INTENTS:'未完成订单意图', UNFINISHED_VENUE_ORDERS:'未结束订单',
      OPEN_OR_UNKNOWN_POSITIONS:'未平仓或状态未知仓位', RUNNING_CAPITAL_TRANSFERS:'运行中资金任务',
      RUNNING_DIRECT_CAPITAL_OPERATIONS:'运行中资金操作',
    };
    const details = String(error.message || '').split(';').map(item => {
      const [code, count] = item.split('=');
      return labels[code] ? `${labels[code]} ${count} 项` : '';
    }).filter(Boolean).join('、');
    return `账户暂不能删除：${details || '仍存在业务引用'}。请先结束这些引用后重试。`;
  }
  const risk = riskReasonGuidance[error?.code] || riskReasonGuidance[error?.message];
  if (risk) return `${risk.label}：${risk.action}`;
  if (actionErrorGuidance[error?.code]) return actionErrorGuidance[error.code];
  if (['BINANCE_CAPITAL_RATE_LIMITED','BINANCE_CONNECTION_RETRY_DEFERRED'].includes(error?.code) && error?.details) {
    return fmtBinanceConnectionDiagnostic({error_code:error.code, diagnostics:error.details});
  }
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
