async function renderCampaignList() {
  const result = await api('/api/campaigns');
  const items = result.data.filter(item => item.environment === 'LIVE');
  main.innerHTML = `<section class="page"><header class="page-head"><div><h1>交易任务</h1><p class="lede">每个交易任务覆盖一笔交易从授权、风险占用和下单意图，到成交、保护、减仓、对账与最终结果的完整生命周期。</p></div><div class="toolbar"><a class="secondary" href="/campaigns/alerts" data-link>运行告警</a><a class="secondary" href="/proposals" data-link>查看提案</a></div></header>
    <div class="stats"><div class="stat"><small>交易任务记录</small><b>${items.length}</b></div><div class="stat"><small>建仓中 / 持仓中</small><b>${items.filter(i => ['OPEN','OPENING'].includes(i.status)).length}</b></div><div class="stat"><small>结果未知</small><b>${items.filter(i => i.status === 'UNKNOWN').length}</b></div><div class="stat"><small>运行范围</small><b style="font-size:14px">生产交易</b></div></div>
    ${items.length ? `<div class="table-wrap campaign-list-table"><table><thead><tr><th>标的 / 方向</th><th>账户 / 场所</th><th>仓位目标</th><th>状态</th><th>最终盈亏</th><th>更新时间</th></tr></thead><tbody>${items.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td data-label="标的 / 方向"><b>${escapeHtml(item.symbol || '标的未配置')}</b><br><span class="${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(fmtDirection(item.direction))}</span><br><a class="row-link" href="/campaigns/${item.campaign_id}" data-link>${shortId(item.campaign_id)} · 查看详情</a></td><td data-label="账户 / 场所">${escapeHtml(fmtDefaultAccountLabel(item.account_id))}<br><span class="subtle">${escapeHtml(fmtVenueLabel(item.venue))}</span></td><td data-label="仓位目标">${escapeHtml(campaignTargetLabel(item))}</td><td data-label="状态"><b class="status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</b></td><td data-label="最终盈亏">${escapeHtml(campaignPnlLabel(item, item.final_pnl))}</td><td data-label="更新时间">${fmtDate(item.updated_at)}</td></tr>`).join('')}</tbody></table></div>` : `<section class="empty-state"><div><h2>当前没有交易任务</h2><p>提案通过审核和风险检查后，交易运维人员才能发起开仓。</p></div></section>`}</section>`;
  bindLinkedRows();
}

function shadowBlockerLabel(code) {
  return ({
    SIGNAL_SOURCE_REQUIRED:'先启用当前团队的 Perptape 或 Webhook 信号源',
    RISK_POLICY_REQUIRED:'先保存当前团队的版本化风险政策',
    RISK_LIMITS_REQUIRED:'补齐单账户、单笔亏损、连续亏损与冷却期限制',
    EXCHANGE_ACCOUNT_REQUIRED:'先登记至少一个当前团队交易账户',
    INDEPENDENT_REVIEWER_REQUIRED:'至少配置两名不同成员承担提案与独立审核',
    OPERATOR_REQUIRED:'至少配置一名交易运维人员',
  })[code] || code;
}

const SHADOW_READINESS_CATALOG = [
  {
    id:'members',
    label:'成员与职责',
    blockers:['INDEPENDENT_REVIEWER_REQUIRED','OPERATOR_REQUIRED'],
    href:'/admin/users',
    capability:'access.manage',
    action:'配置成员权限',
    owner:'团队管理员',
    readyCopy:'提案、独立审核与交易运维职责已在至少一个精确账户范围内形成闭环。',
  },
  {
    id:'signal',
    label:'信号源',
    blockers:['SIGNAL_SOURCE_REQUIRED'],
    href:'/signals',
    capability:'signal.view',
    action:'查看信号源设置',
    owner:'团队管理员',
    readyCopy:'当前团队已启用且只启用一种信号源模式。',
  },
  {
    id:'account',
    label:'交易账户范围',
    blockers:['EXCHANGE_ACCOUNT_REQUIRED'],
    href:'/venues',
    capability:'venue.view',
    action:'查看交易账户',
    owner:'交易运维或团队管理员',
    readyCopy:'当前团队至少有一个启用的精确交易所账户范围。',
  },
  {
    id:'risk',
    label:'版本化风控',
    blockers:['RISK_POLICY_REQUIRED','RISK_LIMITS_REQUIRED'],
    href:'/risk',
    capability:'system.view',
    action:'查看风险配置',
    owner:'系统管理员',
    readyCopy:'版本化风险政策已覆盖账户风险、单笔亏损、连续亏损与冷却期。',
  },
];

function shadowReadinessSteps(activation) {
  const blockers = new Set(activation.blockers || []);
  const recognized = new Set(SHADOW_READINESS_CATALOG.flatMap(item => item.blockers));
  const steps = SHADOW_READINESS_CATALOG.map(item => {
    const activeBlockers = item.blockers.filter(code => blockers.has(code));
    return {...item, activeBlockers, complete:activeBlockers.length === 0};
  });
  [...blockers].filter(code => !recognized.has(code)).forEach(code => steps.push({
    id:`unknown-${code}`,
    label:'未知服务端阻断',
    blockers:[code],
    activeBlockers:[code],
    complete:false,
    href:null,
    capability:null,
    action:'',
    owner:'系统管理员',
    readyCopy:'',
  }));
  return steps;
}

function shadowReadinessItem(step, index) {
  const copy = step.complete
    ? step.readyCopy
    : step.activeBlockers.map(shadowBlockerLabel).join('；');
  const canOpen = step.href && (!step.capability || hasCapability(step.capability));
  const action = canOpen
    ? `<a class="text-button readiness-action" href="${step.href}" data-link>${step.complete ? '查看此项 →' : '处理此项 →'}</a>`
    : '';
  const technicalCodes = step.activeBlockers.length
    ? `<span class="readiness-code" translate="no">${step.activeBlockers.map(escapeHtml).join(' · ')}</span>`
    : '';
  return `<li class="readiness-item ${step.complete ? 'is-complete' : 'is-blocked'}" data-readiness-step="${escapeHtml(step.id)}"><span class="readiness-marker" aria-hidden="true">${step.complete ? '✓' : index + 1}</span><div class="readiness-copy"><div class="readiness-title"><h3>${escapeHtml(step.label)}</h3><span class="status-pill ${step.complete ? 'status-APPROVED' : 'status-BLOCKED'}">${step.complete ? '已满足' : '需处理'}</span></div><p>${escapeHtml(copy)}</p><div class="readiness-meta"><span><b>责任角色</b>${escapeHtml(step.owner)}</span>${technicalCodes}</div></div>${action}</li>`;
}

async function renderShadowWorkspace() {
  const response = await api('/api/shadow');
  const data = response.data;
  const activation = data.activation || {blockers:[], ready:false, can_activate:false};
  const accounts = data.accounts || [];
  const campaigns = data.campaigns || [];
  const virtualCapital = accounts.flatMap(account => account.virtual_capital || []);
  const positions = accounts.flatMap(account => account.positions || []);
  const totalEquity = virtualCapital.reduce((sum, item) => sum + Number(item.equity || 0), 0);
  const totalAvailable = virtualCapital.reduce((sum, item) => sum + Number(item.risk_available || 0), 0);
  const modeLabel = data.execution_mode === 'SHADOW' ? '影子模式已启用' : data.execution_mode === 'LIVE' ? '生产团队 · 影子数据独立' : '安全配置中';
  const readinessSteps = shadowReadinessSteps(activation);
  const firstPendingStep = readinessSteps.find(item => !item.complete);
  const canOpenFirstPending = firstPendingStep?.href
    && (!firstPendingStep.capability || hasCapability(firstPendingStep.capability));
  let activationTitle;
  let activationCopy;
  let activationAction;
  if (data.execution_mode === 'SETUP') {
    activationTitle = activation.ready
      ? '影子模式前置条件已满足'
      : `${activation.blockers.length} 项条件仍需完成`;
    activationCopy = activation.ready
      ? '全部服务端前置条件已满足；仍需团队管理员明确启用，系统不会自动打开业务能力。'
      : '按下面的依赖顺序处理。每项都标明影响、责任角色和下一步；缺少任何一项都会保持交易关闭。';
    activationAction = activation.ready && activation.can_activate
      ? '<button class="primary" data-activate-shadow>明确进入影子模式</button>'
      : canOpenFirstPending
        ? `<a class="primary" href="${firstPendingStep.href}" data-link>${escapeHtml(firstPendingStep.action)}</a>`
        : `<span class="status-pill">${activation.can_activate ? '等待前置条件' : '由团队管理员启用'}</span>`;
  } else if (data.execution_mode === 'SHADOW') {
    activationTitle = !virtualCapital.length
      ? '影子模式已隔离启用；下一步初始化虚拟资金'
      : !campaigns.length
        ? '虚拟资金已建立；运行第一笔完整影子流程'
        : `${campaigns.length} 个影子交易任务已进入可复盘链路`;
    activationCopy = '当前团队只允许 SHADOW 对象；真实订单、资金、签名和广播仍由服务端双重阻断。';
    activationAction = !virtualCapital.length && accounts.some(account => account.can_initialize)
      ? '<a class="primary" href="#shadow-accounts">初始化虚拟资金</a>'
      : !campaigns.length && hasCapability('proposal.create')
        ? '<a class="primary" href="/proposals/new?environment=SHADOW" data-link>创建第一笔影子提案</a>'
        : campaigns.length && hasCapability('results.view')
          ? '<a class="primary" href="/results?environment=SHADOW" data-link>查看影子报表</a>'
          : '<span class="status-pill status-APPROVED">隔离边界已生效</span>';
  } else {
    activationTitle = firstPendingStep
      ? `生产团队仍有 ${activation.blockers.length} 项影子准备度缺口`
      : '生产团队的影子数据保持独立';
    activationCopy = firstPendingStep
      ? '缺口不会放宽生产或影子边界；按依赖顺序补齐后再运行完整模拟链路。'
      : '生产与影子事实按环境、团队、账户、场所和标的分别存储；影子操作不会成为真实发送输入。';
    activationAction = firstPendingStep && canOpenFirstPending
      ? `<a class="primary" href="${firstPendingStep.href}" data-link>${escapeHtml(firstPendingStep.action)}</a>`
      : firstPendingStep
        ? `<span class="status-pill">由${escapeHtml(firstPendingStep.owner)}处理</span>`
        : hasCapability('results.view')
          ? '<a class="secondary" href="/results?environment=SHADOW" data-link>查看影子报表</a>'
          : '<span class="status-pill status-APPROVED">隔离边界已生效</span>';
  }
  const accountCards = accounts.map(account => {
    const capital = account.virtual_capital || [];
    const accountPositions = account.positions || [];
    const instrumentOptions = (account.instruments || []).map(instrument => `<option value="${escapeHtml(instrument.instrument_id)}" data-currency="${escapeHtml(instrument.currency)}">${escapeHtml(instrument.symbol)} · ${escapeHtml(instrument.currency)}</option>`).join('');
    const capitalFacts = capital.length
      ? capital.map(item => `<dl class="definition-grid shadow-capital-grid">${definition('虚拟净值', fmtAmount(item.equity, item.currency))}${definition('未占用风险容量', fmtAmount(item.risk_available, item.currency))}${definition('风险占用', fmtAmount(account.occupied_risk, item.currency))}${definition('事实状态', `${fmtStatus(item.fact_status)} · ${item.control_status === 'CONTROLLED' ? '仅模拟可控' : fmtStatus(item.control_status)}`)}</dl>`).join('')
      : '<p class="safety-note">尚未初始化虚拟资金。缺少这项事实时，服务端会拒绝影子风险占用。</p>';
    const positionRows = accountPositions.length
      ? `<div class="shadow-position-list">${accountPositions.map(item => `<div><span><b>${escapeHtml(item.symbol)}</b><small>${shortId(item.position_id)} · ${fmtDate(item.observed_at)}</small></span><strong>${fmtNumber(item.quantity)}</strong></div>`).join('')}</div>`
      : '<p class="subtle">尚无已初始化的影子标的。</p>';
    const initializeForm = account.can_initialize && instrumentOptions
      ? `<form class="shadow-scope-form action-panel" data-account-id="${escapeHtml(account.account_id)}" data-venue="${escapeHtml(account.venue)}" data-has-capital="${capital.length ? 'true' : 'false'}"><h3>${capital.length ? '增加影子标的' : '初始化虚拟资金与标的'}</h3><div class="field-grid"><label>模拟标的<select name="instrument_id" required>${instrumentOptions}</select></label>${capital.length ? '' : '<label>初始虚拟资金<input name="initial_equity" type="number" step="any" min="0.00000001" value="10000" required></label>'}</div><p class="microcopy">只创建 SHADOW 权益与空仓事实；不会验证、读取或使用账户 API Key。</p><div class="form-error" role="alert"></div><button class="secondary">${capital.length ? '增加标的' : '建立影子范围'}</button></form>`
      : `<p class="safety-note">${!account.can_initialize ? '当前身份没有该账户的管理权限。' : '当前交易所没有可用的 U 本位 Instrument Catalog 标的。'}</p>`;
    return `<article class="card shadow-account-card"><div class="card-heading"><div><p class="eyebrow">${escapeHtml(account.venue)} · ${escapeHtml(account.account_id)}</p><h2>${escapeHtml(account.label)}</h2></div><span class="status-pill">凭据${account.credential_state === 'UNCONFIGURED' ? '未配置' : '已脱敏'}</span></div>${capitalFacts}<div class="card-heading compact-heading"><div><h3>虚拟仓位</h3></div><span class="status-pill">${accountPositions.length} 个标的</span></div>${positionRows}${initializeForm}</article>`;
  }).join('');
  const campaignRows = campaigns.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td data-label="影子任务"><b>${escapeHtml(item.symbol || shortId(item.instrument_id))}</b><br><a class="row-link" href="/campaigns/${item.campaign_id}" data-link>${shortId(item.campaign_id)} · 查看完整链路</a></td><td data-label="范围">${escapeHtml(item.account_id)}<br><span class="subtle">${escapeHtml(item.venue)}</span></td><td data-label="状态"><span class="status-pill status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</span></td><td data-label="虚拟盈亏">${escapeHtml(campaignPnlLabel(item, item.final_pnl))}</td><td data-label="更新时间">${fmtDate(item.updated_at)}</td></tr>`).join('');
  main.innerHTML = `<section class="page shadow-workspace"><header class="page-head"><div><p class="eyebrow">虚拟资金 · 严格隔离</p><h1>影子模式</h1><p class="lede">用当前团队权限、提案、独立审核、风控、执行与报表链路运行模拟交易。全部事实固定为 SHADOW，任何操作都不调用真实订单、资金、签名或广播连接器。</p></div><span class="status-pill ${data.execution_mode === 'SHADOW' ? 'status-APPROVED' : ''}">${escapeHtml(modeLabel)}</span></header>
    <article class="home-status tone-${activation.ready ? 'success' : 'attention'}"><div><p class="eyebrow">团队启用状态</p><h2>${escapeHtml(activationTitle)}</h2><p>${escapeHtml(activationCopy)}</p></div>${activationAction}</article>
    <article class="card shadow-readiness-card" data-shadow-readiness><div class="card-heading"><div><p class="eyebrow">团队启用路径</p><h2>服务端前置条件</h2></div><span class="status-pill ${activation.ready ? 'status-APPROVED' : 'status-BLOCKED'}">${activation.ready ? '已满足' : `${activation.blockers.length} 项阻断`}</span></div><ol class="readiness-list">${readinessSteps.map(shadowReadinessItem).join('')}</ol><p class="microcopy">此清单直接投影 <code translate="no">/api/shadow</code> 返回的阻断码；页面不自行放宽条件。通知路由可独立配置，但不是当前影子启用的服务端门槛。</p></article>
    <div class="stats"><div class="stat"><small>虚拟净值（USD 稳定币）</small><b>${virtualCapital.length ? fmtNumber(totalEquity) : '未初始化'}</b></div><div class="stat"><small>未占用风险容量</small><b>${virtualCapital.length ? fmtNumber(totalAvailable) : '—'}</b></div><div class="stat"><small>影子标的</small><b>${positions.length}</b></div><div class="stat"><small>影子交易任务</small><b>${campaigns.length}</b></div></div>
    <article class="card shadow-safety-card"><div class="card-heading"><div><p class="eyebrow">不可穿透边界</p><h2>真实危险能力全部为关闭</h2></div><span class="status-pill status-APPROVED">VIRTUAL_ONLY</span></div><div class="shadow-safety-grid"><span><b>真实下单</b><small>关闭</small></span><span><b>资金划转</b><small>关闭</small></span><span><b>签名</b><small>关闭</small></span><span><b>广播</b><small>关闭</small></span><span><b>交易所连接器</b><small>本流程不调用</small></span></div><p class="safety-note">服务端以团队执行模式和每个对象的 environment 双重校验；前端状态仅用于说明，不替代拒绝。</p></article>
    <div class="section-head" id="shadow-accounts"><div><p class="eyebrow">账户隔离</p><h2>虚拟资金与仓位范围</h2></div><a class="secondary" href="/venues" data-link>管理交易账户</a></div><div class="shadow-account-grid">${accountCards || '<section class="empty-state"><div><h2>当前没有可见交易账户</h2><p>先在当前团队登记一个账户。影子初始化只引用账户范围，不需要也不会读取明文凭据。</p><a class="primary" href="/venues" data-link>登记交易账户</a></div></section>'}</div>
    <div class="section-head"><div><p class="eyebrow">完整流程</p><h2>影子交易任务</h2></div><div class="toolbar"><a class="secondary" href="/proposals?environment=SHADOW" data-link>查看影子提案</a>${hasCapability('proposal.create') ? '<a class="primary" href="/proposals/new?environment=SHADOW" data-link>创建影子提案</a>' : ''}</div></div>${campaignRows ? `<div class="table-wrap campaign-list-table"><table><thead><tr><th>影子任务</th><th>范围</th><th>状态</th><th>虚拟盈亏</th><th>更新时间</th></tr></thead><tbody>${campaignRows}</tbody></table></div>` : '<section class="empty-state compact-empty-state"><div><h2>尚无影子交易任务</h2><p>先初始化虚拟资金，再创建 SHADOW 提案并完成独立审核、风险检查和短期授权。</p></div></section>'}</section>`;
  document.querySelector('[data-activate-shadow]')?.addEventListener('click', async event => {
    await withPending(event.currentTarget, '启用中…', async () => {
      try {
        const result = await api(`/api/teams/${data.team_id}/shadow-activation`, {method:'POST', body:JSON.stringify({expected_version:data.version, idempotency_key:crypto.randomUUID()})});
        session = result.session;
        setShell(true);
        showToast('当前团队已明确进入影子模式；真实能力保持关闭');
        await route();
      } catch (error) { showApiError(error); }
    });
  });
  document.querySelectorAll('.shadow-scope-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    const option = form.elements.instrument_id.selectedOptions[0];
    const payload = {account_id:form.dataset.accountId, venue:form.dataset.venue, instrument_id:values.instrument_id, currency:option.dataset.currency, initial_equity:form.dataset.hasCapital === 'true' ? null : values.initial_equity, idempotency_key:crypto.randomUUID()};
    await withPending(event.submitter, '初始化中…', async () => {
      try { await api('/api/shadow/scopes', {method:'POST', body:JSON.stringify(payload)}); showToast('虚拟资金与空仓事实已写入影子范围'); await route(); }
      catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  }));
  bindLinkedRows();
}

