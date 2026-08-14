let signalSourceCredentialReveal = null;

const canCreateOpportunityProposal = () => (
  hasCapability('proposal.create') && ['TESTNET','LIVE'].includes(currentWorkflowEnvironment())
);

function signalSourceCredentialLabel(source) {
  if (source.credential?.state === 'CONFIGURED') return `已加密 · v${source.credential.version} · ${source.credential.key_hint || '仅存脱敏指纹'}`;
  if (source.credential?.state === 'RUNTIME_FALLBACK') return '部署级兼容 Key · 尚未绑定空间凭据';
  return '未配置';
}

function signalSourceRuntimeLabel(state) {
  return ({
    WEBSOCKET_LIVE:'WebSocket 实时流', WEBSOCKET_STARTING:'WebSocket 启动中',
    POLLING_FALLBACK:'HTTPS 轮询回退', POLLING_ONLY:'HTTPS 定时轮询',
    WEBSOCKET_FAILED:'连续接入失败', POLLING_FAILED:'轮询失败',
    DISABLED:'已停用', WAITING:'等待首次运行事实',
  })[state] || state || '等待首次运行事实';
}

function signalSourceFreshnessLabel(state) {
  return ({SUCCESS:'当前新鲜', STALE:'已过期', WAITING:'等待首个快照', ON_DEMAND:'按需读取', NOT_CONFIGURED:'未配置', DISABLED:'已停用'})[state] || state || '无数据';
}

function signalSourceStatus(source) {
  if (!source.enabled) return {label:'已停用', tone:'status-DISABLED'};
  if (['WEBSOCKET_FAILED','POLLING_FAILED'].includes(source.runtime?.state) || (source.mode !== 'PERPTAPE' && source.health?.last_error_code)) return {label:'检查失败', tone:'status-REJECTED'};
  if (source.mode === 'PERPTAPE' && ['STALE','WAITING','NOT_CONFIGURED'].includes(source.perptape?.data_status)) return {label:'等待 / 降级', tone:'status-PENDING_REVIEW'};
  if (source.runtime?.state === 'POLLING_FALLBACK' || source.runtime?.state === 'WEBSOCKET_STARTING') return {label:'降级运行', tone:'status-PENDING_REVIEW'};
  return {label:'已启用', tone:'status-APPROVED'};
}

function signalSourceEditor(source) {
  const webhookField = source.mode === 'WEBHOOK'
    ? `<label>最大时效（秒）<input name="webhook_max_age_seconds" type="number" min="30" max="900" value="${escapeHtml(source.webhook.max_age_seconds)}" required></label>`
    : '<input name="webhook_max_age_seconds" type="hidden" value="300">';
  return `<details class="signal-source-editor"><summary>编辑配置</summary><form class="signal-source-edit-form" data-source-id="${escapeHtml(source.signal_source_id)}" data-version="${source.version}"><div class="field-grid"><label>名称<input name="name" value="${escapeHtml(source.name)}" minlength="2" maxlength="120" required></label>${webhookField}</div><p class="microcopy">这里只编辑 ${source.mode === 'WEBHOOK' ? 'Webhook 名称和签名时效' : 'Perptape 名称'}；启停与凭据轮换是独立的版本化操作。</p><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" disabled>保存修改</button></div></form></details>`;
}

function signalSourceRotation(source) {
  const isWebhook = source.mode === 'WEBHOOK';
  return `<details class="signal-source-editor"><summary>轮换${isWebhook ? '签名密钥' : ' API Key'}</summary><form class="signal-source-rotate-form" data-source-id="${escapeHtml(source.signal_source_id)}" data-source-name="${escapeHtml(source.name)}" data-source-mode="${escapeHtml(source.mode)}" data-version="${source.version}"><label>${isWebhook ? '新密钥（留空由服务端生成）' : '新 Perptape API Key'}<input name="secret" type="password" minlength="${isWebhook ? 32 : 8}" maxlength="512" autocomplete="new-password" ${isWebhook ? '' : 'required'}></label><p class="danger-note">保存后旧凭据立即失效。新值只在这次响应中显示一次，刷新或关闭后不再回显。</p><div class="form-error" role="alert"></div><div class="form-actions"><button class="danger">确认轮换</button></div></form></details>`;
}

function signalSourceCard(source) {
  const status = signalSourceStatus(source);
  const checked = source.health?.last_checked_at;
  const perptapeFacts = source.mode === 'PERPTAPE' ? `<div class="signal-source-specific"><h3>Perptape 运行事实</h3><dl class="definition-grid">${definition('轮询 / WebSocket', signalSourceRuntimeLabel(source.runtime?.state))}${definition('数据新鲜度', signalSourceFreshnessLabel(source.perptape?.data_status))}${definition('候选数量', String(source.perptape?.candidate_count || 0))}${definition('快照采集', source.perptape?.fetched_at ? fmtDate(source.perptape.fetched_at) : '尚无快照')}</dl>${source.runtime?.state === 'WEBSOCKET_LIVE' && source.perptape?.data_status === 'SUCCESS' ? '<a class="secondary" href="/opportunities" data-link>查看实时机会</a>' : source.perptape?.data_status === 'SUCCESS' ? '<a class="secondary" href="/opportunities" data-link>查看当前机会快照</a>' : '<span class="subtle">旧快照、失败或降级状态不会标记为实时。</span>'}</div>` : `<div class="signal-source-specific"><h3>Webhook 接收事实</h3><dl class="definition-grid">${definition('接收地址', source.webhook.endpoint_url)}${definition('最大时效', `${source.webhook.max_age_seconds} 秒`)}${definition('最近有效事件', source.webhook.last_valid_event_at ? fmtDate(source.webhook.last_valid_event_at) : '尚未收到')}</dl><p class="microcopy">签名、请求时间、Nonce、幂等、重放、大小和版本格式全部通过后才记为有效事件。</p></div>`;
  const actions = source.can_manage === false ? '' : '';
  return `<article class="card signal-source-card" data-source-id="${escapeHtml(source.signal_source_id)}"><div class="card-heading"><div><p class="eyebrow">${escapeHtml(source.mode === 'PERPTAPE' ? 'Perptape' : 'Webhook')} · 配置 v${source.version}</p><h2>${escapeHtml(source.name)}</h2></div><span class="status-pill ${status.tone}">${escapeHtml(status.label)}</span></div><dl class="signal-source-facts">${definition('凭据状态', signalSourceCredentialLabel(source))}${definition('最近检查', checked ? fmtDate(checked) : '尚未检查')}${definition('最近成功', source.health?.last_success_at ? fmtDate(source.health.last_success_at) : '尚无成功记录')}${definition('最近错误', source.health?.last_error_code || '无')}${definition('连续失败', String(source.health?.consecutive_failures || 0))}${definition('最近收到信号', source.signals?.last_received_at ? fmtDate(source.signals.last_received_at) : '尚未收到')}${definition('信号数量', String(source.signals?.count || 0))}${definition('更新人', source.updated_by_username || shortId(source.updated_by))}</dl>${perptapeFacts}<div class="signal-source-actions"><button class="secondary" data-test-source="${escapeHtml(source.signal_source_id)}" data-version="${source.version}" ${source.enabled ? '' : 'disabled title="启用后才能测试"'}>测试${source.mode === 'WEBHOOK' ? '接收配置' : '连接'}</button><button class="secondary" data-toggle-source="${escapeHtml(source.signal_source_id)}" data-source-name="${escapeHtml(source.name)}" data-version="${source.version}" data-enabled="${source.enabled}">${source.enabled ? '停用' : '启用'}</button>${source.mode === 'WEBHOOK' ? `<button class="danger" data-delete-source="${escapeHtml(source.signal_source_id)}" data-source-name="${escapeHtml(source.name)}" data-version="${source.version}">删除</button>` : ''}</div>${signalSourceEditor(source)}${signalSourceRotation(source)}${actions}</article>`;
}

