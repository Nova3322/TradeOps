const main = document.querySelector('#main');
const sidebar = document.querySelector('#sidebar');
const identityChip = document.querySelector('#identity-chip');
const dialog = document.querySelector('#system-proposal-dialog');
const toast = document.querySelector('#toast');
let session = null;
let authStatus = null;
let instruments = [];

const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const shortId = (value) => value ? `${value.slice(0, 8)}…` : '—';
const fmtDate = (value) => value ? new Intl.DateTimeFormat('zh-CN', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}).format(new Date(value)) : '—';
const fmtNumber = (value) => value === null || value === undefined ? '—' : new Intl.NumberFormat('en-US', {maximumFractionDigits: 6}).format(Number(value));
const roleNames = () => (session?.roles || []).map((item) => item.role);

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: {'content-type': 'application/json', ...(options.headers || {})}
  });
  const data = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data?.error?.message || data?.detail?.error?.code || `HTTP ${response.status}`);
    error.code = data?.error?.code || data?.detail?.error?.code || `HTTP_${response.status}`;
    error.status = response.status;
    throw error;
  }
  return data;
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 2600);
}

function setShell(loggedIn) {
  sidebar.hidden = !loggedIn;
  identityChip.hidden = !loggedIn;
  if (loggedIn) identityChip.textContent = `${session.username} · ${roleNames().join(' / ') || 'NO ROLE'}`;
}

function errorView(error, retry = true) {
  return `<section class="error-state"><div><p class="error-code">${escapeHtml(error.code || 'UNKNOWN')}</p><h2>当前事实无法读取</h2><p>${escapeHtml(error.message)}</p>${retry ? '<button class="secondary" data-retry>重新读取</button>' : ''}</div></section>`;
}

async function bootstrap() {
  authStatus = await api('/api/auth/status');
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
  if (!session) return renderLogin();
  main.innerHTML = '<section class="loading-state"><span class="spinner"></span><p>正在读取当前事实…</p></section>';
  const path = location.pathname;
  try {
    if (path === '/opportunities' || path === '/') return await renderOpportunities();
    if (path === '/proposals/new') return await renderManualProposal();
    if (path === '/reviews') return await renderProposalList('PENDING_REVIEW', '审核队列');
    if (path === '/proposals') return await renderProposalList(null, '全部提案');
    if (path === '/campaigns') return await renderCampaignList();
    if (path === '/positions') return await renderCampaignFacts('positions');
    if (path === '/orders') return await renderCampaignFacts('orders');
    if (path === '/risk') return await renderCampaignFacts('risk');
    if (path === '/capital') return await renderCapitalCenter();
    if (path === '/exceptions') return await renderExceptions();
    if (path === '/venues/binance') return await renderBinanceReadOnly();
    const campaignMatch = path.match(/^\/campaigns\/([0-9a-f-]+)$/i);
    if (campaignMatch) return await renderCampaignDetail(campaignMatch[1]);
    const match = path.match(/^\/proposals\/([0-9a-f-]+)$/i);
    if (match) return await renderProposalDetail(match[1]);
    main.innerHTML = '<section class="empty-state"><div><h2>页面不存在</h2><a class="primary" href="/opportunities" data-link>返回机会页</a></div></section>';
  } catch (error) {
    main.innerHTML = errorView(error);
  }
}

function renderLogin() {
  main.innerHTML = `<section class="login-page"><div class="login-card">
    <span class="mock-ribbon">${authStatus.mock_identity_available ? 'NON-PRODUCTION MOCK' : 'MANAGED IDP REQUIRED'}</span>
    <p class="eyebrow" style="margin-top:18px">INTERNAL ACCESS</p><h1>进入交易控制台</h1>
    <p class="lede">没有外部注册。正式环境使用托管身份源与 Passkey；本地 Mock 只验证已存在的内部用户。</p>
    ${authStatus.mock_identity_available ? `<form id="login-form"><label>内部用户名<input name="username" autocomplete="username" required placeholder="reviewer-1"></label><button class="primary">使用非生产身份进入</button><div class="form-error" role="alert"></div></form>` : '<div class="callout">托管 IdP 尚未接入。系统不会降级为本地密码登录。</div>'}
  </div></section>`;
  document.querySelector('#login-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const result = await api('/api/auth/mock/login', {method:'POST', body: JSON.stringify({username: new FormData(form).get('username')})});
      session = result.session;
      setShell(true);
      history.replaceState({}, '', '/opportunities');
      await route();
    } catch (error) {
      form.querySelector('.form-error').textContent = `${error.code}: ${error.message}`;
    } finally { button.disabled = false; }
  });
}

async function renderOpportunities() {
  const result = await api('/api/opportunities');
  const items = result.data;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">PERPTAPE · ${escapeHtml(result.source_contract_version)}</p><h1>当前机会</h1><p class="lede">这里只展示 Perptape 实际返回的突破候选。数据健康、方向和时间保留来源语义；交易数量与风险由 Trading 独立决定。</p></div><div class="toolbar"><button class="secondary" data-refresh>刷新事实</button></div></header>
    <div class="stats"><div class="stat"><small>当前候选</small><b>${items.length}</b></div><div class="stat"><small>可冻结</small><b>${items.filter(i => i.readiness === 'READY').length}</b></div><div class="stat"><small>数据截止</small><b style="font-size:14px">${fmtDate(result.as_of)}</b></div><div class="stat"><small>执行环境</small><b style="font-size:14px">SHADOW</b></div></div>
    ${items.length ? `<div class="card-grid">${items.map(opportunityCard).join('')}</div>` : '<section class="empty-state"><div><h2>Perptape 当前没有返回候选</h2><p>这不是零风险或无行情，只表示当前接口数据为空。</p></div></section>'}
  </section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  document.querySelectorAll('[data-create-system]').forEach((button) => button.addEventListener('click', () => openSystemDialog(button.dataset.createSystem)));
}

function opportunityCard(item) {
  const directionClass = item.direction === 'LONG' ? 'direction-long' : 'direction-short';
  return `<article class="card"><div class="card-top"><div><span class="subtle">${escapeHtml(item.venue)} · ${escapeHtml(item.timeframe)}</span><div class="symbol">${escapeHtml(item.symbol)}</div></div><span class="tag ${directionClass}">${escapeHtml(item.direction)}</span></div>
    <div class="metric-row"><div><small>参考价格</small><b>${fmtNumber(item.reference_price)}</b></div><div><small>触发时间</small><b>${fmtDate(item.triggered_at)}</b></div><div><small>数据状态</small><b>${escapeHtml(item.readiness)}</b></div></div>
    <p class="subtle">${escapeHtml(item.rationale)}</p><div class="card-actions"><a class="text-button" href="${escapeHtml(item.detail_url)}" target="_blank" rel="noreferrer">Perptape 详情 ↗</a><button class="primary" data-create-system="${escapeHtml(item.candidate_id)}" ${item.readiness !== 'READY' ? 'disabled' : ''}>创建提案</button></div></article>`;
}

function openSystemDialog(candidateId) {
  const form = document.querySelector('#system-proposal-form');
  form.reset();
  form.elements.candidate_id.value = candidateId;
  form.elements.account_id.value = 'acct-1';
  form.elements.expires_in_minutes.value = '120';
  form.elements.rationale.value = 'Perptape 候选进入人工审核，尚未形成任何订单。';
  document.querySelector('#system-form-error').textContent = '';
  dialog.showModal();
}