function systemHealthCard({title, status, copy, tone = 'success', meta = ''}) {
  return `<article class="system-health-card tone-${tone}"><div class="system-health-head"><span class="health-indicator" aria-hidden="true"></span><div><small>${escapeHtml(title)}</small><h2>${escapeHtml(status)}</h2></div></div><p>${escapeHtml(copy)}</p>${meta ? `<span class="system-health-meta">${escapeHtml(meta)}</span>` : ''}</article>`;
}

async function renderSystemStatus() {
  const canViewOperations = hasCapability('operations.view');
  const canViewVenues = hasCapability('venue.view');
  const healthRequest = api('/health/ready').then(() => ({ready:true})).catch(error => ({ready:false, error}));
  const opportunityHealthRequest = hasCapability('opportunity.view')
    ? api('/api/opportunities').catch(error => ({error}))
    : Promise.resolve(null);
  const [health, control, campaignsResponse, exceptionsResponse, runtime, freqtrade, opportunityHealth] = await Promise.all([
    healthRequest,
    api('/api/risk-controls').catch(error => ({error})),
    canViewOperations ? api('/api/campaigns') : Promise.resolve({data:[], as_of:null, restricted:true}),
    canViewOperations ? api('/api/campaign-exceptions') : Promise.resolve({data:[], as_of:null, restricted:true}),
    api('/api/runtime/status').catch(error => ({error})),
    api('/api/execution/freqtrade/status').catch(error => ({error})),
    opportunityHealthRequest,
  ]);
  const campaigns = campaignsResponse.data.filter(item => item.status !== 'CLOSED' && item.environment === 'LIVE');
  const details = await Promise.all(campaigns.map(item => api(`/api/campaigns/${item.campaign_id}`)));
  const liveCampaignIds = new Set(campaigns.map(item => item.campaign_id));
  const exceptions = exceptionsResponse.data.filter(item => liveCampaignIds.has(item.campaign_id));
  const codes = new Set(exceptions.map(item => item.code));
  const unknownIntents = details.flatMap(item => item.intents).filter(item => item.status === 'UNKNOWN').length;
  const dispatchingIntents = details.flatMap(item => item.intents).filter(item => item.status === 'DISPATCHING').length;
  const protectionIssues = exceptions.filter(item => item.code.startsWith('PROTECTION_'));
  const exposureIssues = exceptions.filter(item => ['POSITION_UNKNOWN','POSITION_STALE','RISK_RESERVATION_UNKNOWN'].includes(item.code));
  const reconciliationIssues = exceptions.filter(item => item.code.startsWith('RECONCILIATION_'));
  const controlAvailable = !control.error;
  const policy = control.policy || {system_state:'UNKNOWN', version:'—'};
  const gate = control.auto_add_gate || {status:'UNKNOWN', version:'—'};
  const restoreConditions = control.restore_conditions || {ready:false, blockers:[], checks:[]};
  const blockedRiskChecks = (restoreConditions.checks || []).filter(check => check.status === 'BLOCKED');
  const blockedRiskScopes = [...new Map(blockedRiskChecks
    .filter(check => check.scope?.venue)
    .map(check => [`${check.scope.environment}:${check.scope.account_id}:${check.scope.venue}`, check.scope])).values()];
  const blockedRiskScopeLabels = blockedRiskScopes.map(scope => fmtVenueLabel(scope.venue));
  const entryOpen = controlAvailable && policy.system_state === 'NORMAL' && restoreConditions.ready;
  const addOpen = entryOpen && gate.status === 'ENABLED';
  const entryStatus = !controlAvailable
    ? '风险政策未配置'
    : policy.system_state !== 'NORMAL'
      ? riskControlStatusLabel(policy.system_state)
      : !restoreConditions.ready
        ? blockedRiskScopeLabels.length
          ? `${blockedRiskScopeLabels.length} 个生产范围受阻`
          : '实时安全条件未通过'
        : addOpen
          ? '开仓与加仓逐笔检查'
          : '逐笔开仓可检查';
  const entryCopy = !controlAvailable
    ? '缺少当前风险政策或自动加仓控制，系统会阻止新增风险。'
    : policy.system_state !== 'NORMAL'
      ? `风险政策：${riskControlStatusLabel(policy.system_state)}；自动加仓：${riskControlStatusLabel(gate.status)}。`
      : !restoreConditions.ready
        ? `风险政策正常，但${blockedRiskScopeLabels.length ? ` ${blockedRiskScopeLabels.join('、')} 的` : ''}实时安全条件未通过；通过检查的范围仍需逐笔复核。自动加仓${riskControlStatusLabel(gate.status)}。`
        : `实时安全条件全部通过；每笔开仓仍由服务端重新检查。自动加仓${riskControlStatusLabel(gate.status)}。`;
  const perptape = runtime?.data?.external_boundaries?.perptape || {configured:false,status:'NOT_CONFIGURED',candidate_count:0,last_fetched_at:null,contract_version:'—'};
  const notilt = runtime?.data?.external_boundaries?.notilt || {enabled:false,gateway_available:false,configured_chains:[]};
  const telegram = runtime?.data?.external_boundaries?.telegram || {enabled:false,network_configured:false,polling:{state:'DISABLED'}};
  const telegramPolling = telegram.polling || {state:'DISABLED',last_error_code:null,last_success_at:null};
  const telegramHealthy = telegram.enabled && telegram.network_configured && telegramPolling.state === 'HEALTHY';
  const telegramFailureCopy = ({
    TELEGRAM_POLLING_CONFLICT:'另一个 Bot 实例正在使用同一长轮询；只保留一个生产轮询进程后重试。',
    TELEGRAM_BOT_API_CONFLICT:'Telegram 机器人接口报告会话冲突；检查是否存在另一轮询或 webhook 实例。',
    TELEGRAM_AUTH_FAILED:'Telegram 机器人接口拒绝当前凭据；由系统管理员核对机器人配置。',
    TELEGRAM_RATE_LIMITED:'Telegram 机器人接口正在限流；系统会按有界退避自动重试。',
    TELEGRAM_NETWORK_UNAVAILABLE:'当前无法连接 Telegram 机器人接口；网页端审核队列仍可使用。',
    TELEGRAM_RESPONSE_INVALID:'Telegram 机器人接口返回了无法采信的响应；机器人动作保持关闭。',
    TELEGRAM_BOT_API_REJECTED:'Telegram 机器人接口拒绝轮询请求；由系统管理员检查机器人运行实例。',
  })[telegramPolling.last_error_code] || '机器人尚未完成一次成功轮询；网页端审核队列仍是权威入口。';
  const telegramStatus = telegramHealthy
    ? '通知可用'
    : telegramPolling.state === 'DEGRADED'
      ? '通知受阻'
      : telegram.enabled
        ? '等待首次轮询'
        : '尚未启用';
  const connections = runtime?.data?.connections || {};
  const perptapeAvailable = Boolean(connections.PERPTAPE?.available);
  const notiltConfigured = Boolean(connections.NOTILT?.available);
  const perptapeStatus = perptapeAvailable
    ? '数据可用'
    : perptape.status === 'STALE'
      ? '数据已过期'
      : perptape.status === 'WAITING'
        ? '等待首次同步'
        : perptape.status === 'ON_DEMAND'
          ? '按需读取，尚未验证'
          : perptape.configured
            ? '连接状态未知'
            : '尚未配置';
  const perptapeTone = perptapeAvailable ? 'success' : perptape.configured ? 'attention' : 'danger';
  const accountBoundWorkers = Array.isArray(freqtrade?.account_bindings) ? freqtrade.account_bindings : [];
  const configuredWorkers = accountBoundWorkers.filter(worker => worker.configured);
  const verifiedWorkers = configuredWorkers.filter(worker => worker.status === 'VERIFIED');
  const workersReady = freqtrade?.backend === 'FREQTRADE'
    && freqtrade?.workers_enabled === true
    && configuredWorkers.length > 0
    && verifiedWorkers.length === configuredWorkers.length;
  const workersDisabled = freqtrade?.workers_enabled === false;
  const configuredVenueCounts = configuredWorkers.reduce((counts, worker) => {
    counts[worker.venue] = Number(counts[worker.venue] || 0) + 1;
    return counts;
  }, {});
  const configuredVenueSummary = Object.entries(configuredVenueCounts)
    .map(([venue, count]) => `${fmtVenueLabel(venue)} ${count}`)
    .join('、');
  const executionCopy = workersReady
    ? `${verifiedWorkers.length} 个精确账户 Worker 已通过最近一次验证${configuredVenueSummary ? `：${configuredVenueSummary}` : ''}。`
    : workersDisabled
      ? configuredWorkers.length
        ? `已保存 ${configuredWorkers.length} 个精确账户 Worker 绑定${configuredVenueSummary ? `（${configuredVenueSummary}）` : ''}；当前进程安全开关关闭，不会连接 Worker 或发送订单。`
        : '当前没有已配置的精确账户 Freqtrade Worker；执行进程安全开关保持关闭。'
    : freqtrade?.error
      ? friendlyApiError(freqtrade.error)
      : configuredWorkers.length
        ? `${verifiedWorkers.length} / ${configuredWorkers.length} 个精确账户 Worker 已验证；未验证绑定禁止执行。`
        : '当前没有可由执行进程加载的精确账户 Freqtrade Worker。';
  const tradingConnectionsReady = Boolean(connections.BINANCE?.available && connections.HYPERLIQUID?.available);
  const activeMonitoring = campaigns.length > 0;
  const overallTone = !health.ready || !controlAvailable ? 'danger' : exceptions.length || !entryOpen || !perptapeAvailable || !workersReady || !tradingConnectionsReady || !telegramHealthy ? 'attention' : activeMonitoring ? 'success' : 'neutral';
  const monitoringCards = canViewOperations ? [
    systemHealthCard({title:'减仓与退出', status:!activeMonitoring ? '当前无运行中任务' : unknownIntents ? '部分交易任务需要先对账' : dispatchingIntents ? '原派发等待查询确认' : '路径可用', tone:!activeMonitoring ? 'neutral' : (unknownIntents || dispatchingIntents) ? 'attention' : 'success', copy:!activeMonitoring ? '当前没有需要减仓或退出的交易任务。' : unknownIntents ? `${unknownIntents} 个订单结果未知，相关交易任务禁止重复动作；其他已知仓位仍可减仓或退出。` : dispatchingIntents ? `${dispatchingIntents} 个订单已持久派发，只允许查询原结果，不会再次发送。` : '即使新增风险受限，受控减仓与退出仍然可用。', meta:`${campaigns.length} 个运行中交易任务`}),
    systemHealthCard({title:'止损与保护监控', status:!activeMonitoring ? '当前无监控对象' : protectionIssues.length ? `${protectionIssues.length} 项需要处理` : '监控正常', tone:!activeMonitoring ? 'neutral' : protectionIssues.length ? 'danger' : 'success', copy:!activeMonitoring ? '有交易任务进入持仓后，系统会持续检查止损和保护覆盖。' : protectionIssues.length ? '检测到保护缺失、过期、未知或覆盖不足。' : '运行中的交易任务没有保护异常。', meta:`数据截止 ${fmtDate(exceptionsResponse.as_of)}`}),
    systemHealthCard({title:'风险敞口监控', status:!activeMonitoring ? '当前无监控对象' : exposureIssues.length ? `${exposureIssues.length} 项敞口不确定` : '监控正常', tone:!activeMonitoring ? 'neutral' : exposureIssues.length ? 'danger' : 'success', copy:!activeMonitoring ? '有交易任务进入运行后，系统会检查仓位和风险占用。' : exposureIssues.length ? '仓位或风险占用存在未知或过期数据，系统会阻止新增风险。' : '当前没有仓位未知、仓位过期或风险占用未知。', meta:`${exceptions.length} 项总阻断`}),
    systemHealthCard({title:'对账监控', status:!activeMonitoring ? '暂无对账对象' : reconciliationIssues.length ? `${reconciliationIssues.length} 项未一致` : '对账一致', tone:!activeMonitoring ? 'neutral' : reconciliationIssues.length ? 'attention' : 'success', copy:!activeMonitoring ? '当前没有运行中的交易任务需要对账。' : reconciliationIssues.length ? '至少一个权限范围存在差异、未知、过期或需要人工处理。' : '运行中的交易任务没有派生对账异常。', meta:'只有计算结果为“对账一致”才可作为恢复依据'}),
  ] : [
    systemHealthCard({title:'交易任务监控', status:'当前身份不读取任务详情', tone:'neutral', copy:'系统状态仍展示风险政策、外部连接、执行底座和通知健康；运行任务、保护与对账详情由交易运维人员查看。', meta:'未读取任务数据，不能据此判断任务数量或异常数量'}),
  ];
  const cards = [
    systemHealthCard({title:'核心服务', status:health.ready ? '服务可用' : '服务不可用', tone:health.ready ? 'success' : 'danger', copy:health.ready ? '业务数据库和交易服务运行正常。' : '核心服务检查失败；不能把缺失响应当成正常。', meta:'数据缺失时自动阻止交易'}),
    systemHealthCard({title:'开仓与加仓', status:entryStatus, tone:entryOpen ? (addOpen ? 'success' : 'attention') : 'danger', copy:entryCopy, meta:restoreConditions.ready ? '每笔新增风险仍会重新检查账户、交易所与授权' : `${restoreConditions.blockers?.length || blockedRiskChecks.length} 项实时条件待处理；查看风险控制了解精确原因`}),
    ...monitoringCards,
    systemHealthCard({title:'交易执行底座', status:workersReady ? '精确账户 Worker 已验证' : workersDisabled ? 'Freqtrade 执行进程未启动' : 'Freqtrade 执行进程检查未通过', tone:workersReady || workersDisabled ? 'attention' : 'danger', copy:executionCopy, meta:workersReady ? (freqtrade.live_order_send ? '真实订单发送已启用；账户资格、风险、授权与发送者租约仍会逐项复核' : '精确账户验证已通过；LIVE_ORDER_SEND 保持关闭') : workersDisabled ? `${configuredWorkers.length ? '数据库绑定已保留；' : ''}FREQTRADE_WORKERS_ENABLED 与 LIVE_ORDER_SEND 保持关闭` : '身份、模式或精确账户绑定不一致时禁止发送'}),
    systemHealthCard({title:'Telegram 审核通知', status:telegramStatus, tone:telegramHealthy ? 'success' : 'attention', copy:telegramHealthy ? 'Telegram 私聊机器人最近一次长轮询成功；批准和拒绝仍需二次确认并写入统一审计。' : telegramFailureCopy, meta:telegramHealthy ? `最近成功 ${fmtDate(telegramPolling.last_success_at)}` : '网页端审核队列保持可用；资金、订单、风险开关与权限操作不对 Telegram 机器人开放'}),
    systemHealthCard({title:'Perptape 机会源', status:perptapeStatus, tone:perptapeTone, copy:perptapeAvailable ? `已读取 ${Number(opportunityHealth?.data?.length ?? perptape.candidate_count ?? 0)} 个候选，可用于机会筛选和提案。` : perptape.configured ? 'Perptape 已配置，但最近数据尚未形成可用连接结论。现有交易任务不受影响，新的外部机会不可用。' : 'Perptape 尚未配置；人工提案仍可使用。', meta:`只读 · 最近数据 ${fmtDate(perptape.last_fetched_at)}`}),
  ].join('');
  const connectionLabels = {
    BINANCE:['币安','生产账户','/venues?venue=BINANCE','查看账户数据 →'],
    HYPERLIQUID:['Hyperliquid','生产账户','/venues?venue=HYPERLIQUID','查看账户数据 →'],
    OKX:['OKX','生产账户','/venues?venue=OKX','查看账户数据 →'],
    BYBIT:['Bybit','生产账户','/venues?venue=BYBIT','查看账户数据 →'],
    PERPTAPE:['突破榜单','市场机会','/opportunities','查看机会 →'],
    NOTILT:['链上资金库','生产资金','/capital','查看资金 →'],
  };
  const connectionRows = Object.entries(connectionLabels).map(([key, label]) => {
    const state = connections[key] || {
      available:false,
      category:'NOT_YET_VERIFIED',
      reason:'尚无可核验的只读连接结论。',
      owner_role:key === 'NOTILT' ? '资金管理员' : '系统管理员',
      next_action:'启动只读同步并等待一次探针完成。',
      checked_at:null,
      write_process_enabled:false,
    };
    const destinationCapability = key === 'NOTILT'
      ? 'capital.view'
      : key === 'PERPTAPE'
        ? 'opportunity.view'
        : 'venue.view';
    const restrictedOwner = key === 'NOTILT'
      ? '由资金管理员处理'
      : key === 'PERPTAPE'
        ? '由机会创建者查看'
        : '由交易运维人员查看';
    const action = hasCapability(destinationCapability)
      ? `<a class="text-button" href="${label[2]}" data-link>${label[3]}</a>`
      : `<span class="subtle">${restrictedOwner}</span>`;
    const capability = fmtConnectionCapability(key, state);
    const categoryLabel = fmtConnectionCategory(state.category);
    const errorEvidence = state.error_code ? `<details class="venue-technical-detail"><summary>技术分类</summary><code translate="no">${escapeHtml(state.error_code)}</code></details>` : '';
    const probeEvidence = [
      `${currentLanguage === 'en' ? 'Latest probe: ' : '最近探针：'}${fmtDate(state.checked_at)}`,
      state.last_success_at ? `${currentLanguage === 'en' ? 'Latest success: ' : '最近成功：'}${fmtDate(state.last_success_at)}` : (currentLanguage === 'en' ? 'No successful probe yet' : '尚无成功记录'),
      state.retry_at ? `${currentLanguage === 'en' ? 'Next automatic retry: ' : '下次自动重试：'}${fmtDate(state.retry_at)}` : null,
    ].filter(Boolean).join(' · ');
    const ownership = currentLanguage === 'en'
      ? `Owner: ${translateEnglishText(state.owner_role)} · Next: ${fmtConnectionNextAction(state)}`
      : `负责：${state.owner_role} · 下一步：${fmtConnectionNextAction(state)}`;
    return `<tr><td data-label="数据源"><b>${label[0]}</b>${errorEvidence}</td><td data-label="读取状态与处理建议"><span class="status-pill ${state.available ? 'status-APPROVED' : ''}">${state.available ? (currentLanguage === 'en' ? 'Read-only connected' : '只读已连接') : escapeHtml(categoryLabel)}</span><br><span class="subtle">${escapeHtml(fmtConnectionReason(state))}</span><br><span class="subtle">${escapeHtml(probeEvidence)}</span><br><span class="subtle">${escapeHtml(ownership)}</span></td><td data-label="运行范围">${label[1]}</td><td data-label="可用能力">${escapeHtml(capability)}</td><td data-label="下一步">${action}</td></tr>`;
  }).join('');
  const availableSources = Object.keys(connectionLabels).filter(key => connections[key]?.available).length;
  const connectionSourceCount = Object.keys(connectionLabels).length;
  const executionVerdictTitle = !workersReady
    ? '只读控制台可用，但 Freqtrade 执行底座尚未就绪'
    : 'Freqtrade 执行底座已就绪，但交易所只读连接受限';
  const executionVerdictCopy = !workersReady
    ? `${workersDisabled ? 'Freqtrade 执行进程尚未启动' : 'Freqtrade 执行进程尚未通过检查'}；${!tradingConnectionsReady ? '至少一个交易所只读连接也受限' : '交易所只读连接正常'}。系统不会把页面可访问误报为可执行交易。`
    : '两个 Freqtrade 执行进程已通过仿真模式检查；至少一个交易所只读账户连接当前受限。真实下单继续关闭，系统不会把执行底座可用误报为生产交易就绪。';
  const riskVerdictTitle = policy.system_state === 'NORMAL'
    ? blockedRiskScopeLabels.length
      ? `核心服务可用，但 ${blockedRiskScopeLabels.join('、')} 的实时开仓条件受阻`
      : '核心服务可用，但实时开仓条件未全部通过'
    : `核心服务可用，但风险政策为${riskControlStatusLabel(policy.system_state)}`;
  const blockedRiskReasonCount = blockedRiskChecks.reduce((count, check) => count + new Set((check.reason || []).filter(reason => reason !== 'CURRENT')).size, 0);
  const riskVerdictCopy = blockedRiskChecks.length
    ? `${blockedRiskChecks.length} 个默认账户范围共有 ${blockedRiskReasonCount} 项实时条件未通过；请进入风险控制逐项查看原因、负责人和下一步。通过检查的范围仍需逐笔复核；自动加仓保持关闭。`
    : '风险政策或实时生产事实尚未满足新增风险条件；每笔请求都会继续由服务端拒绝或重新校验。';
  const verdictTitle = !health.ready ? '核心服务未通过就绪检查' : !controlAvailable ? '核心服务可用，但风险政策未配置' : exceptions.length ? '核心服务可用，但存在风险阻断' : !entryOpen ? riskVerdictTitle : !workersReady || !tradingConnectionsReady ? executionVerdictTitle : !telegramHealthy ? '交易管理可用，但 Telegram 审核通知受限' : !perptapeAvailable ? '交易管理可用，但 Perptape 机会源受限' : !canViewOperations ? '核心服务与可见连接状态正常' : activeMonitoring ? '交易系统正在正常监控' : '核心服务可用，当前无运行中交易任务';
  const verdictCopy = !health.ready ? '请先恢复数据库与服务状态，不要继续依赖旧数据。' : !controlAvailable ? `${friendlyApiError(control.error)} 新增风险保持关闭。` : exceptions.length ? `发现 ${exceptions.length} 项安全异常；受影响的新增风险会保持关闭。` : !entryOpen ? riskVerdictCopy : !workersReady || !tradingConnectionsReady ? executionVerdictCopy : !telegramHealthy ? `${telegramFailureCopy} 不影响网页端审核，也不会放宽任何审核或交易边界。` : !perptapeAvailable ? `${perptapeStatus}。现有交易任务仍可管理，但新的 Perptape 机会暂不可用。` : !canViewOperations ? '当前身份未读取交易任务、保护和对账详情；页面仅对已授权的系统事实给出结论。' : activeMonitoring ? '运行中的交易任务没有检测到保护、敞口或对账阻断。' : '当前没有需要监控的交易任务；系统不会把“无监控对象”误报为“监控正常”。';
  const verdictAction = exceptions.length && canViewOperations
    ? '<a class="primary" href="/campaigns/alerts" data-link>查看运行告警</a>'
    : !controlAvailable || !entryOpen
      ? '<a class="secondary" href="/risk" data-link>查看风险控制</a>'
      : (!workersReady || !tradingConnectionsReady) && canViewVenues
        ? '<a class="secondary" href="/venues" data-link>查看交易账户</a>'
        : !workersReady || !tradingConnectionsReady
          ? '<span class="status-pill">由系统管理员或交易运维人员处理</span>'
          : !telegramHealthy
            ? '<a class="secondary" href="/reviews" data-link>使用网页端审核</a>'
            : !perptapeAvailable && hasCapability('opportunity.view')
              ? '<a class="secondary" href="/opportunities" data-link>查看 Perptape</a>'
              : '<span class="status-pill status-APPROVED">无需立即动作</span>';
  main.innerHTML = `<section class="page system-status-page"><header class="page-head"><div><p class="eyebrow">交易系统状态</p><h1>系统状态</h1><p class="lede">这里直接说明系统能否工作、哪些能力受限，以及是否需要处理。绿色表示当前证据正常；黄色表示能力受限；红色表示必须先处理；灰色表示当前没有监控对象。</p></div><div class="toolbar"><button class="secondary" data-refresh>刷新状态</button><a class="secondary" href="/risk" data-link>查看风险控制</a></div></header>
    <article class="home-status tone-${overallTone}"><div><p class="eyebrow">当前结论</p><h2>${escapeHtml(verdictTitle)}</h2><p>${escapeHtml(verdictCopy)}</p></div>${verdictAction}</article>
    <div class="system-health-grid">${cards}</div>
    <section><div class="section-heading"><div><p class="eyebrow">外部数据连接</p><h2>生产数据与资金连接</h2></div><span class="status-pill">${availableSources} / ${connectionSourceCount} 可用</span></div><div class="table-scroll-hint connection-scroll-hint" data-table-hint>左右滑动查看完整连接状态</div><div class="table-wrap connection-status-table"><table><thead><tr><th>数据源</th><th>读取状态与处理建议</th><th>运行范围</th><th>可用能力</th><th></th></tr></thead><tbody>${connectionRows}</tbody></table></div></section>
    ${codes.size ? `<section><div class="section-heading"><div><p class="eyebrow">交易任务运行告警</p><h2>需要处理的问题类型</h2></div><a class="secondary" href="/campaigns/alerts" data-link>查看运行告警</a></div><div class="exception-code-list">${[...codes].sort().map(code => `<span>${escapeHtml(explainException(code).title)}</span>`).join('')}</div></section>` : ''}
  </section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
}

async function renderCampaignFacts(mode) {
  const details = mode === 'risk' && !hasCapability('operations.view')
    ? []
    : await loadCampaignDetails();
  let riskControls = null;
  if (mode === 'risk') {
    try {
      riskControls = await api('/api/risk-controls');
    } catch (error) {
      if (error.status !== 403) throw error;
    }
  }
  const titles = {orders:'订单与成交', risk:'风险与目标'};
  const visibleDetails = mode === 'risk' ? details.filter(item => item.status !== 'CLOSED') : details;
  let rows = '';
  if (mode === 'orders') rows = visibleDetails.flatMap(item => item.intents.map(intent => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(fmtIntentKind(intent.kind))}${intent.reduce_only ? ' · 只减仓' : ''}</td><td>${escapeHtml(fmtSide(intent.side))} ${fmtNumber(intent.quantity)}</td><td>${escapeHtml(fmtStatus(intent.status))}</td><td>${intent.order ? `${escapeHtml(intent.order.venue_order_id)} · ${escapeHtml(fmtStatus(intent.order.status))}` : '尚未记录交易所回执'}</td><td>${fmtDate(intent.updated_at)}</td></tr>`)).join('');
  if (mode === 'risk') rows = visibleDetails.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(fmtStatus(item.status))}</td><td>${item.reservations.map(r => `${escapeHtml(fmtStatus(r.status))} ${fmtNumber(r.amount)}`).join(' · ') || '无预留'}</td><td>${fmtNumber(item.current_target_quantity)} · v${item.target_version}</td><td>${escapeHtml(fmtStatus(item.target_urgency || '—'))}</td><td>${escapeHtml(item.reconciliation ? fmtStatus(item.reconciliation.status) : '未对账')}</td></tr>`).join('');
  const headers = mode === 'orders' ? '<th>交易任务</th><th>意图</th><th>方向 / 数量</th><th>状态</th><th>交易所订单</th><th>更新时间</th>' : '<th>交易任务</th><th>状态</th><th>风险预留</th><th>目标</th><th>紧迫度</th><th>对账</th>';
  const pageLede = mode === 'risk'
    ? '当前团队风险政策与每个生产账户范围分开判断；政策正常不代表所有账户都能新增风险。阻塞项会明确列出原因、负责人和下一步。'
    : '这里显示当前确认的数据；能够重新计算的汇总会按最新数据生成。';
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">生产交易数据</p><h1>${titles[mode]}</h1><p class="lede">${pageLede}</p></div></header>
    ${mode === 'risk' ? renderRiskControlPanel(riskControls) : ''}
    ${mode === 'risk' && roleNames().includes('SYSTEM_ADMIN') ? `<div class="form-panel compact-form"><h2>只允许收紧风险</h2><p class="safety-note">这些入口只能关闭自动加仓，或把系统切换为“仅允许减仓”；不能从这里恢复新增风险。</p><div class="toolbar"><button class="danger" data-disable-global-add ${riskControls?.auto_add_gate?.status === 'DISABLED' ? 'disabled title="自动加仓已经关闭"' : ''}>${riskControls?.auto_add_gate?.status === 'DISABLED' ? '自动加仓已关闭' : '关闭全局自动加仓'}</button><button class="danger" data-pause-new-risk ${riskControls?.policy?.system_state !== 'NORMAL' ? 'disabled title="新增风险已经暂停"' : ''}>${riskControls?.policy?.system_state !== 'NORMAL' ? '新增风险已暂停' : '暂停所有新增风险'}</button></div></div><div style="height:16px"></div>` : ''}
    ${rows ? `<div class="table-wrap"><table><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>` : mode === 'risk' ? (hasCapability('operations.view') ? '<section class="empty-state compact-empty-state"><div><h2>当前没有运行中的风险任务</h2><p>已结束任务不会占用当前风险工作区；可前往交易任务查看完整历史。</p><a class="secondary" href="/campaigns" data-link>查看交易任务</a></div></section>' : '') : '<section class="empty-state"><div><h2>当前没有可展示的数据</h2></div></section>'}</section>`;
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
  {key:'BINANCE', location_type:'VENUE', label:'Binance'},
  {key:'HYPERLIQUID', location_type:'VENUE', label:'Hyperliquid'},
  {key:'VAULT', location_type:'VAULT', label:'链上金库'},
];
const CAPITAL_CHART_RANGE_MAX = 1000;
const DIRECT_CAPITAL_PATHS = [
  {path:'VAULT_TO_BINANCE', from:'链上金库', to:'币安', badge:'二选一', action:'检查转入币安条件', copy:'选择 NoTilt Vault 或 Safe Spending Limits，再按对应额度规则转入币安。', steps:['选择链上金库','实时额度预检','人控确认','进入币安']},
  {path:'VAULT_TO_HYPERLIQUID', from:'链上金库', to:'Hyperliquid', badge:'二选一', action:'检查转入 Hyperliquid 条件', copy:'选择 NoTilt Vault 或 Safe Spending Limits，先到授权自有地址，再进入 Hyperliquid。', steps:['选择链上金库','额度预检','到达自有地址','合约入金']},
  {path:'BINANCE_TO_VAULT', from:'币安', to:'链上金库', badge:'二选一', action:'检查币安回流条件', copy:'回流到用户选择的 NoTilt Vault 或 Safe Smart Account。', steps:['提现预检','授权地址','目标入金','回执验证']},
  {path:'HYPERLIQUID_TO_VAULT', from:'Hyperliquid', to:'链上金库', badge:'二选一', action:'检查 Hyperliquid 回流条件', copy:'先从合约提回授权自有地址，再进入所选链上金库。', steps:['合约提现','授权地址','目标入金','回执验证']},
];
let capitalTrendVisibility = {BINANCE:true, HYPERLIQUID:true, VAULT:true, TOTAL:true};
let capitalChartRangeValue = CAPITAL_CHART_RANGE_MAX;
let capitalChartResizeObserver = null;
const OCCUPIED_CAPITAL_TRANSFER_STATUSES = new Set([
  'SOURCE_RESERVED', 'SUBMITTED', 'IN_FLIGHT', 'DESTINATION_CONFIRMED',
  'UNKNOWN', 'MANUAL_REQUIRED',
]);
async function renderCampaignDetail(id) {
  const item = await api(`/api/campaigns/${id}`);
  if (!['LIVE','SHADOW'].includes(item.environment)) {
    main.innerHTML = '<section class="page"><section class="empty-state"><div><h1>该交易任务不属于当前控制台</h1><p>这里只展示生产或严格隔离的影子交易任务。</p><a class="primary" href="/campaigns" data-link>返回交易任务</a></div></section></section>';
    return;
  }
  const canOperate = roleNames().includes('OPERATOR') || roleNames().includes('SYSTEM_ADMIN');
  const canRecordSyntheticFacts = canOperate && item.environment === 'SHADOW';
  const active = item.intents.find(intent => ['READY','DISPATCHING','SENT','PARTIALLY_FILLED','UNKNOWN'].includes(intent.status));
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
  const nextStep = campaignNextStep(item, active, {canOperate, canRecordSyntheticFacts, positionCurrent, hasPosition, flatKnown, protectionReady, reconciliationMatched, exitTerminal, riskClosable});
  const positionTruth = !item.position ? '未同步' : !positionCurrent ? '需要重新同步' : `${fmtStatus(item.position.fact_status)} · ${fmtNumber(item.position.quantity)}`;
  const protectionTruth = !positionCurrent ? '等待仓位同步' : !hasPosition ? '当前无仓位' : protectionReady ? `完整覆盖 · ${fmtNumber(item.protection.quantity)}` : item.protection ? fmtStatus(item.protection.status) : '尚无保护';
  const activeTruth = active ? `${fmtIntentKind(active.kind)} · ${fmtStatus(active.status)}` : '无进行中意图';
  const reconciliationTruth = item.reconciliation ? `${fmtStatus(item.reconciliation.status)} · ${fmtDate(item.reconciliation.completed_at)}` : '尚未运行';
  const shadowTools = canRecordSyntheticFacts
    ? `<details class="card operation-toolbox"><summary><span><b>模拟数据与维护工具</b><small>仅用于合成数据、盈亏与对账</small></span></summary><div class="toolbox-content">${nextStep.key === 'position' ? '' : positionFactForm(item)}${hasPosition && nextStep.key !== 'protection' ? protectionFactForm(item) : ''}<div class="toolbar"><button class="secondary" data-pnl>按当前数据刷新盈亏</button><button class="secondary" data-reconcile>重新运行对账</button></div><p class="safety-note">这些动作只写入本地模拟数据；不会连接交易所或发送真实订单。</p></div></details>`
    : '';
  const closedFlat = isClosedFlatCampaign(item);
  const pnlLabel = closedFlat ? '最终盈亏' : '当前总盈亏';
  const positionQuantityLabel = closedFlat ? `0（${localizedText('已平仓')}）` : item.position ? fmtNumber(item.position.quantity) : '未知';
  const averageEntryLabel = flatKnown ? '—（当前无仓位）' : item.position ? fmtNumber(item.position.average_entry_price) : '—';
  const protectionStateLabel = flatKnown ? '保护不适用（当前无仓位）' : item.protection ? fmtStatus(item.protection.status) : '尚无数据';
  const management = closedFlat ? '' : managementPanel(item, addCandidates, addCandidateError, canOperate, canAddNow, active, protectionReady, reconciliationMatched);
  main.innerHTML = `<section class="page campaign-detail"><header class="page-head"><div><p class="eyebrow">${escapeHtml(fmtEnvironment(item.environment, true))} · ${escapeHtml(item.venue)}</p><h1>${escapeHtml(item.instrument?.symbol || '交易任务')} ${shortId(item.campaign_id)}</h1><p class="lede"><b class="status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</b> · ${escapeHtml(fmtDirection(item.direction))} · ${closedFlat ? localizedText('已平仓') : `当前目标 ${fmtNumber(item.current_target_quantity)}`}</p></div><a class="secondary" href="${item.environment === 'SHADOW' ? '/shadow' : '/campaigns'}" data-link>返回${item.environment === 'SHADOW' ? '影子模式' : '交易任务'}</a></header>
    <article class="campaign-command tone-${nextStep.tone}"><div><p class="eyebrow">当前唯一推荐动作</p><h2>${escapeHtml(nextStep.title)}</h2><p>${escapeHtml(nextStep.copy)}</p></div><div class="campaign-command-action">${nextStep.action}<div class="form-error" id="campaign-action-error"></div></div></article>
    <div class="campaign-truth-grid"><div class="${item.position && !positionCurrent ? 'truth-danger' : ''}"><small>当前仓位</small><b>${escapeHtml(closedFlat ? localizedText('已平仓') : positionTruth)}</b><span>${item.position ? `${closedFlat ? '确认于' : '上次'} ${fmtDate(item.position.observed_at)}` : '等待交易所仓位数据'}</span></div><div class="${positionCurrent && hasPosition && !protectionReady ? 'truth-danger' : ''}"><small>原生保护</small><b>${escapeHtml(protectionTruth)}</b><span>${!hasPosition ? '当前无仓位，无需保护' : item.protection ? `触发价 ${fmtNumber(item.protection.trigger_price)} · ${fmtDate(item.protection.observed_at)}` : '有仓位时必须确认足额覆盖'}</span></div><div><small>进行中操作</small><b>${escapeHtml(activeTruth)}</b><span>${active ? `${fmtSide(active.side)} ${fmtNumber(active.quantity)} · ${shortId(active.intent_id)}` : '不会与新动作冲突'}</span></div><div class="${item.reconciliation && !reconciliationMatched ? 'truth-danger' : ''}"><small>最近对账</small><b>${escapeHtml(reconciliationTruth)}</b><span>${item.reconciliation?.differences?.length ? `${item.reconciliation.differences.length} 项差异待处理` : reconciliationMatched ? '晚于当前仓位与操作记录' : '需要在最新数据后重跑'}</span></div></div>
    <div class="stats"><div class="stat"><small>已实现盈亏</small><b>${escapeHtml(campaignPnlLabel(item, item.realized_pnl))}</b></div><div class="stat"><small>未实现盈亏</small><b>${escapeHtml(campaignPnlLabel(item, item.unrealized_pnl))}</b></div><div class="stat"><small>${pnlLabel}</small><b>${escapeHtml(campaignPnlLabel(item, item.final_pnl))}</b></div><div class="stat"><small>风险目标</small><b style="font-size:14px">${closedFlat ? localizedText('已平仓') : `${fmtNumber(item.current_target_quantity)} · ${escapeHtml(item.target_urgency ? fmtStatus(item.target_urgency) : '尚未设置')}`}</b></div></div>
    <div class="campaign-command-layout"><div class="stack"><article class="card"><div class="card-heading"><div><p class="eyebrow">执行记录</p><h2>订单操作与成交记录</h2></div><span class="status-pill">${item.intents.length} 个操作 · ${item.fills.length} 笔成交</span></div>${item.intents.length ? item.intents.map(intent => intentCard(intent, item.environment)).join('') : '<p class="subtle">尚无订单操作。</p>'}</article><article class="card"><div class="card-heading"><div><p class="eyebrow">仓位数据</p><h2>仓位与风险保护</h2></div><span class="status-pill ${protectionReady ? 'status-APPROVED' : positionCurrent && hasPosition ? 'status-DENY' : ''}">${!positionCurrent ? '仓位待同步' : hasPosition ? (protectionReady ? '保护完整' : '需要保护') : '当前无仓位'}</span></div><dl class="definition-grid spacious">${definition('仓位数量', positionQuantityLabel)}${definition('平均入场', averageEntryLabel)}${definition('标记价', item.position ? fmtNumber(item.position.mark_price) : '—')}${definition('仓位更新时间', fmtDate(item.position?.observed_at))}${definition('保护状态', protectionStateLabel)}${definition('保护数量', item.protection ? fmtNumber(item.protection.quantity) : '—')}${definition('保护触发价', item.protection ? fmtNumber(item.protection.trigger_price) : '—')}${definition('保护更新时间', fmtDate(item.protection?.observed_at))}</dl></article>${canCreatePositionAction ? `<article class="card risk-reduction-card"><div class="card-heading"><div><p class="eyebrow">降低风险</p><h2>减仓与退出随时可用</h2></div><span class="status-pill">只减险</span></div><p class="subtle">无论新增风险是否暂停，都可以把目标降到更小数量或 0；系统只生成只减仓操作。</p>${targetForm(item)}</article>` : ''}${shadowTools}</div>
      <aside class="stack"><article class="card"><div class="card-heading"><div><p class="eyebrow">风险目标</p><h2>${closedFlat ? '风险预留与平仓结果' : '风险预留与唯一目标'}</h2></div><span class="status-pill">版本 ${item.target_version}</span></div>${item.reservations.map(r => `<div class="callout"><b>${escapeHtml(fmtStatus(r.status))}</b> · ${fmtNumber(r.amount)} ${escapeHtml(item.instrument?.collateral_currency || '')}</div>`).join('') || '<p class="subtle">无风险预留。</p>'}<dl class="definition-grid">${definition(closedFlat ? '最终仓位' : '目标数量', closedFlat ? localizedText('已平仓') : fmtNumber(item.current_target_quantity))}${definition('紧迫度', item.target_urgency ? fmtStatus(item.target_urgency) : '尚未设置')}${definition('目标原因', fmtTargetReason(item.target_reason))}</dl></article>${management}<article class="card"><div class="card-heading"><div><p class="eyebrow">对账</p><h2>对账结论</h2></div><span class="status-pill ${reconciliationMatched ? 'status-APPROVED' : item.reconciliation ? 'status-DENY' : ''}">${escapeHtml(item.reconciliation ? fmtStatus(item.reconciliation.status) : '未运行')}</span></div>${item.reconciliation ? `<p class="subtle">完成于 ${fmtDate(item.reconciliation.completed_at)}</p>${item.reconciliation.differences.length ? `<ul class="exception-list">${item.reconciliation.differences.map(value => `<li>${escapeHtml(value)}</li>`).join('')}</ul>` : '<p class="success-note">订单、成交、仓位和风险保护当前一致。</p>'}` : '<p class="subtle">尚未运行对账；任何不确定结果都必须先对账。</p>'}</article></aside></div></section>`;
  bindCampaignActions(item, active);
}

