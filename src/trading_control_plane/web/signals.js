async function renderSignalSources() {
  const [status, signalResponse, instrumentResponse] = await Promise.all([
    api('/api/signal-source'),
    api('/api/signals'),
    hasCapability('proposal.create') ? api('/api/instruments') : Promise.resolve({data:[]}),
  ]);
  const source = status.source;
  const events = signalResponse.data || [];
  const instruments = instrumentResponse.data || [];
  const canManage = Boolean(status.can_manage);
  const credentialState = source?.credential?.state || 'UNCONFIGURED';
  const credentialLabel = credentialState === 'CONFIGURED'
    ? `已加密 · ${source.credential.key_hint || '脱敏元数据已保存'}`
    : credentialState === 'RUNTIME_FALLBACK'
      ? '仅使用部署级兼容 Key；尚未绑定团队 Key'
      : '未配置';
  const modeLabel = source?.mode === 'WEBHOOK' ? 'Webhook' : source?.mode === 'PERPTAPE' ? 'Perptape' : '未选择';
  const runtimeState = source?.mode === 'PERPTAPE' && source.enabled === false
    ? 'DISABLED'
    : source?.runtime?.state || 'WAITING';
  const runtimeLabels = {
    WEBSOCKET_LIVE:'WebSocket 实时流',
    WEBSOCKET_STARTING:'WebSocket 启动中',
    POLLING_FALLBACK:'HTTPS 轮询回退',
    POLLING_ONLY:'HTTPS 定时轮询',
    WEBSOCKET_FAILED:'连续接入失败',
    POLLING_FAILED:'轮询失败',
    DISABLED:'已停用',
    WAITING:'等待首次运行事实',
  };
  const runtimeLabel = runtimeLabels[runtimeState] || runtimeState;
  const runtimeCopy = {
    WEBSOCKET_LIVE:'已观测到当前团队 WebSocket 心跳或告警；HTTPS 轮询继续校准同一事实。',
    WEBSOCKET_STARTING:'团队 WebSocket 正在建立；已有成功轮询时继续以轮询事实回退。',
    POLLING_FALLBACK:'WebSocket 当前不可用；团队级 HTTPS 轮询仍在更新，不会伪装成实时流。',
    POLLING_ONLY:'当前只观测到团队级 HTTPS 定时轮询，页面不会把轮询快照标成实时流。',
    WEBSOCKET_FAILED:'WebSocket 与轮询均未形成当前可用事实；旧候选不会被当成实时机会。',
    POLLING_FAILED:'最近一次团队轮询失败；旧候选按时效规则转为过期并阻断提案。',
    DISABLED:'当前团队已停用 Perptape；worker 不会使用该凭据或更新机会。',
    WAITING:'信号源已配置，但尚未观测到 worker 的流或轮询运行事实。',
  }[runtimeState] || '只显示服务端已记录的当前运行事实。';
  const runtimeTone = runtimeState === 'WEBSOCKET_LIVE' || runtimeState === 'POLLING_ONLY'
    ? 'success'
    : runtimeState === 'WEBSOCKET_FAILED' || runtimeState === 'POLLING_FAILED'
      ? 'danger'
      : runtimeState === 'DISABLED' ? 'neutral' : 'attention';
  const configForm = canManage ? `<form id="signal-source-form" class="form-panel compact-form"><div class="card-heading"><div><p class="eyebrow">当前团队</p><h2>${source ? '切换或轮换信号源' : '配置唯一信号源'}</h2></div><span class="status-pill">${escapeHtml(modeLabel)}</span></div><div class="field-grid"><label>启用模式<select name="mode"><option value="PERPTAPE" ${source?.mode !== 'WEBHOOK' ? 'selected' : ''}>Perptape</option><option value="WEBHOOK" ${source?.mode === 'WEBHOOK' ? 'selected' : ''}>Webhook</option></select></label><label>Perptape Key / Webhook 签名密钥<input name="secret" type="password" minlength="8" maxlength="512" autocomplete="new-password" required></label><label>Webhook 最大时效（秒）<input name="webhook_max_age_seconds" type="number" min="30" max="900" value="${source?.webhook?.max_age_seconds || 300}" required></label><label class="checkbox-row"><input name="enabled" type="checkbox" ${source?.enabled !== false ? 'checked' : ''}><span>启用当前模式</span></label></div><p class="safety-note">切换为 Webhook 会停止 Perptape 自动提案政策。密钥只进入 AES-256-GCM 加密信封；页面、API、日志和审计均不回显明文。</p><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">加密保存新版本</button></div></form>` : '';
  const sourceFacts = source ? `<article class="card"><div class="card-heading"><div><p class="eyebrow">信号源事实</p><h2>${escapeHtml(modeLabel)} · ${source.enabled ? '已启用' : '已停用'}</h2></div><span class="status-pill ${source.enabled ? 'status-APPROVED' : 'status-DISABLED'}">v${source.version}</span></div><dl class="definition-grid">${definition('凭据状态', credentialLabel)}${definition('服务身份', source.service_principal?.username || (source.mode === 'WEBHOOK' ? '不需要；由人员手动创建提案' : '兼容运行身份'))}${source.mode === 'PERPTAPE' ? definition('连续接入', runtimeLabel) : ''}${definition('更新人', source.updated_by_username || shortId(source.updated_by))}${definition('更新时间', fmtDate(source.updated_at))}</dl>${source.mode === 'PERPTAPE' ? '<div class="form-actions"><a class="primary" href="/opportunities" data-link>查看 Perptape 机会</a></div>' : ''}</article>` : '<article class="card"><p class="eyebrow">Fail closed</p><h2>当前团队尚未选择信号源</h2><p class="subtle">服务端不会读取其他团队或全局默认信号。由系统管理员选择 Perptape 或 Webhook 后再继续。</p></article>';
  const webhookContract = source?.mode === 'WEBHOOK' ? `<article class="card"><p class="eyebrow">签名接入</p><h2>TradingView / 自研模型统一 Webhook</h2><dl class="definition-grid">${definition('接收地址', source.webhook.endpoint_url)}${definition('时效', `${source.webhook.max_age_seconds} 秒`)}${definition('提案语义', '仅记录信号；必须由获权人员手动创建冻结提案')}</dl><p class="safety-note">必须携带 <code>X-TradingOPS-Timestamp</code>、<code>X-TradingOPS-Nonce</code>、<code>Idempotency-Key</code> 和 <code>X-TradingOPS-Signature: v1=HMAC_SHA256(secret, timestamp.nonce.raw_body)</code>。过期、未来、重放、重复外部 ID、格式错误与签名错误均由服务端拒绝。</p></article>` : '';
  const eventCards = events.map(event => {
    const proposal = event.proposal;
    const matching = instruments.filter(item => item.venue === event.venue && item.symbol === event.symbol);
    const options = matching.map(item => `<option value="${escapeHtml(item.instrument_id)}">${escapeHtml(item.venue)} · ${escapeHtml(item.symbol)}</option>`).join('');
    const action = proposal
      ? `<a class="secondary" href="/proposals/${proposal.proposal_id}" data-link>查看提案 · ${escapeHtml(fmtStatus(proposal.status))}</a>`
      : hasCapability('proposal.create') && options
        ? `<details class="operation-toolbox"><summary><span><b>手动创建冻结提案</b><small>信号不会自动审核、风控或下单</small></span><strong>展开</strong></summary><form class="toolbox-content signal-proposal-form" data-signal-event-id="${event.signal_event_id}"><div class="field-grid"><label>账户<input name="account_id" required></label><label>合约<select name="instrument_id">${options}</select></label><label>环境<select name="environment"><option value="SHADOW">影子</option><option value="TESTNET">测试网</option><option value="LIVE">实盘</option></select></label><label>风险档位<select name="risk_tier"><option value="LOW">低</option><option value="MEDIUM">中</option><option value="HIGH">高</option></select></label><label>数量<input name="quantity" type="number" step="any" min="0.000000000000000001" required></label><label>最大风险<input name="max_risk" type="number" step="any" min="0.000000000000000001" required></label><label>有效时间（分钟）<input name="expires_in_minutes" type="number" min="480" max="1440" value="480" required></label></div><label>人工判断理由<textarea name="rationale" minlength="10" rows="3" required></textarea></label><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">创建并提交独立审核</button></div></form></details>`
        : '<p class="safety-note">当前缺少提案权限或精确 Instrument Catalog 匹配；由负责人补齐后再创建提案。</p>';
    return `<article class="card"><div class="card-heading"><div><p class="eyebrow">${escapeHtml(event.provider)} · ${escapeHtml(event.strategy_id)} ${escapeHtml(event.strategy_version)}</p><h2>${escapeHtml(event.symbol)} · ${escapeHtml(fmtDirection(event.direction))}</h2></div><span class="status-pill ${proposal ? 'status-APPROVED' : ''}">${proposal ? '已形成提案' : '仅信号'}</span></div><dl class="definition-grid">${definition('交易所', event.venue)}${definition('参考价', event.reference_price || '未提供')}${definition('信号时间', fmtDate(event.occurred_at))}${definition('接收时间', fmtDate(event.received_at))}${definition('外部 ID', event.external_id)}${definition('载荷版本', event.payload_version)}</dl>${action}</article>`;
  }).join('');
  const perptapeRuntime = source?.mode === 'PERPTAPE'
    ? `<article class="home-status tone-${runtimeTone}"><div><p class="eyebrow">Perptape · ${escapeHtml(runtimeLabel)}</p><h2>当前团队运行事实</h2><p>${escapeHtml(runtimeCopy)} 服务端会在创建提案时重新校验时效与资格。</p></div><a class="primary" href="/opportunities" data-link>查看实时机会</a></article>`
    : '';
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">团队隔离</p><h1>信号源</h1><p class="lede">每个团队只启用 Perptape 或 Webhook 之一。信号最多形成冻结提案，不拥有审核、风控、交易或资金权限。</p></div><button class="secondary" data-refresh>刷新事实</button></header><div class="detail-layout">${sourceFacts}${webhookContract}</div>${perptapeRuntime}${configForm}<div class="section-head"><div><p class="eyebrow">当前团队</p><h2>最近 Webhook 信号</h2></div><span class="status-pill">${events.length} 条</span></div><div class="stack">${eventCards || '<article class="empty-state"><div><h2>尚无 Webhook 信号</h2><p>当前不会伪造样本信号。配置 Webhook 后，只显示签名、时效、重放和格式校验全部通过的事件。</p></div></article>'}</div></section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  document.querySelector('#signal-source-form')?.addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form));
    const body = {mode:data.mode, secret:data.secret, enabled:form.elements.enabled.checked, webhook_max_age_seconds:Number(data.webhook_max_age_seconds), expected_version:Number(source?.version || 0), idempotency_key:crypto.randomUUID()};
    try { await withPending(event.submitter, '加密保存中…', () => api('/api/signal-source', {method:'PUT', body:JSON.stringify(body)})); form.reset(); showToast('当前团队信号源已切换并写入审计'); await route(); }
    catch (error) { showApiError(error, form.querySelector('.form-error')); }
  });
  document.querySelectorAll('.signal-proposal-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault(); const data = Object.fromEntries(new FormData(form)); data.expires_in_minutes = Number(data.expires_in_minutes); data.idempotency_key = crypto.randomUUID();
    try { await withPending(event.submitter, '冻结中…', () => api(`/api/signals/${form.dataset.signalEventId}/proposals`, {method:'POST', body:JSON.stringify(data)})); showToast('冻结提案已进入独立审核；未创建订单'); await route(); }
    catch (error) { showApiError(error, form.querySelector('.form-error')); }
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
  const canPropose = hasCapability('proposal.create');
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
    <div class="stats opportunity-stats"><div class="stat"><small>覆盖币对</small><b>${counts.unique_symbols}</b><span>${escapeHtml(venueBreakdown || '按交易所和合约去重')}</span></div><div class="stat"><small>方向机会</small><b>${counts.directional_opportunities}</b><span>同一币对做多、做空分开</span></div><div class="stat"><small>完整周期信号</small><b>${counts.timeframe_hits}</b><span>同一合约、方向、周期只计一次</span></div><div class="stat"><small>${canPropose ? '可新建提案' : '可交易合约'}</small><b>${counts.eligible_opportunities}</b><span>${counts.active_proposal_opportunities ? `另有 ${counts.active_proposal_opportunities} 个同向机会已进入审核` : '当前快照通过服务端资格检查'}</span></div></div>
    ${items.length ? `<form id="opportunity-filters" class="filter-panel"><fieldset class="opportunity-state-filter"><legend>查看状态</legend><label><input name="view_state" type="radio" value="ACTIONABLE" ${defaultViewState === 'ACTIONABLE' ? 'checked' : ''}><span>${canPropose ? '可新建' : '可交易'} <b>${counts.eligible_opportunities}</b></span></label><label><input name="view_state" type="radio" value="ACTIVE_PROPOSAL"><span>审核中 <b>${counts.active_proposal_opportunities}</b></span></label><label><input name="view_state" type="radio" value="WAITING"><span>待补齐 <b>${counts.waiting_opportunities}</b></span></label><label><input name="view_state" type="radio" value="WATCH_ONLY"><span>仅查看 <b>${counts.watch_only_opportunities}</b></span></label><label><input name="view_state" type="radio" value="ALL" ${defaultViewState === 'ALL' ? 'checked' : ''}><span>全部 <b>${items.length}</b></span></label></fieldset><label>交易所<select name="venue"><option value="">全部</option>${optionTags(venues)}</select></label><label>币对<input name="symbol" type="search" placeholder="例如 BTC、XYZ100"></label><label>共振周期<select name="resonance"><option value="1">至少 1 个周期</option><option value="2">至少 2 个周期</option><option value="3">至少 3 个周期</option><option value="4">4 个周期</option></select></label><fieldset class="timeframe-filter"><legend>突破周期</legend>${OPPORTUNITY_TIMEFRAME_ORDER.map(timeframe => `<label><input name="timeframes" type="checkbox" value="${timeframe}" checked><span>${timeframe}</span></label>`).join('')}</fieldset><label>方向<select name="direction"><option value="">全部</option><option value="LONG">做多</option><option value="SHORT">做空</option></select></label><label>最低成交量<input name="volume" type="number" min="0" placeholder="不限"></label><label>最低持仓量<input name="open_interest" type="number" min="0" placeholder="不限"></label><button type="reset" class="text-button">清除筛选</button></form><div class="result-summary"><span data-filter-summary>正在整理机会…</span><span>默认先显示${canPropose ? '可新建' : '可交易'}机会；已有冻结提案、待补齐和仅查看可单独切换。</span></div><div id="opportunity-grid" class="card-grid"></div><div class="opportunity-pagination"><button class="secondary" type="button" data-load-more-opportunities hidden>显示更多</button></div><section id="opportunity-empty" class="empty-state compact-empty" hidden><div><h2>没有符合条件的机会</h2><p>尝试切换机会状态、降低筛选门槛，或者清除部分筛选。</p></div></section>` : `<section class="empty-state compact-empty"><div><h2>${sourceError ? '等待机会数据恢复' : '当前没有突破候选'}</h2><p>${sourceError ? '人工提案仍然可用；Perptape 恢复后会自动重连。' : '这不代表市场没有风险或行情，只表示当前没有返回候选。'}</p></div></section>`}
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
    hasCapability('proposal.create') ? api('/api/proposal-defaults') : Promise.resolve(null),
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
  const canPropose = hasCapability('proposal.create');
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
  return `<article class="card" data-opportunity-card="${escapeHtml(cardId)}" data-opportunity-state="${viewState}"><div class="card-top"><div><span class="subtle">${escapeHtml(fmtVenueLabel(item.venue))} · ${escapeHtml(timeframes.join(' / '))}</span><div class="symbol">${escapeHtml(item.symbol)}</div></div><span class="tag ${directionClass}">${escapeHtml(fmtDirection(item.direction))}</span></div>
    <div class="opportunity-signals" aria-label="突破周期">${signals}</div>
    <div class="metric-row"><div><small>参考价格</small><b>${fmtNumber(item.reference_price)}</b></div><div><small>触发时间</small><b>${fmtDate(item.triggered_at)}</b></div><div><small>行情状态</small><b class="${marketDataCurrent ? 'direction-long' : 'warning-text'}">${escapeHtml(marketStatus)}</b></div></div>
    <div class="market-facts"><span>成交量 <b>${fmtCompact(item.quote_volume)}</b></span><span>持仓量 <b>${fmtCompact(item.open_interest)}</b></span></div>
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