async function renderManualProposal() {
  const result = await api('/api/instruments');
  instruments = result.data;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">MANUAL · SHADOW</p><h1>创建人工提案</h1><p class="lede">输入方向、数量、风险上限和失效条件。提交后字段被冻结，任何变更都应创建新版本。</p></div></header>
    <form id="manual-form" class="form-panel"><div class="field-grid">
      <label>账户<input name="account_id" value="acct-1" required></label>
      <label>交易标的<select name="instrument_id" required>${instruments.map(i => `<option value="${i.instrument_id}" data-venue="${escapeHtml(i.venue)}">${escapeHtml(i.venue)} · ${escapeHtml(i.symbol)}</option>`).join('')}</select></label>
      <label>方向<select name="direction"><option>LONG</option><option>SHORT</option></select></label>
      <label>风险档位<select name="risk_tier"><option>LOW</option><option selected>MEDIUM</option><option>HIGH</option></select></label>
      <label>总数量上限<input name="quantity" type="number" step="any" min="0" required></label>
      <label>初仓数量<input name="initial_quantity" type="number" step="any" min="0"></label>
      <label>最大风险<input name="max_risk" type="number" step="any" min="0" required></label>
      <label>触发价格<input name="trigger_price" type="number" step="any" min="0" required></label>
      <label>限价（可选）<input name="limit_price" type="number" step="any" min="0"></label>
      <label>失效价格<input name="invalidation_price" type="number" step="any" min="0" required></label>
      <label>允许 AUTO_ADD<select name="allow_auto_add"><option value="false" selected>否</option><option value="true">是</option></select></label>
      <label>预授权 AddUnit<select name="requested_adds"><option value="0" selected>0</option><option value="1">1</option><option value="2">2</option><option value="3">3</option></select></label>
      <label>Add 触发价格<input name="add_trigger_price" type="number" step="any" min="0"></label>
      <label>有效分钟<input name="expires_in_minutes" type="number" min="5" max="1440" value="120" required></label>
    </div><label style="margin-top:16px">提案理由<textarea name="rationale" rows="4" required></textarea></label>
    <div class="safety-note">该表单不会下单；它只创建冻结提案并进入 Reviewer 队列。真实发送能力仍为 DISABLED。</div><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">提交审核</button></div></form></section>`;
  document.querySelector('#manual-form').addEventListener('submit', submitManualProposal);
}

async function submitManualProposal(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  const selected = instruments.find(i => i.instrument_id === data.instrument_id);
  const button = form.querySelector('button');
  button.disabled = true;
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
    showToast('MANUAL 提案已冻结并进入审核');
    navigate(`/proposals/${result.proposal_id}`);
  } catch (error) { form.querySelector('.form-error').textContent = `${error.code}: ${error.message}`; button.disabled = false; }
}

async function renderProposalList(status, title) {
  const result = await api(`/api/proposals${status ? `?proposal_status=${status}` : ''}`);
  const items = result.data;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">AUTHORITATIVE PROPOSALS</p><h1>${escapeHtml(title)}</h1><p class="lede">每一行都是 PostgreSQL 中的当前权威状态，不把通知或界面缓存当作审批结果。</p></div></header>
    ${items.length ? `<div class="table-wrap"><table><thead><tr><th>来源 / 标的</th><th>方向</th><th>风险</th><th>状态</th><th>版本</th><th>到期</th></tr></thead><tbody>${items.map(item => `<tr data-href="/proposals/${item.proposal_id}"><td><b>${escapeHtml(item.source)}</b><br><span class="subtle">${escapeHtml(item.venue)} · ${shortId(item.instrument_id)}</span></td><td class="${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(item.direction)}</td><td>${escapeHtml(item.risk_tier)} · ${fmtNumber(item.max_risk)}</td><td><b class="status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</b></td><td>v${item.version}</td><td>${fmtDate(item.expires_at)}</td></tr>`).join('')}</tbody></table></div>` : '<section class="empty-state"><div><h2>当前没有匹配提案</h2><p>队列为空不代表已批准或已执行任何交易。</p></div></section>'}</section>`;
  document.querySelectorAll('tr[data-href]').forEach(row => row.addEventListener('click', () => navigate(row.dataset.href)));
}

async function renderProposalDetail(id) {
  const item = await api(`/api/proposals/${id}`);
  const canReview = roleNames().includes('REVIEWER') || roleNames().includes('SYSTEM_ADMIN');
  const canOperate = roleNames().includes('OPERATOR') || roleNames().includes('SYSTEM_ADMIN');
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">${escapeHtml(item.environment)} · ${escapeHtml(item.source)}</p><h1>提案 ${shortId(item.proposal_id)}</h1><p class="lede">状态 <b class="status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</b> · 权威版本 v${item.version}</p></div><div class="toolbar"><a class="secondary" href="/reviews" data-link>返回队列</a></div></header>
    <div class="detail-layout"><div class="stack"><article class="card"><h2>冻结交易语义</h2><dl class="definition-grid">${definition('账户', item.account_id)}${definition('场所', item.venue)}${definition('方向', item.direction)}${definition('风险档位', item.risk_tier)}${definition('总数量上限', fmtNumber(item.quantity))}${definition('初仓数量', fmtNumber(item.frozen_payload?.details?.initial_quantity || item.quantity))}${definition('风险上限', fmtNumber(item.max_risk))}${definition('AUTO_ADD', item.frozen_payload?.details?.allow_auto_add ? `允许 · ${item.frozen_payload.details.requested_adds} Unit` : '不允许')}${definition('Add 触发价', fmtNumber(item.frozen_payload?.details?.add_trigger_price))}${definition('来源候选', item.source_candidate_id || 'MANUAL')}${definition('来源时间', fmtDate(item.source_observed_at))}</dl></article>
      <article class="card"><h2>理由与来源事实</h2><pre>${escapeHtml(JSON.stringify(item.frozen_payload, null, 2))}</pre></article>
      <article class="card"><h2>审核记录</h2>${item.approvals.length ? item.approvals.map(a => `<div class="callout"><b>${escapeHtml(a.decision)}</b> · ${escapeHtml(a.reason)}<br><span class="subtle">${shortId(a.reviewer_id)} · ${fmtDate(a.created_at)}</span></div>`).join('') : '<p class="subtle">尚无 Reviewer 投票。</p>'}</article></div>
      <aside class="stack"><article class="card"><h2>安全动作</h2><p class="subtle">批准要求对象版本绑定的短时 step-up。拒绝只会收紧，不生成授权。</p>${item.status === 'PENDING_REVIEW' && canReview ? `<label>审核理由<textarea id="review-reason" rows="3">已核对冻结字段与风险范围</textarea></label><div class="toolbar" style="margin-top:12px"><button class="primary" data-approve>Step-up 并批准</button><button class="danger" data-reject>拒绝</button></div><div class="form-error" id="review-error"></div>` : '<p class="safety-note">当前状态或角色没有可用审核动作。</p>'}</article>
      <article class="card"><h2>风险决定</h2>${item.risk_decision ? `<p><b class="status-${item.risk_decision.result}">${escapeHtml(item.risk_decision.result)}</b></p>${definition('批准数量', item.risk_decision.approved_quantity)}${definition('风险金额', item.risk_decision.risk_amount)}<p class="subtle">${escapeHtml(item.risk_decision.reasons.join(' · '))}</p>` : '<p class="subtle">尚未运行服务端确定性 Risk Engine。</p>'}${item.status === 'APPROVED' && canOperate && !item.risk_decision ? '<button class="primary" data-risk>运行 RiskDecision</button>' : ''}</article>
      <article class="card"><h2>短期交易授权</h2>${item.authorization ? `<p><b>${shortId(item.authorization.authorization_id)}</b></p>${definition('数量上限', item.authorization.quantity_limit)}${definition('风险上限', item.authorization.risk_limit)}${definition('AddUnit', `${item.authorization.used_adds} / ${item.authorization.allowed_adds}`)}${definition('到期', fmtDate(item.authorization.expires_at))}` : '<p class="subtle">尚无 TradingAuthorization。</p>'}${item.risk_decision && item.risk_decision.result !== 'DENY' && canOperate && !item.authorization ? '<button class="primary" data-authorize>签发短期授权</button>' : ''}${item.authorization?.active && canOperate ? '<button class="primary" data-initial style="margin-top:10px">创建 SHADOW 初仓意图</button>' : ''}<p class="safety-note">创建意图会原子预留风险，但不会连接交易所。必须先在仓位页记录当前 SHADOW 仓位与权益事实。</p></article></aside></div></section>`;
  document.querySelector('[data-approve]')?.addEventListener('click', () => approveProposal(item));
  document.querySelector('[data-reject]')?.addEventListener('click', () => rejectProposal(item));
  document.querySelector('[data-risk]')?.addEventListener('click', () => runRisk(item));
  document.querySelector('[data-authorize]')?.addEventListener('click', () => authorize(item));
  document.querySelector('[data-initial]')?.addEventListener('click', () => createInitialIntent(item));
}