function campaignNextStep(item, active, truth) {
  const canOperate = Boolean(truth.canOperate);
  const canRecordSyntheticFacts = Boolean(truth.canRecordSyntheticFacts);
  const venueFactsHref = `/venues?venue=${encodeURIComponent(item.venue)}`;
  const filledIntent = item.intents.some(intent => intent.status === 'FILLED');
  if (item.status === 'CLOSED') return {key:'done', tone:'success', title:'交易任务已完成并关闭', copy:'风险预留已释放，成交与对账记录保留在当前交易任务中。', action:'<a class="secondary" href="/campaigns" data-link>返回交易任务</a>'};
  if (active?.status === 'DISPATCHING') return {key:'dispatch', tone:'attention', title:'已持久派发，等待原结果确认', copy:'系统已冻结 Worker、账户版本和发送者范围；现在只查询同一派发，不会再次触发订单写入。', action:'<a class="secondary" href="/campaigns/alerts" data-link>查看派发告警</a><p class="microcopy">不要创建第二个意图；查询超时会转为结果未知并继续占用风险。</p>'};
  if (active?.status === 'UNKNOWN') return {key:'reconcile', tone:'danger', title:'结果不确定，先对账', copy:'风险继续占用，禁止重发、加仓或释放；先核对交易所订单、成交、仓位和保护。', action:canOperate ? '<button class="danger" data-reconcile>立即运行对账</button>' : '<p class="microcopy">等待交易运维人员运行对账。</p>'};
  if (active?.status === 'READY') return item.environment === 'LIVE'
    ? {key:'intent', tone:'attention', title:`等待${fmtIntentKind(active.kind)}发送`, copy:'实盘意图只能由受控发送进程在控制开关、短期授权和有效租约内推进；页面不会合成交易所回执。', action:'<a class="secondary" href="/campaigns/alerts" data-link>查看运行告警</a><p class="microcopy">若超过预期仍未推进，再按告警事实处理；不要重复创建意图。</p>'}
    : {key:'intent', tone:'attention', title:`记录${fmtIntentKind(active.kind)}发送结果`, copy:'当前只有这个意图可以推进；获取发送租约后记录模拟订单，不会连接交易所。', action:canOperate ? operationForm(active, item) : '<p class="microcopy">等待交易运维人员处理待发送意图。</p>'};
  if (active && ['SENT','PARTIALLY_FILLED'].includes(active.status)) return {key:'intent', tone:'attention', title:`确认${fmtIntentKind(active.kind)}成交结果`, copy:'先记录已确认成交，或在确实无法判断时标记为“结果未知”；不要创建第二个意图。', action:canOperate ? operationForm(active, item) : '<p class="microcopy">等待交易运维人员记录成交结果。</p>'};
  if (!truth.positionCurrent && filledIntent) return {key:'position', tone:'attention', title:'同步成交后的当前仓位', copy:'成交已经记录，但仓位数据早于最新成交或尚未确认；在此之前不能判断保护和下一步。', action:canRecordSyntheticFacts ? positionFactForm(item) : `<a class="secondary" href="${venueFactsHref}" data-link>查看交易账户</a><p class="microcopy">生产仓位只能来自交易所只读事实，不能在页面手工补写。</p>`};
  if (truth.hasPosition && !truth.protectionReady) return {key:'protection', tone:'danger', title:'先补齐足额原生保护', copy:'当前有仓位但保护缺失、未知或不足。优先确认保护；若无法保护，使用下方减仓或退出。', action:canRecordSyntheticFacts ? protectionFactForm(item) : '<a class="secondary" href="/campaigns/alerts" data-link>查看保护告警</a><p class="microcopy">生产保护只能来自受控执行与交易所事实，页面不会手工伪造。</p>'};
  if (!truth.reconciliationMatched) return {key:'reconcile', tone:'attention', title:'运行对账确认当前数据', copy:'只有意图、订单、成交、仓位和保护一致后，才适合继续管理或关闭交易任务。', action:canOperate ? '<button class="primary" data-reconcile>运行当前范围对账</button>' : '<p class="microcopy">等待交易运维人员运行对账。</p>'};
  if (truth.flatKnown && truth.exitTerminal && truth.riskClosable) return {key:'close', tone:'success', title:'仓位已清零，可以关闭交易任务', copy:'退出结果终结且对账一致；关闭后会释放剩余风险预留并把结果固定到审计记录。', action:canOperate ? '<button class="primary" data-close-campaign>关闭交易任务</button>' : '<p class="microcopy">等待交易运维人员关闭交易任务。</p>'};
  if (truth.flatKnown) return {key:'close-blocked', tone:'danger', title:'平仓事实仍缺少关闭证据', copy:'仓位虽然为 0，但退出意图或风险预留尚未终结。不要直接释放风险；先查看运行告警确认原因。', action:'<a class="secondary" href="/campaigns/alerts" data-link>查看运行告警</a>'};
  if (truth.hasPosition) return {key:'hold', tone:'success', title:'仓位已确认且保护完整', copy:'当前没有必须处理的异常。继续观察；需要时可使用下方减仓或退出，加仓仍需通过全部门控。', action:'<span class="status-pill status-APPROVED">当前无需动作</span>'};
  return {key:'reconcile', tone:'attention', title:'确认当前范围数据', copy:'当前没有可确认仓位；先运行对账，避免把缺失数据误认为已经平仓。', action:canOperate ? '<button class="primary" data-reconcile>运行当前范围对账</button>' : '<p class="microcopy">等待交易运维人员运行对账。</p>'};
}