function signalSourceCreatePanel(hasPerptape) {
  return `<details class="signal-source-create"><summary>＋ 新增信号源</summary><form id="signal-source-create-form" class="form-panel compact-form"><div class="card-heading"><div><p class="eyebrow">次级管理入口</p><h2>接入新的信号源</h2></div><span class="status-pill">服务端未保存</span></div><div class="field-grid"><label>类型<select name="mode"><option value="WEBHOOK">Webhook</option>${hasPerptape ? '' : '<option value="PERPTAPE">Perptape</option>'}</select></label><label>名称<input name="name" minlength="2" maxlength="120" placeholder="例如 TradingView BTC" required></label><label data-webhook-create>签名密钥（留空由服务端生成）<input name="webhook_secret" type="password" minlength="32" maxlength="512" autocomplete="new-password"></label><label data-webhook-create>最大时效（秒）<input name="webhook_max_age_seconds" type="number" min="30" max="900" value="300" required></label><label data-perptape-create hidden>Perptape API Key<input name="perptape_secret" type="password" minlength="8" maxlength="512" autocomplete="new-password"></label><label class="signal-source-checkbox"><input name="enabled" type="checkbox" checked><span>创建后立即启用</span></label></div><p class="safety-note">两类信号源可以同时启用。Webhook 只记录通过校验的信号；任何信号都只能形成待审核提案，不能绕过审核、风控、授权或交易安全开关。</p><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" disabled>创建信号源</button></div></form></details>`;
}

async function renderSignalSources() {
  const status = await api('/api/signal-sources');
  const sources = status.data || [];
  sources.forEach(source => { source.can_manage = Boolean(status.can_manage); });
  const canManage = Boolean(status.can_manage);
  const reveal = signalSourceCredentialReveal;
  signalSourceCredentialReveal = null;
  const credentialReveal = reveal ? `<article class="card signal-credential-reveal" role="status"><div><p class="eyebrow">仅显示一次</p><h2>${escapeHtml(reveal.name)} 的新${reveal.mode === 'WEBHOOK' ? '签名密钥' : ' API Key'}</h2><p>立即保存到受控密钥库；关闭或刷新后，TradingOPS 不再回显。</p><code data-one-time-secret>${escapeHtml(reveal.secret)}</code></div><div class="toolbar"><button class="secondary" data-copy-one-time-secret>复制</button><button class="secondary" data-dismiss-one-time-secret>我已保存</button></div></article>` : '';
  const cards = sources.map(signalSourceCard).join('');
  main.innerHTML = `<section class="page signal-sources-page"><header class="page-head"><div><p class="eyebrow">空间隔离 · ${sources.length} 个接入</p><h1>信号源设置</h1><p class="lede">配置当前空间的 Perptape 和独立 Webhook 接入。实时信号分别在 Perptape 与 Webhook 页面展示，不在设置页聚合或混排。</p></div><div class="toolbar"><button class="secondary" data-refresh>刷新服务端事实</button></div></header>${credentialReveal}<section><div class="section-heading"><div><p class="eyebrow">服务端配置真源</p><h2>当前接入的信号源</h2></div><span class="subtle">截止 ${fmtDate(status.as_of)}</span></div>${cards ? `<div class="signal-source-grid">${cards}</div>` : '<div class="callout tone-attention"><b>Fail closed：</b>当前空间尚无信号源，其他空间或部署默认值不会被当作当前配置。</div>'}</section>${canManage ? signalSourceCreatePanel(sources.some(item => item.mode === 'PERPTAPE')) : '<div class="callout"><b>只读：</b>当前身份可以查看服务端事实，但没有信号源管理权限。</div>'}</section>`;
  bindSignalSourceActions();
  enhanceRenderedPage();
}

const WEBHOOK_SIGNAL_BLOCKERS = Object.freeze({
  SIGNAL_SOURCE_UNAVAILABLE:'原始信号源不可用',
  SIGNAL_SOURCE_DELETED:'信号源已删除；仅保留历史',
  SIGNAL_SOURCE_DISABLED:'信号源已停用',
  SIGNAL_STALE:'信号已超过来源时效窗口',
  INSTRUMENT_UNAVAILABLE:'缺少精确交易所与币种目录匹配',
  RBAC_DENIED:'当前职责缺少提案权限',
});

function webhookSignalProposalLabel(item) {
  if (item.proposal) return `已创建 · ${fmtStatus(item.proposal.status)}`;
  if (item.proposal_eligibility === 'ELIGIBLE') return '可创建冻结提案';
  return '提案已阻断';
}

function webhookSignalCard(item, canPropose) {
  const directionClass = item.direction === 'LONG' ? 'direction-long' : 'direction-short';
  const sourceName = item.signal_source_name || shortId(item.signal_source_id);
  const instruments = item.matching_instruments || [];
  const options = instruments.map(instrument => `<option value="${escapeHtml(instrument.instrument_id)}">${escapeHtml(instrument.venue)} · ${escapeHtml(instrument.symbol)}</option>`).join('');
  const proposalStatus = webhookSignalProposalLabel(item);
  const blocker = item.proposal_blocker ? (WEBHOOK_SIGNAL_BLOCKERS[item.proposal_blocker] || item.proposal_blocker) : '无';
  const freshness = item.freshness?.status === 'CURRENT'
    ? `新鲜 · ${item.freshness.age_seconds} 秒`
    : `已过期 · ${item.freshness?.age_seconds ?? '—'} 秒`;
  const action = item.proposal
    ? `<a class="secondary" href="/proposals/${item.proposal.proposal_id}" data-link>查看提案 · ${escapeHtml(fmtStatus(item.proposal.status))}</a>`
    : canPropose && item.proposal_eligibility === 'ELIGIBLE' && options
      ? `<details class="operation-toolbox"><summary><span><b>手动创建冻结提案</b><small>仍需独立审核、服务端风控与交易授权</small></span><strong>展开</strong></summary><form class="toolbox-content signal-proposal-form" data-signal-event-id="${escapeHtml(item.signal_event_id)}"><div class="field-grid"><label>账户<input name="account_id" required></label><label>合约<select name="instrument_id">${options}</select></label><label>环境<input name="environment" value="${escapeHtml(currentWorkflowEnvironment())}" readonly aria-describedby="signal-environment-note"></label><label>风险档位<select name="risk_tier"><option value="LOW">低</option><option value="MEDIUM">中</option><option value="HIGH">高</option></select></label><label>数量<input name="quantity" type="number" step="any" min="0.000000000000000001" required></label><label>最大风险<input name="max_risk" type="number" step="any" min="0.000000000000000001" required></label><label>有效时间（分钟）<input name="expires_in_minutes" type="number" min="480" max="1440" value="480" required></label></div><p id="signal-environment-note" class="microcopy">环境由服务端按团队当前模式固化，不能在此切换。</p><label>人工判断理由<textarea name="rationale" minlength="10" rows="3" required></textarea></label><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">创建并提交独立审核</button></div></form></details>`
      : `<p class="safety-note">${escapeHtml(blocker === '无' ? '服务端未开放当前信号的提案操作。' : blocker)} 信号记录仍保留且不会触发自动审核或下单。</p>`;
  return `<article class="card webhook-signal-card" data-signal-direction="${escapeHtml(item.direction)}" data-signal-event-id="${escapeHtml(item.signal_event_id)}" data-signal-source-id="${escapeHtml(item.signal_source_id)}">
    <div class="webhook-signal-main">
      <div class="webhook-signal-identity"><div><h2>${escapeHtml(item.symbol)}</h2><span class="tag ${directionClass}">${escapeHtml(fmtDirection(item.direction))}</span></div><small>${escapeHtml(sourceName)}${item.signal_source_deleted ? ' · 已删除' : ''}</small></div>
      <dl class="webhook-signal-primary-facts">${definition('策略', item.strategy_id)}${definition('版本', item.strategy_version)}${definition('周期', item.timeframe || '未提供')}${definition('交易所', fmtVenueLabel(item.venue))}${definition('价格', item.reference_price == null ? '未提供' : fmtNumber(item.reference_price))}${definition('新鲜度', freshness)}</dl>
      <span class="status-pill webhook-proposal-state ${item.proposal_eligibility === 'ELIGIBLE' ? 'status-APPROVED' : item.proposal ? 'status-PENDING_REVIEW' : 'status-REJECTED'}">${escapeHtml(proposalStatus)}</span>
    </div>
    <div class="webhook-signal-meta"><span>发生时间 <b>${escapeHtml(fmtDate(item.occurred_at))}</b></span><span>接收时间 <b>${escapeHtml(fmtDate(item.received_at))}</b></span><span class="webhook-signal-integrity">${item.freshness?.status === 'CURRENT' ? '✓' : '⚠'} ${escapeHtml(blocker === '无' ? '来源校验通过' : blocker)}</span></div>
    ${action}
  </article>`;
}

function webhookSignalQuery(filters) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') params.set(key, value);
  });
  params.set('limit', '200');
  return params.toString();
}