const definition = (label, value) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? '—')}</dd></div>`;

async function approveProposal(item) {
  const errorBox = document.querySelector('#review-error');
  try {
    const grant = await api('/api/auth/mock/step-up', {method:'POST', body:JSON.stringify({action:'proposal.approve', object_id:item.proposal_id, object_version:item.version})});
    await api(`/api/proposals/${item.proposal_id}/reviews`, {method:'POST', body:JSON.stringify({decision:'APPROVE', reason:document.querySelector('#review-reason').value, expected_version:item.version, action_grant:grant.action_grant})});
    showToast('Reviewer 投票已原子记录'); await route();
  } catch (error) { errorBox.textContent = `${error.code}: ${error.message}`; }
}

async function rejectProposal(item) {
  try {
    await api(`/api/proposals/${item.proposal_id}/reviews`, {method:'POST', body:JSON.stringify({decision:'REJECT', reason:document.querySelector('#review-reason').value, expected_version:item.version})});
    showToast('提案已拒绝'); await route();
  } catch (error) { document.querySelector('#review-error').textContent = `${error.code}: ${error.message}`; }
}

async function runRisk(item) {
  try { await api(`/api/proposals/${item.proposal_id}/risk-decisions`, {method:'POST', body:JSON.stringify({idempotency_key:crypto.randomUUID()})}); showToast('RiskDecision 已保存'); await route(); }
  catch (error) { showToast(`${error.code}: ${error.message}`); }
}

async function authorize(item) {
  const allowedAdds = item.frozen_payload?.details?.allow_auto_add ? Number(item.frozen_payload.details.requested_adds || 0) : 0;
  try { await api(`/api/proposals/${item.proposal_id}/authorizations`, {method:'POST', body:JSON.stringify({idempotency_key:crypto.randomUUID(), expires_in_minutes:30, allowed_adds:allowedAdds})}); showToast('短期授权已签发'); await route(); }
  catch (error) { showToast(`${error.code}: ${error.message}`); }
}

async function createInitialIntent(item) {
  try {
    const initialQuantity = item.frozen_payload?.details?.initial_quantity || item.authorization.quantity_limit;
    const result = await api(`/api/authorizations/${item.authorization.authorization_id}/intents`, {method:'POST', body:JSON.stringify({kind:'INITIAL', account_id:item.account_id, venue:item.venue, instrument_id:item.instrument_id, direction:item.direction, quantity:initialQuantity, idempotency_key:crypto.randomUUID()})});
    showToast('风险已原子预留，SHADOW 初仓意图已创建'); navigate(`/campaigns/${result.campaign_id}`);
  } catch (error) { showToast(`${error.code}: ${error.message}`); }
}

async function loadCampaignDetails() {
  const result = await api('/api/campaigns');
  return Promise.all(result.data.map((item) => api(`/api/campaigns/${item.campaign_id}`)));
}

async function renderCampaignList() {
  const result = await api('/api/campaigns');
  const items = result.data;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">SHADOW OPERATIONS</p><h1>Campaign 运营台</h1><p class="lede">从短期授权、风险预留和订单意图，到成交、保护、减仓、对账和 PnL。所有发送动作均为本地 SHADOW 事实。</p></div><div class="toolbar"><a class="secondary" href="/proposals" data-link>全部提案</a></div></header>
    <div class="stats"><div class="stat"><small>Campaign</small><b>${items.length}</b></div><div class="stat"><small>Open / Opening</small><b>${items.filter(i => ['OPEN','OPENING'].includes(i.status)).length}</b></div><div class="stat"><small>Unknown</small><b>${items.filter(i => i.status === 'UNKNOWN').length}</b></div><div class="stat"><small>环境</small><b style="font-size:14px">SHADOW ONLY</b></div></div>
    ${items.length ? `<div class="table-wrap"><table><thead><tr><th>Campaign</th><th>范围</th><th>方向 / 目标</th><th>状态</th><th>PnL</th><th>更新时间</th></tr></thead><tbody>${items.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td><b>${shortId(item.campaign_id)}</b><br><span class="subtle">Proposal ${shortId(item.proposal_id)}</span></td><td>${escapeHtml(item.account_id)}<br><span class="subtle">${escapeHtml(item.venue)}</span></td><td class="${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(item.direction)} · ${fmtNumber(item.current_target_quantity)}</td><td><b class="status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</b></td><td>${fmtNumber(item.final_pnl)}</td><td>${fmtDate(item.updated_at)}</td></tr>`).join('')}</tbody></table></div>` : '<section class="empty-state"><div><h2>尚无 Campaign</h2><p>批准提案并签发短期授权后，Operator 才能创建 SHADOW 初仓意图。</p></div></section>'}</section>`;
  document.querySelectorAll('tr[data-href]').forEach(row => row.addEventListener('click', () => navigate(row.dataset.href)));
}

async function renderCampaignFacts(mode) {
  const details = await loadCampaignDetails();
  const titles = {positions:'仓位与保护', orders:'订单与成交', risk:'风险与目标'};
  let rows = '';
  if (mode === 'positions') rows = details.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(item.instrument?.symbol || shortId(item.instrument_id))}</td><td>${item.position ? `${fmtNumber(item.position.quantity)} @ ${fmtNumber(item.position.average_entry_price)}` : '无事实'}</td><td>${item.position ? escapeHtml(item.position.fact_status) : 'UNKNOWN'}</td><td>${item.protection ? `${escapeHtml(item.protection.status)} · ${item.protection.fully_covered ? '完整覆盖' : '覆盖不足'}` : '无保护事实'}</td><td>${fmtDate(item.position?.observed_at)}</td></tr>`).join('');
  if (mode === 'orders') rows = details.flatMap(item => item.intents.map(intent => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(intent.kind)}${intent.reduce_only ? ' · reduce-only' : ''}</td><td>${escapeHtml(intent.side)} ${fmtNumber(intent.quantity)}</td><td>${escapeHtml(intent.status)}</td><td>${intent.order ? `${escapeHtml(intent.order.venue_order_id)} · ${escapeHtml(intent.order.status)}` : '未记录 SHADOW send'}</td><td>${fmtDate(intent.updated_at)}</td></tr>`)).join('');
  if (mode === 'risk') rows = details.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(item.status)}</td><td>${item.reservations.map(r => `${escapeHtml(r.status)} ${fmtNumber(r.amount)}`).join(' · ') || '无预留'}</td><td>${fmtNumber(item.current_target_quantity)} · v${item.target_version}</td><td>${escapeHtml(item.target_urgency || '—')}</td><td>${escapeHtml(item.reconciliation?.status || '未对账')}</td></tr>`).join('');
  const headers = mode === 'positions' ? '<th>Campaign</th><th>标的</th><th>仓位</th><th>事实</th><th>保护</th><th>观测时间</th>' : mode === 'orders' ? '<th>Campaign</th><th>意图</th><th>方向 / 数量</th><th>状态</th><th>SHADOW Order</th><th>更新时间</th>' : '<th>Campaign</th><th>状态</th><th>风险预留</th><th>目标</th><th>紧迫度</th><th>对账</th>';
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">POSTGRESQL AUTHORITY</p><h1>${titles[mode]}</h1><p class="lede">这些页面直接读取当前权威状态；可重新计算的投影不另行持久化。</p></div></header>
    ${mode === 'risk' && roleNames().includes('SYSTEM_ADMIN') ? '<div class="form-panel compact-form"><h2>全局只收紧动作</h2><p class="safety-note">这些入口只能关闭 AUTO_ADD 或把系统切到 REDUCE_ONLY；不能从这里恢复新增风险。</p><div class="toolbar"><button class="danger" data-disable-global-add>关闭全局 AUTO_ADD</button><button class="danger" data-pause-new-risk>暂停所有新增风险</button></div></div><div style="height:16px"></div>' : ''}
    ${mode === 'positions' ? shadowFactsForm() : ''}
    ${rows ? `<div class="table-wrap"><table><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>` : '<section class="empty-state"><div><h2>当前没有可展示事实</h2></div></section>'}</section>`;
  document.querySelectorAll('tr[data-href]').forEach(row => row.addEventListener('click', () => navigate(row.dataset.href)));
  document.querySelector('#shadow-facts-form')?.addEventListener('submit', recordStartingFacts);
  document.querySelector('[data-disable-global-add]')?.addEventListener('click', () => campaignAction('/api/operations/auto-add/disable', {reason:'administrator disabled AUTO_ADD from Web', idempotency_key:crypto.randomUUID()}));
  document.querySelector('[data-pause-new-risk]')?.addEventListener('click', () => campaignAction('/api/operations/pause-new-risk', {reason:'administrator paused new risk from Web', idempotency_key:crypto.randomUUID()}));
}

function shadowFactsForm() {
  return `<form id="shadow-facts-form" class="form-panel compact-form"><h2>记录当前 SHADOW 事实</h2><p class="safety-note">这是 Operator 明确录入的合成非生产事实，不来自真实交易所，也不会被标记为真实账户数据。</p><div class="field-grid"><label>账户<input name="account_id" value="acct-1" required></label><label>场所<input name="venue" value="BINANCE" required></label><label>Instrument UUID<input name="instrument_id" required></label><label>仓位数量<input name="quantity" type="number" step="any" value="0" required></label><label>平均入场价<input name="average_entry_price" type="number" step="any" value="0" required></label><label>标记价<input name="mark_price" type="number" step="any" required></label><label>权益<input name="equity" type="number" step="any" required></label><label>可用余额<input name="available_balance" type="number" step="any" required></label><label>币种<input name="currency" value="USDT" required></label></div><div class="form-error"></div><div class="form-actions"><button class="primary">记录合成事实</button></div></form><div style="height:16px"></div>`;
}

async function recordStartingFacts(event) {
  event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form));
  try {
    await api('/api/facts/positions', {method:'POST', body:JSON.stringify({account_id:data.account_id, venue:data.venue, instrument_id:data.instrument_id, quantity:data.quantity, average_entry_price:data.average_entry_price, mark_price:data.mark_price, known:true})});
    await api('/api/facts/account-equity', {method:'POST', body:JSON.stringify({account_id:data.account_id, venue:data.venue, equity:data.equity, available_balance:data.available_balance, currency:data.currency, known:true})});
    showToast('当前 SHADOW 仓位与权益事实已记录'); await route();
  } catch (error) { form.querySelector('.form-error').textContent = `${error.code}: ${error.message}`; }
}

async function renderCapitalCenter() {
  const result = await api('/api/capital');
  const item = result.data;
  const canTreasury = roleNames().includes('TREASURY_ADMIN');
  const automation = item.automation || {gates:{}, policies:[]};
  const balanceRows = item.balances.map(balance => `<tr><td>${escapeHtml(balance.location_type)}<br><span class="subtle">${escapeHtml(balance.location_id)}</span></td><td>${escapeHtml(balance.environment)} · ${escapeHtml(balance.venue)}</td><td>${fmtNumber(balance.confirmed_available)} ${escapeHtml(balance.asset)}</td><td>${fmtNumber(balance.source_reserved)}</td><td><b>${fmtNumber(balance.effective_available)}</b></td><td>${escapeHtml(balance.control_status)} / ${escapeHtml(balance.deposit_status)}</td><td>${fmtDate(balance.observed_at)}</td></tr>`).join('');
  const proposalRows = item.proposals.map(proposal => {
    const actions = [];
    if (canTreasury && proposal.status === 'DRAFT' && proposal.proposer_id === session.user_id) actions.push(`<button class="secondary" data-cap-submit="${proposal.transfer_proposal_id}">提交</button>`);
    if (canTreasury && proposal.status === 'PENDING_REVIEW' && proposal.proposer_id !== session.user_id) actions.push(`<button class="secondary" data-cap-review="${proposal.transfer_proposal_id}" data-version="${proposal.version}">批准</button>`);
    if (canTreasury && proposal.status === 'APPROVED' && !proposal.authorization && proposal.proposer_id !== session.user_id) actions.push(`<button class="secondary" data-cap-authorize="${proposal.transfer_proposal_id}">签发授权</button>`);
    if (canTreasury && proposal.authorization?.active && proposal.proposer_id !== session.user_id) actions.push(`<button class="primary" data-cap-execute="${proposal.authorization.transfer_authorization_id}">Mock 执行</button>`);
    return `<tr><td>${shortId(proposal.transfer_proposal_id)}<br><span class="subtle">v${proposal.version}</span></td><td>${escapeHtml(proposal.direction)}<br><span class="subtle">${escapeHtml(proposal.purpose)}</span></td><td>${escapeHtml(proposal.source_id)} → ${escapeHtml(proposal.destination_id)}</td><td>${fmtNumber(proposal.amount)} ${escapeHtml(proposal.asset)}</td><td><b>${escapeHtml(proposal.status)}</b></td><td><div class="toolbar">${actions.join('')}</div></td></tr>`;
  }).join('');
  const transferRows = item.transfers.map(transfer => {
    const actions = [];
    if (canTreasury && transfer.status === 'SUBMITTED') actions.push(`<button class="secondary" data-cap-observe="${transfer.capital_transfer_id}" data-status="IN_FLIGHT">标记在途</button>`);
    if (canTreasury && ['SUBMITTED','IN_FLIGHT'].includes(transfer.status)) actions.push(`<button class="danger" data-cap-observe="${transfer.capital_transfer_id}" data-status="UNKNOWN">标记 Unknown</button>`);
    if (canTreasury && ['IN_FLIGHT','UNKNOWN','MANUAL_REQUIRED'].includes(transfer.status)) actions.push(`<button class="secondary" data-cap-confirm="${transfer.capital_transfer_id}">目的端确认</button>`);
    if (canTreasury) actions.push(`<button class="secondary" data-cap-reconcile="${transfer.capital_transfer_id}">对账</button>`);
    return `<tr><td>${shortId(transfer.capital_transfer_id)}</td><td>${escapeHtml(transfer.direction)}</td><td>${fmtNumber(transfer.gross_amount)} ${escapeHtml(transfer.asset)}</td><td><b>${escapeHtml(transfer.status)}</b><br><span class="subtle">${escapeHtml(transfer.reconciliation_status)}</span></td><td>${escapeHtml(transfer.external_transfer_id || '未提交')}</td><td><div class="toolbar">${actions.join('')}</div></td></tr>`;
  }).join('');
  const treasuryForms = canTreasury ? `<div class="detail-layout"><form id="capital-proposal-form" class="form-panel compact-form"><h2>人工资金 Proposal</h2><p class="safety-note">独立于交易 Proposal；两名不同 Treasury Reviewer 批准后才可签发一次性授权。</p><div class="field-grid"><label>环境<select name="environment"><option>TESTNET</option><option>SHADOW</option></select></label><label>方向<select name="direction"><option>VAULT_TO_VENUE</option><option>VENUE_TO_VAULT</option></select></label><label>交易账户<input name="account_id" value="acct-1" required></label><label>场所<input name="venue" value="BINANCE" required></label><label>Vault ID<input name="vault_id" value="vault-1" required></label><label>资产<input name="asset" value="USDT" required></label><label>网络<input name="network" value="TESTNET" required></label><label>目的端引用<input name="destination_reference" value="approved-test-destination" required></label><label>Gross 金额<input name="amount" type="number" step="any" min="0" required></label><label>最大费用<input name="max_fee" type="number" step="any" min="0" value="1" required></label><label>最小到账<input name="min_received" type="number" step="any" min="0" required></label><label>有效分钟<input name="expires_in_minutes" type="number" min="5" value="120" required></label></div><label>理由<textarea name="reason" rows="2" required>manual test capital allocation</textarea></label><button class="primary">创建草稿</button></form><form id="capital-fact-form" class="form-panel compact-form"><h2>Mock 只读资金事实</h2><p class="safety-note">只写入 TESTNET/SHADOW 观测，不连接 Vault、链或交易所，不移动资金。</p><div class="field-grid"><label>位置<select name="location_type"><option>VAULT</option><option>VENUE</option></select></label><label>位置 ID<input name="location_id" value="vault-1" required></label><label>场所<input name="venue" value="BINANCE" required></label><label>资产<input name="asset" value="USDT" required></label><label>权益<input name="equity" type="number" step="any" required></label><label>已确认可用<input name="available_balance" type="number" step="any" required></label><label>可划出<input name="withdrawable_balance" type="number" step="any" required></label><label>网络<input name="network" value="TESTNET"></label><label>控制状态<select name="control_status"><option>CONTROLLED</option><option>READ_ONLY</option><option>UNKNOWN</option></select></label><label>充值状态<select name="deposit_status"><option>READY</option><option>PENDING</option><option>UNKNOWN</option></select></label></div><button class="secondary">记录 Mock 事实</button></form></div>` : '';
  const policyRows = automation.policies.map(policy => `<tr><td>${escapeHtml(policy.environment)} · ${escapeHtml(policy.account_id)} / ${escapeHtml(policy.venue)}<br><span class="subtle">${escapeHtml(policy.asset)} · v${policy.version}</span></td><td>${fmtNumber(policy.operating_low)} / <b>${fmtNumber(policy.operating_target)}</b> / ${fmtNumber(policy.operating_high)}</td><td>${fmtNumber(policy.vault_minimum_reserve)} / ${fmtNumber(policy.minimum_transfer)}–${fmtNumber(policy.maximum_transfer)}</td><td><div class="toolbar"><button class="secondary" data-cap-scope-reconcile="${policy.policy_id}" data-environment="${policy.environment}" data-account="${escapeHtml(policy.account_id)}" data-venue="${escapeHtml(policy.venue)}">空仓对账</button><button class="secondary" data-cap-auto="${policy.policy_id}" data-purpose="AUTO_PROFIT_SWEEP" ${automation.gates.AUTO_PROFIT_SWEEP !== 'ENABLED' ? 'disabled' : ''}>评估利润归集</button><button class="secondary" data-cap-auto="${policy.policy_id}" data-purpose="AUTO_OPERATING_REFILL" ${automation.gates.AUTO_OPERATING_REFILL !== 'ENABLED' ? 'disabled' : ''}>评估运营补充</button></div></td></tr>`).join('');
  const automationPanel = `<section><h2>自动资金候选</h2><p class="safety-note">利润归集与运营补充使用独立 Gate，当前只生成需双人复核和独立授权的候选 Proposal，不自动提交资金。浮盈、活动仓位、订单、Unknown 或非 MATCH 均阻断。</p><div class="stats"><div class="stat"><small>AUTO_PROFIT_SWEEP</small><b style="font-size:14px">${escapeHtml(automation.gates.AUTO_PROFIT_SWEEP || 'MISSING')}</b></div><div class="stat"><small>AUTO_OPERATING_REFILL</small><b style="font-size:14px">${escapeHtml(automation.gates.AUTO_OPERATING_REFILL || 'MISSING')}</b></div></div>${canTreasury ? `<form id="capital-policy-form" class="form-panel compact-form"><h3>SHADOW / TESTNET 运营阈值</h3><div class="field-grid"><label>环境<select name="environment"><option>TESTNET</option><option>SHADOW</option></select></label><label>交易账户<input name="account_id" value="acct-1" required></label><label>场所<input name="venue" value="BINANCE" required></label><label>Vault ID<input name="vault_id" value="vault-1" required></label><label>资产<input name="asset" value="USDT" required></label><label>网络<input name="network" value="TESTNET" required></label><label>Vault 目的端引用<input name="vault_destination_reference" value="approved-test-vault" required></label><label>场所目的端引用<input name="venue_destination_reference" value="approved-test-venue" required></label><label>运营下沿<input name="operating_low" type="number" step="any" value="400" required></label><label>运营目标<input name="operating_target" type="number" step="any" value="500" required></label><label>运营上沿<input name="operating_high" type="number" step="any" value="600" required></label><label>Vault 最低储备<input name="vault_minimum_reserve" type="number" step="any" value="500" required></label><label>最小划转<input name="minimum_transfer" type="number" step="any" value="10" required></label><label>最大划转<input name="maximum_transfer" type="number" step="any" value="200" required></label><label>最大费用<input name="max_fee" type="number" step="any" value="1" required></label></div><button class="secondary">保存非生产策略</button></form>` : ''}${policyRows ? `<div class="table-wrap"><table><thead><tr><th>作用域</th><th>下沿 / 目标 / 上沿</th><th>Vault 储备 / 划转限额</th><th>动作</th></tr></thead><tbody>${policyRows}</tbody></table></div>` : '<div class="callout">尚无资金自动化策略；两个 Gate 默认关闭。</div>'}</section>`;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">CAPITAL AUTHORITY · MOCK ONLY</p><h1>资金中心</h1><p class="lede">Vault 与交易所已确认资本、源端预留、在途和目的端确认保持唯一归属。Telegram 只通知，不能批准或执行资金动作。</p></div></header><div class="stats"><div class="stat"><small>真实划转 Gate</small><b style="font-size:14px">${escapeHtml(item.real_transfer_gate)}</b></div><div class="stat"><small>资金位置</small><b>${item.balances.length}</b></div><div class="stat"><small>在途 / 占用</small><b>${fmtNumber(item.in_transit)}</b></div><div class="stat"><small>外部适配</small><b style="font-size:14px">NOT CONFIGURED</b></div></div>${treasuryForms}${automationPanel}<section><h2>确认资本与源端预留</h2>${balanceRows ? `<div class="table-wrap"><table><thead><tr><th>位置</th><th>环境 / 场所</th><th>已确认可用</th><th>源端预留</th><th>有效可用</th><th>控制 / 充值</th><th>观测</th></tr></thead><tbody>${balanceRows}</tbody></table></div>` : '<div class="callout">尚无 Vault 或交易所资金事实。</div>'}</section><section><h2>资金 Proposal</h2>${proposalRows ? `<div class="table-wrap"><table><thead><tr><th>Proposal</th><th>方向 / 用途</th><th>路径</th><th>金额</th><th>状态</th><th>动作</th></tr></thead><tbody>${proposalRows}</tbody></table></div>` : '<div class="callout">尚无资金 Proposal。</div>'}</section><section><h2>Capital Transfer</h2>${transferRows ? `<div class="table-wrap"><table><thead><tr><th>Transfer</th><th>方向</th><th>Gross</th><th>状态 / 对账</th><th>外部引用</th><th>动作</th></tr></thead><tbody>${transferRows}</tbody></table></div>` : '<div class="callout">尚无划转状态。</div>'}</section></section>`;
  bindCapitalActions();
}