function intentCard(intent, environment = 'SHADOW') { const dispatch = intent.dispatch ? `<p class="subtle">受控派发 · ${escapeHtml(intent.dispatch.backend)} · 账户版本 ${escapeHtml(intent.dispatch.account_version)} · ${fmtDate(intent.dispatch.started_at)}</p>` : ''; return `<div class="intent-row"><div><b>${escapeHtml(fmtIntentKind(intent.kind))} · ${escapeHtml(fmtSide(intent.side))} ${fmtNumber(intent.quantity)}</b><br><span class="subtle">${shortId(intent.intent_id)} · ${intent.reduce_only ? '只减仓' : '会增加风险'} · ${fmtDate(intent.updated_at)}</span></div><b class="status-${escapeHtml(intent.status)}">${escapeHtml(fmtStatus(intent.status))}</b></div>${dispatch}${intent.order ? `<p class="subtle">${escapeHtml(fmtEnvironment(environment, true))}订单 ${escapeHtml(intent.order.venue_order_id)} · 已成交 ${fmtNumber(intent.order.filled_quantity)} / ${fmtNumber(intent.order.ordered_quantity)}</p>` : ''}`; }

function operationForm(intent, item) { if (intent.status === 'DISPATCHING') return '<p class="safety-note">派发已经持久化：只允许查询原结果，不提供再次发送或释放按钮。</p>'; if (intent.status === 'UNKNOWN') return '<p class="safety-note">结果不确定：风险保持占用，不提供重发或释放按钮。必须先人工对账。</p>'; if (intent.status === 'READY') return `<form id="shadow-simulation-form" class="action-panel"><h3>运行确定性成交模拟</h3><p class="microcopy">按不利方向加入滑点、对齐价格步长并计入手续费；一次性生成模拟订单、成交、仓位、保护和虚拟净值变化。</p><div class="field-grid"><label>参考价格<input name="reference_price" type="number" step="any" min="0.00000001" value="${escapeHtml(formNumber(intent.limit_price || item.position?.mark_price))}" required></label><label>手续费（bps）<input name="fee_bps" type="number" step="0.01" min="0" max="100" value="4" required></label><label>不利滑点（bps）<input name="slippage_bps" type="number" step="0.01" min="0" max="500" value="2" required></label></div><div class="toolbar" style="margin-top:12px"><button class="primary">生成模拟成交</button></div><div class="form-error" role="alert"></div></form>`; return `<form id="fill-form" class="action-panel"><h3>记录已确认的模拟成交</h3><div class="field-grid"><label>成交编号<input name="venue_fill_id" value="fill-${crypto.randomUUID().slice(0,8)}" required></label><label>成交方向<select name="side"><option value="BUY" ${intent.side === 'BUY' ? 'selected' : ''}>买入</option><option value="SELL" ${intent.side === 'SELL' ? 'selected' : ''}>卖出</option></select></label><label>成交数量<input name="quantity" type="number" step="any" value="${escapeHtml(intent.quantity)}" required></label><label>成交价格<input name="price" type="number" step="any" required></label><label>手续费<input name="fee" type="number" step="any" value="0"></label><label>币种<input name="fee_currency" value="${escapeHtml(item.instrument?.collateral_currency || 'USDT')}"></label><label>滑点成本<input name="slippage_cost" type="number" step="any" value="0"></label></div><div class="toolbar" style="margin-top:12px"><button class="primary">确认并记录成交</button><button type="button" class="danger" data-unknown>结果无法确认</button></div></form>`; }