async function renderWebhookSignals(filters = {}) {
  const result = await api(`/api/webhook-signals?${webhookSignalQuery(filters)}`);
  const sources = result.sources || [];
  const facets = result.facets || {};
  const items = result.data || [];
  const selectedSource = filters.signal_source_id || '';
  const sourceTabs = [
    `<button type="button" role="tab" aria-selected="${String(!selectedSource)}" tabindex="${selectedSource ? '-1' : '0'}" data-webhook-source="">全部</button>`,
    ...sources.map(source => `<button type="button" role="tab" aria-selected="${String(selectedSource === source.signal_source_id)}" tabindex="${selectedSource === source.signal_source_id ? '0' : '-1'}" data-webhook-source="${escapeHtml(source.signal_source_id)}">${escapeHtml(source.name)}${source.deleted ? ' · 已删除' : ''}</button>`),
  ].join('');
  const options = (values, selected, formatter = value => value) => (values || []).map(value => `<option value="${escapeHtml(value)}" ${selected === value ? 'selected' : ''}>${escapeHtml(formatter(value))}</option>`).join('');
  const filterForm = `<details class="signal-filter-drawer"><summary><span>筛选信号</span><small>交易所、币种、方向、周期、新鲜度与提案状态</small></summary><form id="webhook-signal-filters" class="filter-panel webhook-signal-filters"><label>交易所<select name="venue"><option value="">全部</option>${options(facets.venues, filters.venue, fmtVenueLabel)}</select></label><label>币种<select name="symbol"><option value="">全部</option>${options(facets.symbols, filters.symbol)}</select></label><label>方向<select name="direction"><option value="">全部</option>${options(facets.directions, filters.direction, fmtDirection)}</select></label><label>周期<select name="timeframe"><option value="">全部</option>${options(facets.timeframes, filters.timeframe)}</select></label><label>新鲜度<select name="freshness"><option value="">全部</option><option value="CURRENT" ${filters.freshness === 'CURRENT' ? 'selected' : ''}>新鲜</option><option value="STALE" ${filters.freshness === 'STALE' ? 'selected' : ''}>已过期</option></select></label><label>提案资格<select name="proposal_eligibility"><option value="">全部</option><option value="ELIGIBLE" ${filters.proposal_eligibility === 'ELIGIBLE' ? 'selected' : ''}>可创建</option><option value="BLOCKED" ${filters.proposal_eligibility === 'BLOCKED' ? 'selected' : ''}>已阻断</option><option value="CREATED" ${filters.proposal_eligibility === 'CREATED' ? 'selected' : ''}>已创建</option></select></label><button class="text-button" type="reset">清除筛选</button></form></details>`;
  const cards = items.map(item => webhookSignalCard(item, Boolean(result.can_propose))).join('');
  main.innerHTML = `<section class="page webhook-signals-page"><header class="page-head"><div><p class="eyebrow">Webhook · 空间隔离</p><h1>Webhook 信号</h1><p class="lede">只展示当前空间通过签名、时效、重放、幂等和格式校验的 Webhook 信号。Perptape 保留在独立页面，不在这里聚合、去重或混排。</p></div><div class="toolbar"><span class="status-pill">${result.total} 条</span><button class="secondary" data-refresh-webhook-signals>刷新</button></div></header><div class="webhook-source-tabs" role="tablist" aria-label="Webhook 信号源">${sourceTabs}</div>${filterForm}<div class="result-summary"><span>显示 ${items.length} / ${result.total} 条信号</span><span>截止 ${fmtDate(result.as_of)}；同一外部 ID 仅在各自信号源内判重。</span></div><div class="webhook-signal-list">${cards || '<section class="empty-state compact-empty"><div><h2>没有符合条件的 Webhook 信号</h2><p>切换信号源或清除筛选。页面不会用 Perptape、其他空间或样本数据填充。</p></div></section>'}</div></section>`;
  const sourceButtons = [...document.querySelectorAll('[data-webhook-source]')];
  const selectSource = async (sourceId, restoreFocus = false) => {
    await renderWebhookSignals({...filters, signal_source_id:sourceId});
    if (restoreFocus) [...document.querySelectorAll('[data-webhook-source]')].find(button => (button.dataset.webhookSource || '') === sourceId)?.focus();
  };
  sourceButtons.forEach((button, index) => {
    button.addEventListener('click', event => selectSource(button.dataset.webhookSource || '', event.detail === 0));
    button.addEventListener('keydown', event => {
      const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
      if (!keys.includes(event.key)) return;
      event.preventDefault();
      const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? sourceButtons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + sourceButtons.length) % sourceButtons.length;
      selectSource(sourceButtons[nextIndex].dataset.webhookSource || '', true);
    });
  });
  const form = document.querySelector('#webhook-signal-filters');
  form.addEventListener('change', () => renderWebhookSignals({...filters, ...Object.fromEntries(new FormData(form))}));
  form.addEventListener('reset', event => {
    event.preventDefault();
    renderWebhookSignals(selectedSource ? {signal_source_id:selectedSource} : {});
  });
  document.querySelector('[data-refresh-webhook-signals]')?.addEventListener('click', () => renderWebhookSignals(filters));
  document.querySelectorAll('.signal-proposal-form').forEach(formElement => formElement.addEventListener('submit', async event => {
    event.preventDefault(); const data = Object.fromEntries(new FormData(formElement)); data.expires_in_minutes = Number(data.expires_in_minutes); data.idempotency_key = crypto.randomUUID();
    try { await withPending(event.submitter, '冻结中…', () => api(`/api/signals/${formElement.dataset.signalEventId}/proposals`, {method:'POST', body:JSON.stringify(data)})); showToast('冻结提案已进入独立审核；未创建订单'); await renderWebhookSignals(filters); }
    catch (error) { showApiError(error, formElement.querySelector('.form-error')); }
  }));
  enhanceRenderedPage();
}

function signalSourceFormDirty(form) {
  const baseline = new URLSearchParams(new FormData(form)).toString();
  const button = form.querySelector('button[type="submit"], .primary');
  const sync = () => { button.disabled = baseline === new URLSearchParams(new FormData(form)).toString() || !form.checkValidity(); };
  form.addEventListener('input', sync); form.addEventListener('change', sync); sync();
}

