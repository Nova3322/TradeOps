async function renderManualProposal() {
  const [result, accountResult] = await Promise.all([
    api('/api/instruments'),
    api('/api/exchange-accounts'),
  ]);
  instruments = result.data;
  const proposalAccounts = accountResult.data?.data || [];
  const environment = currentWorkflowEnvironment();
  const environmentCopy = fmtEnvironment(environment, true);
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">人工创建交易提案</p><h1>创建人工提案</h1><p class="lede">先说明交易方向、计划规模和最多可以承受的损失。分批入场和自动加仓是高级选项；提交后只会保存当前判断，仍需独立审核和系统风险检查。</p></div><span class="status-pill ${environment === 'TESTNET' ? 'status-APPROVED' : ''}">${escapeHtml(environmentCopy)}</span></header>
    <div class="compose-layout"><form id="manual-form" class="form-panel proposal-compose" data-environment="${escapeHtml(environment)}">
      <section class="form-section"><div class="section-title"><span>1</span><div><h2>交易意图</h2><p>选标的、定方向，说清从哪个价格开始执行。</p></div></div><div class="field-grid">
        <label>账户<span class="field-help">仅显示当前团队与交易所中获权的精确账户</span><select name="account_id" required></select></label>
        <div class="instrument-field"><span class="field-label">交易标的</span><span class="field-help">先选交易所，再输入完整币对；支持键盘输入和建议匹配，共 ${instruments.length} 个在线 U 本位合约</span><div class="instrument-picker"><label><span>交易所</span><select name="venue" aria-label="交易所" required><option value="BINANCE">币安</option><option value="HYPERLIQUID">Hyperliquid</option><option value="OKX">OKX</option><option value="BYBIT">Bybit</option></select></label><label><span>币对</span><input name="instrument_symbol" aria-label="币对" list="manual-instrument-options" autocomplete="off" spellcheck="false" placeholder="例如 BTCUSDT" required></label></div><input name="instrument_id" type="hidden"><datalist id="manual-instrument-options"></datalist><span class="instrument-match" data-instrument-match data-state="idle" role="status" aria-live="polite"></span></div>
        <label>方向<span class="field-help">做多或做空</span><select name="direction"><option value="LONG">做多</option><option value="SHORT">做空</option></select></label>
        <label>触发价格<span class="field-help">计划开始执行的位置</span><input name="trigger_price" type="number" step="any" min="0" required></label>
      </div></section>
      <section class="form-section"><div class="section-title"><span>2</span><div><h2>风险边界</h2><p>用最大持仓金额、最大损失和失效点限制这笔交易。</p></div></div><div class="field-grid">
        <label>风险档位<span class="field-help">决定审核要求和允许的加仓次数</span><select name="risk_tier"><option value="LOW">低</option><option value="MEDIUM" selected>中</option><option value="HIGH">高</option></select></label>
        <label>最大持仓金额<span class="field-help">单位：<b data-position-currency>USDT</b>；服务端按触发价、合约乘数和数量步长换算</span><input name="max_position_notional" type="number" step="any" min="0" required></label>
        <label>最大风险<span class="field-help">以账户结算币计价</span><input name="max_risk" type="number" step="any" min="0" required></label>
        <label>失效价格<span class="field-help">到达后交易逻辑不再成立</span><input name="invalidation_price" type="number" step="any" min="0" required></label>
      </div></section>
      <details class="advanced-form"><summary><span>高级执行参数</span><small>分批入场、限价、自动加仓与有效期</small></summary><div class="field-grid">
        <label><span class="field-label">初仓金额（<span data-position-currency>USDT</span>）</span><input name="initial_position_notional" type="number" step="any" min="0" placeholder="默认等于最大持仓金额"></label>
        <label>限价（可选）<input name="limit_price" type="number" step="any" min="0"></label>
        <label>允许自动加仓<select name="allow_auto_add"><option value="false" selected>否</option><option value="true">是</option></select></label>
        <label>可用加仓次数<select name="requested_adds"><option value="0" selected>0</option><option value="1">1</option><option value="2">2</option><option value="3">3</option></select></label>
        <label>加仓触发价格<input name="add_trigger_price" type="number" step="any" min="0"></label>
        <label>有效时间（小时，至少 8 小时）<input name="expires_in_hours" type="number" min="8" max="24" value="8" required></label>
      </div></details>
      <label class="rationale-field">提案理由<span class="field-help">至少说明触发逻辑和主要风险</span><textarea name="rationale" rows="4" required placeholder="例如：4h 突破确认，成交量扩张；若跌破失效价则退出。"></textarea></label>
      <div class="form-error" role="alert"></div><div class="form-actions"><span class="submit-disclosure">提交后保存当前参数并进入审核队列，不会直接下单，仍需独立审核和服务端风控。</span><button class="primary">创建并提交审核</button></div>
    </form>
    <aside class="proposal-preview" aria-live="polite"><p class="eyebrow">提案预览</p><h2>提交前摘要</h2><div class="preview-symbol" data-preview-symbol>选择交易标的</div><div class="preview-direction" data-preview-direction>做多</div><dl class="preview-metrics"><div><dt>计划名义价值</dt><dd data-preview-notional>—</dd></div><div><dt>最大风险</dt><dd data-preview-risk>—</dd></div><div><dt>失效距离</dt><dd data-preview-distance>—</dd></div><div><dt>有效期</dt><dd data-preview-expiry>8 小时</dd></div></dl><div class="preview-checks"><p data-check-intent>○ 补全交易意图</p><p data-check-risk>○ 补全风险边界</p><p>✓ 只创建提案，不直接下单</p></div></aside></div></section>`;
  const form = document.querySelector('#manual-form');
  form.addEventListener('submit', submitManualProposal);
  form.addEventListener('input', updateManualProposalPreview);
  form.elements.venue.addEventListener('change', () => {
    syncManualAccountPicker(form, proposalAccounts);
    syncManualInstrumentPicker(form, {clearSymbol:true});
    updateManualProposalPreview({currentTarget:form});
    form.elements.instrument_symbol.focus();
  });
  form.elements.instrument_symbol.addEventListener('input', () => syncManualInstrumentPicker(form));
  form.elements.instrument_symbol.addEventListener('change', () => {
    const selected = syncManualInstrumentPicker(form);
    if (selected) form.elements.instrument_symbol.value = selected.symbol;
  });
  syncManualAccountPicker(form, proposalAccounts);
  syncManualInstrumentPicker(form);
  updateManualProposalPreview({currentTarget:form});
}

function updateManualProposalPreview(event) {
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  const selected = instruments.find(item => item.instrument_id === data.instrument_id);
  const trigger = Number(data.trigger_price); const positionNotional = Number(data.max_position_notional);
  const intentReady = Boolean(selected && trigger > 0 && data.direction);
  const riskReady = Number(data.max_risk) > 0 && Number(data.invalidation_price) > 0 && positionNotional > 0;
  document.querySelectorAll('[data-position-currency]').forEach(node => { node.textContent = selected?.collateral_currency || 'U'; });
  document.querySelector('[data-preview-symbol]').textContent = selected ? `${selected.symbol} · ${selected.venue === 'HYPERLIQUID' ? 'Hyperliquid' : selected.venue}` : '选择交易标的';
  const direction = document.querySelector('[data-preview-direction]');
  direction.textContent = fmtDirection(data.direction);
  direction.className = `preview-direction ${data.direction === 'SHORT' ? 'direction-short' : 'direction-long'}`;
  document.querySelector('[data-preview-notional]').textContent = positionNotional > 0 ? fmtAmount(positionNotional, selected?.quote_currency) : '—';
  document.querySelector('[data-preview-risk]').textContent = data.max_risk ? `${fmtAmount(data.max_risk, selected?.collateral_currency)} · ${fmtRisk(data.risk_tier)}` : '—';
  document.querySelector('[data-preview-distance]').textContent = percentageDistance(data.trigger_price, data.invalidation_price);
  document.querySelector('[data-preview-expiry]').textContent = `${data.expires_in_hours || 8} 小时`;
  document.querySelector('[data-check-intent]').textContent = `${intentReady ? '✓' : '○'} ${intentReady ? '交易意图完整' : '补全交易意图'}`;
  document.querySelector('[data-check-risk]').textContent = `${riskReady ? '✓' : '○'} ${riskReady ? '风险边界完整' : '补全风险边界'}`;
  applyLanguageToDocument(document.querySelector('.proposal-preview'));
}

async function submitManualProposal(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  const selected = instruments.find(i => i.instrument_id === data.instrument_id);
  if (!selected) {
    form.elements.instrument_symbol.setCustomValidity(currentLanguage === 'en'
      ? 'Choose an exact active contract from the selected exchange.'
      : '请先输入并匹配所选交易所中当前在线的完整币对。');
    form.elements.instrument_symbol.reportValidity();
    return;
  }
  const button = form.querySelector('button');
  button.disabled = true;
  data.environment = form.dataset.environment || 'LIVE';
  data.venue = selected.venue;
  data.limit_price = data.limit_price || null;
  data.initial_position_notional = data.initial_position_notional || null;
  data.add_trigger_price = data.add_trigger_price || null;
  data.allow_auto_add = data.allow_auto_add === 'true';
  data.requested_adds = Number(data.requested_adds);
  data.idempotency_key = crypto.randomUUID();
  for (const field of ['max_position_notional','initial_position_notional','max_risk','trigger_price','limit_price','invalidation_price','add_trigger_price']) if (data[field] !== null) data[field] = String(data[field]);
  data.expires_in_minutes = Number(data.expires_in_hours) * 60;
  delete data.expires_in_hours;
  delete data.instrument_symbol;
  try {
    const result = await api('/api/proposals/manual', {method:'POST', body: JSON.stringify(data)});
    showToast('提案已进入审核；相同交易参数不会重复创建');
    navigate(`/proposals/${result.proposal_id}`);
  } catch (error) { showApiError(error, form.querySelector('.form-error')); button.disabled = false; }
}

function reviewEnvironmentSelection(search = location.search) {
  const requested = new URLSearchParams(search).get('environment');
  return ['LIVE','TESTNET'].includes(requested) ? requested : 'ALL';
}

async function renderProposalList(status, title, historyMode = false) {
  const result = await api(`/api/proposals${status ? `?proposal_status=${status}` : ''}`);
  const environment = currentWorkflowEnvironment();
  const reviewEnvironment = status ? reviewEnvironmentSelection() : environment;
  const allItems = status ? result.data : result.data.filter(item => item.environment === environment);
  const proposerOnly = hasCapability('proposal.create') && !hasCapability('proposal.review') && !hasCapability('operations.view');
  const visibleItems = proposerOnly ? allItems.filter(item => item.proposer_id === session.user_id) : allItems;
  const operationsView = hasCapability('operations.view');
  const isCurrentProposal = item => isCurrentProposalItem(item, operationsView);
  const reviewableItems = status ? visibleItems.filter(item => item.actionable_for_current_user) : [];
  const scopedItems = status
    ? reviewableItems.filter(item => reviewEnvironment === 'ALL' || item.environment === reviewEnvironment)
    : visibleItems.filter(item => historyMode ? !isCurrentProposal(item) : isCurrentProposal(item));
  const items = scopedItems
    .sort((left, right) => status
      ? new Date(left.expires_at) - new Date(right.expires_at)
      : new Date(right.created_at) - new Date(left.created_at));
  const expiring = items.filter(item => { const remaining = new Date(item.expires_at) - Date.now(); return remaining > 0 && remaining < 30 * 60 * 1000; }).length;
  const doubleReview = items.filter(item => item.status === 'PENDING_REVIEW' && item.risk_tier === 'HIGH').length;
  const systemCount = items.filter(item => item.source === 'SYSTEM').length;
  const manualCount = items.length - systemCount;
  const approved = items.filter(item => item.status === 'APPROVED' && !proposalLaunchWindowExpired(item)).length;
  const expired = items.filter(item => item.status === 'EXPIRED' || proposalLaunchWindowExpired(item)).length;
  const rejected = items.filter(item => item.status === 'REJECTED').length;
  const earliestExpiry = items[0]?.expires_at;
  const reviewCounts = {
    ALL: reviewableItems.length,
    LIVE: reviewableItems.filter(item => item.environment === 'LIVE').length,
    TESTNET: reviewableItems.filter(item => item.environment === 'TESTNET').length,
  };
  const canPropose = roleNames().includes('PROPOSER') || roleNames().includes('SYSTEM_ADMIN');
  const detailOrigin = status ? 'reviews' : historyMode ? 'history' : 'current';
  const proposalDetailHref = item => {
    const params = new URLSearchParams({from:detailOrigin});
    if (status && reviewEnvironment !== 'ALL') params.set('environment', reviewEnvironment);
    return `/proposals/${item.proposal_id}?${params}`;
  };
  const environmentQuery = environment === 'LIVE' ? '' : `?environment=${environment}`;
  const createActions = !status && canPropose ? `<div class="toolbar"><a class="secondary" href="${environment === 'TESTNET' ? '/accounts' : '/opportunities'}" data-link>${environment === 'TESTNET' ? '返回账户管理' : '查看机会'}</a><a class="primary" href="/proposals/new${environmentQuery}" data-link>新建人工提案</a></div>` : '';
  const reviewEnvironmentSwitcher = status ? `<section class="review-environment-switcher" aria-label="审核环境范围"><div class="review-environment-copy"><b>审核范围</b><span>生产与测试提案保持独立标记；切换筛选不会改变团队交易模式。</span></div><nav class="review-environment-tabs" aria-label="选择审核环境"><a class="${reviewEnvironment === 'ALL' ? 'active' : ''}" href="/reviews" data-link ${reviewEnvironment === 'ALL' ? 'aria-current="page"' : ''}>全部 <b>${reviewCounts.ALL}</b></a><a class="${reviewEnvironment === 'LIVE' ? 'active' : ''}" href="/reviews?environment=LIVE" data-link ${reviewEnvironment === 'LIVE' ? 'aria-current="page"' : ''}>生产 <b>${reviewCounts.LIVE}</b></a><a class="${reviewEnvironment === 'TESTNET' ? 'active' : ''}" href="/reviews?environment=TESTNET" data-link ${reviewEnvironment === 'TESTNET' ? 'aria-current="page"' : ''}>测试 <b>${reviewCounts.TESTNET}</b></a></nav></section>` : '';
  const emptyState = status
    ? '<section class="empty-state"><div><h2>当前没有待你审核的提案</h2><p>自己的提案、已经投过票、已到期或已结束的提案不会留在这里。</p><div class="toolbar empty-actions"><a class="secondary" href="/home" data-link>返回当前任务</a><a class="primary" href="/reviews?view=current" data-link>查看全部提案</a></div></div></section>'
    : `<section class="empty-state"><div><h2>${historyMode ? '当前没有历史提案' : '当前没有进行中的提案'}</h2><p>${historyMode ? '已批准、已过期或已拒绝的提案会保留在这里供审计。' : canPropose ? '可以从机会页一键创建，或提交一份人工提案。' : '当前作用域内还没有需要继续跟踪的提案。'}</p>${historyMode ? '<a class="secondary" href="/reviews?view=current" data-link>返回当前提案</a>' : createActions}</div></section>`;
  const proposalScopeCopy = operationsView
    ? '这里展示草稿、等待审核，以及仍在启动窗口内的已批准提案；审核达标后系统自动完成实时风控、授权和执行。'
    : '这里只展示仍在草稿或等待审核中的提案；批准后进入历史，后续交易生命周期由系统自动推进。';
  const proposalHistoryCopy = operationsView
    ? '这里保留已进入交易任务的提案、启动窗口已过期的批准记录，以及已过期或已拒绝记录；仍可启动的提案留在当前列表。'
    : '这里只保留已批准、已过期或已拒绝的审计记录，不会把历史数量混入当前待办。';
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">提案审核</p><h1>${escapeHtml(title)}</h1><p class="lede">${status ? '统一汇总当前团队内需要你独立判断、尚未到期且尚未投票的提案。生产与测试环境始终分开标记；达到审批阈值并通过实时风控后会自动执行。' : historyMode ? proposalHistoryCopy : proposalScopeCopy}</p></div>${createActions}</header>
    ${reviewEnvironmentSwitcher}
    <div class="stats proposal-stats">${status
      ? `<div class="stat"><small>待我审核</small><b>${items.length}</b></div><div class="stat"><small>需两人审核</small><b class="${doubleReview ? 'warning-text' : ''}">${doubleReview}</b></div><div class="stat"><small>30 分钟内到期</small><b class="${expiring ? 'danger-text' : ''}">${expiring}</b></div><div class="stat"><small>最早到期</small><b class="stat-date">${earliestExpiry ? fmtDate(earliestExpiry) : '—'}</b></div>`
      : historyMode
        ? `<div class="stat"><small>历史记录</small><b>${items.length}</b></div><div class="stat"><small>${operationsView ? '已进入交易' : '已批准'}</small><b>${approved}</b></div><div class="stat"><small>已过期</small><b>${expired}</b></div><div class="stat"><small>已拒绝</small><b>${rejected}</b></div>`
        : `<div class="stat"><small>当前提案</small><b>${items.length}</b></div><div class="stat"><small>等待审核</small><b>${items.filter(item => item.status === 'PENDING_REVIEW').length}</b></div>`}</div>
    <div class="section-tabs">${hasCapability('proposal.review') ? `<a class="${status ? 'active' : ''}" href="/reviews?view=review" data-link>待我审核</a>` : ''}<a class="${!status && !historyMode ? 'active' : ''}" href="/reviews?view=current" data-link>当前提案</a><a class="${historyMode ? 'active' : ''}" href="/reviews?view=history" data-link>历史记录</a></div>
    ${status && items.length ? `<p class="review-queue-summary">当前筛选：生产 ${items.filter(item => item.environment === 'LIVE').length} 笔 · 测试 ${items.filter(item => item.environment === 'TESTNET').length} 笔；系统机会 ${systemCount} 笔 · 人工判断 ${manualCount} 笔。这里只统计你尚未投票、仍在有效期内的提案。</p>` : ''}
    ${items.length ? `<details class="proposal-filter-disclosure" ${window.matchMedia('(min-width: 781px)').matches ? 'open' : ''}><summary><span><b>筛选提案</b><small>按标的、方向、风险与${status ? '来源' : '状态'}缩小结果</small></span><strong><span><b data-proposal-count>${items.length}</b> 个结果</span><span class="proposal-filter-when-closed">展开</span><span class="proposal-filter-when-open">收起</span></strong></summary><div class="proposal-list-tools"><label>搜索标的<input id="proposal-search" type="search" placeholder="BTCUSDT / xyz:TSLA"></label><label>方向<select id="proposal-direction"><option value="">全部方向</option><option value="LONG">做多</option><option value="SHORT">做空</option></select></label><label>风险<select id="proposal-risk"><option value="">全部档位</option><option value="LOW">低</option><option value="MEDIUM">中</option><option value="HIGH">高</option></select></label>${status ? '<label>来源<select id="proposal-source"><option value="">全部来源</option><option value="SYSTEM">系统机会</option><option value="MANUAL">人工判断</option></select></label>' : '<label>状态<select id="proposal-status"><option value="">全部状态</option><option value="DRAFT">草稿</option><option value="PENDING_REVIEW">待审核</option><option value="APPROVED">已批准</option><option value="REJECTED">已拒绝</option><option value="EXPIRED">已过期</option></select></label>'}<span role="status" aria-live="polite"><b data-proposal-visible-count>${Math.min(items.length, 12)}</b> / <b data-proposal-count>${items.length}</b> 个结果</span></div></details><div class="table-wrap proposal-table"><table><thead><tr><th>提案</th><th>方向 / 交易规模</th><th>风险边界</th><th>${status ? '审核进度' : '状态'}</th><th>提交时间</th><th>有效期 / 结果</th></tr></thead><tbody id="proposal-list-body">${items.map(item => `<tr data-href="${escapeHtml(proposalDetailHref(item))}" data-proposal-row data-search="${escapeHtml(`${item.symbol || ''} ${item.venue} ${item.proposer_username || ''}`.toLowerCase())}" data-direction="${escapeHtml(item.direction)}" data-risk="${escapeHtml(item.risk_tier)}" data-source="${escapeHtml(item.source)}" data-status="${escapeHtml(item.status)}"><td data-label="提案"><b>${escapeHtml(item.symbol || shortId(item.instrument_id))}</b><br><span class="subtle">${escapeHtml(fmtVenueLabel(item.venue))} · ${escapeHtml(item.source === 'SYSTEM' ? '系统机会' : '人工判断')}</span>${status ? `<span class="proposal-environment">${escapeHtml(fmtEnvironment(item.environment, true))}</span>` : ''}</td><td data-label="方向 / 交易规模"><span class="direction-pill ${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(fmtDirection(item.direction))}</span><br><b>${item.estimated_notional === null ? '名义价值待详情确认' : `名义价值 ${escapeHtml(fmtAmount(item.estimated_notional, item.quote_currency))}`}</b><br><span class="subtle">合约数量 ${fmtNumber(item.quantity)}</span></td><td data-label="风险边界"><b>最多损失 ${escapeHtml(fmtAmount(item.max_risk, item.collateral_currency))}</b><br><span class="subtle">${fmtRisk(item.risk_tier)}</span></td><td data-label="${status ? '审核进度' : '状态'}">${status ? `<b>已 ${Number(item.approval_count || 0)} / ${Number(item.required_approvals || (item.risk_tier === 'HIGH' ? 2 : 1))}</b><br><span class="subtle">仍需你的独立判断</span>` : `<span class="status-pill status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</span>${proposalStatusSupplement(item) ? `<br><span class="subtle">${escapeHtml(proposalStatusSupplement(item))}</span>` : ''}`}</td><td data-label="提交时间">${fmtDate(item.created_at)}<br><span class="subtle">版本 ${item.version}</span></td><td data-label="有效期 / 结果">${fmtDate(proposalExpiryPresentation(item).at)}<br><span class="subtle">${escapeHtml(proposalExpiryPresentation(item).state)}</span></td></tr>`).join('')}</tbody></table></div><div class="proposal-pagination"><button class="secondary" type="button" data-load-more-proposals aria-controls="proposal-list-body">显示更多</button></div><section id="proposal-filter-empty" class="empty-state compact-empty" hidden><div><h2>没有符合条件的提案</h2><p>请清除搜索或调整筛选。</p></div></section>` : emptyState}</section>`;
  bindLinkedRows();
  if (!items.length) return;
  const pageSize = window.matchMedia('(max-width: 780px)').matches ? 8 : 12;
  let visibleLimit = pageSize;
  const filter = ({resetLimit = false} = {}) => {
    if (resetLimit) visibleLimit = pageSize;
    const query = document.querySelector('#proposal-search')?.value.toLowerCase().trim() || '';
    const direction = document.querySelector('#proposal-direction')?.value || '';
    const risk = document.querySelector('#proposal-risk')?.value || '';
    const proposalStatus = document.querySelector('#proposal-status')?.value || '';
    const source = document.querySelector('#proposal-source')?.value || '';
    let matched = 0;
    let rendered = 0;
    document.querySelectorAll('[data-proposal-row]').forEach(row => {
      const matches = (!query || row.dataset.search.includes(query)) && (!direction || row.dataset.direction === direction) && (!risk || row.dataset.risk === risk) && (!source || row.dataset.source === source) && (!proposalStatus || row.dataset.status === proposalStatus);
      if (matches) matched += 1;
      const render = matches && rendered < visibleLimit;
      row.hidden = !render;
      if (render) rendered += 1;
    });
    document.querySelectorAll('[data-proposal-count]').forEach(node => { node.textContent = matched; });
    document.querySelector('[data-proposal-visible-count]').textContent = rendered;
    document.querySelector('#proposal-filter-empty').hidden = matched !== 0;
    const loadMore = document.querySelector('[data-load-more-proposals]');
    loadMore.hidden = matched <= rendered;
    loadMore.textContent = matched > rendered ? `显示更多（剩余 ${matched - rendered}）` : '已显示全部';
  };
  ['#proposal-search','#proposal-direction','#proposal-risk','#proposal-source','#proposal-status'].forEach(selector => document.querySelector(selector)?.addEventListener('input', () => filter({resetLimit:true})));
  document.querySelector('[data-load-more-proposals]')?.addEventListener('click', () => {
    visibleLimit += pageSize;
    filter();
  });
  filter();
}

function proposalResonanceTimeframes(details, candidate) {
  const frozen = Array.isArray(details?.resonance_timeframes)
    ? details.resonance_timeframes
    : Array.isArray(candidate?.resonance_timeframes)
      ? candidate.resonance_timeframes
      : [candidate?.timeframe];
  return [...new Set(frozen.map(value => String(value || '').trim()).filter(Boolean))];
}

function formatProposalRationale(value) {
  const rationale = String(value || '').trim();
  const automatic = rationale.match(
    /^Perptape current exact-instrument resonance across ([^;]+);\s*(.*?)\s*Proposal only, pending human review\.$/,
  );
  const legacyChinese = rationale.match(
    /^Perptape 当前同一精确合约、同一方向在 (.+?) 同时突破。\s*(.*?)\s*系统仅创建冻结待审核提案，不会自动审核、授权或下单。$/,
  );
  if (!automatic && !legacyChinese) return rationale;
  const timeframes = automatic
    ? automatic[1].split(',').map(item => item.trim()).filter(Boolean).join('、')
    : legacyChinese[1].trim();
  const configuredRationale = (automatic ? automatic[2] : legacyChinese[2]).trim();
  const defaultConfigCopy = '使用管理员保存的一键创建默认配置，仅创建待审核提案。';
  const riskCopy = configuredRationale === defaultConfigCopy
    ? '风险参数来自管理员保存的默认配置。'
    : configuredRationale;
  const normalizedRiskCopy = riskCopy ? `${riskCopy.replace(/[。；;]\s*$/, '')}。` : '';
  return `创建提案时，突破榜单中同一精确合约、同一方向在 ${timeframes} 同时突破。${normalizedRiskCopy}来源与参数已冻结；达到人工审核阈值并通过实时风控后，系统自动推进受控交易。`;
}

async function renderProposalDetail(id) {
  const item = await api(`/api/proposals/${id}`);
  const preview = item.execution_preview;
  if (!['LIVE','TESTNET'].includes(item.environment)) {
    main.innerHTML = '<section class="page"><section class="empty-state"><div><h1>该提案不属于当前控制台</h1><p>这里只展示生产或严格隔离的测试提案。</p><a class="primary" href="/reviews?view=current" data-link>返回审核队列</a></div></section></section>';
    return;
  }
  const testnetProposal = item.environment === 'TESTNET';
  const detailParams = new URLSearchParams(location.search);
  const detailOrigin = detailParams.get('from');
  const reviewEnvironment = ['LIVE', 'TESTNET'].includes(detailParams.get('environment'))
    ? detailParams.get('environment')
    : null;
  const returnDestination = detailOrigin === 'reviews' && hasCapability('proposal.review')
    ? {href:`/reviews${reviewEnvironment ? `?environment=${reviewEnvironment}` : ''}`, label:'审核队列'}
    : detailOrigin === 'history'
      ? {href:'/reviews?view=history', label:'历史记录'}
      : {href:'/reviews?view=current', label:testnetProposal ? '测试提案' : '当前提案'};
  const reviewedByMe = item.approvals.some(approval => approval.reviewer_id === session.user_id);
  const isExpired = item.status === 'PENDING_REVIEW' && new Date(item.expires_at).getTime() <= Date.now();
  const launchWindowExpired = proposalLaunchWindowExpired(item);
  const canReview = Boolean(item.actionable_for_current_user);
  const details = item.frozen_payload?.details || {};
  const candidate = details.candidate || {};
  const triggerPrice = details.trigger_price || candidate.reference_price || candidate.threshold_price;
  const invalidationPrice = details.invalidation_price;
  const notional = item.estimated_notional === null ? null : Number(item.estimated_notional);
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
  const terminal = isExpired || launchWindowExpired || ['REJECTED','EXPIRED'].includes(item.status);
  const rationale = formatProposalRationale(
    details.rationale || candidate.rationale || '未提供补充理由',
  );
  const sourceLink = item.source_link || candidate.detail_url;
  const chartLink = candidate.chart_url;
  const resonanceTimeframes = proposalResonanceTimeframes(details, candidate);
  const sourceFacts = item.source === 'SYSTEM'
    ? `<div class="source-facts"><div><small>创建时来源快照</small><b class="${item.source_readiness === 'READY' ? 'direction-long' : 'direction-short'}">${escapeHtml(item.source_readiness === 'READY' ? '创建时可用' : fmtReadiness(item.source_readiness))}</b></div><div><small>共振周期</small><b>${escapeHtml(resonanceTimeframes.join(' / ') || '—')}</b></div><div><small>成交量</small><b>${fmtCompact(candidate.quote_volume)}</b></div><div><small>持仓量</small><b>${fmtCompact(candidate.open_interest)}</b></div><div><small>快照时间</small><b>${fmtDate(item.source_observed_at)}</b></div></div>`
    : '<div class="source-facts manual-source"><div><small>来源</small><b>人工输入</b></div><div><small>审核依据</small><b>保存参数与提案理由</b></div></div>';
  const highRiskReviewCopy = item.risk_tier === 'HIGH' ? `高风险提案需要两名不同审核人；当前已记录 ${item.approvals.length} 票。达到所需票数并通过实时风控后，系统会自动授权、预留并交给 Freqtrade 执行。` : '批准并通过实时风控后，系统会自动授权、预留并交给 Freqtrade 执行。';
  const nextAction = launchWindowExpired
    ? {title:'审核已批准，但启动窗口已过期', copy:'审核结论已保留，但不能再运行风险检查、签发授权或创建交易任务。需要按当前市场条件创建新提案。', tone:'danger'}
    : terminal
    ? {title:isExpired ? '提案已到期' : '流程已终止', copy:'该提案不能继续扩大风险。条件改变后需创建新提案。', tone:'danger'}
    : item.status === 'PENDING_REVIEW'
      ? canReview
        ? {title:'需要你的独立判断', copy:`核对名义价值、最大风险、失效位置和创建时来源快照。${highRiskReviewCopy}${fmtTimeRemaining(item.expires_at)}。`, tone:'attention'}
        : reviewedByMe
          ? {title:'你的审核已记录', copy:'这笔提案仍在等待另一名独立审核人；你无需再次操作。', tone:'success'}
          : {title:'等待独立审核', copy:item.proposer_id === session.user_id ? '你是提案创建者，不能审核自己的提案。' : '当前角色没有审核权限。', tone:'neutral'}
      : initialEntry
        ? {title:'交易任务已进入自动执行', copy:'该提案不能再创建第二个初仓意图；Freqtrade 发送、成交确认、Facts 和异常处理统一在交易任务中自动推进。', tone:'success'}
        : item.status === 'APPROVED' && (!riskDone || riskDenied)
          ? riskDenied
            ? {title:riskHelp.label, copy:riskHelp.action, tone:'danger'}
            : {title:'系统正在运行风险检查', copy:'审核已完成，系统会基于最新账户与受管资金事实自动冻结风控结果。', tone:'attention'}
        : needsFreshRisk
          ? {title:'短期授权已经失效', copy:'重新读取当前账户事实并运行风险检查；通过后才能签发新的短期授权。', tone:'danger'}
        : needsAuthorization
          ? {title:'系统正在签发短期授权', copy:'风险检查已通过；系统正在自动签发限时、限数量、限风险的交易授权。', tone:'attention'}
          : authorizationUsable
            ? {title:'系统正在创建交易任务', copy:'系统正在自动预留风险并创建唯一初仓意图；随后由受控执行进程在 Gate、租约和幂等边界内推进。', tone:'attention'}
            : {title:'当前没有待办动作', copy:'请核对授权有效期和当前状态。', tone:'neutral'};
  const executionAction = initialEntry
    ? `<a class="primary wide-action" href="/campaigns/${initialEntry.campaign_id}" data-link>进入交易任务</a><p class="microcopy">初仓意图 ${shortId(initialEntry.intent_id)} · ${escapeHtml(fmtStatus(initialEntry.intent_status))}</p>`
    : item.status === 'APPROVED' && !riskDenied
        ? '<p class="microcopy">无需人工点击；系统会从当前持久状态继续推进。</p>'
        : item.status === 'APPROVED' && riskDenied
          ? '<p class="microcopy">无需人工重试；相关事实、风险容量或政策发生变化后，系统会自动重新检查。静态边界不满足时保持阻断。</p>'
        : '';
  const riskOutcomeCopy = launchWindowExpired
    ? '启动窗口已过期；以下是该提案最后一次风险检查结果，仅供审计。'
    : !riskDone
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
      ? `<div class="risk-guidance-list">${item.risk_decision.reasons.map(reason => { const guidance = riskGuidance(reason); return `<div><b>${escapeHtml(guidance.label)}</b><span>${escapeHtml(launchWindowExpired ? '这是最后一次检查记录；如需重新评估，必须按当前事实创建新提案。' : guidance.action)}</span></div>`; }).join('')}</div>`
      : '<p class="success-note">仓位、权益、受管资金、系统状态和总风险容量均通过。</p>';
  const riskDecisionPanel = riskDone
    ? `<p class="risk-outcome-copy">${escapeHtml(riskOutcomeCopy)}</p><dl class="definition-grid risk-decision-grid">${definition('请求数量', fmtNumber(riskContext.requested_quantity))}${definition('系统批准数量', fmtNumber(item.risk_decision.approved_quantity))}${definition('冻结杠杆', `${fmtNumber(item.risk_decision.leverage)}x`)}${definition('本次风险占用', fmtAmount(item.risk_decision.risk_amount, item.collateral_currency))}${definition('组合风险容量', riskCapacityCopy)}${definition('事实年龄', `${fmtSeconds(riskContext.fact_age_seconds)} / 上限 ${fmtSeconds(riskContext.max_fact_age_seconds)}`)}${definition('数据截止', fmtDate(item.risk_decision.data_as_of))}</dl><div class="risk-fact-strip"><span>仓位 <b>${escapeHtml(factStatusLabel(riskContext.position_status))}</b></span><span>权益 <b>${escapeHtml(factStatusLabel(riskContext.equity_status))}</b></span><span>受管资金 <b>${riskContext.managed_capital_known ? '已确认' : '缺失'}</b></span><span>保护 <b>${escapeHtml(factStatusLabel(riskContext.protection_status))}</b></span></div>${riskReasons}`
    : item.status === 'APPROVED'
      ? '<div class="empty-inline"><b>系统风控处理中</b><span>达到审批阈值后由系统自动读取最新仓位、权益、受管资金、保护和总风险容量。</span></div>'
      : '<div class="empty-inline"><b>等待审核通过</b><span>达到审批阈值后系统会自动运行风险检查。</span></div>';
  const authorizationState = !authorizationDone ? '未签发' : authorizationUsable ? '有效' : item.authorization.active ? '已过期' : '已撤销';
  const authorizationPanel = launchWindowExpired
    ? '<div class="empty-inline"><b>当前提案不可再签发</b><span>启动窗口已经过期；审核结论继续保留，重新交易必须创建新提案。</span></div>'
    : authorizationDone
    ? `<dl class="definition-grid authorization-grid">${definition('批准数量', fmtNumber(item.authorization.quantity_limit))}${definition('冻结杠杆', `${fmtNumber(item.authorization.leverage)}x`)}${definition('已使用', fmtNumber(item.authorization.used_quantity))}${definition('剩余数量', fmtNumber(item.authorization.remaining_quantity))}${definition('风险上限', fmtAmount(item.authorization.risk_limit, item.collateral_currency))}${definition('可用加仓次数', `${item.authorization.used_adds} / ${item.authorization.allowed_adds}`)}${definition('到期', fmtDate(item.authorization.expires_at))}</dl>${initialEntry ? `<div class="entry-boundary"><b>一次性初仓已使用</b><span>意图 ${shortId(initialEntry.intent_id)} · ${escapeHtml(fmtStatus(initialEntry.intent_status))} · ${fmtNumber(initialEntry.leverage)}x</span><a href="/campaigns/${initialEntry.campaign_id}" data-link>查看交易任务 →</a></div>` : '<p class="microcopy">系统正在读取最新事实、预留风险并创建唯一初仓意图。</p>'}`
    : '<div class="empty-inline"><b>风险通过后可签发</b><span>授权同时限制有效期、数量、风险金额、权限范围和可用加仓次数。</span></div>';
  main.innerHTML = `<section class="page proposal-detail"><header class="page-head"><div><p class="eyebrow">${escapeHtml(fmtEnvironment(item.environment, true))} · ${escapeHtml(item.source === 'SYSTEM' ? 'Perptape 机会' : '人工提案')}</p><div class="proposal-title-row"><h1>${escapeHtml(item.symbol || candidate.symbol || '交易提案')}</h1><span class="direction-pill ${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(fmtDirection(item.direction))}</span><span class="status-pill status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</span></div><p class="lede">${escapeHtml(item.venue)} · ${testnetProposal ? '测试账户范围' : '生产账户范围'} · 提案 ${shortId(item.proposal_id)} · 版本 ${item.version}</p></div><div class="toolbar"><a class="secondary" href="${escapeHtml(returnDestination.href)}" data-link>返回${escapeHtml(returnDestination.label)}</a>${sourceLink ? `<a class="secondary" href="${escapeHtml(sourceLink)}" target="_blank" rel="noreferrer">突破详情 ↗</a>` : ''}${chartLink ? `<a class="secondary" href="${escapeHtml(chartLink)}" target="_blank" rel="noreferrer">交易所图表 ↗</a>` : ''}</div></header>
    <ol class="workflow-stepper" aria-label="提案流程"><li class="done"><span>1</span><div><b>提案已保存</b><small>${fmtDate(item.frozen_at)}</small></div></li><li class="${reviewDone ? 'done' : 'current'}"><span>2</span><div><b>独立审核</b><small>${isExpired ? '已到期' : reviewDone ? fmtStatus(item.status) : reviewedByMe ? '你的审核已记录' : '等待判断'}</small></div></li><li class="${launchWindowExpired || riskDenied ? 'blocked' : riskDone ? 'done' : reviewDone && !terminal ? 'current' : ''}"><span>3</span><div><b>风险检查</b><small>${launchWindowExpired ? '启动窗口已过期' : riskDone ? fmtStatus(item.risk_decision.result) : '尚未运行'}</small></div></li><li class="${initialEntry || authorizationUsable ? 'done' : needsAuthorization ? 'current' : ''}"><span>4</span><div><b>短期授权</b><small>${initialEntry ? '已生成初仓意图' : authorizationDone ? (authorizationUsable ? '有效' : '已失效') : '尚未签发'}</small></div></li></ol>
    <div class="proposal-detail-layout"><div class="stack">
      <article class="card decision-brief"><div class="card-heading"><div><p class="eyebrow">交易判断摘要</p><h2>这笔交易要做什么</h2></div><span class="risk-badge risk-${escapeHtml(item.risk_tier)}">${escapeHtml(fmtRisk(item.risk_tier))}</span></div><p class="proposal-rationale">${escapeHtml(rationale)}</p><div class="decision-metrics"><div><small>实际名义价值</small><b>${notional === null ? '—' : escapeHtml(fmtAmount(notional, item.quote_currency))}</b><span>触发价 ${fmtNumber(triggerPrice)}</span></div><div><small>实际最大风险</small><b>${escapeHtml(fmtAmount(item.max_risk, item.collateral_currency))}</b><span>${fmtRisk(item.risk_tier)} · ${fmtNumber(item.leverage)}x</span></div><div><small>最终合约数量</small><b>${fmtNumber(item.quantity)}</b><span>初仓 ${fmtNumber(details.initial_quantity || item.quantity)}</span></div><div><small>失效位置</small><b>${fmtNumber(invalidationPrice)}</b><span>距触发 ${percentageDistance(triggerPrice, invalidationPrice)}</span></div></div>${sourceFacts}</article>
      <article class="card frozen-scope"><div class="card-heading"><div><p class="eyebrow">已保存参数</p><h2>提案范围</h2></div><span class="status-pill">不可编辑</span></div><dl class="definition-grid spacious">${definition('账户', testnetProposal ? '测试账户范围' : '生产账户范围')}${definition('创建人', item.source === 'SYSTEM' ? '系统自动创建' : item.proposer_username || shortId(item.proposer_id))}${definition('交易所', item.venue)}${definition('方向', fmtDirection(item.direction))}${definition('风险档位', fmtRisk(item.risk_tier))}${definition('冻结杠杆', `${fmtNumber(item.leverage)}x`)}${definition('限价', fmtNumber(details.limit_price))}${definition('有效期 / 结果', `${fmtDate(proposalExpiryPresentation(item).at)} · ${proposalExpiryPresentation(item).state}`)}${definition('自动加仓', details.allow_auto_add ? `允许 · ${details.requested_adds} 次` : '关闭')}${definition('加仓触发价', fmtNumber(details.add_trigger_price))}${definition('来源候选', item.source === 'SYSTEM' ? '已冻结来源快照' : '人工创建')}${definition('来源快照时间', fmtDate(item.source_observed_at))}</dl></article>
      <article class="card review-trail"><div class="card-heading"><div><p class="eyebrow">独立判断</p><h2>审核历史</h2></div><span class="subtle">${item.approvals.length} 条记录</span></div>${item.approvals.length ? `<div class="review-timeline">${item.approvals.map(a => `<div class="review-event"><span class="${a.decision === 'APPROVE' ? 'approve-dot' : 'reject-dot'}"></span><div><b>${a.decision === 'APPROVE' ? '批准提案' : '拒绝提案'}</b><p>${escapeHtml(a.reason)}</p><small>${escapeHtml(a.reviewer_username || shortId(a.reviewer_id))} · ${fmtDate(a.created_at)}</small></div></div>`).join('')}</div>` : '<div class="empty-inline"><b>尚无审核记录</b><span>审核人的独立判断会按时间出现在这里。</span></div>'}</article>
    </div><aside class="stack proposal-actions-column">
      <article class="card next-action tone-${nextAction.tone}"><p class="eyebrow">下一步</p><h2>${escapeHtml(nextAction.title)}</h2><p>${escapeHtml(nextAction.copy)}</p>${item.status === 'PENDING_REVIEW' && canReview ? `<div class="empty-inline"><b>审批后自动执行</b><span>账户 ${escapeHtml(preview?.account_id || '—')} · 场所 ${escapeHtml(preview?.venue || '—')} · 标的 ${escapeHtml(preview?.symbol || '—')} · 方向 ${escapeHtml(preview?.side || '—')} · 类型 ${escapeHtml(preview?.order_type || '—')} · 数量 ${escapeHtml(fmtNumber(preview?.quantity))} · 预计名义价值 ${preview?.estimated_notional ? escapeHtml(fmtAmount(preview.estimated_notional, preview.quote_currency)) : '—'} · 杠杆 ${escapeHtml(fmtNumber(preview?.leverage))}x · 最大风险 ${escapeHtml(fmtAmount(item.max_risk, item.collateral_currency))}。达到审核阈值后自动签发授权、预留风险并由 Freqtrade 发送；超时或状态不明时只查询，不重复下单。</span></div><label>审核意见<span class="field-help">说明你核对了什么，以及判断依据</span><textarea id="review-reason" rows="4">已核对交易逻辑、保存参数与最大风险边界</textarea></label><div class="review-actions"><button class="primary" data-approve>批准提案</button><button class="danger" data-reject>拒绝提案</button></div><p class="microcopy">批准是最后一个常规人工节点。登录会话、Reviewer 权限、账户与场所范围、版本、幂等和审计仍由服务端强制检查；不再重复输入当前密码。</p><div class="form-error" id="review-error"></div>` : ''}${executionAction}<div class="form-error" id="execution-error"></div></article>
      <article class="card risk-engine-card"><div class="card-heading"><div><p class="eyebrow">风险检查</p><h2>${launchWindowExpired ? '最后一次风险检查' : '系统允许开多少'}</h2></div>${item.risk_decision ? `<span class="status-pill status-${escapeHtml(item.risk_decision.result)}">${escapeHtml(fmtStatus(item.risk_decision.result))}</span>` : '<span class="status-pill">未运行</span>'}</div>${riskDecisionPanel}</article>
      <article class="card authorization-card"><div class="card-heading"><div><p class="eyebrow">限时授权</p><h2>这份许可还能做什么</h2></div><span class="status-pill ${authorizationUsable ? 'status-APPROVED' : authorizationDone ? 'status-EXPIRED' : ''}">${escapeHtml(localizedText(authorizationState))}</span></div>${authorizationPanel}</article>
    </aside></div></section>`;
  document.querySelector('[data-approve]')?.addEventListener('click', (event) => approveProposal(item, event.currentTarget));
  document.querySelector('[data-reject]')?.addEventListener('click', (event) => rejectProposal(item, event.currentTarget));
}

const definition = (label, value) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? '—')}</dd></div>`;

async function approveProposal(item, button) {
  const errorBox = document.querySelector('#review-error');
  const preview = item.execution_preview;
  if (!preview || !preview.account_id || !preview.venue || !preview.symbol || !preview.side || !preview.order_type || !preview.quantity || !preview.estimated_notional || !preview.quote_currency || !preview.leverage) {
    showApiError({code:'EXECUTION_PREVIEW_UNAVAILABLE', message:'生产订单确认字段不完整；审核保持未提交。'}, errorBox);
    return;
  }
  await withPending(button, '提交中…', async () => {
    try {
      await api(`/api/proposals/${item.proposal_id}/reviews`, {method:'POST', body:JSON.stringify({decision:'APPROVE', reason:document.querySelector('#review-reason').value, expected_version:item.version, idempotency_key:crypto.randomUUID()})});
      showToast('审核结果已记录'); await route();
    } catch (error) { showApiError(error, errorBox); }
  });
}

async function rejectProposal(item, button) {
  await withPending(button, '提交中…', async () => {
    try {
      await api(`/api/proposals/${item.proposal_id}/reviews`, {method:'POST', body:JSON.stringify({decision:'REJECT', reason:document.querySelector('#review-reason').value, expected_version:item.version, idempotency_key:crypto.randomUUID()})});
      showToast('提案已拒绝，当前流程已结束'); await route();
    } catch (error) { showApiError(error, document.querySelector('#review-error')); }
  });
}

async function loadCampaignDetails() {
  const result = await api('/api/campaigns');
  const visible = result.data.filter(item => item.environment === 'LIVE');
  return Promise.all(visible.map((item) => api(`/api/campaigns/${item.campaign_id}`)));
}