function positionFactForm(item) { return `<form id="position-form" class="action-panel"><h3>同步当前模拟仓位</h3><p class="microcopy">只录入已经确认的交易所数据；不确定时不要把数量填成 0。</p><div class="field-grid"><label>数量<input name="quantity" type="number" step="any" value="${escapeHtml(formNumber(item.position?.quantity, '0'))}" required></label><label>平均入场价<input name="average_entry_price" type="number" step="any" value="${escapeHtml(formNumber(item.position?.average_entry_price, '0'))}" required></label><label>标记价<input name="mark_price" type="number" step="any" value="${escapeHtml(formNumber(item.position?.mark_price))}" required></label></div><button class="secondary">确认并记录仓位</button></form>`; }

function protectionFactForm(item) { return `<form id="protection-form" class="action-panel"><h3>确认当前模拟保护</h3><div class="field-grid"><label>保护订单编号<input name="venue_order_id" value="${escapeHtml(item.protection?.venue_order_id || 'shadow-stop')}" required></label><label>保护数量<input name="quantity" type="number" step="any" value="${escapeHtml(formNumber(Math.abs(Number(item.position.quantity))))}" required></label><label>触发价<input name="trigger_price" type="number" step="any" value="${escapeHtml(formNumber(item.protection?.trigger_price))}" required></label><label>覆盖状态<select name="coverage"><option value="full">已知且完整</option><option value="degraded">已知但不足</option><option value="unknown">结果未知</option></select></label></div><button class="primary">确认保护数据</button></form>`; }

function targetForm(item) { return `<form id="target-form" class="action-panel"><h3>设定唯一减仓目标</h3><label>减仓后剩余数量<input name="target_quantity" type="number" step="any" min="0" max="${escapeHtml(Math.abs(Number(item.position.quantity)))}" required></label><label>处理速度<select name="urgency"><option value="NORMAL">常规</option><option value="URGENT" selected>紧急</option><option value="IMMEDIATE">立即</option></select></label><label>原因<input name="reason" value="人工降低当前风险" required></label><label>执行限价（Hyperliquid 必填）<input name="limit_price" type="number" step="any" min="0"></label><button class="primary">创建只减仓操作</button><button type="button" class="danger" data-auto-exit>评估失效价并退出</button></form>`; }

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
  document.querySelector('#shadow-simulation-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const payload = {...values, expected_version:Number(active.version), idempotency_key:crypto.randomUUID()};
    await withPending(event.submitter, '模拟中…', async () => {
      try { await api(`/api/intents/${active.intent_id}/shadow-simulations`, {method:'POST', body:JSON.stringify(payload)}); showToast('模拟成交、仓位、保护与虚拟净值已原子更新'); await route(); }
      catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
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