function bindSignalSourceActions() {
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  document.querySelector('[data-copy-one-time-secret]')?.addEventListener('click', async () => {
    await navigator.clipboard.writeText(document.querySelector('[data-one-time-secret]').textContent); showToast('新凭据已复制；请保存到受控密钥库');
  });
  document.querySelector('[data-dismiss-one-time-secret]')?.addEventListener('click', event => event.currentTarget.closest('.signal-credential-reveal')?.remove());
  document.querySelectorAll('.signal-source-edit-form').forEach(form => {
    signalSourceFormDirty(form);
    form.addEventListener('submit', async event => {
      event.preventDefault(); const data = Object.fromEntries(new FormData(form));
      const body = {name:data.name, webhook_max_age_seconds:Number(data.webhook_max_age_seconds), expected_version:Number(form.dataset.version), idempotency_key:crypto.randomUUID()};
      try { await withPending(event.submitter, '保存中…', () => api(`/api/signal-sources/${form.dataset.sourceId}`, {method:'PUT', body:JSON.stringify(body)})); showToast('信号源配置已保存为新版本'); await route(); }
      catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
  const createForm = document.querySelector('#signal-source-create-form');
  if (createForm) {
    const syncMode = () => {
      const perptape = createForm.elements.mode.value === 'PERPTAPE';
      createForm.querySelectorAll('[data-webhook-create]').forEach(item => { item.hidden = perptape; item.querySelector('input').disabled = perptape; });
      createForm.querySelectorAll('[data-perptape-create]').forEach(item => { item.hidden = !perptape; item.querySelector('input').disabled = !perptape; item.querySelector('input').required = perptape; });
    };
    createForm.elements.mode.addEventListener('change', syncMode); syncMode(); signalSourceFormDirty(createForm);
    createForm.addEventListener('submit', async event => {
      event.preventDefault(); const data = Object.fromEntries(new FormData(createForm)); const isWebhook = data.mode === 'WEBHOOK';
      const body = {name:data.name, mode:data.mode, enabled:createForm.elements.enabled.checked, webhook_max_age_seconds:isWebhook ? Number(data.webhook_max_age_seconds) : 300, expected_version:0, idempotency_key:crypto.randomUUID()};
      const secret = isWebhook ? data.webhook_secret : data.perptape_secret; if (secret) body.secret = secret;
      try { const result = await withPending(event.submitter, '创建中…', () => api('/api/signal-sources', {method:'POST', body:JSON.stringify(body)})); if (result.one_time_secret) signalSourceCredentialReveal = {name:result.source.name, mode:result.source.mode, secret:result.one_time_secret}; showToast('新信号源已创建并写入空间审计'); await route(); }
      catch (error) { showApiError(error, createForm.querySelector('.form-error')); }
    });
  }
  document.querySelectorAll('.signal-source-rotate-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault(); const confirmed = await confirmAction({title:`轮换 ${form.dataset.sourceName} 的凭据？`, message:'旧凭据会立即失效。正在发送的请求可能失败；新凭据只显示一次。', confirmLabel:'确认轮换'}); if (!confirmed) return;
    const secret = new FormData(form).get('secret'); const body = {expected_version:Number(form.dataset.version), idempotency_key:crypto.randomUUID()}; if (secret) body.secret = secret;
    try { const result = await withPending(event.submitter, '轮换中…', () => api(`/api/signal-sources/${form.dataset.sourceId}/credential-rotations`, {method:'POST', body:JSON.stringify(body)})); if (result.one_time_secret) signalSourceCredentialReveal = {name:result.source.name, mode:result.source.mode, secret:result.one_time_secret}; showToast('凭据已轮换；旧值已失效'); await route(); }
    catch (error) { showApiError(error, form.querySelector('.form-error')); }
  }));
  document.querySelectorAll('[data-test-source]').forEach(button => button.addEventListener('click', async () => {
    try { const result = await withPending(button, '测试中…', () => api(`/api/signal-sources/${button.dataset.testSource}/tests`, {method:'POST', body:JSON.stringify({expected_version:Number(button.dataset.version), idempotency_key:crypto.randomUUID()})})); showToast(result.status === 'SUCCESS' ? `连接检查成功；观测 ${result.items_observed || 0} 项` : `连接检查失败：${result.error_code}`); await route(); }
    catch (error) { showApiError(error); }
  }));
  document.querySelectorAll('[data-toggle-source]').forEach(button => button.addEventListener('click', async () => {
    const enabled = button.dataset.enabled !== 'true';
    if (!enabled) { const confirmed = await confirmAction({title:`停用 ${button.dataset.sourceName}？`, message:'停用后不再轮询或接收新信号；历史信号、提案和审计仍保留。', confirmLabel:'确认停用'}); if (!confirmed) return; }
    try { await withPending(button, enabled ? '启用中…' : '停用中…', () => api(`/api/signal-sources/${button.dataset.toggleSource}/state`, {method:'POST', body:JSON.stringify({enabled, expected_version:Number(button.dataset.version), idempotency_key:crypto.randomUUID()})})); showToast(enabled ? '信号源已启用' : '信号源已停用，历史仍保留'); await route(); }
    catch (error) { showApiError(error); }
  }));
  document.querySelectorAll('[data-delete-source]').forEach(button => button.addEventListener('click', async () => {
    const confirmed = await confirmAction({title:`删除 ${button.dataset.sourceName}？`, message:'接收地址会立即失效。该操作只做逻辑删除，历史信号和审计记录继续保留且仍指向原始来源。', confirmLabel:'确认删除'}); if (!confirmed) return;
    try { await withPending(button, '删除中…', () => api(`/api/signal-sources/${button.dataset.deleteSource}`, {method:'DELETE', body:JSON.stringify({confirm_name:button.dataset.sourceName, expected_version:Number(button.dataset.version), idempotency_key:crypto.randomUUID()})})); showToast('信号源已删除；历史与审计已保留'); await route(); }
    catch (error) { showApiError(error); }
  }));
}
const OPPORTUNITY_TIMEFRAME_ORDER = ['1h', '4h', '1d', '1w'];

function groupOpportunities(items) {
  const grouped = new Map();
  items.forEach(item => {
    const key = [item.venue, item.source_exchange, item.symbol, item.canonical_symbol, item.direction].join(':');
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(item);
  });
  return [...grouped.entries()].map(([groupId, candidates]) => {
    const newest = [...candidates].sort((left, right) => String(right.observed_at || '').localeCompare(String(left.observed_at || '')));
    const actionable = newest.filter(item => item.readiness === 'READY' && item.proposal_eligible);
    const currentCandidates = newest.filter(item => item.readiness === 'READY' && item.data_health === 'CURRENT');
    const primary = actionable[0] || currentCandidates[0] || newest[0];
    const observedTimeframes = [...new Set(candidates.map(item => item.timeframe))].sort((left, right) => OPPORTUNITY_TIMEFRAME_ORDER.indexOf(left) - OPPORTUNITY_TIMEFRAME_ORDER.indexOf(right));
    const timeframeStates = observedTimeframes.map(timeframe => {
      const timeframeCandidates = newest.filter(item => item.timeframe === timeframe);
      const complete = timeframeCandidates.find(item => item.readiness === 'READY' && item.data_health === 'CURRENT') || null;
      const latest = timeframeCandidates[0] || null;
      const latestIncomplete = latest && (latest.readiness !== 'READY' || latest.data_health !== 'CURRENT') ? latest : null;
      return {timeframe, complete, latest, latestIncomplete};
    });
    const completeTimeframes = timeframeStates.filter(item => item.complete).map(item => item.timeframe);
    const timeframes = completeTimeframes.length ? completeTimeframes : observedTimeframes;
    const unavailableTimeframes = timeframeStates.filter(item => !item.complete).map(item => item.timeframe);
    const pendingRefreshTimeframes = timeframeStates.filter(item => item.complete && item.latestIncomplete).map(item => item.timeframe);
    const relevantIncompleteCandidates = timeframeStates.map(item => item.latestIncomplete).filter(Boolean);
    const factCandidates = timeframeStates.map(item => item.complete || item.latest).filter(Boolean);
    const numericMaximum = key => {
      const values = factCandidates.map(item => item[key]).filter(value => value !== null && value !== undefined).map(Number).filter(Number.isFinite);
      return values.length ? Math.max(...values) : null;
    };
    return {
      ...primary,
      group_id:groupId,
      candidates,
      timeframes,
      observed_timeframes:observedTimeframes,
      complete_timeframes:completeTimeframes,
      action_candidate_id:actionable[0]?.candidate_id || null,
      proposal_eligible:Boolean(actionable.length),
      proposal_blocker:actionable.length ? null : primary?.proposal_blocker || null,
      incomplete_candidates:relevantIncompleteCandidates,
      unavailable_timeframes:unavailableTimeframes,
      pending_refresh_timeframes:pendingRefreshTimeframes,
      missing_fields:[...new Set((relevantIncompleteCandidates.length ? relevantIncompleteCandidates : [primary]).flatMap(item => item?.missing_fields || []))],
      missing_field_labels:[...new Set((relevantIncompleteCandidates.length ? relevantIncompleteCandidates : [primary]).flatMap(item => item?.missing_field_labels || []))],
      last_complete_at:currentCandidates.map(item => item.last_complete_at).filter(Boolean).sort().at(-1) || null,
      quote_volume:numericMaximum('quote_volume'),
      open_interest:numericMaximum('open_interest'),
    };
  }).sort((left, right) => String(right.triggered_at || right.observed_at || '').localeCompare(String(left.triggered_at || left.observed_at || '')));
}

function opportunitySnapshotCounts(items, groups) {
  const symbolKeys = items.map(item => [
    item.venue,
    item.source_exchange,
    item.canonical_symbol || item.symbol,
  ].join(':'));
  const venueSymbols = {};
  items.forEach(item => {
    const venue = item.venue || item.source_exchange || 'UNKNOWN';
    venueSymbols[venue] ||= new Set();
    venueSymbols[venue].add([item.source_exchange, item.canonical_symbol || item.symbol].join(':'));
  });
  return {
    unique_symbols:new Set(symbolKeys).size,
    symbols_by_venue:Object.fromEntries(Object.entries(venueSymbols).map(([venue, symbols]) => [venue, symbols.size])),
    directional_opportunities:groups.length,
    timeframe_hits:groups.reduce((total, item) => total + (item.complete_timeframes || []).length, 0),
    eligible_opportunities:groups.filter(item => opportunityViewState(item) === 'ACTIONABLE').length,
    active_proposal_opportunities:groups.filter(item => opportunityViewState(item) === 'ACTIVE_PROPOSAL').length,
    waiting_opportunities:groups.filter(item => opportunityViewState(item) === 'WAITING').length,
    watch_only_opportunities:groups.filter(item => opportunityViewState(item) === 'WATCH_ONLY').length,
  };
}

function opportunityViewState(item) {
  if (item.active_proposal?.proposal_id) return 'ACTIVE_PROPOSAL';
  if ((item.action_candidate_id || (!Array.isArray(item.candidates) && item.candidate_id)) && item.proposal_eligible) return 'ACTIONABLE';
  if (['PERPTAPE_REQUIRED_FIELDS_MISSING', 'PERPTAPE_CANDIDATE_NOT_CURRENT'].includes(item.proposal_blocker)) return 'WAITING';
  return 'WATCH_ONLY';
}

function opportunityVenueBreakdown(symbolsByVenue) {
  const order = ['BINANCE', '币安', 'HYPERLIQUID', '链上永续'];
  const labels = {BINANCE:'币安', '币安':'币安', HYPERLIQUID:'Hyperliquid', '链上永续':'Hyperliquid'};
  return Object.entries(symbolsByVenue)
    .sort(([left], [right]) => (order.indexOf(left) === -1 ? 99 : order.indexOf(left)) - (order.indexOf(right) === -1 ? 99 : order.indexOf(right)))
    .map(([venue, count]) => `${labels[venue] || venue} ${count}`)
    .join(' · ');
}

function currentOpportunityFilters() {
  const form = document.querySelector('#opportunity-filters');
  if (!form) return {};
  const data = new FormData(form);
  return {...Object.fromEntries(data), timeframes:data.getAll('timeframes')};
}

function renderOpportunitySnapshot(result, sourceError = null, preservedFilters = {}) {
  opportunities = (result?.data || []).map(item => ({...item, retry_at:result?.retry_at || null}));
  opportunityGroups = groupOpportunities(opportunities);
  const items = opportunityGroups;
  const canPropose = canCreateOpportunityProposal();
  const venues = [...new Set(items.map(item => item.venue).filter(Boolean))].sort();
  const optionTags = values => values.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(fmtVenueLabel(value))}</option>`).join('');
  const counts = opportunitySnapshotCounts(opportunities, items);
  const venueBreakdown = opportunityVenueBreakdown(counts.symbols_by_venue);
  const defaultViewState = counts.eligible_opportunities ? 'ACTIONABLE' : 'ALL';
  const transportState = opportunitySourceRuntime?.state || 'WAITING';
  const upstreamLive = transportState === 'WEBSOCKET_LIVE';
  const transportLabel = ({
    WEBSOCKET_LIVE:'上游 WebSocket 实时流',
    WEBSOCKET_STARTING:'上游流启动中',
    POLLING_FALLBACK:'HTTPS 轮询回退',
    POLLING_ONLY:'HTTPS 定时轮询',
    WEBSOCKET_FAILED:'上游流不可用',
    POLLING_FAILED:'上游轮询失败',
    WAITING:'等待首次同步',
  })[transportState] || transportState;
  const transportNotice = transportState === 'POLLING_FALLBACK'
    ? '<article class="source-status tone-attention"><div><p class="eyebrow">连续接入降级</p><h2>当前使用 HTTPS 轮询事实</h2><p>WebSocket 当前不可用；页面继续展示团队轮询快照，并明确保留采集时间与时效阻断。</p></div></article>'
    : transportState === 'WEBSOCKET_FAILED' || transportState === 'POLLING_FAILED'
      ? '<article class="source-status tone-danger"><div><p class="eyebrow">Fail closed</p><h2>连续信号接入未通过</h2><p>仅保留可辨识的旧快照；过期或缺失事实不会创建新提案。</p></div></article>'
      : '';
  main.innerHTML = `<section class="page" data-opportunity-snapshot="${escapeHtml(result?.snapshot_id || '')}"><header class="page-head"><div><p class="eyebrow">Perptape · ${escapeHtml(transportLabel)}</p><h1>${upstreamLive ? '实时机会' : '机会快照'}</h1><p class="lede">${upstreamLive ? '实时流' : '当前快照'}汇总同一币对、同一方向的多个突破周期。同一轮卡片、状态和按钮都来自同一个服务端事实快照；创建时仍会重新校验。</p></div><div class="toolbar"><span class="status-pill" data-live-status>${sourceError ? '页面连接已中断，正在重连' : '页面正在连接'}</span>${canPropose ? '<a class="primary" href="/proposals/new" data-link>＋ 人工提案</a>' : ''}<button class="secondary" data-refresh>刷新机会</button></div></header>
    ${transportNotice}
    ${sourceError ? `<article class="source-status tone-attention"><div><p class="eyebrow">Perptape 数据源</p><h2>外部机会当前不可用</h2><p>${escapeHtml(friendlyApiError(sourceError))} 系统不会把过期候选当成当前机会。</p></div>${canPropose ? '<a class="secondary" href="/proposals/new" data-link>创建人工提案</a>' : ''}</article>` : ''}
    ${!canPropose ? '<div class="callout"><b>只读模式：</b>当前身份可以查看、筛选候选并打开外部图表，但不能创建或修改提案。</div>' : ''}
    <div class="opportunity-tools"><span>信号快照 ${fmtDate(result?.snapshot_generated_at || result?.as_of)}</span>${canPropose ? `<a class="secondary" href="/opportunities/defaults" data-link>默认配置${proposalDefaults.configured ? '' : ' · 未完成'}</a>` : ''}</div>
    <div class="signal-summary-strip" aria-label="Perptape 信号概览"><span><small>覆盖币对</small><b>${counts.unique_symbols}</b><em>${escapeHtml(venueBreakdown || '按合约去重')}</em></span><span><small>方向机会</small><b>${counts.directional_opportunities}</b></span><span><small>完整周期</small><b>${counts.timeframe_hits}</b></span><span><small>${canPropose ? '可新建' : '可交易'}</small><b>${counts.eligible_opportunities}</b></span></div>
    ${items.length ? `<details class="signal-filter-drawer"><summary><span>筛选机会</span><small>状态、交易所、币对、周期、方向与市场规模</small></summary><form id="opportunity-filters" class="filter-panel"><fieldset class="opportunity-state-filter"><legend>查看状态</legend><label><input name="view_state" type="radio" value="ACTIONABLE" ${defaultViewState === 'ACTIONABLE' ? 'checked' : ''}><span>${canPropose ? '可新建' : '可交易'} <b>${counts.eligible_opportunities}</b></span></label><label><input name="view_state" type="radio" value="ACTIVE_PROPOSAL"><span>审核中 <b>${counts.active_proposal_opportunities}</b></span></label><label><input name="view_state" type="radio" value="WAITING"><span>待补齐 <b>${counts.waiting_opportunities}</b></span></label><label><input name="view_state" type="radio" value="WATCH_ONLY"><span>仅查看 <b>${counts.watch_only_opportunities}</b></span></label><label><input name="view_state" type="radio" value="ALL" ${defaultViewState === 'ALL' ? 'checked' : ''}><span>全部 <b>${items.length}</b></span></label></fieldset><label>交易所<select name="venue"><option value="">全部</option>${optionTags(venues)}</select></label><label>币对<input name="symbol" type="search" placeholder="例如 BTC、XYZ100"></label><label>共振周期<select name="resonance"><option value="1">至少 1 个周期</option><option value="2">至少 2 个周期</option><option value="3">至少 3 个周期</option><option value="4">4 个周期</option></select></label><fieldset class="timeframe-filter"><legend>突破周期</legend>${OPPORTUNITY_TIMEFRAME_ORDER.map(timeframe => `<label><input name="timeframes" type="checkbox" value="${timeframe}" checked><span>${timeframe}</span></label>`).join('')}</fieldset><label>方向<select name="direction"><option value="">全部</option><option value="LONG">做多</option><option value="SHORT">做空</option></select></label><label>最低成交量<input name="volume" type="number" min="0" placeholder="不限"></label><label>最低持仓量<input name="open_interest" type="number" min="0" placeholder="不限"></label><button type="reset" class="text-button">清除筛选</button></form></details><div class="result-summary"><span data-filter-summary>正在整理机会…</span><span>默认显示${canPropose ? '可新建' : '可交易'}机会；其他状态可在筛选中切换。</span></div><div id="opportunity-grid" class="card-grid opportunity-card-grid"></div><div class="opportunity-pagination"><button class="secondary" type="button" data-load-more-opportunities hidden>显示更多</button></div><section id="opportunity-empty" class="empty-state compact-empty" hidden><div><h2>没有符合条件的机会</h2><p>尝试切换机会状态、降低筛选门槛，或者清除部分筛选。</p></div></section>` : `<section class="empty-state compact-empty"><div><h2>${sourceError ? '等待机会数据恢复' : '当前没有突破候选'}</h2><p>${sourceError ? (canPropose ? '人工提案仍然可用；Perptape 恢复后会自动重连。' : '当前身份保持只读；Perptape 恢复后会自动重连。如需形成提案，请联系提案发起人。') : '这不代表市场没有风险或行情，只表示当前没有返回候选。'}</p></div></section>`}
  </section>`;
  const filterForm = document.querySelector('#opportunity-filters');
  if (filterForm) Object.entries(preservedFilters).forEach(([key, value]) => {
    if (key === 'timeframes' && Array.isArray(value)) {
      filterForm.querySelectorAll('input[name="timeframes"]').forEach(input => { input.checked = value.includes(input.value); });
    } else if (filterForm.elements[key]) filterForm.elements[key].value = value;
  });
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  bindOpportunityActions();
  enhanceRenderedPage();
}

function setOpportunityConnectionState(label, connected = false) {
  const indicator = document.querySelector('[data-live-status]');
  if (!indicator) return;
  indicator.textContent = localizedText(label);
  indicator.classList.toggle('status-APPROVED', connected);
}

function stopOpportunityStream() {
  if (opportunityReconnectTimer) clearTimeout(opportunityReconnectTimer);
  opportunityReconnectTimer = null;
  const socket = opportunitySocket;
  opportunitySocket = null;
  if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, 'route changed');
}

function openOpportunityStream() {
  if (opportunitySocket || location.pathname !== '/opportunities' || !session) return;
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${scheme}://${location.host}/ws/opportunities`);
  opportunitySocket = socket;
  setOpportunityConnectionState('正在连接');
  socket.addEventListener('open', () => {
    opportunityReconnectAttempt = 0;
    setOpportunityConnectionState('页面更新正常', true);
  });
  socket.addEventListener('message', event => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.type === 'snapshot' && location.pathname === '/opportunities') {
      const filters = currentOpportunityFilters();
      renderOpportunitySnapshot(message, null, filters);
      setOpportunityConnectionState('页面更新正常', true);
    } else if (message.type === 'heartbeat') {
      setOpportunityConnectionState('页面更新正常', true);
    } else if (message.type === 'error') {
      setOpportunityConnectionState('页面更新正常', true);
    }
  });
  socket.addEventListener('close', () => {
    if (opportunitySocket !== socket) return;
    opportunitySocket = null;
    if (location.pathname !== '/opportunities' || !session) return;
    setOpportunityConnectionState('连接已中断，正在重连');
    const delay = Math.min(10_000, 1_000 * (2 ** opportunityReconnectAttempt));
    opportunityReconnectAttempt += 1;
    opportunityReconnectTimer = setTimeout(openOpportunityStream, delay);
  });
  socket.addEventListener('error', () => setOpportunityConnectionState('连接已中断，正在重连'));
}

