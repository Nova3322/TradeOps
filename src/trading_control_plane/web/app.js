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
      <label>请求数量<input name="quantity" type="number" step="any" min="0" required></label>
      <label>最大风险<input name="max_risk" type="number" step="any" min="0" required></label>
      <label>触发价格<input name="trigger_price" type="number" step="any" min="0" required></label>
      <label>限价（可选）<input name="limit_price" type="number" step="any" min="0"></label>
      <label>失效价格<input name="invalidation_price" type="number" step="any" min="0" required></label>
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
  data.idempotency_key = crypto.randomUUID();
  for (const field of ['quantity','max_risk','trigger_price','limit_price','invalidation_price']) if (data[field] !== null) data[field] = String(data[field]);
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
    <div class="detail-layout"><div class="stack"><article class="card"><h2>冻结交易语义</h2><dl class="definition-grid">${definition('账户', item.account_id)}${definition('场所', item.venue)}${definition('方向', item.direction)}${definition('风险档位', item.risk_tier)}${definition('数量上限', fmtNumber(item.quantity))}${definition('风险上限', fmtNumber(item.max_risk))}${definition('来源候选', item.source_candidate_id || 'MANUAL')}${definition('来源时间', fmtDate(item.source_observed_at))}</dl></article>
      <article class="card"><h2>理由与来源事实</h2><pre>${escapeHtml(JSON.stringify(item.frozen_payload, null, 2))}</pre></article>
      <article class="card"><h2>审核记录</h2>${item.approvals.length ? item.approvals.map(a => `<div class="callout"><b>${escapeHtml(a.decision)}</b> · ${escapeHtml(a.reason)}<br><span class="subtle">${shortId(a.reviewer_id)} · ${fmtDate(a.created_at)}</span></div>`).join('') : '<p class="subtle">尚无 Reviewer 投票。</p>'}</article></div>
      <aside class="stack"><article class="card"><h2>安全动作</h2><p class="subtle">批准要求对象版本绑定的短时 step-up。拒绝只会收紧，不生成授权。</p>${item.status === 'PENDING_REVIEW' && canReview ? `<label>审核理由<textarea id="review-reason" rows="3">已核对冻结字段与风险范围</textarea></label><div class="toolbar" style="margin-top:12px"><button class="primary" data-approve>Step-up 并批准</button><button class="danger" data-reject>拒绝</button></div><div class="form-error" id="review-error"></div>` : '<p class="safety-note">当前状态或角色没有可用审核动作。</p>'}</article>
      <article class="card"><h2>风险决定</h2>${item.risk_decision ? `<p><b class="status-${item.risk_decision.result}">${escapeHtml(item.risk_decision.result)}</b></p>${definition('批准数量', item.risk_decision.approved_quantity)}${definition('风险金额', item.risk_decision.risk_amount)}<p class="subtle">${escapeHtml(item.risk_decision.reasons.join(' · '))}</p>` : '<p class="subtle">尚未运行服务端确定性 Risk Engine。</p>'}${item.status === 'APPROVED' && canOperate && !item.risk_decision ? '<button class="primary" data-risk>运行 RiskDecision</button>' : ''}</article>
      <article class="card"><h2>短期交易授权</h2>${item.authorization ? `<p><b>${shortId(item.authorization.authorization_id)}</b></p>${definition('数量上限', item.authorization.quantity_limit)}${definition('风险上限', item.authorization.risk_limit)}${definition('到期', fmtDate(item.authorization.expires_at))}` : '<p class="subtle">尚无 TradingAuthorization。</p>'}${item.risk_decision && item.risk_decision.result !== 'DENY' && canOperate && !item.authorization ? '<button class="primary" data-authorize>签发短期授权</button>' : ''}<p class="safety-note">授权仍不会发送真实订单。LIVE_ORDER_SEND 默认关闭。</p></article></aside></div></section>`;
  document.querySelector('[data-approve]')?.addEventListener('click', () => approveProposal(item));
  document.querySelector('[data-reject]')?.addEventListener('click', () => rejectProposal(item));
  document.querySelector('[data-risk]')?.addEventListener('click', () => runRisk(item));
  document.querySelector('[data-authorize]')?.addEventListener('click', () => authorize(item));
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
  try { await api(`/api/proposals/${item.proposal_id}/authorizations`, {method:'POST', body:JSON.stringify({idempotency_key:crypto.randomUUID(), expires_in_minutes:30, allowed_adds:0})}); showToast('短期授权已签发'); await route(); }
  catch (error) { showToast(`${error.code}: ${error.message}`); }
}

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
  event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form)); const candidateId = data.candidate_id; delete data.candidate_id; data.expires_in_minutes = Number(data.expires_in_minutes);
  try { const result = await api(`/api/opportunities/${candidateId}/proposals`, {method:'POST', body:JSON.stringify(data)}); dialog.close(); showToast('SYSTEM 提案已冻结并进入审核'); navigate(`/proposals/${result.proposal_id}`); }
  catch (error) { document.querySelector('#system-form-error').textContent = `${error.code}: ${error.message}`; }
});
document.querySelector('#logout-button').addEventListener('click', async () => { await api('/api/auth/logout', {method:'POST'}); session = null; setShell(false); history.replaceState({}, '', '/'); route(); });
document.querySelector('#theme-toggle').addEventListener('click', () => { const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = next; localStorage.setItem('trading-theme', next); });
document.documentElement.dataset.theme = localStorage.getItem('trading-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
bootstrap().catch((error) => { main.innerHTML = errorView(error, false); });