function bindCapitalActions() {
  document.querySelector('#capital-proposal-form')?.addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); data.expires_in_minutes = Number(data.expires_in_minutes); data.idempotency_key = crypto.randomUUID(); try { await api('/api/capital/proposals', {method:'POST', body:JSON.stringify(data)}); showToast('资金 Proposal 草稿已创建'); await route(); } catch (error) { showToast(`${error.code}: ${error.message}`); } });
  document.querySelector('#capital-fact-form')?.addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); data.environment = 'TESTNET'; data.address_reference = 'masked-test-reference'; data.known = true; try { await api('/api/capital/balances/mock', {method:'POST', body:JSON.stringify(data)}); showToast('Mock 只读资金事实已记录'); await route(); } catch (error) { showToast(`${error.code}: ${error.message}`); } });
  document.querySelector('#capital-policy-form')?.addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); data.idempotency_key = crypto.randomUUID(); try { await api('/api/capital/automation/policies', {method:'POST', body:JSON.stringify(data)}); showToast('非生产资金阈值已保存；Gate 状态未改变'); await route(); } catch (error) { showToast(`${error.code}: ${error.message}`); } });
  document.querySelectorAll('[data-cap-scope-reconcile]').forEach(button => button.addEventListener('click', () => capitalAction('/api/capital/reconciliations', {environment:button.dataset.environment, account_id:button.dataset.account, venue:button.dataset.venue})));
  document.querySelectorAll('[data-cap-auto]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/automation/policies/${button.dataset.capAuto}/evaluate`, {purpose:button.dataset.purpose, idempotency_key:crypto.randomUUID()})));
  document.querySelectorAll('[data-cap-submit]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/proposals/${button.dataset.capSubmit}/submit`, {})));
  document.querySelectorAll('[data-cap-review]').forEach(button => button.addEventListener('click', async () => { try { const proposalId = button.dataset.capReview; const version = Number(button.dataset.version); const grant = await api('/api/auth/mock/step-up', {method:'POST', body:JSON.stringify({action:'capital.approve', object_id:proposalId, object_version:version})}); await api(`/api/capital/proposals/${proposalId}/reviews`, {method:'POST', body:JSON.stringify({decision:'APPROVE', reason:'independent Treasury review', expected_version:version, action_grant:grant.action_grant})}); showToast('资金审核已记录'); await route(); } catch (error) { showToast(`${error.code}: ${error.message}`); } }));
  document.querySelectorAll('[data-cap-authorize]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/proposals/${button.dataset.capAuthorize}/authorizations`, {idempotency_key:crypto.randomUUID(), expires_in_minutes:30})));
  document.querySelectorAll('[data-cap-execute]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/authorizations/${button.dataset.capExecute}/transfers/mock`, {idempotency_key:crypto.randomUUID()})));
  document.querySelectorAll('[data-cap-observe]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/transfers/${button.dataset.capObserve}/observations/mock`, {status:button.dataset.status, transaction_reference:`mock-${crypto.randomUUID()}`})));
  document.querySelectorAll('[data-cap-confirm]').forEach(button => button.addEventListener('click', () => { const fee = prompt('Mock 费用', '1'); const net = prompt('Mock 实际到账', '99'); if (fee !== null && net !== null) capitalAction(`/api/capital/transfers/${button.dataset.capConfirm}/observations/mock`, {status:'DESTINATION_CONFIRMED', transaction_reference:`mock-${crypto.randomUUID()}`, fee_amount:fee, net_received:net}); }));
  document.querySelectorAll('[data-cap-reconcile]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/transfers/${button.dataset.capReconcile}/reconcile`, {})));
}

async function capitalAction(path, body) { try { await api(path, {method:'POST', body:JSON.stringify(body)}); showToast('资金权威状态已更新（Mock）'); await route(); } catch (error) { showToast(`${error.code}: ${error.message}`); } }

async function renderExceptions() {
  const result = await api('/api/campaign-exceptions'); const items = result.data;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">FAIL CLOSED</p><h1>异常处理</h1><p class="lede">Unknown、保护不足与对账差异均阻止新增风险或自动重发。这里只显示可由权威状态派生的当前异常。</p></div><button class="secondary" data-refresh>重新计算视图</button></header>
    ${items.length ? `<div class="card-grid">${items.map(item => `<article class="card"><span class="tag">${escapeHtml(item.severity)}</span><h2 style="margin-top:16px">${escapeHtml(item.code)}</h2><p class="subtle">Campaign ${shortId(item.campaign_id)}</p>${item.details.length ? `<pre>${escapeHtml(item.details.join('\n'))}</pre>` : ''}<a class="primary" href="/campaigns/${item.campaign_id}" data-link>处理 Campaign</a></article>`).join('')}</div>` : '<section class="empty-state"><div><h2>当前没有派生异常</h2><p>这只表示当前数据库事实未触发异常条件。</p></div></section>'}</section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
}

async function renderBinanceReadOnly() {
  const binanceRole = session.roles.find((item) => item.venue_scope === 'BINANCE' && item.account_scope);
  const accountId = new URLSearchParams(location.search).get('account_id') || binanceRole?.account_scope || 'acct-1';
  const [status, response] = await Promise.all([
    api('/api/venues/binance/status'),
    api(`/api/venues/binance/facts?account_id=${encodeURIComponent(accountId)}`),
  ]);
  const facts = response.data;
  const canSync = status.enabled && status.configured;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">${escapeHtml(status.fact_environment)} · BINANCE USDⓈ-M · USER_DATA</p><h1>Binance 私有事实</h1><p class="lede">只读同步 Instrument、订单、成交、仓位、保护、权益和资金费。此适配器没有下单方法，LIVE_ORDER_SEND 仍为 DISABLED。</p></div><div class="toolbar"><button class="secondary" data-refresh>刷新本地事实</button></div></header>
    <div class="stats"><div class="stat"><small>读取开关</small><b style="font-size:14px">${status.enabled ? 'ENABLED' : 'DISABLED'}</b></div><div class="stat"><small>凭据配置</small><b style="font-size:14px">${status.configured ? 'CONFIGURED' : 'NOT CONFIGURED'}</b></div><div class="stat"><small>订单发送</small><b style="font-size:14px">UNAVAILABLE</b></div><div class="stat"><small>本地事实截止</small><b style="font-size:14px">${fmtDate(response.as_of)}</b></div></div>
    <article class="card"><h2>读取作用域</h2><form id="binance-account-form" class="inline-form"><label>内部账户<input name="account_id" value="${escapeHtml(accountId)}" required></label><button class="secondary">查看本地事实</button></form>${canSync ? `<form id="binance-sync-form" class="inline-form"><input name="account_id" type="hidden" value="${escapeHtml(accountId)}"><label>Binance Symbol<input name="symbol" value="BTCUSDT" pattern="[A-Z0-9_]+" required></label><button class="primary">从 Binance 只读同步</button><span class="form-error" role="alert"></span></form>` : '<p class="safety-note">真实 USER_DATA 读取保持关闭或未配置。页面只展示已持久化事实；不会尝试网络连接。</p>'}</article>
    ${venueFactSections(facts)}
  </section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  document.querySelector('#binance-account-form')?.addEventListener('submit', (event) => {
    event.preventDefault();
    const next = new FormData(event.currentTarget).get('account_id');
    navigate(`/venues/binance?account_id=${encodeURIComponent(next)}`);
  });
  document.querySelector('#binance-sync-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form));
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const result = await api('/api/venues/binance/sync', {method:'POST', body:JSON.stringify(payload)});
      showToast(`只读同步完成；对账 ${result.reconciliation.status}`);
      await route();
    } catch (error) {
      form.querySelector('.form-error').textContent = `${error.code}: ${error.message}`;
      button.disabled = false;
    }
  });
}

function venueFactSections(facts) {
  const positions = facts.positions.map(item => `<tr><td>${escapeHtml(item.symbol)}</td><td>${fmtNumber(item.quantity)} @ ${fmtNumber(item.average_entry_price)}</td><td>${fmtNumber(item.mark_price)}</td><td>${escapeHtml(item.fact_status)}</td><td>${item.protection ? `${escapeHtml(item.protection.status)} · ${item.protection.fully_covered ? '足额' : '不足'}` : '无保护事实'}</td><td>${fmtDate(item.observed_at)}</td></tr>`).join('');
  const orders = facts.orders.map(item => `<tr><td>${escapeHtml(item.venue_order_id)}</td><td>${escapeHtml(item.symbol)}</td><td>${escapeHtml(item.status)}</td><td>${fmtNumber(item.filled_quantity)} / ${fmtNumber(item.ordered_quantity)}</td><td>${item.intent_id ? shortId(item.intent_id) : '外部未绑定'}</td><td>${fmtDate(item.observed_at)}</td></tr>`).join('');
  const fills = facts.fills.map(item => `<tr><td>${escapeHtml(item.venue_fill_id)}</td><td>${escapeHtml(item.symbol)}</td><td>${escapeHtml(item.side)} ${fmtNumber(item.quantity)}</td><td>${fmtNumber(item.price)}</td><td>${fmtNumber(item.fee)} ${escapeHtml(item.fee_currency)}</td><td>${fmtDate(item.executed_at)}</td></tr>`).join('');
  const funding = facts.funding.map(item => `<tr><td>${escapeHtml(item.venue_payment_id)}</td><td>${escapeHtml(item.symbol)}</td><td>${fmtNumber(item.amount)} ${escapeHtml(item.currency)}</td><td>${fmtDate(item.paid_at)}</td></tr>`).join('');
  return `<div class="stats"><div class="stat"><small>权益</small><b>${fmtNumber(facts.equity?.equity)}</b></div><div class="stat"><small>可用余额</small><b>${fmtNumber(facts.equity?.available_balance)}</b></div><div class="stat"><small>权益状态</small><b style="font-size:14px">${escapeHtml(facts.equity?.fact_status || 'UNKNOWN')}</b></div><div class="stat"><small>观测时间</small><b style="font-size:14px">${fmtDate(facts.equity?.observed_at)}</b></div></div>
    ${factTable('仓位与保护', '<th>标的</th><th>数量 / 入场</th><th>标记价</th><th>事实</th><th>保护</th><th>观测时间</th>', positions)}
    ${factTable('当前场所订单', '<th>Venue Order</th><th>标的</th><th>状态</th><th>成交 / 委托</th><th>内部意图</th><th>观测时间</th>', orders)}
    ${factTable('最近成交', '<th>Venue Fill</th><th>标的</th><th>方向 / 数量</th><th>价格</th><th>手续费</th><th>成交时间</th>', fills)}
    ${factTable('资金费', '<th>Payment</th><th>标的</th><th>金额</th><th>支付时间</th>', funding)}`;
}

function factTable(title, headers, rows) {
  return `<section><h2>${escapeHtml(title)}</h2>${rows ? `<div class="table-wrap"><table><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="callout">当前没有已持久化事实。</div>'}</section>`;
}

async function renderCampaignDetail(id) {
  const item = await api(`/api/campaigns/${id}`); const canOperate = roleNames().includes('OPERATOR') || roleNames().includes('SYSTEM_ADMIN');
  let addCandidates = []; let addCandidateError = null;
  if (item.management?.allow_auto_add && Number(item.management.remaining_adds) > 0) {
    try { addCandidates = (await api(`/api/campaigns/${id}/add-candidates`)).data; }
    catch (error) { addCandidateError = `${error.code}: ${error.message}`; }
  }
  const active = item.intents.find(intent => ['READY','SENT','PARTIALLY_FILLED','UNKNOWN'].includes(intent.status));
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">SHADOW · ${escapeHtml(item.venue)}</p><h1>${escapeHtml(item.instrument?.symbol || 'Campaign')} ${shortId(item.campaign_id)}</h1><p class="lede"><b class="status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</b> · ${escapeHtml(item.direction)} · 目标 ${fmtNumber(item.current_target_quantity)}</p></div><div class="toolbar"><a class="secondary" href="/campaigns" data-link>返回运营台</a><button class="secondary" data-pnl>刷新 PnL</button><button class="secondary" data-reconcile>运行对账</button></div></header>
    <div class="stats"><div class="stat"><small>已实现 PnL</small><b>${fmtNumber(item.realized_pnl)}</b></div><div class="stat"><small>未实现 PnL</small><b>${fmtNumber(item.unrealized_pnl)}</b></div><div class="stat"><small>最终 / 当前 PnL</small><b>${fmtNumber(item.final_pnl)}</b></div><div class="stat"><small>对账</small><b style="font-size:14px">${escapeHtml(item.reconciliation?.status || 'NOT RUN')}</b></div></div>
    <div class="detail-layout"><div class="stack"><article class="card"><h2>订单意图与成交</h2>${item.intents.length ? item.intents.map(intentCard).join('') : '<p class="subtle">无订单意图。</p>'}${active && canOperate ? operationForm(active, item) : ''}</article><article class="card"><h2>仓位与保护</h2><dl class="definition-grid">${definition('仓位', item.position ? fmtNumber(item.position.quantity) : 'UNKNOWN')}${definition('平均入场', item.position ? fmtNumber(item.position.average_entry_price) : '—')}${definition('标记价', item.position ? fmtNumber(item.position.mark_price) : '—')}${definition('观测时间', fmtDate(item.position?.observed_at))}${definition('保护状态', item.protection?.status || 'UNKNOWN')}${definition('保护数量', item.protection ? fmtNumber(item.protection.quantity) : '—')}</dl>${canOperate ? positionProtectionForms(item) : ''}</article></div>
      <aside class="stack"><article class="card"><h2>风险与目标</h2>${item.reservations.map(r => `<div class="callout"><b>${escapeHtml(r.status)}</b> · ${fmtNumber(r.amount)}</div>`).join('') || '<p class="subtle">无风险预留。</p>'}<dl class="definition-grid">${definition('目标版本', `v${item.target_version}`)}${definition('紧迫度', item.target_urgency)}${definition('原因', item.target_reason)}</dl>${canOperate && item.position ? targetForm(item) : ''}</article>${managementPanel(item, addCandidates, addCandidateError, canOperate)}<article class="card"><h2>Sender fencing</h2>${definition('Owner', item.sender_lease?.owner_id)}${definition('Token', item.sender_lease?.fencing_token)}${definition('到期', fmtDate(item.sender_lease?.expires_at))}<p class="safety-note">Web 动作只记录合成 SHADOW Order。LIVE_ORDER_SEND 仍为 DISABLED。</p></article><article class="card"><h2>对账差异</h2>${item.reconciliation ? `<p><b>${escapeHtml(item.reconciliation.status)}</b></p><pre>${escapeHtml(item.reconciliation.differences.join('\n') || 'MATCH')}</pre>` : '<p class="subtle">尚未运行对账。</p>'}</article></aside></div></section>`;
  bindCampaignActions(item, active);
}

function intentCard(intent) { return `<div class="intent-row"><div><b>${escapeHtml(intent.kind)} · ${escapeHtml(intent.side)} ${fmtNumber(intent.quantity)}</b><br><span class="subtle">${shortId(intent.intent_id)} · ${intent.reduce_only ? 'REDUCE ONLY' : 'RISK ADDING'}</span></div><b class="status-${escapeHtml(intent.status)}">${escapeHtml(intent.status)}</b></div>${intent.order ? `<p class="subtle">SHADOW order ${escapeHtml(intent.order.venue_order_id)} · filled ${fmtNumber(intent.order.filled_quantity)}</p>` : ''}`; }

function operationForm(intent, item) { if (intent.status === 'UNKNOWN') return '<p class="safety-note">结果为 UNKNOWN：风险保持占用，不提供重发或释放按钮。必须人工对账。</p>'; if (intent.status === 'READY') return `<div class="action-panel"><h3>记录 SHADOW send</h3><label>合成 Venue Order ID<input id="venue-order-id" value="shadow-${intent.intent_id.slice(0,8)}"></label><button class="primary" data-shadow-send>获取 lease 并记录</button><button class="danger" data-unknown>标记 UNKNOWN</button></div>`; return `<form id="fill-form" class="action-panel"><h3>记录合成成交</h3><div class="field-grid"><label>Fill ID<input name="venue_fill_id" value="fill-${crypto.randomUUID().slice(0,8)}" required></label><label>方向<select name="side"><option ${intent.side === 'BUY' ? 'selected' : ''}>BUY</option><option ${intent.side === 'SELL' ? 'selected' : ''}>SELL</option></select></label><label>数量<input name="quantity" type="number" step="any" value="${escapeHtml(intent.quantity)}" required></label><label>成交价<input name="price" type="number" step="any" required></label><label>手续费<input name="fee" type="number" step="any" value="0"></label><label>币种<input name="fee_currency" value="${escapeHtml(item.instrument?.collateral_currency || 'USDT')}"></label><label>滑点成本<input name="slippage_cost" type="number" step="any" value="0"></label></div><div class="toolbar" style="margin-top:12px"><button class="primary">记录 SHADOW fill</button><button type="button" class="danger" data-unknown>标记 UNKNOWN</button></div></form>`; }

function positionProtectionForms(item) { return `<form id="position-form" class="action-panel"><h3>更新合成仓位事实</h3><div class="field-grid"><label>数量<input name="quantity" type="number" step="any" value="${escapeHtml(item.position?.quantity || '0')}" required></label><label>平均入场价<input name="average_entry_price" type="number" step="any" value="${escapeHtml(item.position?.average_entry_price || '0')}" required></label><label>标记价<input name="mark_price" type="number" step="any" value="${escapeHtml(item.position?.mark_price || '')}" required></label></div><button class="secondary">更新仓位</button></form>${item.position && Math.abs(Number(item.position.quantity)) > 0 ? `<form id="protection-form" class="action-panel"><h3>更新保护事实</h3><div class="field-grid"><label>保护 Order ID<input name="venue_order_id" value="${escapeHtml(item.protection?.venue_order_id || 'shadow-stop')}" required></label><label>保护数量<input name="quantity" type="number" step="any" value="${escapeHtml(Math.abs(Number(item.position.quantity)))}" required></label><label>触发价<input name="trigger_price" type="number" step="any" value="${escapeHtml(item.protection?.trigger_price || '')}" required></label><label>状态<select name="coverage"><option value="full">已知且完整</option><option value="degraded">已知但不足</option><option value="unknown">未知</option></select></label></div><button class="secondary">更新保护</button></form>` : ''}`; }

function targetForm(item) { return `<form id="target-form" class="action-panel"><h3>原子生成唯一减仓目标</h3><label>目标剩余数量<input name="target_quantity" type="number" step="any" min="0" max="${escapeHtml(Math.abs(Number(item.position.quantity)))}" required></label><label>紧迫度<select name="urgency"><option>NORMAL</option><option selected>URGENT</option><option>IMMEDIATE</option></select></label><label>原因<input name="reason" value="operator risk reduction" required></label><label>执行限价（Hyperliquid TESTNET 必填）<input name="limit_price" type="number" step="any" min="0"></label><button class="primary">生成 reduce-only 意图</button><button type="button" class="danger" data-auto-exit>评估冻结失效价并自动退出</button></form>`; }

function managementPanel(item, candidates, candidateError, canOperate) {
  const management = item.management || {};
  const candidateOptions = candidates.map(candidate => `<option value="${escapeHtml(candidate.candidate_id)}">${escapeHtml(candidate.timeframe)} · ${fmtNumber(candidate.reference_price)} · ${fmtDate(candidate.observed_at)}</option>`).join('');
  const addForm = canOperate && management.allow_auto_add && Number(management.remaining_adds) > 0
    ? `<form id="auto-add-form" class="action-panel"><h3>Perptape Add 候选</h3>${candidateError ? `<p class="safety-note">${escapeHtml(candidateError)}</p>` : candidateOptions ? `<label>后续候选<select name="candidate_id">${candidateOptions}</select></label><label>Add 数量<input name="quantity" type="number" step="any" min="0" max="${escapeHtml(management.remaining_quantity)}" required></label><button class="primary" ${management.auto_add_gate !== 'ENABLED' ? 'disabled' : ''}>最终风控并创建 Add 意图</button>` : '<p class="safety-note">当前没有同场所、同标的、同方向的后续 Perptape 候选。</p>'}</form>`
    : '<p class="safety-note">该 Campaign 没有剩余的已冻结 AddUnit，或 Proposal 未允许 AUTO_ADD。</p>';
  return `<article class="card"><h2>AUTO_ADD 管理</h2><dl class="definition-grid">${definition('全局 Gate', management.auto_add_gate)}${definition('AddUnit', `${item.authorization?.used_adds || 0} / ${item.authorization?.allowed_adds || 0}`)}${definition('剩余数量', fmtNumber(management.remaining_quantity))}${definition('冻结触发价', fmtNumber(management.add_trigger_price))}</dl>${addForm}${canOperate ? '<button class="danger" data-disable-campaign-add>关闭本 Campaign 后续 Add</button>' : ''}<p class="safety-note">只有首个真实正成交消费 AddUnit；零成交取消或拒绝不消费，UNKNOWN 冻结新增风险。</p></article>`;
}

function bindCampaignActions(item, active) {
  document.querySelector('[data-pnl]')?.addEventListener('click', () => campaignAction(`/api/campaigns/${item.campaign_id}/pnl`, {}));
  document.querySelector('[data-reconcile]')?.addEventListener('click', () => campaignAction(`/api/campaigns/${item.campaign_id}/reconcile`, {execution_scope:`${item.account_id}:${item.venue}`}));
  document.querySelector('[data-shadow-send]')?.addEventListener('click', async () => { const owner = `web-${session.user_id.slice(0,8)}`; try { const lease = await api('/api/sender-leases', {method:'POST', body:JSON.stringify({execution_scope:`${item.account_id}:${item.venue}`, owner_id:owner, lease_seconds:60})}); await api(`/api/intents/${active.intent_id}/shadow-send`, {method:'POST', body:JSON.stringify({execution_scope:`${item.account_id}:${item.venue}`, owner_id:owner, fencing_token:lease.fencing_token, venue_order_id:document.querySelector('#venue-order-id').value})}); showToast('已记录 SHADOW send；没有连接交易所'); await route(); } catch (error) { showToast(`${error.code}: ${error.message}`); } });
  document.querySelectorAll('[data-unknown]').forEach(button => button.addEventListener('click', () => campaignAction(`/api/intents/${active.intent_id}/unknown`, {reason:'operator marked uncertain SHADOW outcome'})));
  document.querySelector('#fill-form')?.addEventListener('submit', event => submitNamedForm(event, `/api/intents/${active.intent_id}/fills`));
  document.querySelector('#position-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction('/api/facts/positions', {...data, account_id:item.account_id, venue:item.venue, instrument_id:item.instrument_id, known:true}); });
  document.querySelector('#protection-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction(`/api/campaigns/${item.campaign_id}/protection`, {position_id:item.position.position_id, venue_order_id:data.venue_order_id, quantity:data.quantity, trigger_price:data.trigger_price, fully_covered:data.coverage === 'full', known:data.coverage !== 'unknown'}); });
  document.querySelector('#target-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction(`/api/campaigns/${item.campaign_id}/managed-reductions`, {target_quantity:data.target_quantity, urgency:data.urgency, reason:data.reason, limit_price:data.limit_price || null, idempotency_key:crypto.randomUUID()}); });
  document.querySelector('[data-auto-exit]')?.addEventListener('click', () => campaignAction(`/api/campaigns/${item.campaign_id}/automatic-exit`, {idempotency_key:crypto.randomUUID(), limit_price:document.querySelector('#target-form')?.elements.limit_price.value || null}));
  document.querySelector('#auto-add-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction(`/api/campaigns/${item.campaign_id}/auto-add`, {candidate_id:data.candidate_id, quantity:data.quantity, idempotency_key:crypto.randomUUID()}); });
  document.querySelector('[data-disable-campaign-add]')?.addEventListener('click', () => campaignAction(`/api/campaigns/${item.campaign_id}/auto-add/disable`, {reason:'operator disabled further Campaign AddUnits', idempotency_key:crypto.randomUUID()}));
}

async function campaignAction(path, body) { try { await api(path, {method:'POST', body:JSON.stringify(body)}); showToast('SHADOW 权威状态已更新'); await route(); } catch (error) { showToast(`${error.code}: ${error.message}`); } }
async function submitNamedForm(event, path) { event.preventDefault(); await campaignAction(path, Object.fromEntries(new FormData(event.currentTarget))); }

function navigate(path) { history.pushState({}, '', path); route(); }
function updateActiveNav() { document.querySelectorAll('nav a').forEach(link => link.classList.toggle('active', location.pathname === link.getAttribute('href'))); }

document.addEventListener('click', (event) => {
  const link = event.target.closest('[data-link]');
  if (link) { event.preventDefault(); navigate(link.getAttribute('href')); }
  if (event.target.closest('[data-retry]')) route();
});
window.addEventListener('popstate', route);
document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => dialog.close()));
document.querySelector('#system-proposal-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form)); const candidateId = data.candidate_id; delete data.candidate_id; data.expires_in_minutes = Number(data.expires_in_minutes); data.initial_quantity = data.initial_quantity || null; data.add_trigger_price = data.add_trigger_price || null; data.allow_auto_add = data.allow_auto_add === 'true'; data.requested_adds = Number(data.requested_adds);
  try { const result = await api(`/api/opportunities/${candidateId}/proposals`, {method:'POST', body:JSON.stringify(data)}); dialog.close(); showToast('SYSTEM 提案已冻结并进入审核'); navigate(`/proposals/${result.proposal_id}`); }
  catch (error) { document.querySelector('#system-form-error').textContent = `${error.code}: ${error.message}`; }
});
document.querySelector('#logout-button').addEventListener('click', async () => { await api('/api/auth/logout', {method:'POST'}); session = null; setShell(false); history.replaceState({}, '', '/'); route(); });
document.querySelector('#theme-toggle').addEventListener('click', () => { const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = next; localStorage.setItem('trading-theme', next); });
document.documentElement.dataset.theme = localStorage.getItem('trading-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
bootstrap().catch((error) => { main.innerHTML = errorView(error, false); });