async function renderOpportunities() {
  opportunityVisibleLimit = OPPORTUNITY_PAGE_SIZE;
  let result = null;
  let sourceError = null;
  const [opportunityResponse, defaultResponse, sourceResponse] = await Promise.allSettled([
    api('/api/opportunities'),
    canCreateOpportunityProposal() ? api('/api/proposal-defaults') : Promise.resolve(null),
    api('/api/signal-source'),
  ]);
  if (defaultResponse.status === 'fulfilled') {
    if (defaultResponse.value) proposalDefaults = defaultResponse.value;
  } else if (defaultResponse.reason?.status === 401 || defaultResponse.reason?.status === 403) {
    throw defaultResponse.reason;
  }
  if (opportunityResponse.status === 'fulfilled') {
    result = opportunityResponse.value;
  } else {
    const error = opportunityResponse.reason;
    if (error?.status === 401 || error?.status === 403) throw error;
    sourceError = error;
  }
  if (sourceResponse.status === 'fulfilled') {
    opportunitySourceRuntime = sourceResponse.value?.source?.runtime || null;
  } else if (sourceResponse.reason?.status === 401 || sourceResponse.reason?.status === 403) {
    throw sourceResponse.reason;
  } else {
    opportunitySourceRuntime = null;
  }
  renderOpportunitySnapshot(result, sourceError);
  openOpportunityStream();
}

function proposalDefaultEditor(currentDefault) {
  const expiryHours = currentDefault ? Number(currentDefault.expires_in_minutes) / 60 : 8;
  return `<form id="proposal-default-form" class="form-panel proposal-default-form"><div class="field-grid"><label>账户<input name="account_id" value="${escapeHtml(currentDefault?.account_id || '')}" required></label><label>风险档位<select name="risk_tier"><option value="LOW" ${currentDefault?.risk_tier === 'LOW' ? 'selected' : ''}>低</option><option value="MEDIUM" ${!currentDefault || currentDefault.risk_tier === 'MEDIUM' ? 'selected' : ''}>中</option><option value="HIGH" ${currentDefault?.risk_tier === 'HIGH' ? 'selected' : ''}>高</option></select></label><label>名义仓位（USDT）<input name="notional" type="number" step="any" min="0" value="${escapeHtml(currentDefault?.notional || '100')}" required></label><label>最大风险<input name="max_risk" type="number" step="any" min="0" value="${escapeHtml(currentDefault?.max_risk || '1')}" required></label><label>失效距离（基点）<input name="invalidation_bps" type="number" min="1" max="5000" value="${escapeHtml(currentDefault?.invalidation_bps || '200')}" required></label><label>有效时间（小时，至少 8 小时）<input name="expires_in_hours" type="number" min="8" max="24" value="${escapeHtml(Math.max(8, expiryHours))}" required></label><label>自动提案阈值<select name="auto_proposal_min_timeframes"><option value="3" ${currentDefault?.auto_proposal_min_timeframes !== 4 ? 'selected' : ''}>至少 3 个不同周期</option><option value="4" ${currentDefault?.auto_proposal_min_timeframes === 4 ? 'selected' : ''}>4 个不同周期</option></select></label><label class="checkbox-row"><input name="auto_proposal_enabled" type="checkbox" ${currentDefault?.auto_proposal_enabled ? 'checked' : ''}><span>自动创建多周期冻结待审核提案</span></label></div><label>默认理由<textarea name="rationale" rows="3" required>${escapeHtml(currentDefault?.rationale || '使用管理员保存的一键创建默认配置，仅创建待审核提案。')}</textarea></label><p class="safety-note">提案至少保留 8 小时。自动提案只会进入待审核队列；不会自动审核、授权、创建订单或下单。阈值变更只影响之后观察到的新鲜信号。</p><div class="form-error" role="alert"></div><div class="form-actions"><a class="secondary" href="/opportunities" data-link>取消</a><button class="primary">保存新版本</button></div></form>`;
}

function bindProposalDefaultForm() {
  document.querySelector('#proposal-default-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = Object.fromEntries(new FormData(form));
    const button = event.submitter || form.querySelector('button');
    data.invalidation_bps = Number(data.invalidation_bps);
    data.expires_in_minutes = Number(data.expires_in_hours) * 60;
    delete data.expires_in_hours;
    data.auto_proposal_min_timeframes = Number(data.auto_proposal_min_timeframes);
    data.auto_proposal_enabled = form.elements.auto_proposal_enabled.checked;
    data.idempotency_key = crypto.randomUUID();
    await withPending(button, '保存中…', async () => {
      try {
        await api('/api/proposal-defaults', {method:'PUT', body:JSON.stringify(data)});
        showToast('一键创建默认配置已保存为新版本');
        navigate('/opportunities');
      } catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
}

async function renderOpportunityDefaults() {
  proposalDefaults = await api('/api/proposal-defaults');
  const currentDefault = proposalDefaults.data;
  const summary = currentDefault
    ? `${currentDefault.notional} USDT 名义仓位 · 最大风险 ${currentDefault.max_risk} · 有效期 ${Number(currentDefault.expires_in_minutes) / 60} 小时 · 自动提案${currentDefault.auto_proposal_enabled ? `开启（${currentDefault.auto_proposal_min_timeframes} 周期）` : '关闭'}`
    : '尚未配置；保存前一键创建和自动提案保持关闭。';
  main.innerHTML = `<section class="page proposal-default-page"><header class="page-head"><div><p class="eyebrow">机会 · 管理员设置</p><h1>默认提案配置</h1><p class="lede">这里只管理“一键创建”和之后新鲜多周期信号使用的默认值。每张卡片的高级配置只覆盖当前提案，人工提案不使用这里的参数。</p></div><a class="secondary" href="/opportunities" data-link>返回机会</a></header><article class="card proposal-default-summary"><div class="card-heading"><div><p class="eyebrow">当前生效版本</p><h2>${currentDefault ? `版本 v${currentDefault.version}` : '尚未配置'}</h2></div><span class="status-pill ${currentDefault ? 'status-APPROVED' : 'status-DISABLED'}">${currentDefault ? '已生效' : '安全关闭'}</span></div><p>${escapeHtml(summary)}</p>${currentDefault ? `<dl class="definition-grid">${definition('账户', currentDefault.account_id)}${definition('风险档位', fmtRisk(currentDefault.risk_tier))}${definition('更新人', currentDefault.updated_by_username || shortId(currentDefault.updated_by))}${definition('生效时间', fmtDate(currentDefault.effective_at))}</dl>` : '<p class="danger-note">最高管理员保存首个版本后，一键创建才会启用；系统不会猜测账户、风险或金额。</p>'}</article>${proposalDefaults.can_manage ? `<section><div class="section-heading"><div><p class="eyebrow">${currentDefault ? '新建版本' : '首次配置'}</p><h2>${currentDefault ? '修改之后的新提案默认值' : '配置安全默认值'}</h2></div></div>${proposalDefaultEditor(currentDefault)}</section>` : '<div class="callout"><b>只读：</b>当前身份可以查看默认值，但只有系统管理员可以修改全局配置。</div>'}</section>`;
  bindProposalDefaultForm();
}

function opportunityCard(item) {
  const directionClass = item.direction === 'LONG' ? 'direction-long' : 'direction-short';
  const canPropose = hasCapability('proposal.create')
    && (typeof currentWorkflowEnvironment !== 'function' || ['TESTNET','LIVE'].includes(currentWorkflowEnvironment()));
  const grouped = Array.isArray(item.candidates);
  const actionCandidateId = grouped ? item.action_candidate_id : (item.proposal_eligible ? item.candidate_id : null);
  const candidateEligible = Boolean(actionCandidateId) && item.proposal_eligible;
  const canUseCandidate = canPropose && candidateEligible;
  const canCreateProposal = canUseCandidate && proposalDefaults.configured;
  const activeProposal = item.active_proposal?.proposal_id ? item.active_proposal : null;
  const timeframes = item.timeframes || [item.timeframe];
  const cardId = item.group_id || item.candidate_id;
  const signalLabel = item.direction === 'LONG' ? '向上突破' : '向下突破';
  const compactSignalLabel = currentLanguage === 'en'
    ? (item.direction === 'LONG' ? 'Up' : 'Down')
    : (item.direction === 'LONG' ? '上' : '下');
  const signals = timeframes.map(timeframe => `<span class="signal-chip ${directionClass}" aria-label="${escapeHtml(`${timeframe} · ${signalLabel}`)}"><span class="signal-chip-full" aria-hidden="true">${escapeHtml(timeframe)} · ${signalLabel}</span><span class="signal-chip-short" aria-hidden="true">${escapeHtml(timeframe)} ${compactSignalLabel}</span></span>`).join('');
  const missingFieldLabels = {
    threshold:'突破阈值',
    'klineReadiness.status=ready':'K 线就绪状态',
    'data_health=CURRENT':'实时完整数据',
    'active Instrument Catalog match':'可交易合约目录',
  };
  const missingLabels = item.missing_field_labels?.length
    ? item.missing_field_labels
    : (item.missing_fields || []).map(field => missingFieldLabels[field] || '必要行情字段');
  const unavailableTimeframes = item.unavailable_timeframes || [];
  const pendingRefreshTimeframes = item.pending_refresh_timeframes || [];
  const blockerCopy = ({
    VENUE_UNSUPPORTED:'该交易所不支持一键创建',
    PERPTAPE_REQUIRED_FIELDS_MISSING:`${unavailableTimeframes.join('、') || '当前周期'} 的实时数据尚未完整${missingLabels.length ? `（缺少：${missingLabels.join('、')}）` : ''}`,
    PERPTAPE_CANDIDATE_NOT_CURRENT:'当前候选已过期或尚未形成完整实时快照',
    INSTRUMENT_UNAVAILABLE:item.symbol?.includes(':')
      ? '该 HIP-3 市场尚未进入当前 Freqtrade worker 的精确合约目录，只能查看信号'
      : '该交易合约尚未进入可交易合约目录',
  }[item.proposal_blocker] || (actionCandidateId ? '' : '当前没有可用于创建的完整周期'));
  const incompleteCopy = unavailableTimeframes.length && actionCandidateId
    ? `${unavailableTimeframes.join('、')} 数据尚未完整，未计入当前共振；创建时会再次校验。`
    : '';
  const partialCopy = pendingRefreshTimeframes.length && item.last_complete_at
    ? `新一轮信号正在补齐：${pendingRefreshTimeframes.join('、')}。当前仍使用 ${fmtDate(item.last_complete_at)} 的完整快照；创建时会再次校验。`
    : incompleteCopy;
  const retryCopy = !actionCandidateId
    && ['PERPTAPE_REQUIRED_FIELDS_MISSING', 'PERPTAPE_CANDIDATE_NOT_CURRENT'].includes(item.proposal_blocker)
    && item.retry_at
    ? `系统将在 ${fmtDate(item.retry_at)} 后重试。`
    : '';
  const viewState = opportunityViewState(item);
  const marketDataCurrent = item.readiness === 'READY' && (!item.data_health || item.data_health === 'CURRENT');
  const marketStatus = marketDataCurrent
    ? '行情可用'
    : item.proposal_blocker === 'PERPTAPE_CANDIDATE_NOT_CURRENT' ? '信号已过期' : '等待补齐';
  const blockedStatusCopy = !actionCandidateId
    ? viewState === 'WAITING'
      ? `<p class="opportunity-status-note tone-attention"><b>等待行情补齐：</b>${escapeHtml(blockerCopy)}。${retryCopy ? ` ${escapeHtml(retryCopy)}` : ''}</p>`
      : `<p class="safety-note opportunity-status-note"><b>仅查看：</b>${escapeHtml(blockerCopy)}。</p>`
    : '';
  const oneClickBlocker = canUseCandidate && !proposalDefaults.configured
    ? '最高管理员尚未保存一键创建默认配置'
    : blockerCopy;
  const activeProposalCopy = activeProposal
    ? `<p class="opportunity-status-note tone-attention"><b>已有待审核提案：</b>同一交易所、合约和方向已经冻结，不会重复创建。${activeProposal.active_count > 1 ? ` 当前检测到 ${activeProposal.active_count} 条活跃记录，请先在审核队列处理。` : ` ${fmtTimeRemaining(activeProposal.expires_at)}。`}</p>`
    : '';
  return `<article class="card opportunity-card" data-opportunity-card="${escapeHtml(cardId)}" data-opportunity-direction="${escapeHtml(item.direction)}" data-opportunity-state="${viewState}"><div class="card-top opportunity-card-head"><div><div class="opportunity-symbol-line"><div class="symbol">${escapeHtml(item.symbol)}</div><time>${escapeHtml(fmtDate(item.triggered_at))}</time></div><span class="subtle">${escapeHtml(fmtVenueLabel(item.venue))}</span></div><span class="tag ${directionClass}">${escapeHtml(fmtDirection(item.direction))}</span></div>
    <div class="opportunity-signals" aria-label="突破周期">${signals}</div>
    <div class="opportunity-facts"><span><small>参考价格</small><b>${fmtNumber(item.reference_price)}</b></span><span><small>行情状态</small><b class="${marketDataCurrent ? 'direction-long' : 'warning-text'}">${escapeHtml(marketStatus)}</b></span><span><small>成交量</small><b>${fmtCompact(item.quote_volume)}</b></span><span><small>持仓量</small><b>${fmtCompact(item.open_interest)}</b></span></div>
    ${activeProposalCopy}${partialCopy ? `<p class="safety-note opportunity-status-note">${escapeHtml(partialCopy)}</p>` : ''}${blockedStatusCopy}${!activeProposal && canPropose && canUseCandidate && !proposalDefaults.configured ? `<p class="danger-note opportunity-status-note"><b>一键创建关闭：</b>${escapeHtml(oneClickBlocker)}；仍可使用当前卡片的高级配置。</p>` : ''}<div class="link-row"><a class="text-button" href="${escapeHtml(item.detail_url)}" target="_blank" rel="noreferrer">突破详情 ↗</a><a class="text-button" href="${escapeHtml(item.chart_url)}" target="_blank" rel="noreferrer">交易所图表 ↗</a></div>${activeProposal ? `<div class="card-actions proposal-actions"><a class="secondary" href="/proposals/${escapeHtml(activeProposal.proposal_id)}" data-link>查看待审核提案</a></div>` : canPropose && canUseCandidate ? `<div class="card-actions proposal-actions"><button class="secondary" data-advanced-system="${escapeHtml(actionCandidateId)}">高级配置</button><button class="primary" data-create-system="${escapeHtml(actionCandidateId)}" ${canCreateProposal ? '' : 'disabled'} title="${escapeHtml(oneClickBlocker)}">一键创建</button></div>` : ''}</article>`;
}

function openSystemDialog(candidateId) {
  const form = document.querySelector('#system-proposal-form');
  const item = opportunities.find(candidate => candidate.candidate_id === candidateId);
  form.reset();
  form.elements.candidate_id.value = candidateId;
  const defaults = proposalDefaults.data;
  form.elements.account_id.value = defaults?.account_id || '';
  form.elements.risk_tier.value = defaults?.risk_tier || '';
  const price = Number(item?.reference_price || 1);
  const notional = defaults ? Number(defaults.notional) : null;
  const invalidationRate = defaults ? Number(defaults.invalidation_bps) / 10000 : null;
  form.elements.quantity.value = defaults ? Math.max(0.000001, notional / price).toPrecision(6) : '';
  form.elements.max_risk.value = defaults?.max_risk || '';
  form.elements.invalidation_price.value = defaults ? (price * (item?.direction === 'SHORT' ? 1 + invalidationRate : 1 - invalidationRate)).toPrecision(8) : '';
  form.elements.expires_in_hours.value = Math.max(8, Number(defaults?.expires_in_minutes || 480) / 60);
  form.elements.rationale.value = defaults?.rationale || '高级配置覆盖仅作用于当前候选提案，尚未形成任何订单。';
  const summary = form.querySelector('.proposal-default b');
  if (summary) summary.textContent = defaults ? `基于全局默认 v${defaults.version} 预填；本次修改只覆盖当前卡片` : '未配置全局默认；请完整填写本次高级参数';
  document.querySelector('#system-form-error').textContent = '';
  dialog.showModal();
}

function bindOpportunityCardActions() {
  document.querySelectorAll('[data-advanced-system]').forEach(button => button.addEventListener('click', () => openSystemDialog(button.dataset.advancedSystem)));
  document.querySelectorAll('[data-create-system]').forEach(button => button.addEventListener('click', async () => {
    const item = opportunities.find(candidate => candidate.candidate_id === button.dataset.createSystem);
    if (!item) return;
    button.disabled = true; button.textContent = '创建中…';
    try {
      const result = await api(`/api/opportunities/${item.candidate_id}/proposals/default`, {method:'POST', body:JSON.stringify({})});
      showToast(`${item.symbol} 提案已按默认配置创建`);
      navigate(`/proposals/${result.proposal_id}`);
    } catch (error) { showApiError(error); button.disabled = false; button.textContent = '一键创建'; }
  }));
}

function opportunityMatchesFilters(item, values, selectedTimeframes) {
  const minimumResonance = Number(values.resonance || 1);
  return (!values.view_state || values.view_state === 'ALL' || opportunityViewState(item) === values.view_state)
    && (!values.venue || item.venue === values.venue)
    && (!values.symbol || `${item.symbol} ${item.canonical_symbol}`.toLowerCase().includes(values.symbol.toLowerCase().trim()))
    && item.timeframes.length >= minimumResonance
    && selectedTimeframes.some(timeframe => item.timeframes.includes(timeframe))
    && (!values.direction || item.direction === values.direction)
    && (!values.volume || (item.quote_volume !== null && Number(item.quote_volume) >= Number(values.volume)))
    && (!values.open_interest || (item.open_interest !== null && Number(item.open_interest) >= Number(values.open_interest)));
}

function opportunityVisiblePage(groups, values, selectedTimeframes, visibleLimit) {
  const matches = groups.filter(item => opportunityMatchesFilters(item, values, selectedTimeframes));
  return {matches, rendered:matches.slice(0, visibleLimit)};
}

function bindOpportunityActions() {
  const filters = document.querySelector('#opportunity-filters');
  if (!filters) return;
  const grid = document.querySelector('#opportunity-grid');
  const loadMore = document.querySelector('[data-load-more-opportunities]');
  const applyFilters = () => {
    const values = Object.fromEntries(new FormData(filters));
    const selectedTimeframes = new FormData(filters).getAll('timeframes');
    const {matches, rendered} = opportunityVisiblePage(opportunityGroups, values, selectedTimeframes, opportunityVisibleLimit);
    grid.innerHTML = rendered.map(opportunityCard).join('');
    bindOpportunityCardActions();
    applyLanguageToDocument(grid);
    document.querySelector('[data-filter-summary]').textContent = localizedText(`显示 ${rendered.length} / ${matches.length} 个匹配机会（全部 ${opportunityGroups.length}）`);
    document.querySelector('#opportunity-empty').hidden = matches.length !== 0;
    const remaining = Math.max(0, matches.length - rendered.length);
    loadMore.hidden = remaining === 0;
    loadMore.textContent = remaining ? `显示更多（剩余 ${remaining}）` : '已显示全部';
  };
  filters.addEventListener('input', () => { opportunityVisibleLimit = OPPORTUNITY_PAGE_SIZE; applyFilters(); });
  filters.addEventListener('reset', () => requestAnimationFrame(() => { opportunityVisibleLimit = OPPORTUNITY_PAGE_SIZE; applyFilters(); }));
  loadMore.addEventListener('click', () => { opportunityVisibleLimit += OPPORTUNITY_PAGE_SIZE; applyFilters(); });
  applyFilters();
}

function manualInstrumentOptions(allInstruments, venue) {
  return allInstruments.filter(item => item.venue === venue);
}

function manualInstrumentMatch(allInstruments, venue, symbol) {
  const normalized = String(symbol || '').trim().toLocaleUpperCase('en-US');
  if (!normalized) return null;
  return manualInstrumentOptions(allInstruments, venue).find(
    item => String(item.symbol).toLocaleUpperCase('en-US') === normalized,
  ) || null;
}

function manualAccountOptions(allAccounts, venue) {
  return allAccounts.filter(item => item.active && item.venue === venue);
}

function syncManualAccountPicker(form, allAccounts) {
  const venue = form.elements.venue.value;
  const accountSelect = form.elements.account_id;
  const available = manualAccountOptions(allAccounts, venue);
  const current = available.find(item => item.account_id === accountSelect.value);
  accountSelect.innerHTML = available.length
    ? available.map(item => `<option value="${escapeHtml(item.account_id)}" ${item.account_id === current?.account_id ? 'selected' : ''}>${escapeHtml(item.label)} · ${escapeHtml(item.account_id)}</option>`).join('')
    : '<option value="">当前交易所没有可用账户</option>';
  accountSelect.setCustomValidity(available.length ? '' : '请先在交易账户中登记当前交易所账户。');
  return available;
}

function syncManualInstrumentPicker(form, {clearSymbol = false} = {}) {
  const venue = form.elements.venue.value;
  const symbolInput = form.elements.instrument_symbol;
  const instrumentId = form.elements.instrument_id;
  const datalist = form.querySelector('#manual-instrument-options');
  const status = form.querySelector('[data-instrument-match]');
  const available = manualInstrumentOptions(instruments, venue);
  if (clearSymbol) symbolInput.value = '';
  datalist.innerHTML = available.map(item => `<option value="${escapeHtml(item.symbol)}"></option>`).join('');
  const selected = manualInstrumentMatch(instruments, venue, symbolInput.value);
  instrumentId.value = selected?.instrument_id || '';
  symbolInput.setCustomValidity(
    symbolInput.value && !selected
      ? (currentLanguage === 'en' ? 'Enter an exact active symbol for the selected exchange.' : '请输入所选交易所中完整、当前在线的币对。')
      : '',
  );
  if (selected) {
    status.textContent = currentLanguage === 'en'
      ? `Matched: ${fmtVenueLabel(venue)}${venue === 'HYPERLIQUID' ? ' / HIP-3' : ''} · ${selected.symbol}`
      : `已匹配：${fmtVenueLabel(venue)}${venue === 'HYPERLIQUID' ? '（含 HIP-3）' : ''} · ${selected.symbol}`;
    status.dataset.state = 'matched';
  } else if (symbolInput.value) {
    status.textContent = currentLanguage === 'en'
      ? `No exact match among ${available.length} active contracts on this exchange.`
      : `尚未精确匹配该交易所的 ${available.length} 个在线合约，请继续输入或从建议中选择。`;
    status.dataset.state = 'unmatched';
  } else {
    status.textContent = currentLanguage === 'en'
      ? `${available.length} active contracts. Type a full symbol or choose a suggestion.`
      : `当前交易所共 ${available.length} 个在线合约；请输入完整币对或从建议中选择。`;
    status.dataset.state = 'idle';
  }
  return selected;
}
