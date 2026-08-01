const main = document.querySelector('#main');
const sidebar = document.querySelector('#sidebar');
const identityChip = document.querySelector('#identity-chip');
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

const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const shortId = (value) => value ? `${value.slice(0, 8)}…` : '—';
const fmtDate = (value) => value ? new Intl.DateTimeFormat('zh-CN', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}).format(new Date(value)) : '—';
const fmtNumber = (value) => value === null || value === undefined ? '—' : new Intl.NumberFormat('en-US', {maximumFractionDigits: 6}).format(Number(value));
const fmtCompact = (value) => value === null || value === undefined ? '暂无数据' : new Intl.NumberFormat('zh-CN', {notation:'compact', maximumFractionDigits:1}).format(Number(value));
const fmtAmount = (value, currency) => value === null || value === undefined ? '—' : `${fmtNumber(value)}${currency ? ` ${currency}` : ''}`;
const statusLabels = {DRAFT:'草稿',PENDING_REVIEW:'待审核',APPROVED:'已批准',REJECTED:'已拒绝',EXPIRED:'已过期',ALLOW:'通过',SCALE:'缩小仓位',DENY:'拒绝',PENDING:'等待中',RESERVED:'已预留',READY:'待发送',SENT:'已发送',PARTIALLY_FILLED:'部分成交',FILLED:'已成交',CANCELLED:'已取消',UNKNOWN:'结果未知',KNOWN:'已确认',OPENING:'建仓中',OPEN:'持仓中',REDUCING:'减仓中',CLOSING:'退出中',CLOSED:'已关闭',ACTIVE:'有效',DEGRADED:'保护不足',RELEASED:'已释放',MATCH:'一致',DIFFERENCE:'有差异',MANUAL_REQUIRED:'需要人工处理',RESOLVED:'已解决',NORMAL:'常规',URGENT:'紧急',IMMEDIATE:'立即'};
const riskLabels = {LOW:'低风险',MEDIUM:'中风险',HIGH:'高风险'};
const intentKindLabels = {INITIAL:'初仓',ADD:'加仓',REDUCE:'减仓',EXIT:'退出'};
const fmtIntentKind = (value) => intentKindLabels[value] || value || '未知意图';
const exceptionGuidance = {
  CAMPAIGN_UNKNOWN:{priority:1,title:'Campaign 状态不确定',copy:'系统无法确认这笔交易当前处于哪个阶段，因此不会继续增加风险。',next:'先核对订单、成交和仓位，再运行对账。'},
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
const explainException = (code) => exceptionGuidance[code] || {priority:9,title:'需要人工核实',copy:'系统发现一项无法自动解释的阻断事实。',next:'进入 Campaign 查看技术详情并完成对账。'};
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
  INITIAL_INTENT_ALREADY_EXISTS:'这个冻结提案已经创建过初仓意图。请进入原 Campaign 继续处理，不能重复开仓。',
  ACTIVE_ORDER_INTENT:'当前 Campaign 还有未完成意图。请先确认原意图结果，不要重复提交。',
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
const fmtStatus = (value) => statusLabels[value] || value || '未知';
const fmtRisk = (value) => riskLabels[value] || value || '未知';
const riskGuidance = (reason) => riskReasonGuidance[reason] || {label:'风险检查未通过',action:'查看当前风险事实，处理阻塞后重新检查。'};
const friendlyApiError = (error) => {
  const risk = riskReasonGuidance[error?.code] || riskReasonGuidance[error?.message];
  if (risk) return `${risk.label}：${risk.action}`;
  return actionErrorGuidance[error?.code] || error?.message || '请求失败';
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
  view:['OBSERVER','PROPOSER','REVIEWER','OPERATOR'],
  'capital.view':['OBSERVER','PROPOSER','REVIEWER','OPERATOR','TREASURY_ADMIN'],
  'proposal.create':['PROPOSER'],
  'proposal.review':['REVIEWER'],
};
const hasCapability = (capability) => roleNames().includes('SYSTEM_ADMIN') || (capabilityRoles[capability] || []).some(role => roleNames().includes(role));
const routeCapability = (path) => {
  if (path === '/') return null;
  if (path === '/capital') return 'capital.view';
  if (path === '/proposals/new') return 'proposal.create';
  if (path === '/reviews') return 'proposal.review';
  return 'view';
};
const capabilityLabel = (capability) => ({view:'交易只读作用域','capital.view':'资金查看','proposal.create':'创建提案','proposal.review':'独立审核'}[capability] || capability);
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
  toast.textContent = message;
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
  const message = `${friendlyApiError(error)}（${error?.code || 'UNKNOWN'}）`;
  if (target) target.textContent = message;
  else showToast(message, 'error');
}

function setShell(loggedIn) {
  sidebar.hidden = !loggedIn;
  identityChip.hidden = !loggedIn;
  mobileNavToggle.hidden = !loggedIn;
  if (loggedIn) {
    const identity = `${session.username} · ${roleNames().join(' / ') || 'NO ROLE'}`;
    identityChip.textContent = identity;
    mobileSessionSummary.textContent = identity;
    document.querySelectorAll('[data-nav-capability]').forEach(link => {
      link.hidden = !hasCapability(link.dataset.navCapability);
    });
  }
  closeMobileNav({restoreFocus:false});
}

function errorView(error, retry = true) {
  return `<section class="error-state"><div><p class="error-code">${escapeHtml(error.code || 'UNKNOWN')}</p><h2>当前事实无法读取</h2><p>${escapeHtml(error.message)}</p>${retry ? '<button class="secondary" data-retry>重新读取</button>' : ''}</div></section>`;
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
}

function confirmAction({title, message, confirmLabel}) {
  document.querySelector('#confirm-title').textContent = title;
  document.querySelector('#confirm-message').textContent = message;
  document.querySelector('#confirm-submit').textContent = confirmLabel || '确认并继续';
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
  button.textContent = pendingLabel;
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
    main.innerHTML = `<section class="empty-state"><div><p class="eyebrow">ROLE BOUNDARY</p><h2>当前职责不包含这个页面</h2><p>此页面需要“${escapeHtml(capabilityLabel(requiredCapability))}”能力。侧栏只展示当前角色可用入口；直接链接也不会绕过服务端权限。</p><div class="toolbar empty-actions"><a class="secondary" href="/" data-link>返回今日</a>${hasCapability('capital.view') ? '<a class="primary" href="/capital" data-link>进入资金中心</a>' : ''}</div></div></section>`;
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
    else if (path === '/positions') await renderCampaignFacts('positions');
    else if (path === '/orders') await renderCampaignFacts('orders');
    else if (path === '/risk') await renderCampaignFacts('risk');
    else if (path === '/capital') await renderCapitalCenter();
    else if (path === '/results') await renderActualResults();
    else if (path === '/exceptions') await renderExceptions();
    else if (path === '/venues/binance') await renderBinanceReadOnly();
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
    <span class="mock-ribbon">${authStatus.mock_identity_available ? 'NON-PRODUCTION MOCK' : 'MANAGED IDP REQUIRED'}</span>
    <p class="eyebrow" style="margin-top:18px">INTERNAL ACCESS</p><h1>进入交易控制台</h1>
    <p class="lede">没有外部注册。正式环境使用托管身份源与 Passkey；本地 Mock 只验证已存在的内部用户。</p>
    ${sessionNotice ? `<div class="callout" role="status">${escapeHtml(sessionNotice)}</div>` : ''}
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
  if (!hasCapability('view')) {
    const hasCapital = hasCapability('capital.view');
    main.innerHTML = `<section class="page home-page"><article class="home-status tone-${hasCapital ? 'success' : 'neutral'}"><div><p class="eyebrow">ROLE-CORRECT HOME</p><h1>${hasCapital ? '今日只显示你的资金职责' : '当前身份尚未分配业务职责'}</h1><p>${hasCapital ? '你可以查看资金事实、净值完整性、在途占用和资金对账；交易提案、Campaign 与风险操作不在当前角色范围内。' : '请由管理员分配明确角色和作用域；系统不会把缺少权限当作空数据。'}</p></div>${hasCapital ? '<a class="primary" href="/capital" data-link>进入资金中心</a>' : ''}</article></section>`;
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
  const pending = proposalResponse.data.filter(item => new Date(item.expires_at).getTime() > now);
  const actionableReviews = canReview ? pending.filter(item => item.actionable_for_current_user) : [];
  const expiringReviews = actionableReviews.filter(item => new Date(item.expires_at).getTime() - now < 30 * 60 * 1000);
  const nextReview = [...actionableReviews].sort((left, right) => new Date(left.expires_at) - new Date(right.expires_at))[0];
  const activeCampaigns = campaignResponse.data.filter(item => item.status !== 'CLOSED');
  const exceptions = exceptionResponse.data;
  const exceptionCampaigns = new Set(exceptions.map(item => item.campaign_id));
  const riskLimited = Boolean(riskControl && riskControl.policy.system_state !== 'NORMAL');
  const clearScopeLabel = riskControl ? '当前安全' : '当前作用域无异常';
  const safety = exceptions.length
    ? {
        tone:'danger',
        eyebrow:'RISK ATTENTION',
        title:`${exceptionCampaigns.size} 个 Campaign 需要先处理`,
        copy:`当前有 ${exceptions.length} 项阻断问题。相关新增风险保持关闭；先确认 Unknown、仓位、保护和对账事实。`,
        href:'/exceptions',
        action:canOperate ? '处理风险异常' : '查看风险异常',
      }
    : riskLimited
      ? {
          tone:'attention',
          eyebrow:'RISK RESTRICTED',
          title:'新增风险已受限，减仓和退出仍可用',
          copy:`当前系统风险状态为“${riskControlStatusLabel(riskControl.policy.system_state)}”。先完成恢复条件，不会自动放开旧授权。`,
          href:'/risk',
          action:'查看限制与恢复条件',
        }
      : actionableReviews.length
        ? {
            tone:'attention',
            eyebrow:'DECISION NEEDED',
            title:`${clearScopeLabel}；有 ${actionableReviews.length} 笔非本人提案等待审核`,
            copy:'打开队列确认是否需要你投票；批准只进入风险检查，不会直接产生订单。',
            href:'/reviews',
            action:'查看审核队列',
          }
        : activeCampaigns.length
          ? {
              tone:'success',
              eyebrow:'OPERATIONS NORMAL',
              title:`${clearScopeLabel}；${activeCampaigns.length} 个 Campaign 正在运行`,
              copy:'没有派生异常。继续观察仓位、保护、意图和最近对账；需要降险时可随时减仓或退出。',
              href:'/campaigns',
              action:'查看运行中 Campaign',
            }
          : {
              tone:'success',
              eyebrow:'ALL CLEAR',
              title:`${clearScopeLabel}，没有必须立即处理的事项`,
              copy:`系统没有发现阻断异常、待你审核的提案或运行中 Campaign。${riskControl ? '' : '全局风险恢复仍由管理员控制。'} 可以继续观察机会。`,
              href:'/opportunities',
              action:'查看市场机会',
            };
  const priorityCards = [];
  if (exceptions.length) priorityCards.push(`<a class="home-priority danger" href="/exceptions" data-link><span class="priority-number">1</span><div><small>必须先处理</small><b>${exceptions.length} 项阻断异常</b><p>影响 ${exceptionCampaigns.size} 个 Campaign；Unknown 和保护问题不会被自动忽略。</p></div><strong>进入异常队列 →</strong></a>`);
  if (riskLimited) priorityCards.push(`<a class="home-priority attention" href="/risk" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>新增风险受限</small><b>${escapeHtml(riskControlStatusLabel(riskControl.policy.system_state))}</b><p>${riskControl.restore_conditions.blockers.length ? `${riskControl.restore_conditions.blockers.length} 项恢复条件尚未满足。` : '恢复条件已满足，仍需完成受控审核与执行。'} 减仓和退出不受阻断。</p></div><strong>查看恢复条件 →</strong></a>`);
  if (actionableReviews.length) priorityCards.push(`<a class="home-priority attention" href="/reviews" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>独立审核队列</small><b>${actionableReviews.length} 笔非本人提案等待审核</b><p>${expiringReviews.length ? `${expiringReviews.length} 笔将在 30 分钟内到期。` : `最早一笔到期于 ${fmtDate(nextReview.expires_at)}。`} 已投票的高风险提案可能仍在等待另一名 Reviewer。</p></div><strong>打开审核队列 →</strong></a>`);
  if (activeCampaigns.length) priorityCards.push(`<a class="home-priority" href="/campaigns" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>持续观察</small><b>${activeCampaigns.length} 个运行中 Campaign</b><p>${escapeHtml(activeCampaigns.slice(0, 3).map(item => `${item.venue} · ${item.direction} · ${fmtStatus(item.status)}`).join('；'))}</p></div><strong>查看当前仓位 →</strong></a>`);
  if (!priorityCards.length) priorityCards.push(`<a class="home-priority clear" href="/opportunities" data-link><span class="priority-number">✓</span><div><small>当前无待办</small><b>继续观察，不必为了操作而操作</b><p>${canPropose ? '机会只是候选；只有形成清楚交易假设时才创建提案。' : '当前角色可以观察机会，但不能创建提案；如有判断请交由 Proposer 冻结参数。'}</p></div><strong>查看机会 →</strong></a>`);
  main.innerHTML = `<section class="page home-page"><article class="home-status tone-${safety.tone}"><div><p class="eyebrow">${safety.eyebrow}</p><h1>${escapeHtml(safety.title)}</h1><p>${escapeHtml(safety.copy)}</p></div><a class="primary" href="${safety.href}" data-link>${escapeHtml(safety.action)}</a></article>
    <div class="stats home-stats"><div class="stat"><small>受影响 Campaign</small><b class="${exceptions.length ? 'danger-text' : ''}">${exceptionCampaigns.size}</b><span>${exceptions.length ? `${exceptions.length} 项问题` : '没有派生异常'}</span></div><div class="stat"><small>非本人待审核</small><b class="${expiringReviews.length ? 'warning-text' : ''}">${actionableReviews.length}</b><span>${expiringReviews.length ? `${expiringReviews.length} 笔即将到期` : canReview ? '创建者不可审核自己的提案' : '当前身份不是 Reviewer'}</span></div><div class="stat"><small>运行中 Campaign</small><b>${activeCampaigns.length}</b><span>${activeCampaigns.length ? '保护与对账需持续有效' : '当前没有活动仓位流程'}</span></div><div class="stat"><small>新增风险状态</small><b class="${riskLimited ? 'warning-text status-copy' : 'status-copy'}">${escapeHtml(riskControl ? riskControlStatusLabel(riskControl.policy.system_state) : '由管理员控制')}</b><span>${riskControl ? `AUTO_ADD ${escapeHtml(riskControlStatusLabel(riskControl.auto_add_gate.status))}` : '当前角色无全局恢复权限'}</span></div></div>
    <div class="home-layout"><section><div class="section-heading"><div><p class="eyebrow">PRIORITY ORDER</p><h2>现在按这个顺序处理</h2></div><button class="secondary" data-refresh>刷新当前事实</button></div><div class="home-priority-list">${priorityCards.join('')}</div></section>
      <aside class="stack"><article class="card home-quick-start"><p class="eyebrow">${canPropose ? 'NEW DECISION' : 'MARKET OBSERVATION'}</p><h2>${canPropose ? '开始新的判断' : '继续观察市场机会'}</h2><p class="subtle">${canPropose ? '先看机会或写交易假设；这两条路径都只会创建提案，并进入独立审核。' : '当前角色只查看候选，不冻结交易参数，也不会从这里新增风险。'}</p><div class="stacked-actions"><a class="primary" href="/opportunities" data-link>查看 PerpTape 机会</a>${canPropose ? '<a class="secondary" href="/proposals/new" data-link>创建人工提案</a>' : ''}</div></article>
        <article class="card home-boundary"><p class="eyebrow">SYSTEM BOUNDARY</p><h2>当前运行边界</h2><dl class="definition-grid">${definition('环境', 'SHADOW')}${definition('真实订单', '关闭')}${definition('风险政策', riskControl ? riskControlStatusLabel(riskControl.policy.system_state) : '由管理员控制')}${definition('自动加仓', riskControl ? riskControlStatusLabel(riskControl.auto_add_gate.status) : '由管理员控制')}</dl><p class="safety-note">首页只汇总你当前可见的权威事实。没有异常不代表交易盈利，也不代表 LIVE 已准备好。</p></article></aside></div></section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
}

async function renderOpportunities() {
  const result = await api('/api/opportunities');
  opportunities = result.data;
  const items = opportunities;
  const canPropose = hasCapability('proposal.create');
  const options = (key) => [...new Set(items.map(item => item[key]).filter(Boolean))].sort().map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('');
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">PERPTAPE · ${escapeHtml(result.source_contract_version)}</p><h1>当前机会</h1><p class="lede">这里只展示 Perptape 实际返回的突破候选。数据健康、方向和时间保留来源语义；交易数量与风险由 Trading 独立决定。</p></div><div class="toolbar"><button class="secondary" data-refresh>刷新事实</button></div></header>
    <div class="stats"><div class="stat"><small>当前候选</small><b>${items.length}</b></div><div class="stat"><small>${canPropose ? '可冻结' : 'Catalog 可用'}</small><b>${items.filter(i => i.readiness === 'READY' && i.proposal_eligible).length}</b></div><div class="stat"><small>数据截止</small><b style="font-size:14px">${fmtDate(result.as_of)}</b></div><div class="stat"><small>执行环境</small><b style="font-size:14px">SHADOW</b></div></div>
    ${items.length ? `<form id="opportunity-filters" class="filter-panel"><label>交易所<select name="venue"><option value="">全部</option>${options('venue')}</select></label><label>币对<input name="symbol" type="search" placeholder="例如 BTC、XYZ100"></label><label>共振周期<select name="timeframe"><option value="">全部周期</option>${options('timeframe')}</select></label><label>方向<select name="direction"><option value="">全部</option><option>LONG</option><option>SHORT</option></select></label><label>最低成交量<input name="volume" type="number" min="0" placeholder="不限"></label><label>最低持仓量<input name="open_interest" type="number" min="0" placeholder="不限"></label><button type="reset" class="text-button">清除筛选</button></form><div class="result-summary"><span>显示 <b data-filter-count>${items.length}</b> / ${items.length} 个机会</span><span>成交量与持仓量缺失时不会通过数值筛选</span></div><div id="opportunity-grid" class="card-grid">${items.map(opportunityCard).join('')}</div><section id="opportunity-empty" class="empty-state compact-empty" hidden><div><h2>没有符合条件的机会</h2><p>尝试降低成交量/持仓量门槛，或清除部分筛选。</p></div></section>` : '<section class="empty-state"><div><h2>Perptape 当前没有返回候选</h2><p>这不是零风险或无行情，只表示当前接口数据为空。</p></div></section>'}
  </section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  bindOpportunityActions();
}

function opportunityCard(item) {
  const directionClass = item.direction === 'LONG' ? 'direction-long' : 'direction-short';
  const canPropose = hasCapability('proposal.create');
  const canCreateProposal = canPropose && item.readiness === 'READY' && item.proposal_eligible;
  const catalogStatus = item.proposal_blocker === 'INSTRUMENT_UNAVAILABLE'
    ? '<p class="callout">Catalog 未认证此交易合约，暂不能创建提案。</p>'
    : '';
  return `<article class="card" data-opportunity-card="${escapeHtml(item.candidate_id)}"><div class="card-top"><div><span class="subtle">${escapeHtml(item.venue)} · ${escapeHtml(item.timeframe)}</span><div class="symbol">${escapeHtml(item.symbol)}</div></div><span class="tag ${directionClass}">${escapeHtml(item.direction)}</span></div>
    <div class="metric-row"><div><small>参考价格</small><b>${fmtNumber(item.reference_price)}</b></div><div><small>触发时间</small><b>${fmtDate(item.triggered_at)}</b></div><div><small>数据状态</small><b>${escapeHtml(item.readiness)}</b></div></div>
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
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">MANUAL PROPOSAL · SHADOW</p><h1>创建人工提案</h1><p class="lede">先说清要做什么、最多亏多少；分批入场和 AUTO_ADD 等执行细节按需展开。</p></div><a class="secondary" href="/proposals" data-link>查看全部提案</a></header>
    <div class="compose-layout"><form id="manual-form" class="form-panel proposal-compose">
      <section class="form-section"><div class="section-title"><span>1</span><div><h2>交易意图</h2><p>选标的、定方向，说清从哪个价格开始执行。</p></div></div><div class="field-grid">
        <label>账户<span class="field-help">资金和权限归属</span><input name="account_id" value="acct-1" required></label>
        <label>交易标的<span class="field-help">仅展示 Catalog 中已启用的合约</span><select name="instrument_id" required>${instruments.map(i => `<option value="${i.instrument_id}" data-venue="${escapeHtml(i.venue)}">${escapeHtml(i.venue)} · ${escapeHtml(i.symbol)}</option>`).join('')}</select></label>
        <label>方向<span class="field-help">做多或做空</span><select name="direction"><option>LONG</option><option>SHORT</option></select></label>
        <label>触发价格<span class="field-help">计划开始执行的位置</span><input name="trigger_price" type="number" step="any" min="0" required></label>
      </div></section>
      <section class="form-section"><div class="section-title"><span>2</span><div><h2>风险边界</h2><p>用数量、最大损失和失效点限制这笔交易。</p></div></div><div class="field-grid">
        <label>风险档位<span class="field-help">影响审批门槛与加仓上限</span><select name="risk_tier"><option>LOW</option><option selected>MEDIUM</option><option>HIGH</option></select></label>
        <label>总数量上限<span class="field-help">这份提案不能突破的数量</span><input name="quantity" type="number" step="any" min="0" required></label>
        <label>最大风险<span class="field-help">以账户结算币计价</span><input name="max_risk" type="number" step="any" min="0" required></label>
        <label>失效价格<span class="field-help">到达后交易逻辑不再成立</span><input name="invalidation_price" type="number" step="any" min="0" required></label>
      </div></section>
      <details class="advanced-form"><summary><span>高级执行参数</span><small>分批入场、限价、AUTO_ADD 与有效期</small></summary><div class="field-grid">
        <label>初仓数量<input name="initial_quantity" type="number" step="any" min="0" placeholder="默认等于总数量"></label>
        <label>限价（可选）<input name="limit_price" type="number" step="any" min="0"></label>
        <label>允许 AUTO_ADD<select name="allow_auto_add"><option value="false" selected>否</option><option value="true">是</option></select></label>
        <label>预授权 AddUnit<select name="requested_adds"><option value="0" selected>0</option><option value="1">1</option><option value="2">2</option><option value="3">3</option></select></label>
        <label>Add 触发价格<input name="add_trigger_price" type="number" step="any" min="0"></label>
        <label>有效分钟<input name="expires_in_minutes" type="number" min="5" max="1440" value="120" required></label>
      </div></details>
      <label class="rationale-field">提案理由<span class="field-help">至少说明触发逻辑和主要风险</span><textarea name="rationale" rows="4" required placeholder="例如：4h 突破确认，成交量扩张；若跌破失效价则退出。"></textarea></label>
      <div class="form-error" role="alert"></div><div class="form-actions"><span class="submit-disclosure">提交后冻结当前参数并进入 Reviewer 队列，不会直接下单。</span><button class="primary">创建并提交审核</button></div>
    </form>
    <aside class="proposal-preview" aria-live="polite"><p class="eyebrow">LIVE SUMMARY</p><h2>提交前摘要</h2><div class="preview-symbol" data-preview-symbol>选择交易标的</div><div class="preview-direction" data-preview-direction>LONG</div><dl class="preview-metrics"><div><dt>计划名义价值</dt><dd data-preview-notional>—</dd></div><div><dt>最大风险</dt><dd data-preview-risk>—</dd></div><div><dt>失效距离</dt><dd data-preview-distance>—</dd></div><div><dt>有效期</dt><dd data-preview-expiry>120 分钟</dd></div></dl><div class="preview-checks"><p data-check-intent>○ 补全交易意图</p><p data-check-risk>○ 补全风险边界</p><p>✓ 只创建提案，不直接下单</p></div></aside></div></section>`;
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
  direction.textContent = data.direction || '—';
  direction.className = `preview-direction ${data.direction === 'SHORT' ? 'direction-short' : 'direction-long'}`;
  document.querySelector('[data-preview-notional]').textContent = trigger > 0 && quantity > 0 ? fmtAmount(trigger * quantity, selected?.quote_currency) : '—';
  document.querySelector('[data-preview-risk]').textContent = data.max_risk ? `${fmtAmount(data.max_risk, selected?.collateral_currency)} · ${fmtRisk(data.risk_tier)}` : '—';
  document.querySelector('[data-preview-distance]').textContent = percentageDistance(data.trigger_price, data.invalidation_price);
  document.querySelector('[data-preview-expiry]').textContent = `${data.expires_in_minutes || 120} 分钟`;
  document.querySelector('[data-check-intent]').textContent = `${intentReady ? '✓' : '○'} ${intentReady ? '交易意图完整' : '补全交易意图'}`;
  document.querySelector('[data-check-risk]').textContent = `${riskReady ? '✓' : '○'} ${riskReady ? '风险边界完整' : '补全风险边界'}`;
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
  } catch (error) { showApiError(error, form.querySelector('.form-error')); button.disabled = false; }
}

async function renderProposalList(status, title) {
  const result = await api(`/api/proposals${status ? `?proposal_status=${status}` : ''}`);
  const allItems = result.data;
  const items = status ? allItems.filter(item => item.actionable_for_current_user) : allItems;
  const pending = status ? items.length : items.filter(item => item.status === 'PENDING_REVIEW' && item.actionable_for_current_user).length;
  const expiring = items.filter(item => { const remaining = new Date(item.expires_at) - Date.now(); return remaining > 0 && remaining < 30 * 60 * 1000; }).length;
  const canPropose = roleNames().includes('PROPOSER') || roleNames().includes('SYSTEM_ADMIN');
  const createActions = canPropose ? '<div class="toolbar"><a class="secondary" href="/opportunities" data-link>从机会创建</a><a class="primary" href="/proposals/new" data-link>新建人工提案</a></div>' : '';
  const emptyState = status
    ? '<section class="empty-state"><div><h2>当前没有待你审核的提案</h2><p>自己的提案、已经投过票、已到期或已结束的提案不会留在这里。</p><div class="toolbar empty-actions"><a class="secondary" href="/" data-link>返回今日</a><a class="primary" href="/proposals" data-link>查看全部提案</a></div></div></section>'
    : `<section class="empty-state"><div><h2>当前没有匹配提案</h2><p>${canPropose ? '可以从机会页一键创建，或提交一份人工提案。' : '当前作用域内还没有提案。'}</p>${createActions}</div></section>`;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">PROPOSAL CONTROL</p><h1>${escapeHtml(title)}</h1><p class="lede">${status ? '这里只保留真正需要你独立判断的提案；审批不等于下单。' : '集中查看提案从创建、审核到授权的权威状态。'}</p></div>${createActions}</header>
    <div class="stats proposal-stats"><div class="stat"><small>当前列表</small><b>${items.length}</b></div><div class="stat"><small>等待审核</small><b>${pending}</b></div><div class="stat"><small>高风险</small><b>${items.filter(item => item.risk_tier === 'HIGH').length}</b></div><div class="stat"><small>30 分钟内到期</small><b>${expiring}</b></div></div>
    <div class="section-tabs"><a class="${status ? 'active' : ''}" href="/reviews" data-link>待我审核${pending ? `<span>${pending}</span>` : ''}</a><a class="${status ? '' : 'active'}" href="/proposals" data-link>全部提案</a></div>
    ${items.length ? `<div class="proposal-list-tools"><label>搜索标的或账户<input id="proposal-search" type="search" placeholder="BTCUSDT / acct-1"></label><label>方向<select id="proposal-direction"><option value="">全部方向</option><option>LONG</option><option>SHORT</option></select></label><label>风险<select id="proposal-risk"><option value="">全部档位</option><option>LOW</option><option>MEDIUM</option><option>HIGH</option></select></label><span><b data-proposal-count>${items.length}</b> 个结果</span></div><div class="table-wrap proposal-table"><table><thead><tr><th>提案</th><th>方向 / 数量</th><th>风险边界</th><th>状态</th><th>提交时间</th><th>到期</th></tr></thead><tbody>${items.map(item => `<tr data-href="/proposals/${item.proposal_id}" data-proposal-row data-search="${escapeHtml(`${item.symbol || ''} ${item.account_id} ${item.venue}`.toLowerCase())}" data-direction="${escapeHtml(item.direction)}" data-risk="${escapeHtml(item.risk_tier)}"><td><b>${escapeHtml(item.symbol || shortId(item.instrument_id))}</b><br><span class="subtle">${escapeHtml(item.venue)} · ${escapeHtml(item.source === 'SYSTEM' ? 'Perptape' : '人工')}</span></td><td><span class="direction-pill ${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(item.direction)}</span><br><span class="subtle">数量 ${fmtNumber(item.quantity)}</span></td><td><b>${fmtRisk(item.risk_tier)}</b><br><span class="subtle">最多 ${escapeHtml(fmtAmount(item.max_risk, item.collateral_currency))}</span></td><td><span class="status-pill status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</span></td><td>${fmtDate(item.created_at)}<br><span class="subtle">v${item.version}</span></td><td>${fmtDate(item.expires_at)}</td></tr>`).join('')}</tbody></table></div><section id="proposal-filter-empty" class="empty-state compact-empty" hidden><div><h2>没有符合条件的提案</h2><p>请清除搜索或调整筛选。</p></div></section>` : emptyState}</section>`;
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
    ? `<div class="source-facts"><div><small>来源状态</small><b class="${item.source_readiness === 'READY' ? 'direction-long' : 'direction-short'}">${escapeHtml(item.source_readiness || '未知')}</b></div><div><small>共振周期</small><b>${escapeHtml(candidate.timeframe || '—')}</b></div><div><small>成交量</small><b>${fmtCompact(candidate.quote_volume)}</b></div><div><small>持仓量</small><b>${fmtCompact(candidate.open_interest)}</b></div><div><small>观测时间</small><b>${fmtDate(item.source_observed_at)}</b></div></div>`
    : '<div class="source-facts manual-source"><div><small>来源</small><b>人工输入</b></div><div><small>审核依据</small><b>冻结参数与提案理由</b></div></div>';
  const highRiskReviewCopy = item.risk_tier === 'HIGH' ? `高风险提案需要两名不同 Reviewer；当前已记录 ${item.approvals.length} 票。` : '批准后仍需运行服务端风险检查。';
  const nextAction = terminal
    ? {title:isExpired ? '提案已到期' : '流程已终止', copy:'该提案不能继续扩大风险。条件改变后需创建新提案。', tone:'danger'}
    : item.status === 'PENDING_REVIEW'
      ? canReview
        ? {title:'需要你的独立判断', copy:`核对方向、触发价、失效位置和最大风险。${highRiskReviewCopy}`, tone:'attention'}
        : reviewedByMe
          ? {title:'你的审核已记录', copy:'这笔提案仍在等待另一名独立 Reviewer；你无需再次操作。', tone:'success'}
          : {title:'等待独立审核', copy:item.proposer_id === session.user_id ? '你是提案创建者，不能审核自己的提案。' : '当前角色没有审核权限。', tone:'neutral'}
      : initialEntry
        ? {title:'初仓意图已经创建', copy:'该冻结提案不能再创建第二个初仓意图；后续执行、保护和异常处理统一进入 Campaign。', tone:'success'}
        : item.status === 'APPROVED' && (!riskDone || riskDenied)
          ? riskDenied
            ? {title:riskHelp.label, copy:riskHelp.action, tone:'danger'}
            : {title:'下一步：运行风险检查', copy:'审核已完成，Operator 需要基于最新账户事实运行确定性风控。', tone:'attention'}
        : needsFreshRisk
          ? {title:'短期授权已经失效', copy:'重新读取当前账户事实并运行风险检查；通过后才能签发新的短期授权。', tone:'danger'}
        : needsAuthorization
          ? {title:'下一步：签发短期授权', copy:'风险检查已通过，可签发限时、限数量、限风险的交易授权。', tone:'attention'}
          : authorizationUsable
            ? {title:'已准备创建初仓意图', copy:'授权仍在有效期内；创建后只记录 SHADOW 风险预留与意图。', tone:'success'}
            : {title:'当前没有待办动作', copy:'请核对授权有效期和当前状态。', tone:'neutral'};
  const canRunRisk = item.status === 'APPROVED' && canOperate && (!riskDone || riskDenied || needsFreshRisk);
  const executionAction = initialEntry
    ? `<a class="primary wide-action" href="/campaigns/${initialEntry.campaign_id}" data-link>进入 Campaign</a><p class="microcopy">初仓意图 ${shortId(initialEntry.intent_id)} · ${escapeHtml(fmtStatus(initialEntry.intent_status))}</p>`
    : canRunRisk
      ? `<button class="primary wide-action" data-risk>${riskDenied ? '处理后重新检查' : needsFreshRisk ? '重新检查当前风险' : '运行风险检查'}</button>`
      : needsAuthorization && canOperate
        ? '<button class="primary wide-action" data-authorize>签发 30 分钟授权</button>'
        : authorizationUsable && canOperate
          ? '<button class="primary wide-action" data-initial>创建一次性 SHADOW 初仓意图</button>'
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
      ? `<div class="risk-guidance-list">${item.risk_decision.reasons.map(reason => { const guidance = riskGuidance(reason); return `<div><b>${escapeHtml(guidance.label)}</b><span>${escapeHtml(guidance.action)}</span><code>${escapeHtml(reason)}</code></div>`; }).join('')}</div>`
      : '<p class="success-note">仓位、权益、受管资金、系统状态和总风险容量均通过。</p>';
  const riskDecisionPanel = riskDone
    ? `<p class="risk-outcome-copy">${escapeHtml(riskOutcomeCopy)}</p><dl class="definition-grid risk-decision-grid">${definition('请求数量', fmtNumber(riskContext.requested_quantity))}${definition('系统批准数量', fmtNumber(item.risk_decision.approved_quantity))}${definition('本次风险占用', fmtAmount(item.risk_decision.risk_amount, item.collateral_currency))}${definition('组合风险容量', riskCapacityCopy)}${definition('事实年龄', `${fmtSeconds(riskContext.fact_age_seconds)} / 上限 ${fmtSeconds(riskContext.max_fact_age_seconds)}`)}${definition('数据截止', fmtDate(item.risk_decision.data_as_of))}</dl><div class="risk-fact-strip"><span>仓位 <b>${escapeHtml(factStatusLabel(riskContext.position_status))}</b></span><span>权益 <b>${escapeHtml(factStatusLabel(riskContext.equity_status))}</b></span><span>受管资金 <b>${riskContext.managed_capital_known ? '已确认' : '缺失'}</b></span><span>保护 <b>${escapeHtml(factStatusLabel(riskContext.protection_status))}</b></span></div>${riskReasons}`
    : '<div class="empty-inline"><b>等待审核通过</b><span>风险检查会读取服务端最新仓位、权益、受管资金、保护和总风险容量。</span></div>';
  const authorizationState = !authorizationDone ? '未签发' : authorizationUsable ? '有效' : item.authorization.active ? '已过期' : '已撤销';
  const authorizationPanel = authorizationDone
    ? `<dl class="definition-grid authorization-grid">${definition('批准数量', fmtNumber(item.authorization.quantity_limit))}${definition('已使用', fmtNumber(item.authorization.used_quantity))}${definition('剩余数量', fmtNumber(item.authorization.remaining_quantity))}${definition('风险上限', fmtAmount(item.authorization.risk_limit, item.collateral_currency))}${definition('AddUnit', `${item.authorization.used_adds} / ${item.authorization.allowed_adds}`)}${definition('到期', fmtDate(item.authorization.expires_at))}</dl>${initialEntry ? `<div class="entry-boundary"><b>一次性初仓已消费</b><span>意图 ${shortId(initialEntry.intent_id)} · ${escapeHtml(fmtStatus(initialEntry.intent_status))}</span><a href="/campaigns/${initialEntry.campaign_id}" data-link>查看 Campaign →</a></div>` : '<p class="microcopy">授权仍不是订单；创建初仓意图时还会再次读取事实并原子预留风险。</p>'}`
    : '<div class="empty-inline"><b>风险通过后可签发</b><span>授权同时限制有效期、数量、风险金额、作用域和 AddUnit。</span></div>';
  main.innerHTML = `<section class="page proposal-detail"><header class="page-head"><div><p class="eyebrow">${escapeHtml(item.environment)} · ${escapeHtml(item.source === 'SYSTEM' ? 'PERPTAPE SYSTEM' : 'MANUAL')}</p><div class="proposal-title-row"><h1>${escapeHtml(item.symbol || candidate.symbol || '交易提案')}</h1><span class="direction-pill ${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(item.direction)}</span><span class="status-pill status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</span></div><p class="lede">${escapeHtml(item.venue)} · ${escapeHtml(item.account_id)} · 提案 ${shortId(item.proposal_id)} · v${item.version}</p></div><div class="toolbar"><a class="secondary" href="/reviews" data-link>返回审核队列</a>${sourceLink ? `<a class="secondary" href="${escapeHtml(sourceLink)}" target="_blank" rel="noreferrer">Perptape 榜单 ↗</a>` : ''}${chartLink ? `<a class="secondary" href="${escapeHtml(chartLink)}" target="_blank" rel="noreferrer">交易所图表 ↗</a>` : ''}</div></header>
    <ol class="workflow-stepper" aria-label="提案流程"><li class="done"><span>1</span><div><b>提案已冻结</b><small>${fmtDate(item.frozen_at)}</small></div></li><li class="${reviewDone ? 'done' : 'current'}"><span>2</span><div><b>独立审核</b><small>${isExpired ? '已到期' : reviewDone ? fmtStatus(item.status) : reviewedByMe ? '你的票已记录' : '等待判断'}</small></div></li><li class="${riskDenied ? 'blocked' : riskDone ? 'done' : reviewDone && !terminal ? 'current' : ''}"><span>3</span><div><b>风险检查</b><small>${riskDone ? fmtStatus(item.risk_decision.result) : '尚未运行'}</small></div></li><li class="${initialEntry || authorizationUsable ? 'done' : needsAuthorization ? 'current' : ''}"><span>4</span><div><b>短期授权</b><small>${initialEntry ? '已生成初仓意图' : authorizationDone ? (authorizationUsable ? '有效' : '已失效') : '尚未签发'}</small></div></li></ol>
    <div class="proposal-detail-layout"><div class="stack">
      <article class="card decision-brief"><div class="card-heading"><div><p class="eyebrow">DECISION BRIEF</p><h2>这笔交易要做什么</h2></div><span class="risk-badge risk-${escapeHtml(item.risk_tier)}">${escapeHtml(fmtRisk(item.risk_tier))}</span></div><p class="proposal-rationale">${escapeHtml(rationale)}</p><div class="decision-metrics"><div><small>计划数量</small><b>${fmtNumber(item.quantity)}</b><span>初仓 ${fmtNumber(details.initial_quantity || item.quantity)}</span></div><div><small>估算名义价值</small><b>${notional === null ? '—' : escapeHtml(fmtAmount(notional, item.quote_currency))}</b><span>触发价 ${fmtNumber(triggerPrice)}</span></div><div><small>最大风险</small><b>${escapeHtml(fmtAmount(item.max_risk, item.collateral_currency))}</b><span>${fmtRisk(item.risk_tier)}</span></div><div><small>失效位置</small><b>${fmtNumber(invalidationPrice)}</b><span>距触发 ${percentageDistance(triggerPrice, invalidationPrice)}</span></div></div>${sourceFacts}</article>
      <article class="card frozen-scope"><div class="card-heading"><div><p class="eyebrow">FROZEN SCOPE</p><h2>冻结范围</h2></div><span class="status-pill">不可编辑</span></div><dl class="definition-grid spacious">${definition('账户', item.account_id)}${definition('交易场所', item.venue)}${definition('方向', item.direction)}${definition('风险档位', fmtRisk(item.risk_tier))}${definition('限价', fmtNumber(details.limit_price))}${definition('有效至', fmtDate(item.expires_at))}${definition('AUTO_ADD', details.allow_auto_add ? `允许 · ${details.requested_adds} Unit` : '关闭')}${definition('Add 触发价', fmtNumber(details.add_trigger_price))}${definition('来源候选', item.source_candidate_id || '人工创建')}${definition('来源观测', fmtDate(item.source_observed_at))}</dl><details class="technical-details"><summary>查看技术载荷与语义哈希</summary><pre>${escapeHtml(JSON.stringify(item.frozen_payload, null, 2))}</pre><p class="subtle">Semantic hash · ${escapeHtml(item.semantic_hash)}</p></details></article>
      <article class="card review-trail"><div class="card-heading"><div><p class="eyebrow">REVIEW TRAIL</p><h2>审核记录</h2></div><span class="subtle">${item.approvals.length} 条记录</span></div>${item.approvals.length ? `<div class="review-timeline">${item.approvals.map(a => `<div class="review-event"><span class="${a.decision === 'APPROVE' ? 'approve-dot' : 'reject-dot'}"></span><div><b>${a.decision === 'APPROVE' ? '批准提案' : '拒绝提案'}</b><p>${escapeHtml(a.reason)}</p><small>${shortId(a.reviewer_id)} · ${fmtDate(a.created_at)}</small></div></div>`).join('')}</div>` : '<div class="empty-inline"><b>尚无审核记录</b><span>Reviewer 的独立判断会按时间出现在这里。</span></div>'}</article>
    </div><aside class="stack proposal-actions-column">
      <article class="card next-action tone-${nextAction.tone}"><p class="eyebrow">NEXT ACTION</p><h2>${escapeHtml(nextAction.title)}</h2><p>${escapeHtml(nextAction.copy)}</p>${item.status === 'PENDING_REVIEW' && canReview ? `<label>审核意见<span class="field-help">说明你核对了什么，以及判断依据</span><textarea id="review-reason" rows="4">已核对交易逻辑、冻结参数与最大风险边界</textarea></label><div class="review-actions"><button class="primary" data-approve>批准提案</button><button class="danger" data-reject>拒绝提案</button></div><p class="microcopy">批准会触发对象版本绑定的短时 step-up；不会直接下单。</p><div class="form-error" id="review-error"></div>` : ''}${executionAction}<div class="form-error" id="execution-error"></div></article>
      <article class="card risk-engine-card"><div class="card-heading"><div><p class="eyebrow">RISK ENGINE</p><h2>系统允许开多少</h2></div>${item.risk_decision ? `<span class="status-pill status-${escapeHtml(item.risk_decision.result)}">${escapeHtml(fmtStatus(item.risk_decision.result))}</span>` : '<span class="status-pill">未运行</span>'}</div>${riskDecisionPanel}</article>
      <article class="card authorization-card"><div class="card-heading"><div><p class="eyebrow">LIMITED AUTHORIZATION</p><h2>这份许可还能做什么</h2></div><span class="status-pill ${authorizationUsable ? 'status-APPROVED' : authorizationDone ? 'status-EXPIRED' : ''}">${authorizationState}</span></div>${authorizationPanel}</article>
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
    showToast('Reviewer 投票已原子记录'); await route();
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
      showToast('风险已原子预留，SHADOW 初仓意图已创建'); navigate(`/campaigns/${result.campaign_id}`);
    } catch (error) { showApiError(error, document.querySelector('#execution-error')); }
  });
}

async function loadCampaignDetails() {
  const result = await api('/api/campaigns');
  return Promise.all(result.data.map((item) => api(`/api/campaigns/${item.campaign_id}`)));
}

const riskControlStatusLabel = (value) => ({
  NORMAL:'正常开放', NO_PYRAMID:'禁止加仓', REDUCE_ONLY:'仅允许减仓', KILL_SWITCH:'紧急停止',
  ENABLED:'已启用', DISABLED:'已关闭', PENDING_REVIEW:'等待双人审核', APPROVED:'审核完成待执行',
  REJECTED:'已拒绝', EXPIRED:'已过期', EXECUTED:'已执行',
}[value] || value);

function renderRiskControlPanel(control) {
  if (!control) return `<article class="card"><div class="card-heading"><div><p class="eyebrow">GLOBAL CONTROL</p><h2>全局风险恢复由管理员控制</h2></div><span class="status-pill">作用域视图</span></div><p class="subtle">当前身份只能查看被分配账户与交易所的 Campaign 风险，不能读取或执行全局 Policy、AUTO_ADD Gate 与恢复申请。</p><p class="safety-note">这不代表全局风险状态正常。新增风险仍会由服务端 Risk Engine 强制检查；你仍可使用下表查看风险预留、唯一减仓目标和最近对账。</p><div class="toolbar"><a class="secondary" href="/" data-link>返回今日</a><a class="primary" href="/exceptions" data-link>查看当前异常</a></div></article>`;
  const policy = control.policy;
  const gate = control.auto_add_gate;
  const conditions = control.restore_conditions;
  const hasLiveScope = conditions.required_scopes.some(scope => scope.environment === 'LIVE');
  const restoreGateLabel = conditions.ready
    ? (conditions.live_scope_required ? '生产条件满足' : (hasLiveScope ? '条件满足' : '本地条件满足'))
    : `${conditions.blockers.length} 项阻塞`;
  const isAdmin = roleNames().includes('SYSTEM_ADMIN');
  const canReview = roleNames().includes('REVIEWER') || isAdmin;
  const activeRequest = control.requests.find(item => ['PENDING_REVIEW','APPROVED'].includes(item.status));
  const requestForm = isAdmin && !activeRequest && (policy.system_state !== 'NORMAL' || gate.status !== 'ENABLED')
    ? `<form id="risk-restore-form" class="form-panel compact-form"><h2>申请受审核恢复</h2><p class="danger-note"><b>这不是反向开关。</b>申请只冻结当前 Policy、Gate 和受控 scope；两名独立 Reviewer 分别完成强身份验证后，还要在执行时重新检查事实、计算型对账、未决订单、冷却期和版本漂移。旧提案、旧授权和旧 AddUnit 永远不会复活。</p><label>恢复理由<textarea name="reason" rows="4" minlength="10" required>已完成异常处置，并准备由两名独立审核人复核全部恢复条件</textarea></label><label class="checkbox-row"><input name="restore_auto_add" type="checkbox">同时申请恢复全局 AUTO_ADD Gate（旧 AddUnit 仍保持撤销）</label><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">创建冻结申请</button></div></form>`
    : '';
  const requestCards = control.requests.length ? control.requests.map(item => {
    const isRequester = item.requester_id === session.user_id;
    const reviewedByMe = item.reviews.some(review => review.reviewer_id === session.user_id);
    const reviewUi = item.status === 'PENDING_REVIEW' && canReview && !isRequester && !reviewedByMe
      ? `<label>独立审核理由<textarea id="risk-review-${item.request_id}" rows="3">已核对冻结版本、恢复影响和当前阻塞条件</textarea></label><div class="toolbar"><button class="primary" data-risk-review="${item.request_id}" data-decision="APPROVE" data-version="${item.version}">强验证并批准</button><button class="danger" data-risk-review="${item.request_id}" data-decision="REJECT" data-version="${item.version}">拒绝申请</button></div>`
      : '<p class="subtle">当前身份或申请状态没有可用审核动作。</p>';
    const executeUi = item.status === 'APPROVED' && isAdmin
      ? `<button class="danger" data-risk-execute="${item.request_id}" data-version="${item.version}">强验证并执行恢复</button><p class="safety-note">执行会再次 fail closed；任何事实、scope、Policy 或 Gate 漂移都会拒绝。</p>`
      : '';
    return `<article class="card"><div class="card-head"><div><p class="eyebrow">RESTORE REQUEST · v${item.version}</p><h2>${riskControlStatusLabel(item.status)}</h2></div><span class="tag">${shortId(item.request_id)}</span></div><dl class="definition-grid">${definition('申请人', shortId(item.requester_id))}${definition('恢复 AUTO_ADD', item.restore_auto_add ? '是' : '否')}${definition('最早执行', fmtDate(item.execute_after))}${definition('到期', fmtDate(item.expires_at))}${definition('冻结 Policy', `${escapeHtml(item.source_policy_version)} · r${item.source_policy_revision}`)}${definition('冻结 Gate', `${escapeHtml(item.source_auto_add_status)} · v${item.source_auto_add_version}`)}</dl><p>${escapeHtml(item.reason)}</p><h3>审核记录</h3>${item.reviews.length ? item.reviews.map(review => `<div class="callout"><b>${escapeHtml(review.decision)}</b> · ${escapeHtml(review.reason)}<br><span class="subtle">${shortId(review.reviewer_id)} · ${fmtDate(review.created_at)}</span></div>`).join('') : '<p class="subtle">尚无审核票。</p>'}<div class="review-action-panel">${reviewUi}${executeUi}</div></article>`;
  }).join('') : '<section class="empty-state"><div><h2>尚无恢复申请</h2><p>收紧控制不会自动恢复；需要创建冻结申请并完成双人独立审核。</p></div></section>';
  return `<section class="risk-control-overview"><div class="stats"><div class="stat"><small>RiskPolicy</small><b>${riskControlStatusLabel(policy.system_state)}</b></div><div class="stat"><small>Policy 版本</small><b>${escapeHtml(policy.version)} · r${policy.revision}</b></div><div class="stat"><small>AUTO_ADD</small><b>${riskControlStatusLabel(gate.status)} · v${gate.version}</b></div><div class="stat"><small>恢复门</small><b>${restoreGateLabel}</b></div></div><div class="detail-layout"><article class="card"><h2>当前权威控制</h2><dl class="definition-grid">${definition('Policy 原因', policy.reason)}${definition('Policy 更新人', shortId(policy.updated_by))}${definition('Policy 更新时间', fmtDate(policy.updated_at))}${definition('Gate 原因', gate.reason)}${definition('Gate 操作人', shortId(gate.operator_id))}${definition('Gate 更新时间', fmtDate(gate.updated_at))}</dl><p class="safety-note">REDUCE_ONLY 仍允许减仓与退出；暂停会永久失效当时所有未过期的新风险授权。</p></article><article class="card"><h2>实时恢复条件</h2>${conditions.blockers.length ? `<ul class="exception-list">${conditions.blockers.map(item => `<li><code>${escapeHtml(item)}</code></li>`).join('')}</ul>` : `<p class="success-note">${conditions.live_scope_required ? '生产 LIVE 恢复条件当前无阻塞' : '当前本地读取未发现阻塞'}；执行事务仍会重新验证。</p>`}<h3>冻结范围来源</h3>${conditions.required_scopes.length ? conditions.required_scopes.map(scope => `<div class="callout"><b>${escapeHtml(scope.environment)}</b> · ${escapeHtml(scope.account_id)} · ${escapeHtml(scope.venue)}</div>`).join('') : `<p class="danger-note">${conditions.live_scope_required ? 'LIVE_SCOPE_CONFIGURATION_REQUIRED：未配置生产 LIVE 受控 scope，恢复执行保持关闭。' : '当前为本地模式且未配置受控 scope；生产环境会返回 LIVE_SCOPE_CONFIGURATION_REQUIRED 并禁止执行。'}</p>`}</article></div>${requestForm}<div class="section-head"><div><p class="eyebrow">FOUR-EYES WORKFLOW</p><h2>恢复申请与独立审核</h2></div></div><div class="stack">${requestCards}</div></section>`;
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
    const confirmed = await confirmAction({title:'执行受审核恢复？', message:'系统将重新验证所有冻结 scope、事实、计算型 MATCH、未决订单、冷却期和控制版本。只会创建新的 NORMAL Policy；旧授权与旧 AddUnit 永不复活。', confirmLabel:'重新验证并执行'}); if (!confirmed) return;
    await withPending(button, '验证中…', async () => { try { const grant = await api('/api/auth/mock/step-up', {method:'POST', body:JSON.stringify({action:'risk.restore.execute', object_id:requestId, object_version:version})}); await api(`/api/risk-controls/restores/${requestId}/execute`, {method:'POST', body:JSON.stringify({expected_version:version, idempotency_key:crypto.randomUUID(), action_grant:grant.action_grant})}); showToast('新 NORMAL Policy 已创建；旧授权保持失效'); await route(); } catch (error) { showApiError(error); } });
  }));
}

async function renderCampaignList() {
  const result = await api('/api/campaigns');
  const items = result.data;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">SHADOW OPERATIONS</p><h1>Campaign 运营台</h1><p class="lede">从短期授权、风险预留和订单意图，到成交、保护、减仓、对账和 PnL。所有发送动作均为本地 SHADOW 事实。</p></div><div class="toolbar"><a class="secondary" href="/proposals" data-link>全部提案</a></div></header>
    <div class="stats"><div class="stat"><small>Campaign</small><b>${items.length}</b></div><div class="stat"><small>Open / Opening</small><b>${items.filter(i => ['OPEN','OPENING'].includes(i.status)).length}</b></div><div class="stat"><small>Unknown</small><b>${items.filter(i => i.status === 'UNKNOWN').length}</b></div><div class="stat"><small>环境</small><b style="font-size:14px">SHADOW ONLY</b></div></div>
    ${items.length ? `<div class="table-wrap"><table><thead><tr><th>Campaign</th><th>范围</th><th>方向 / 目标</th><th>状态</th><th>PnL</th><th>更新时间</th></tr></thead><tbody>${items.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td><b>${shortId(item.campaign_id)}</b><br><span class="subtle">Proposal ${shortId(item.proposal_id)}</span></td><td>${escapeHtml(item.account_id)}<br><span class="subtle">${escapeHtml(item.venue)}</span></td><td class="${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(item.direction)} · ${fmtNumber(item.current_target_quantity)}</td><td><b class="status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</b></td><td>${fmtNumber(item.final_pnl)}</td><td>${fmtDate(item.updated_at)}</td></tr>`).join('')}</tbody></table></div>` : '<section class="empty-state"><div><h2>尚无 Campaign</h2><p>批准提案并签发短期授权后，Operator 才能创建 SHADOW 初仓意图。</p></div></section>'}</section>`;
  bindLinkedRows();
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
  const titles = {positions:'仓位与保护', orders:'订单与成交', risk:'风险与目标'};
  let rows = '';
  if (mode === 'positions') rows = details.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(item.instrument?.symbol || shortId(item.instrument_id))}</td><td>${item.position ? `${fmtNumber(item.position.quantity)} @ ${fmtNumber(item.position.average_entry_price)}` : '无事实'}</td><td>${item.position ? escapeHtml(fmtStatus(item.position.fact_status)) : '结果未知'}</td><td>${item.protection ? `${escapeHtml(fmtStatus(item.protection.status))} · ${item.protection.fully_covered ? '完整覆盖' : '覆盖不足'}` : '无保护事实'}</td><td>${fmtDate(item.position?.observed_at)}</td></tr>`).join('');
  if (mode === 'orders') rows = details.flatMap(item => item.intents.map(intent => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(fmtIntentKind(intent.kind))}${intent.reduce_only ? ' · 只减仓' : ''}</td><td>${escapeHtml(intent.side)} ${fmtNumber(intent.quantity)}</td><td>${escapeHtml(fmtStatus(intent.status))}</td><td>${intent.order ? `${escapeHtml(intent.order.venue_order_id)} · ${escapeHtml(fmtStatus(intent.order.status))}` : '尚未记录 SHADOW 发送'}</td><td>${fmtDate(intent.updated_at)}</td></tr>`)).join('');
  if (mode === 'risk') rows = details.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(fmtStatus(item.status))}</td><td>${item.reservations.map(r => `${escapeHtml(fmtStatus(r.status))} ${fmtNumber(r.amount)}`).join(' · ') || '无预留'}</td><td>${fmtNumber(item.current_target_quantity)} · v${item.target_version}</td><td>${escapeHtml(fmtStatus(item.target_urgency || '—'))}</td><td>${escapeHtml(item.reconciliation ? fmtStatus(item.reconciliation.status) : '未对账')}</td></tr>`).join('');
  const headers = mode === 'positions' ? '<th>Campaign</th><th>标的</th><th>仓位</th><th>事实</th><th>保护</th><th>观测时间</th>' : mode === 'orders' ? '<th>Campaign</th><th>意图</th><th>方向 / 数量</th><th>状态</th><th>SHADOW Order</th><th>更新时间</th>' : '<th>Campaign</th><th>状态</th><th>风险预留</th><th>目标</th><th>紧迫度</th><th>对账</th>';
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">POSTGRESQL AUTHORITY</p><h1>${titles[mode]}</h1><p class="lede">这些页面直接读取当前权威状态；可重新计算的投影不另行持久化。</p></div></header>
    ${mode === 'risk' ? renderRiskControlPanel(riskControls) : ''}
    ${mode === 'risk' && roleNames().includes('SYSTEM_ADMIN') ? '<div class="form-panel compact-form"><h2>全局只收紧动作</h2><p class="safety-note">这些入口只能关闭 AUTO_ADD 或把系统切到 REDUCE_ONLY；不能从这里恢复新增风险。</p><div class="toolbar"><button class="danger" data-disable-global-add>关闭全局 AUTO_ADD</button><button class="danger" data-pause-new-risk>暂停所有新增风险</button></div></div><div style="height:16px"></div>' : ''}
    ${mode === 'positions' ? shadowFactsForm() : ''}
    ${rows ? `<div class="table-wrap"><table><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>` : '<section class="empty-state"><div><h2>当前没有可展示事实</h2></div></section>'}</section>`;
  bindLinkedRows();
  document.querySelector('#shadow-facts-form')?.addEventListener('submit', recordStartingFacts);
  if (mode === 'risk') await bindRiskControlActions();
  document.querySelector('[data-disable-global-add]')?.addEventListener('click', (event) => campaignAction('/api/operations/auto-add/disable', {reason:'administrator disabled AUTO_ADD from Web', idempotency_key:crypto.randomUUID()}, {
    button:event.currentTarget,
    successMessage:'全局 AUTO_ADD 已关闭；现有仓位与退出能力不受影响',
    confirm:{title:'关闭全局 AUTO_ADD？', message:'确认后，所有 Campaign 都不能继续新增 AddUnit。该入口只会收紧风险，无法在此页重新开启。', confirmLabel:'关闭 AUTO_ADD'},
  }));
  document.querySelector('[data-pause-new-risk]')?.addEventListener('click', (event) => campaignAction('/api/operations/pause-new-risk', {reason:'administrator paused new risk from Web', idempotency_key:crypto.randomUUID()}, {
    button:event.currentTarget,
    successMessage:'系统已切换到 REDUCE_ONLY；仅允许收紧和退出',
    confirm:{title:'暂停所有新增风险？', message:'确认后，系统会进入 REDUCE_ONLY。已有仓位仍可减仓或退出，但新的初仓和加仓会被拒绝。', confirmLabel:'切换到 REDUCE_ONLY'},
  }));
}

function shadowFactsForm() {
  return `<form id="shadow-facts-form" class="form-panel compact-form"><h2>记录当前 SHADOW 事实</h2><p class="safety-note">这是 Operator 明确录入的合成非生产事实，不来自真实交易所，也不会被标记为真实账户数据。</p><div class="field-grid"><label>账户<input name="account_id" value="acct-1" required></label><label>场所<input name="venue" value="BINANCE" required></label><label>Instrument UUID<input name="instrument_id" required></label><label>仓位数量<input name="quantity" type="number" step="any" value="0" required></label><label>平均入场价<input name="average_entry_price" type="number" step="any" value="0" required></label><label>标记价<input name="mark_price" type="number" step="any" required></label><label>权益<input name="equity" type="number" step="any" required></label><label>可用余额<input name="available_balance" type="number" step="any" required></label><label>币种<input name="currency" value="USDT" required></label></div><div class="form-error"></div><div class="form-actions"><button class="primary">记录合成事实</button></div></form><div style="height:16px"></div>`;
}

async function recordStartingFacts(event) {
  event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form));
  const button = event.submitter || form.querySelector('button');
  await withPending(button, '记录中…', async () => {
    form.querySelector('.form-error').textContent = '';
    try {
      await api('/api/facts/positions', {method:'POST', body:JSON.stringify({account_id:data.account_id, venue:data.venue, instrument_id:data.instrument_id, quantity:data.quantity, average_entry_price:data.average_entry_price, mark_price:data.mark_price, known:true})});
      await api('/api/facts/account-equity', {method:'POST', body:JSON.stringify({account_id:data.account_id, venue:data.venue, equity:data.equity, available_balance:data.available_balance, currency:data.currency, known:true})});
      showToast('当前 SHADOW 仓位与权益事实已记录'); await route();
    } catch (error) {
      showApiError(error, form.querySelector('.form-error'));
      if (!error.handled) showToast('SHADOW 事实未完整记录，请先核对当前事实再决定是否继续', 'error');
    }
  });
}

const LIVE_CAPITAL_SOURCES = [
  {key:'BINANCE', location_type:'VENUE', label:'Binance'},
  {key:'HYPERLIQUID', location_type:'VENUE', label:'Hyperliquid'},
  {key:'VAULT', location_type:'VAULT', label:'链上 Vault'},
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
    const sourceLabel = balance.source_label || (balance.location_type === 'VAULT' ? '链上 Vault' : balance.location_type);
    const state = missing
      ? `<b class="capital-status-missing">MISSING</b><br><span class="subtle">${escapeHtml(balance.missing_detail)}</span>`
      : `${escapeHtml(balance.control_status)} / ${escapeHtml(balance.deposit_status)}`;
    return `<tr${missing ? ' class="capital-missing-row"' : ''}><td>${escapeHtml(sourceLabel)}<br><span class="subtle">${escapeHtml(balance.location_id)}</span></td><td>${escapeHtml(balance.environment)} · ${escapeHtml(balance.venue)}</td><td>${fmtNumber(balance.confirmed_available)} ${escapeHtml(balance.asset)}</td><td>${balance.usd_equity === null ? 'UNKNOWN' : `${fmtNumber(balance.usd_equity)} USD`}</td><td>${fmtNumber(balance.source_reserved)}</td><td><b>${fmtNumber(balance.effective_available)}</b></td><td>${state}</td><td>${missing ? '—' : fmtDate(balance.observed_at)}</td></tr>`;
  }).join('');
}

function capitalBalanceTable(rows, emptyMessage) {
  return rows
    ? `<div class="table-scroll-hint">左右滑动查看完整资金事实</div><div class="table-wrap is-scrollable"><table><thead><tr><th>位置</th><th>环境 / 场所</th><th>已确认可用</th><th>USD 净值</th><th>源端预留</th><th>有效可用</th><th>控制 / 充值</th><th>观测</th></tr></thead><tbody>${rows}</tbody></table></div>`
    : `<div class="callout">${escapeHtml(emptyMessage)}</div>`;
}

async function renderCapitalCenter() {
  const [result, notiltStatus] = await Promise.all([
    api('/api/capital'),
    api('/api/notilt/status').catch(() => null),
  ]);
  const item = result.data;
  const canTreasury = roleNames().includes('TREASURY_ADMIN');
  const automation = item.automation || {gates:{}, policies:[]};
  const netWorth = item.net_worth || {currency:'USD', venues:{}, vault:'0', total:'0', complete:false, issues:[]};
  const balances = partitionCapitalRecords(item.balances);
  const proposals = partitionCapitalRecords(item.proposals);
  const transfers = partitionCapitalRecords(item.transfers);
  const liveInTransit = liveCapitalInTransit(transfers.live);
  const venueNetWorth = Object.entries(netWorth.venues).map(([venue, value]) => `<div class="stat"><small>${escapeHtml(venue)} 净值</small><b>${fmtNumber(value)} ${escapeHtml(netWorth.currency)}</b></div>`).join('');
  const chartBalances = balances.live.filter(balance => balance.usd_equity !== null).sort((a, b) => new Date(a.observed_at) - new Date(b.observed_at));
  const chartLegend = chartBalances.map((balance, index) => `<span><i style="--legend-index:${index}"></i>${escapeHtml(balance.location_type === 'VAULT' ? 'VAULT' : balance.venue)} <b>${fmtCompact(balance.usd_equity)} USD</b></span>`).join('');
  const liveBalanceRows = capitalBalanceRows(capitalSourceSlots(balances.live, notiltStatus));
  const simulatedBalanceRows = capitalBalanceRows(balances.simulated);
  const renderProposalRows = records => records.map(proposal => {
    const actions = [];
    if (canTreasury && proposal.status === 'DRAFT' && proposal.proposer_id === session.user_id) actions.push(`<button class="secondary" data-cap-submit="${proposal.transfer_proposal_id}">提交</button>`);
    if (canTreasury && proposal.status === 'PENDING_REVIEW' && proposal.proposer_id !== session.user_id) actions.push(`<button class="secondary" data-cap-review="${proposal.transfer_proposal_id}" data-version="${proposal.version}">批准</button>`);
    if (canTreasury && proposal.status === 'APPROVED' && !proposal.authorization && proposal.proposer_id !== session.user_id) actions.push(`<button class="secondary" data-cap-authorize="${proposal.transfer_proposal_id}">签发授权</button>`);
    if (canTreasury && proposal.authorization?.active && proposal.proposer_id !== session.user_id) actions.push(proposal.environment === 'LIVE' ? `<button class="primary" data-cap-notilt="${proposal.authorization.transfer_authorization_id}">生成 NoTilt 计划</button>` : `<button class="primary" data-cap-execute="${proposal.authorization.transfer_authorization_id}">Mock 执行</button>`);
    return `<tr><td>${shortId(proposal.transfer_proposal_id)}<br><span class="subtle">v${proposal.version}</span></td><td>${escapeHtml(proposal.direction)}<br><span class="subtle">${escapeHtml(proposal.purpose)}</span></td><td>${escapeHtml(proposal.source_id)} → ${escapeHtml(proposal.destination_id)}</td><td>${fmtNumber(proposal.amount)} ${escapeHtml(proposal.asset)}</td><td><b>${escapeHtml(proposal.status)}</b></td><td><div class="toolbar">${actions.join('')}</div></td></tr>`;
  }).join('');
  const renderTransferRows = records => records.map(transfer => {
    const actions = [];
    if (canTreasury && transfer.transport === 'MOCK' && transfer.status === 'SUBMITTED') actions.push(`<button class="secondary" data-cap-observe="${transfer.capital_transfer_id}" data-status="IN_FLIGHT">标记在途</button>`);
    if (canTreasury && transfer.transport === 'MOCK' && ['SUBMITTED','IN_FLIGHT'].includes(transfer.status)) actions.push(`<button class="danger" data-cap-observe="${transfer.capital_transfer_id}" data-status="UNKNOWN">标记 Unknown</button>`);
    if (canTreasury && transfer.transport === 'MOCK' && ['IN_FLIGHT','UNKNOWN','MANUAL_REQUIRED'].includes(transfer.status)) actions.push(`<button class="secondary" data-cap-confirm="${transfer.capital_transfer_id}">目的端确认</button>`);
    if (canTreasury && ['DEPOSIT_PLAN_READY','RELEASE_REQUEST_PLAN_READY','RELEASE_EXECUTION_PLAN_READY','RELEASE_CANCELLATION_PLAN_READY'].includes(transfer.transport_state)) actions.push(`<button class="primary" data-notilt-receipt="${transfer.capital_transfer_id}">验证链上回执</button>`);
    if (canTreasury && transfer.transport_state === 'RELEASE_REQUEST_CONFIRMED' && transfer.status === 'IN_FLIGHT') actions.push(`<button class="primary" data-notilt-execute="${transfer.capital_transfer_id}">生成释放执行计划</button>`);
    if (canTreasury && transfer.transport_state === 'RELEASE_REQUEST_CONFIRMED' && ['IN_FLIGHT','MANUAL_REQUIRED'].includes(transfer.status)) actions.push(`<button class="danger" data-notilt-cancel="${transfer.capital_transfer_id}">生成释放取消计划</button>`);
    if (canTreasury) actions.push(`<button class="secondary" data-cap-reconcile="${transfer.capital_transfer_id}">对账</button>`);
    const plan = transfer.planned_transactions?.length ? `<details><summary>${transfer.planned_transactions.length} 笔未签名交易</summary><pre>${escapeHtml(JSON.stringify(transfer.planned_transactions, null, 2))}</pre></details>` : '';
    return `<tr><td>${shortId(transfer.capital_transfer_id)}<br><span class="subtle">${escapeHtml(transfer.transport || 'MOCK')}</span></td><td>${escapeHtml(transfer.direction)}</td><td>${fmtNumber(transfer.gross_amount)} ${escapeHtml(transfer.asset)}</td><td><b>${escapeHtml(transfer.status)}</b><br><span class="subtle">${escapeHtml(transfer.transport_state || transfer.reconciliation_status)}</span></td><td>${escapeHtml(transfer.external_transfer_id || '未提交')}${plan}</td><td><div class="toolbar">${actions.join('')}</div></td></tr>`;
  }).join('');
  const liveProposalRows = renderProposalRows(proposals.live);
  const simulatedProposalRows = renderProposalRows(proposals.simulated);
  const liveTransferRows = renderTransferRows(transfers.live);
  const simulatedTransferRows = renderTransferRows(transfers.simulated);
  const capitalProposalForm = canTreasury ? `<form id="capital-proposal-form" class="form-panel compact-form"><h2>模拟资金 Proposal</h2><p class="safety-note">仅创建 TESTNET / SHADOW Proposal；不提供 LIVE 创建入口，不连接 Vault、链或交易所。</p><div class="field-grid"><label>环境<select name="environment"><option>TESTNET</option><option>SHADOW</option></select></label><label>方向<select name="direction"><option>VAULT_TO_VENUE</option><option>VENUE_TO_VAULT</option></select></label><label>交易账户<input name="account_id" value="acct-1" required></label><label>场所<input name="venue" value="BINANCE" required></label><label>Vault ID<input name="vault_id" value="vault-1" required></label><label>资产<input name="asset" value="USDT" required></label><label>网络<input name="network" value="TESTNET" required></label><label>目的端引用<input name="destination_reference" value="approved-test-destination" required></label><label>Gross 金额<input name="amount" type="number" step="any" min="0" required></label><label>最大费用<input name="max_fee" type="number" step="any" min="0" value="1" required></label><label>最小到账<input name="min_received" type="number" step="any" min="0" required></label><label>有效分钟<input name="expires_in_minutes" type="number" min="5" value="120" required></label></div><label>理由<textarea name="reason" rows="2" required>manual capital allocation</textarea></label><button class="primary">创建模拟草稿</button></form>` : '';
  const mockFactForm = canTreasury ? `<form id="capital-fact-form" class="form-panel compact-form"><h2>Mock 只读资金事实</h2><p class="safety-note">只写入 TESTNET/SHADOW 观测，不连接 Vault、链或交易所，不移动资金。</p><div class="field-grid"><label>位置<select name="location_type"><option>VAULT</option><option>VENUE</option></select></label><label>位置 ID<input name="location_id" value="vault-1" required></label><label>场所<input name="venue" value="BINANCE" required></label><label>资产<input name="asset" value="USDT" required></label><label>权益<input name="equity" type="number" step="any" required></label><label>已确认可用<input name="available_balance" type="number" step="any" required></label><label>可划出<input name="withdrawable_balance" type="number" step="any" required></label><label>网络<input name="network" value="TESTNET"></label><label>控制状态<select name="control_status"><option>CONTROLLED</option><option>READ_ONLY</option><option>UNKNOWN</option></select></label><label>充值状态<select name="deposit_status"><option>READY</option><option>PENDING</option><option>UNKNOWN</option></select></label></div><button class="secondary">记录 Mock 事实</button></form>` : '';
  const policyRows = automation.policies.map(policy => `<tr><td>${escapeHtml(policy.environment)} · ${escapeHtml(policy.account_id)} / ${escapeHtml(policy.venue)}<br><span class="subtle">${escapeHtml(policy.asset)} · v${policy.version}</span></td><td>${fmtNumber(policy.operating_low)} / <b>${fmtNumber(policy.operating_target)}</b> / ${fmtNumber(policy.operating_high)}</td><td>${fmtNumber(policy.vault_minimum_reserve)} / ${fmtNumber(policy.minimum_transfer)}–${fmtNumber(policy.maximum_transfer)}</td><td><div class="toolbar"><button class="secondary" data-cap-scope-reconcile="${policy.policy_id}" data-environment="${policy.environment}" data-account="${escapeHtml(policy.account_id)}" data-venue="${escapeHtml(policy.venue)}">空仓对账</button><button class="secondary" data-cap-auto="${policy.policy_id}" data-purpose="AUTO_PROFIT_SWEEP" ${automation.gates.AUTO_PROFIT_SWEEP !== 'ENABLED' ? 'disabled' : ''}>评估利润归集</button><button class="secondary" data-cap-auto="${policy.policy_id}" data-purpose="AUTO_OPERATING_REFILL" ${automation.gates.AUTO_OPERATING_REFILL !== 'ENABLED' ? 'disabled' : ''}>评估运营补充</button></div></td></tr>`).join('');
  const automationPanel = `<section><h2>自动资金候选</h2><p class="safety-note">利润归集与运营补充使用独立 Gate，当前只生成需双人复核和独立授权的候选 Proposal，不自动提交资金。浮盈、活动仓位、订单、Unknown 或非 MATCH 均阻断。</p><div class="stats"><div class="stat"><small>AUTO_PROFIT_SWEEP</small><b style="font-size:14px">${escapeHtml(automation.gates.AUTO_PROFIT_SWEEP || 'MISSING')}</b></div><div class="stat"><small>AUTO_OPERATING_REFILL</small><b style="font-size:14px">${escapeHtml(automation.gates.AUTO_OPERATING_REFILL || 'MISSING')}</b></div></div>${canTreasury ? `<form id="capital-policy-form" class="form-panel compact-form"><h3>SHADOW / TESTNET 运营阈值</h3><div class="field-grid"><label>环境<select name="environment"><option>TESTNET</option><option>SHADOW</option></select></label><label>交易账户<input name="account_id" value="acct-1" required></label><label>场所<input name="venue" value="BINANCE" required></label><label>Vault ID<input name="vault_id" value="vault-1" required></label><label>资产<input name="asset" value="USDT" required></label><label>网络<input name="network" value="TESTNET" required></label><label>Vault 目的端引用<input name="vault_destination_reference" value="approved-test-vault" required></label><label>场所目的端引用<input name="venue_destination_reference" value="approved-test-venue" required></label><label>运营下沿<input name="operating_low" type="number" step="any" value="400" required></label><label>运营目标<input name="operating_target" type="number" step="any" value="500" required></label><label>运营上沿<input name="operating_high" type="number" step="any" value="600" required></label><label>Vault 最低储备<input name="vault_minimum_reserve" type="number" step="any" value="500" required></label><label>最小划转<input name="minimum_transfer" type="number" step="any" value="10" required></label><label>最大划转<input name="maximum_transfer" type="number" step="any" value="200" required></label><label>最大费用<input name="max_fee" type="number" step="any" value="1" required></label></div><button class="secondary">保存非生产策略</button></form>` : ''}${policyRows ? `<div class="table-wrap"><table><thead><tr><th>作用域</th><th>下沿 / 目标 / 上沿</th><th>Vault 储备 / 划转限额</th><th>动作</th></tr></thead><tbody>${policyRows}</tbody></table></div>` : '<div class="callout">尚无资金自动化策略；两个 Gate 默认关闭。</div>'}</section>`;
  const proposalTable = (rows, emptyMessage) => rows ? `<div class="table-scroll-hint">左右滑动查看完整 Proposal</div><div class="table-wrap is-scrollable"><table><thead><tr><th>Proposal</th><th>方向 / 用途</th><th>路径</th><th>金额</th><th>状态</th><th>动作</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="callout">${escapeHtml(emptyMessage)}</div>`;
  const transferTable = (rows, emptyMessage) => rows ? `<div class="table-scroll-hint">左右滑动查看完整 Transfer</div><div class="table-wrap is-scrollable"><table><thead><tr><th>Transfer</th><th>方向</th><th>Gross</th><th>状态 / 对账</th><th>外部引用</th><th>动作</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="callout">${escapeHtml(emptyMessage)}</div>`;
  const simulatedCount = balances.simulated.length + proposals.simulated.length + transfers.simulated.length;
  const simulationPanel = `<details class="simulation-panel"><summary><span><b>模拟数据（SHADOW / TESTNET）</b><small>独立隔离，不计入真实净值</small></span><span class="tag">${simulatedCount} 条记录</span></summary><div class="simulation-content"><div class="safety-note"><b>非生产区域：</b>以下表单、余额、Proposal 与 Transfer 不属于 LIVE，不参与顶部总净值、Vault 净值、LIVE 在途占用或资金构成曲线。</div>${capitalProposalForm}${mockFactForm}${automationPanel}<section><h3>模拟资金事实</h3>${capitalBalanceTable(simulatedBalanceRows, '尚无 SHADOW 或 TESTNET 资金事实。')}</section><section><h3>模拟资金 Proposal</h3>${proposalTable(simulatedProposalRows, '尚无 SHADOW 或 TESTNET 资金 Proposal。')}</section><section><h3>模拟 Capital Transfer</h3>${transferTable(simulatedTransferRows, '尚无 SHADOW 或 TESTNET 划转状态。')}</section></div></details>`;
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">LIVE CAPITAL AUTHORITY · FAIL CLOSED</p><h1>资金中心</h1><p class="lede">默认区域只展示 LIVE：Binance、Hyperliquid 与 NoTilt Vault 使用已确认事实合并计算 USD 净值；任何未知或过期估值都会将净值标记为不完整。Telegram 只通知，不能批准或执行资金动作。</p></div></header><div class="stats"><div class="stat"><small>LIVE 总净值</small><b>${fmtNumber(netWorth.total)} ${escapeHtml(netWorth.currency)}</b></div><div class="stat"><small>链上 Vault 净值</small><b>${fmtNumber(netWorth.vault)} ${escapeHtml(netWorth.currency)}</b></div>${venueNetWorth}<div class="stat"><small>净值状态</small><b style="font-size:14px">${netWorth.complete ? 'CURRENT' : 'INCOMPLETE'}</b></div><div class="stat"><small>真实划转 Gate</small><b style="font-size:14px">${escapeHtml(item.real_transfer_gate || 'DISABLED')}</b></div><div class="stat"><small>LIVE 在途 / 占用</small><b>${fmtNumber(liveInTransit)}</b></div></div><section class="capital-chart-panel"><div class="chart-head"><div><p class="eyebrow">LIVE CAPITAL SNAPSHOT</p><h2>资金构成曲线</h2><p class="subtle">只按 LIVE 各资金位置最新有效 USD 估值累计；这是当前快照，不冒充历史净值。</p></div><b>${fmtNumber(netWorth.total)} <small>${escapeHtml(netWorth.currency)}</small></b></div>${chartBalances.length ? `<canvas id="capital-chart" height="210" aria-label="LIVE 当前资金构成累计曲线"></canvas><div class="chart-legend">${chartLegend}</div>` : '<div class="chart-empty">LIVE 有效资金估值就绪后将在这里显示曲线</div>'}</section>${netWorth.complete ? '' : `<div class="callout"><b>LIVE 净值不完整：</b>${escapeHtml((netWorth.issues || []).join(', ') || '尚无资金事实')}</div>`}<section><h2>LIVE 确认资本、USD 估值与源端预留</h2><p class="subtle">固定展示 Binance、Hyperliquid 与链上 Vault；0 与 MISSING 同时出现表示当前没有可确认事实，不代表来源已完成同步。</p>${capitalBalanceTable(liveBalanceRows, '尚无 LIVE 资金事实。')}</section><section><h2>LIVE 资金 Proposal</h2>${proposalTable(liveProposalRows, '尚无 LIVE 资金 Proposal。')}</section><section><h2>LIVE Capital Transfer</h2>${transferTable(liveTransferRows, '尚无 LIVE 划转状态。')}</section>${simulationPanel}</section>`;
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
  document.querySelector('#capital-proposal-form')?.addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); data.expires_in_minutes = Number(data.expires_in_minutes); data.idempotency_key = crypto.randomUUID(); try { await api('/api/capital/proposals', {method:'POST', body:JSON.stringify(data)}); showToast('资金 Proposal 草稿已创建'); await route(); } catch (error) { showApiError(error); } });
  document.querySelector('#capital-fact-form')?.addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); data.environment = 'TESTNET'; data.address_reference = 'masked-test-reference'; data.known = true; try { await api('/api/capital/balances/mock', {method:'POST', body:JSON.stringify(data)}); showToast('Mock 只读资金事实已记录'); await route(); } catch (error) { showApiError(error); } });
  document.querySelector('#capital-policy-form')?.addEventListener('submit', async event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); data.idempotency_key = crypto.randomUUID(); try { await api('/api/capital/automation/policies', {method:'POST', body:JSON.stringify(data)}); showToast('非生产资金阈值已保存；Gate 状态未改变'); await route(); } catch (error) { showApiError(error); } });
  document.querySelectorAll('[data-cap-scope-reconcile]').forEach(button => button.addEventListener('click', () => capitalAction('/api/capital/reconciliations', {environment:button.dataset.environment, account_id:button.dataset.account, venue:button.dataset.venue})));
  document.querySelectorAll('[data-cap-auto]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/automation/policies/${button.dataset.capAuto}/evaluate`, {purpose:button.dataset.purpose, idempotency_key:crypto.randomUUID()})));
  document.querySelectorAll('[data-cap-submit]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/proposals/${button.dataset.capSubmit}/submit`, {})));
  document.querySelectorAll('[data-cap-review]').forEach(button => button.addEventListener('click', async () => { try { const proposalId = button.dataset.capReview; const version = Number(button.dataset.version); const grant = await api('/api/auth/mock/step-up', {method:'POST', body:JSON.stringify({action:'capital.approve', object_id:proposalId, object_version:version})}); await api(`/api/capital/proposals/${proposalId}/reviews`, {method:'POST', body:JSON.stringify({decision:'APPROVE', reason:'independent Treasury review', expected_version:version, action_grant:grant.action_grant})}); showToast('资金审核已记录'); await route(); } catch (error) { showApiError(error); } }));
  document.querySelectorAll('[data-cap-authorize]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/proposals/${button.dataset.capAuthorize}/authorizations`, {idempotency_key:crypto.randomUUID(), expires_in_minutes:30})));
  document.querySelectorAll('[data-cap-execute]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/authorizations/${button.dataset.capExecute}/transfers/mock`, {idempotency_key:crypto.randomUUID()})));
  document.querySelectorAll('[data-cap-notilt]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/authorizations/${button.dataset.capNotilt}/transfers/notilt-plan`, {idempotency_key:crypto.randomUUID()})));
  document.querySelectorAll('[data-notilt-execute]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/transfers/${button.dataset.notiltExecute}/notilt-release-execution-plan`, {})));
  document.querySelectorAll('[data-notilt-cancel]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/transfers/${button.dataset.notiltCancel}/notilt-release-cancellation-plan`, {})));
  document.querySelectorAll('[data-notilt-receipt]').forEach(button => button.addEventListener('click', () => { const transactionHash = prompt('输入独立钱包已广播交易的 tx hash'); if (transactionHash) capitalAction(`/api/capital/transfers/${button.dataset.notiltReceipt}/notilt-receipt`, {transaction_hash:transactionHash.trim()}); }));
  document.querySelectorAll('[data-cap-observe]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/transfers/${button.dataset.capObserve}/observations/mock`, {status:button.dataset.status, transaction_reference:`mock-${crypto.randomUUID()}`})));
  document.querySelectorAll('[data-cap-confirm]').forEach(button => button.addEventListener('click', () => { const fee = prompt('Mock 费用', '1'); const net = prompt('Mock 实际到账', '99'); if (fee !== null && net !== null) capitalAction(`/api/capital/transfers/${button.dataset.capConfirm}/observations/mock`, {status:'DESTINATION_CONFIRMED', transaction_reference:`mock-${crypto.randomUUID()}`, fee_amount:fee, net_received:net}); }));
  document.querySelectorAll('[data-cap-reconcile]').forEach(button => button.addEventListener('click', () => capitalAction(`/api/capital/transfers/${button.dataset.capReconcile}/reconcile`, {})));
}

async function capitalAction(path, body) { try { await api(path, {method:'POST', body:JSON.stringify(body)}); showToast('资金权威状态已更新'); await route(); } catch (error) { showApiError(error); } }

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
    title:`${affectedCount} 个 Campaign 有事实或对账问题`,
    copy:'这些数字不能直接当作最终结果。先处理 Unknown、过期事实、保护不足或对账差异。',
    href:'/exceptions',
    action:'先处理异常',
  };
  if (activeCount) return {
    tone:'attention',
    title:`${activeCount} 个 Campaign 仍在运行`,
    copy:'当前盈亏会随仓位和成交事实继续变化；只有仓位归零、退出终结且对账一致后，结果才会固定。',
    href:'/campaigns',
    action:'查看进行中 Campaign',
  };
  if (closedCount) return {
    tone:'success',
    title:`已结算 ${closedCount} 个 Campaign，当前没有待处理异常`,
    copy:'已关闭 Campaign 已满足退出终结、仓位归零与对账一致；下方保留盈亏、成本和完整审计链。',
    href:'/campaigns',
    action:'查看 Campaign 记录',
  };
  return {
    tone:'clear',
    title:'该环境尚未形成可结算结果',
    copy:'没有持久化的 Campaign 事实，因此这里不会推测盈亏。先从机会和提案流程形成可审计的交易记录。',
    href:'/opportunities',
    action:'查看市场机会',
  };
}

function resultsEnvironmentNotice(environment) {
  return {
    SHADOW:'SHADOW 仅展示合成记录，不代表交易所执行或真实收益。',
    TESTNET:'TESTNET 仅展示非生产环境记录，不代表真实收益。',
    LIVE:'LIVE 只展示系统实际收到并持久化的事实，不承诺盈利。',
  }[environment];
}

async function renderActualResults() {
  const environment = new URLSearchParams(location.search).get('environment') || 'SHADOW';
  const [resultResponse, auditResponse, runtimeResponse, exceptionResponse] = await Promise.all([
    api(`/api/results?environment=${encodeURIComponent(environment)}`),
    api(`/api/audit?environment=${encodeURIComponent(environment)}&limit=200`),
    api('/api/runtime/status'),
    api('/api/campaign-exceptions'),
  ]);
  const results = resultResponse.data;
  const runtime = runtimeResponse.data;
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
  const campaigns = results.campaigns.map(item => `<tr><td><a class="table-primary-link" href="/campaigns/${item.campaign_id}" data-link><b>${escapeHtml(item.symbol || 'Campaign')}</b><span>${shortId(item.campaign_id)} · 打开明细 →</span></a><span class="subtle">${escapeHtml(item.actuality)}</span></td><td>${escapeHtml(item.source || 'UNKNOWN')} · ${escapeHtml(item.source_type || 'UNKNOWN')}<br><span class="subtle">${escapeHtml(item.source_candidate_id || 'MANUAL')} · ${escapeHtml(item.source_version || 'no version')}</span></td><td>${escapeHtml(item.venue)} · ${escapeHtml(item.symbol || item.instrument_id)}<br><span class="subtle">${escapeHtml(item.account_id)} · ${escapeHtml(item.direction)} · ${escapeHtml(item.risk_tier || 'UNKNOWN')}</span></td><td><b>${escapeHtml(item.status)}</b><br><span class="subtle">${item.fill_count} fills</span></td><td><b class="${resultValueClass(item.final_pnl)}">${signedResult(item.final_pnl)}</b> ${escapeHtml(item.currency)}</td><td>${fmtNumber(item.fees)} / ${fmtNumber(item.funding)} / ${fmtNumber(item.slippage)}</td><td>${fmtDate(item.updated_at)}</td></tr>`).join('');
  const curves = Object.entries(results.curves_by_currency).flatMap(([currency, curve]) => curve.points.map(point => `<tr><td>${escapeHtml(currency)}</td><td><a class="table-primary-link compact" href="/campaigns/${point.campaign_id}" data-link>${shortId(point.campaign_id)} →</a></td><td>${signedResult(point.cumulative_pnl)}</td><td>${signedResult(point.running_peak)}</td><td>${fmtNumber(point.drawdown)}</td><td>${fmtDate(point.at)}</td></tr>`)).join('');
  const audits = auditResponse.data.map(item => {
    const href = item.object_type === 'Campaign' ? `/campaigns/${item.object_id}` : item.object_type === 'Proposal' ? `/proposals/${item.object_id}` : null;
    const object = `<b>${escapeHtml(item.event_type)}</b><br><span class="subtle">${escapeHtml(item.object_type)} · ${shortId(item.object_id)}${href ? ' · 打开 →' : ''}</span>`;
    return `<tr><td>${fmtDate(item.created_at)}</td><td>${escapeHtml(item.actor)}</td><td>${href ? `<a class="table-primary-link compact" href="${href}" data-link>${object}</a>` : object}</td><td>${escapeHtml(item.reason)}</td><td>${shortId(item.correlation_id)}<br><span class="subtle">v${item.object_version}</span></td></tr>`;
  }).join('');
  const gates = Object.entries(runtime.capability_gates).map(([key, gate]) => `<tr><td>${escapeHtml(key)}</td><td><b>${escapeHtml(gate.status)}</b></td><td>${escapeHtml(gate.reason)}</td><td>${fmtDate(gate.updated_at)}</td></tr>`).join('');
  main.innerHTML = `<section class="page results-page"><header class="page-head"><div><p class="eyebrow">RECORDED FACTS · ${escapeHtml(environment)}</p><h1>实际结果</h1><p class="lede">先看实际盈亏和当前结论，再下钻到每个 Campaign 的成交、成本、对账与审计记录。不同环境强制分开。</p></div><label>环境<select id="results-environment"><option ${environment === 'SHADOW' ? 'selected' : ''}>SHADOW</option><option ${environment === 'TESTNET' ? 'selected' : ''}>TESTNET</option><option ${environment === 'LIVE' ? 'selected' : ''}>LIVE</option></select></label></header>
    <div class="callout"><b>${escapeHtml(resultsEnvironmentNotice(environment))}</b></div>
    <article class="results-verdict tone-${verdict.tone}"><div><p class="eyebrow">当前结论</p><h2>${escapeHtml(verdict.title)}</h2><p>${escapeHtml(verdict.copy)}</p></div><a class="${verdict.tone === 'danger' ? 'danger' : 'secondary'}" href="${verdict.href}" data-link>${escapeHtml(verdict.action)}</a></article>
    <section aria-labelledby="results-outcome-heading"><div class="section-heading"><div><p class="eyebrow">OUTCOME</p><h2 id="results-outcome-heading">按结算币种看结果</h2></div><p class="subtle">进行中为当前值，已关闭才是最终值</p></div>${outcomeCards ? `<div class="result-outcome-grid">${outcomeCards}</div>` : '<div class="empty-state compact-empty"><div><h2>暂无结果</h2><p>系统没有收到可归因的 Campaign 事实，因此不会展示推测数字。</p></div></div>'}</section>
    <div class="stats results-stats"><div class="stat"><small>Campaign</small><b>${results.campaigns.length}</b></div><div class="stat"><small>已关闭</small><b>${closedCount}</b></div><div class="stat"><small>待处理 Campaign</small><b class="${affectedCount ? 'danger-text' : ''}">${affectedCount}</b></div><div class="stat"><small>审计事件</small><b>${auditResponse.data.length}</b></div></div>
    <section><h2>盈亏与成本明细</h2>${totals ? `<div class="table-wrap"><table><thead><tr><th>币种</th><th>已实现</th><th>未实现</th><th>最终 / 当前</th><th>手续费</th><th>资金费</th><th>滑点</th></tr></thead><tbody>${totals}</tbody></table></div>` : '<div class="callout">该环境尚无可归因 Campaign。</div>'}</section>
    <section><h2>Campaign 实际事实</h2>${campaigns ? `<div class="table-wrap"><table><thead><tr><th>Campaign / 事实类型</th><th>来源</th><th>作用域</th><th>状态</th><th>PnL</th><th>费用 / 资金费 / 滑点</th><th>更新时间</th></tr></thead><tbody>${campaigns}</tbody></table></div>` : '<div class="callout">当前环境没有 Campaign。</div>'}</section>
    <section><h2>已关闭 Campaign 累计 PnL 与绝对回撤</h2><p class="safety-note">没有可靠期初资本时只展示结算币种绝对值，不伪造百分比收益率或回撤。</p>${curves ? `<div class="table-wrap"><table><thead><tr><th>币种</th><th>Campaign</th><th>累计 PnL</th><th>历史峰值</th><th>回撤</th><th>时间</th></tr></thead><tbody>${curves}</tbody></table></div>` : '<div class="callout">没有已关闭 Campaign 曲线点。</div>'}</section>
    <section><h2>作用域审计时间线</h2><p class="subtle">可打开 Proposal 或 Campaign 继续追查；Correlation 与版本用于定位同一条审计链。</p>${audits ? `<div class="table-wrap"><table><thead><tr><th>时间</th><th>操作者</th><th>事件 / 对象</th><th>原因</th><th>Correlation / 版本</th></tr></thead><tbody>${audits}</tbody></table></div>` : '<div class="callout">当前身份和环境下没有可见审计事件。</div>'}</section>
    <details class="results-technical"><summary><span><b>系统运行边界与技术状态</b><small>仅在排查连接、功能开关或数据版本时查看</small></span><strong>展开技术详情</strong></summary><div class="results-technical-content"><div class="stats"><div class="stat"><small>数据库</small><b style="font-size:14px">${runtime.database_ready ? 'READY' : 'NOT READY'}</b></div><div class="stat"><small>运行环境</small><b style="font-size:14px">${escapeHtml(runtime.runtime_environment)}</b></div><div class="stat"><small>Schema / 表</small><b style="font-size:14px">${escapeHtml(runtime.schema_revision)} · ${runtime.business_table_count}</b></div><div class="stat"><small>外部资金</small><b style="font-size:14px">MOCK ONLY</b></div></div><div class="table-wrap"><table><thead><tr><th>Capability</th><th>状态</th><th>原因</th><th>更新时间</th></tr></thead><tbody>${gates}</tbody></table></div><details class="technical-details"><summary>查看外部边界原始记录</summary><pre>${escapeHtml(JSON.stringify(runtime.external_boundaries, null, 2))}</pre></details></div></details>
  </section>`;
  document.querySelector('#results-environment')?.addEventListener('change', event => navigate(`/results?environment=${encodeURIComponent(event.target.value)}`));
}

async function renderExceptions() {
  const result = await api('/api/campaign-exceptions'); const items = result.data;
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
    return `<article class="card exception-card"><div class="exception-card-head"><div><p class="eyebrow">RECOVERY QUEUE</p><h2>Campaign ${shortId(group.campaignId)}</h2></div><span class="status-pill status-DENY">${group.items.length} 项阻断</span></div><ol class="exception-steps">${issues.map((issue, index) => `<li><span class="exception-order">${index + 1}</span><div><h3>${escapeHtml(issue.guidance.title)}${issue.matching.length > 1 ? ` × ${issue.matching.length}` : ''}</h3><p>${escapeHtml(issue.guidance.copy)}</p><strong>下一步：${escapeHtml(issue.guidance.next)}</strong><details class="exception-technical"><summary>查看技术依据</summary><code>${escapeHtml(issue.code)}</code>${issue.matching.some(item => item.details.length) ? `<pre>${escapeHtml(issue.matching.flatMap(item => item.details).join('\n'))}</pre>` : ''}</details></div></li>`).join('')}</ol><a class="primary" href="/campaigns/${group.campaignId}" data-link>打开 Campaign 按顺序处理</a></article>`;
  }).join('');
  main.innerHTML = `<section class="page exceptions-page"><header class="page-head"><div><p class="eyebrow">FAIL CLOSED</p><h1>异常与恢复</h1><p class="lede">只看当前活动 Campaign 的阻断问题。系统按安全顺序说明发生了什么、为什么不能继续，以及下一步该做什么。</p></div><button class="secondary" data-refresh>重新读取当前事实</button></header>
    <div class="stats exception-stats"><div class="stat"><small>受影响 Campaign</small><b class="${groups.length ? 'danger-text' : ''}">${groups.length}</b></div><div class="stat"><small>阻断问题</small><b>${items.length}</b></div><div class="stat"><small>结果未知</small><b class="${unknownCount ? 'danger-text' : ''}">${unknownCount}</b></div><div class="stat"><small>事实过期</small><b class="${staleCount ? 'warning-text' : ''}">${staleCount}</b><span>截止 ${fmtDate(result.as_of)}</span></div></div>
    ${items.length ? `<div class="exception-grid">${cards}</div>` : '<section class="empty-state"><div><h2>当前活动 Campaign 没有阻断异常</h2><p>没有发现 Unknown、过期事实、保护不足或对账差异。已关闭历史不会因为事实变旧而重新报警。</p><div class="toolbar empty-actions"><a class="secondary" href="/" data-link>返回今日</a><a class="primary" href="/campaigns" data-link>查看 Campaign</a></div></div></section>'}</section>`;
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
      showApiError(error, form.querySelector('.form-error'));
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
      addCandidateError = `${error.code}: ${error.message}`;
    }
  }
  const nextStep = campaignNextStep(item, active, canOperate, {positionCurrent, hasPosition, flatKnown, protectionReady, reconciliationMatched, exitTerminal, riskClosable});
  const positionTruth = !item.position ? '未同步' : !positionCurrent ? '需要重新同步' : `${fmtStatus(item.position.fact_status)} · ${fmtNumber(item.position.quantity)}`;
  const protectionTruth = !positionCurrent ? '等待仓位同步' : !hasPosition ? '当前无仓位' : protectionReady ? `完整覆盖 · ${fmtNumber(item.protection.quantity)}` : item.protection ? fmtStatus(item.protection.status) : '尚无保护';
  const activeTruth = active ? `${fmtIntentKind(active.kind)} · ${fmtStatus(active.status)}` : '无进行中意图';
  const reconciliationTruth = item.reconciliation ? `${fmtStatus(item.reconciliation.status)} · ${fmtDate(item.reconciliation.completed_at)}` : '尚未运行';
  const shadowTools = canOperate
    ? `<details class="card operation-toolbox"><summary><span><b>SHADOW 事实与维护工具</b><small>仅用于合成事实、PnL 与对账</small></span></summary><div class="toolbox-content">${nextStep.key === 'position' ? '' : positionFactForm(item)}${hasPosition && nextStep.key !== 'protection' ? protectionFactForm(item) : ''}<div class="toolbar"><button class="secondary" data-pnl>按当前事实刷新 PnL</button><button class="secondary" data-reconcile>重新运行对账</button></div><p class="safety-note">这些动作只写入本地 SHADOW 事实；不会连接交易所或发送真实订单。</p></div></details>`
    : '';
  main.innerHTML = `<section class="page campaign-detail"><header class="page-head"><div><p class="eyebrow">SHADOW · ${escapeHtml(item.venue)}</p><h1>${escapeHtml(item.instrument?.symbol || 'Campaign')} ${shortId(item.campaign_id)}</h1><p class="lede"><b class="status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</b> · ${escapeHtml(item.direction)} · 当前目标 ${fmtNumber(item.current_target_quantity)}</p></div><a class="secondary" href="/campaigns" data-link>返回 Campaign</a></header>
    <article class="campaign-command tone-${nextStep.tone}"><div><p class="eyebrow">当前唯一推荐动作</p><h2>${escapeHtml(nextStep.title)}</h2><p>${escapeHtml(nextStep.copy)}</p></div><div class="campaign-command-action">${nextStep.action}<div class="form-error" id="campaign-action-error"></div></div></article>
    <div class="campaign-truth-grid"><div class="${item.position && !positionCurrent ? 'truth-danger' : ''}"><small>当前仓位</small><b>${escapeHtml(positionTruth)}</b><span>${item.position ? `上次 ${fmtNumber(item.position.quantity)} · ${fmtDate(item.position.observed_at)}` : '等待权威仓位事实'}</span></div><div class="${positionCurrent && hasPosition && !protectionReady ? 'truth-danger' : ''}"><small>原生保护</small><b>${escapeHtml(protectionTruth)}</b><span>${item.protection ? `触发价 ${fmtNumber(item.protection.trigger_price)} · ${fmtDate(item.protection.observed_at)}` : '有仓位时必须确认足额覆盖'}</span></div><div><small>进行中意图</small><b>${escapeHtml(activeTruth)}</b><span>${active ? `${active.side} ${fmtNumber(active.quantity)} · ${shortId(active.intent_id)}` : '不会与新动作冲突'}</span></div><div class="${item.reconciliation && !reconciliationMatched ? 'truth-danger' : ''}"><small>最近对账</small><b>${escapeHtml(reconciliationTruth)}</b><span>${item.reconciliation?.differences?.length ? `${item.reconciliation.differences.length} 项差异待处理` : reconciliationMatched ? '晚于当前仓位与意图' : '需要在最新事实后重跑'}</span></div></div>
    <div class="stats"><div class="stat"><small>已实现 PnL</small><b>${fmtNumber(item.realized_pnl)}</b></div><div class="stat"><small>未实现 PnL</small><b>${fmtNumber(item.unrealized_pnl)}</b></div><div class="stat"><small>最终 / 当前 PnL</small><b>${fmtNumber(item.final_pnl)}</b></div><div class="stat"><small>风险目标</small><b style="font-size:14px">${fmtNumber(item.current_target_quantity)} · ${escapeHtml(item.target_urgency ? fmtStatus(item.target_urgency) : '尚未设置')}</b></div></div>
    <div class="campaign-command-layout"><div class="stack"><article class="card"><div class="card-heading"><div><p class="eyebrow">EXECUTION LEDGER</p><h2>意图与成交记录</h2></div><span class="status-pill">${item.intents.length} 个意图 · ${item.fills.length} 笔成交</span></div>${item.intents.length ? item.intents.map(intentCard).join('') : '<p class="subtle">尚无订单意图。</p>'}</article><article class="card"><div class="card-heading"><div><p class="eyebrow">POSITION TRUTH</p><h2>仓位与保护事实</h2></div><span class="status-pill ${protectionReady ? 'status-APPROVED' : positionCurrent && hasPosition ? 'status-DENY' : ''}">${!positionCurrent ? '仓位待同步' : hasPosition ? (protectionReady ? '保护完整' : '需要保护') : '当前无仓位'}</span></div><dl class="definition-grid spacious">${definition('仓位数量', item.position ? fmtNumber(item.position.quantity) : '未知')}${definition('平均入场', item.position ? fmtNumber(item.position.average_entry_price) : '—')}${definition('标记价', item.position ? fmtNumber(item.position.mark_price) : '—')}${definition('仓位观测', fmtDate(item.position?.observed_at))}${definition('保护状态', item.protection ? fmtStatus(item.protection.status) : '尚无事实')}${definition('保护数量', item.protection ? fmtNumber(item.protection.quantity) : '—')}${definition('保护触发价', item.protection ? fmtNumber(item.protection.trigger_price) : '—')}${definition('保护观测', fmtDate(item.protection?.observed_at))}</dl></article>${canCreatePositionAction ? `<article class="card risk-reduction-card"><div class="card-heading"><div><p class="eyebrow">RISK REDUCTION</p><h2>减仓与退出随时可用</h2></div><span class="status-pill">只减险</span></div><p class="subtle">无论新增风险是否被暂停，都可以把目标降到更小数量或 0；系统只生成 reduce-only 意图。</p>${targetForm(item)}</article>` : ''}${shadowTools}</div>
      <aside class="stack"><article class="card"><div class="card-heading"><div><p class="eyebrow">RISK TARGET</p><h2>风险预留与唯一目标</h2></div><span class="status-pill">v${item.target_version}</span></div>${item.reservations.map(r => `<div class="callout"><b>${escapeHtml(fmtStatus(r.status))}</b> · ${fmtNumber(r.amount)} ${escapeHtml(item.instrument?.collateral_currency || '')}</div>`).join('') || '<p class="subtle">无风险预留。</p>'}<dl class="definition-grid">${definition('目标数量', fmtNumber(item.current_target_quantity))}${definition('紧迫度', item.target_urgency ? fmtStatus(item.target_urgency) : '尚未设置')}${definition('目标原因', item.target_reason || '—')}</dl></article>${managementPanel(item, addCandidates, addCandidateError, canOperate, canAddNow, active, protectionReady, reconciliationMatched)}<article class="card"><div class="card-heading"><div><p class="eyebrow">RECONCILIATION</p><h2>对账结论</h2></div><span class="status-pill ${reconciliationMatched ? 'status-APPROVED' : item.reconciliation ? 'status-DENY' : ''}">${escapeHtml(item.reconciliation ? fmtStatus(item.reconciliation.status) : '未运行')}</span></div>${item.reconciliation ? `<p class="subtle">完成于 ${fmtDate(item.reconciliation.completed_at)}</p>${item.reconciliation.differences.length ? `<ul class="exception-list">${item.reconciliation.differences.map(value => `<li><code>${escapeHtml(value)}</code></li>`).join('')}</ul>` : '<p class="success-note">内部意图、场所事实、仓位和保护当前一致。</p>'}` : '<p class="subtle">尚未运行对账；任何不确定结果都必须先对账。</p>'}</article><details class="card technical-details"><summary>发送租约与技术边界</summary><div class="toolbox-content"><dl class="definition-grid">${definition('Owner', item.sender_lease?.owner_id)}${definition('Fencing token', item.sender_lease?.fencing_token)}${definition('到期', fmtDate(item.sender_lease?.expires_at))}</dl><p class="safety-note">Web 动作只记录合成 SHADOW Order；LIVE_ORDER_SEND 仍为关闭。</p></div></details></aside></div></section>`;
  bindCampaignActions(item, active);
}

function campaignNextStep(item, active, canOperate, truth) {
  const filledIntent = item.intents.some(intent => intent.status === 'FILLED');
  if (item.status === 'CLOSED') return {key:'done', tone:'success', title:'Campaign 已完成并关闭', copy:'风险预留已释放，结果保留在审计与实际结果中。', action:'<a class="secondary" href="/results" data-link>查看实际结果</a>'};
  if (active?.status === 'UNKNOWN') return {key:'reconcile', tone:'danger', title:'结果不确定，先对账', copy:'风险继续占用，禁止重发、加仓或释放；先核对场所订单、成交、仓位和保护。', action:canOperate ? '<button class="danger" data-reconcile>立即运行对账</button>' : '<p class="microcopy">等待 Operator 运行对账。</p>'};
  if (active?.status === 'READY') return {key:'intent', tone:'attention', title:`记录${fmtIntentKind(active.kind)}发送结果`, copy:'当前只有这个意图可以推进；获取发送租约后记录 SHADOW Order，不会连接交易所。', action:canOperate ? operationForm(active, item) : '<p class="microcopy">等待 Operator 处理待发送意图。</p>'};
  if (active && ['SENT','PARTIALLY_FILLED'].includes(active.status)) return {key:'intent', tone:'attention', title:`确认${fmtIntentKind(active.kind)}成交结果`, copy:'先记录已确认成交，或在确实无法判断时标记 UNKNOWN；不要创建第二个意图。', action:canOperate ? operationForm(active, item) : '<p class="microcopy">等待 Operator 记录成交结果。</p>'};
  if (!truth.positionCurrent && filledIntent) return {key:'position', tone:'attention', title:'同步成交后的当前仓位', copy:'成交已经记录，但仓位事实早于最新成交或尚未确认；在此之前不能判断保护和下一步。', action:canOperate ? positionFactForm(item) : '<p class="microcopy">等待 Operator 同步仓位事实。</p>'};
  if (truth.hasPosition && !truth.protectionReady) return {key:'protection', tone:'danger', title:'先补齐足额原生保护', copy:'当前有仓位但保护缺失、未知或不足。优先确认保护；若无法保护，使用下方减仓或退出。', action:canOperate ? protectionFactForm(item) : '<p class="microcopy">等待 Operator 确认保护或减仓退出。</p>'};
  if (!truth.reconciliationMatched) return {key:'reconcile', tone:'attention', title:'运行对账确认当前事实', copy:'只有意图、订单、成交、仓位和保护一致后，才适合继续管理或关闭 Campaign。', action:canOperate ? '<button class="primary" data-reconcile>运行当前作用域对账</button>' : '<p class="microcopy">等待 Operator 运行对账。</p>'};
  if (truth.flatKnown && truth.exitTerminal && truth.riskClosable) return {key:'close', tone:'success', title:'仓位已清零，可以关闭 Campaign', copy:'退出结果终结且对账一致；关闭后会释放剩余风险预留并把结果固定到审计记录。', action:canOperate ? '<button class="primary" data-close-campaign>关闭 Campaign</button>' : '<p class="microcopy">等待 Operator 关闭 Campaign。</p>'};
  if (truth.flatKnown) return {key:'close-blocked', tone:'danger', title:'平仓事实仍缺少关闭证据', copy:'仓位虽然为 0，但退出意图或风险预留尚未终结。不要直接释放风险；先到异常页确认原因。', action:'<a class="secondary" href="/exceptions" data-link>查看异常与恢复</a>'};
  if (truth.hasPosition) return {key:'hold', tone:'success', title:'仓位已确认且保护完整', copy:'当前没有必须处理的异常。继续观察；需要时可使用下方减仓或退出，加仓仍需通过全部门控。', action:'<span class="status-pill status-APPROVED">当前无需动作</span>'};
  return {key:'reconcile', tone:'attention', title:'确认当前作用域事实', copy:'当前没有可确认仓位；先运行对账，避免把缺失事实误认为已经平仓。', action:canOperate ? '<button class="primary" data-reconcile>运行当前作用域对账</button>' : '<p class="microcopy">等待 Operator 运行对账。</p>'};
}

function intentCard(intent) { return `<div class="intent-row"><div><b>${escapeHtml(fmtIntentKind(intent.kind))} · ${escapeHtml(intent.side)} ${fmtNumber(intent.quantity)}</b><br><span class="subtle">${shortId(intent.intent_id)} · ${intent.reduce_only ? '只减仓' : '会增加风险'} · ${fmtDate(intent.updated_at)}</span></div><b class="status-${escapeHtml(intent.status)}">${escapeHtml(fmtStatus(intent.status))}</b></div>${intent.order ? `<p class="subtle">SHADOW Order ${escapeHtml(intent.order.venue_order_id)} · 已成交 ${fmtNumber(intent.order.filled_quantity)} / ${fmtNumber(intent.order.ordered_quantity)}</p>` : ''}`; }

function operationForm(intent, item) { if (intent.status === 'UNKNOWN') return '<p class="safety-note">结果不确定：风险保持占用，不提供重发或释放按钮。必须先人工对账。</p>'; if (intent.status === 'READY') return `<div class="action-panel"><h3>记录已发送的 SHADOW 订单</h3><label>交易所订单编号（合成）<input id="venue-order-id" value="shadow-${intent.intent_id.slice(0,8)}"></label><button class="primary" data-shadow-send>确认已发送</button><button class="danger" data-unknown>结果无法确认</button></div>`; return `<form id="fill-form" class="action-panel"><h3>记录已确认的 SHADOW 成交</h3><div class="field-grid"><label>成交编号<input name="venue_fill_id" value="fill-${crypto.randomUUID().slice(0,8)}" required></label><label>成交方向<select name="side"><option ${intent.side === 'BUY' ? 'selected' : ''}>BUY</option><option ${intent.side === 'SELL' ? 'selected' : ''}>SELL</option></select></label><label>成交数量<input name="quantity" type="number" step="any" value="${escapeHtml(intent.quantity)}" required></label><label>成交价格<input name="price" type="number" step="any" required></label><label>手续费<input name="fee" type="number" step="any" value="0"></label><label>币种<input name="fee_currency" value="${escapeHtml(item.instrument?.collateral_currency || 'USDT')}"></label><label>滑点成本<input name="slippage_cost" type="number" step="any" value="0"></label></div><div class="toolbar" style="margin-top:12px"><button class="primary">确认并记录成交</button><button type="button" class="danger" data-unknown>结果无法确认</button></div></form>`; }

function positionFactForm(item) { return `<form id="position-form" class="action-panel"><h3>同步当前 SHADOW 仓位</h3><p class="microcopy">只录入已经确认的场所事实；不确定时不要把数量填成 0。</p><div class="field-grid"><label>数量<input name="quantity" type="number" step="any" value="${escapeHtml(formNumber(item.position?.quantity, '0'))}" required></label><label>平均入场价<input name="average_entry_price" type="number" step="any" value="${escapeHtml(formNumber(item.position?.average_entry_price, '0'))}" required></label><label>标记价<input name="mark_price" type="number" step="any" value="${escapeHtml(formNumber(item.position?.mark_price))}" required></label></div><button class="secondary">确认并记录仓位</button></form>`; }

function protectionFactForm(item) { return `<form id="protection-form" class="action-panel"><h3>确认当前 SHADOW 保护</h3><div class="field-grid"><label>保护 Order ID<input name="venue_order_id" value="${escapeHtml(item.protection?.venue_order_id || 'shadow-stop')}" required></label><label>保护数量<input name="quantity" type="number" step="any" value="${escapeHtml(formNumber(Math.abs(Number(item.position.quantity))))}" required></label><label>触发价<input name="trigger_price" type="number" step="any" value="${escapeHtml(formNumber(item.protection?.trigger_price))}" required></label><label>覆盖状态<select name="coverage"><option value="full">已知且完整</option><option value="degraded">已知但不足</option><option value="unknown">结果未知</option></select></label></div><button class="primary">确认保护事实</button></form>`; }

function targetForm(item) { return `<form id="target-form" class="action-panel"><h3>设定唯一减仓目标</h3><label>减仓后剩余数量<input name="target_quantity" type="number" step="any" min="0" max="${escapeHtml(Math.abs(Number(item.position.quantity)))}" required></label><label>处理速度<select name="urgency"><option value="NORMAL">常规</option><option value="URGENT" selected>紧急</option><option value="IMMEDIATE">立即</option></select></label><label>原因<input name="reason" value="人工降低当前风险" required></label><label>执行限价（Hyperliquid TESTNET 必填）<input name="limit_price" type="number" step="any" min="0"></label><button class="primary">创建只减仓意图</button><button type="button" class="danger" data-auto-exit>评估失效价并退出</button></form>`; }

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
          ? '全局 AUTO_ADD Gate 当前关闭。'
          : '';
  const addForm = canOperate && management.allow_auto_add && Number(management.remaining_adds) > 0 && canAddNow
    ? `<form id="auto-add-form" class="action-panel"><h3>Perptape Add 候选</h3>${candidateError ? `<p class="safety-note">${escapeHtml(candidateError)}</p>` : candidateOptions ? `<label>后续候选<select name="candidate_id">${candidateOptions}</select></label><label>Add 数量<input name="quantity" type="number" step="any" min="0" max="${escapeHtml(management.remaining_quantity)}" required></label><button class="primary" ${management.auto_add_gate !== 'ENABLED' ? 'disabled' : ''}>最终风控并创建 Add 意图</button>` : '<p class="safety-note">当前没有同场所、同标的、同方向的后续 Perptape 候选。</p>'}</form>`
    : `<p class="safety-note">${escapeHtml(addBlockedReason || '该 Campaign 没有剩余的已冻结 AddUnit，或 Proposal 未允许 AUTO_ADD。')}</p>`;
  const canDisableAdd = canOperate && management.allow_auto_add && Number(management.remaining_adds) > 0;
  return `<article class="card"><div class="card-heading"><div><p class="eyebrow">OPTIONAL RISK</p><h2>AUTO_ADD 管理</h2></div><span class="status-pill ${management.auto_add_gate === 'ENABLED' ? 'status-APPROVED' : 'status-EXPIRED'}">${escapeHtml(management.auto_add_gate === 'ENABLED' ? '全局已开启' : '全局已关闭')}</span></div><dl class="definition-grid">${definition('AddUnit', `${item.authorization?.used_adds || 0} / ${item.authorization?.allowed_adds || 0}`)}${definition('剩余数量', fmtNumber(management.remaining_quantity))}${definition('冻结触发价', fmtNumber(management.add_trigger_price))}</dl>${addForm}${canDisableAdd ? '<button class="danger" data-disable-campaign-add>永久关闭本 Campaign 后续 Add</button>' : ''}<p class="safety-note">加仓是高级可选动作，永远排在成交确认、足额保护和一致对账之后。只有首个正成交消费 AddUnit；UNKNOWN 会冻结新增风险。</p></article>`;
}

function bindCampaignActions(item, active) {
  document.querySelectorAll('[data-pnl]').forEach(button => button.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/pnl`, {}, {button:event.currentTarget, pendingLabel:'刷新中…', successMessage:'PnL 已按当前 SHADOW 事实重新计算'})));
  document.querySelectorAll('[data-reconcile]').forEach(button => button.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/reconcile`, {execution_scope:`${item.account_id}:${item.venue}`}, {button:event.currentTarget, pendingLabel:'对账中…', successMessage:'对账已完成；结果已写入审计事实'})));
  document.querySelector('[data-close-campaign]')?.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/close`, {}, {
    button:event.currentTarget,
    pendingLabel:'关闭中…',
    successMessage:'Campaign 已关闭，剩余风险预留已释放',
    confirm:{title:'关闭这个 Campaign？', message:'系统会再次确认仓位已清零、没有进行中意图且最近对账一致。关闭后会释放剩余风险预留，历史事实仍可审计。', confirmLabel:'确认关闭'},
  }));
  document.querySelector('[data-shadow-send]')?.addEventListener('click', async (event) => withPending(event.currentTarget, '记录中…', async () => {
    const owner = `web-${session.user_id.slice(0,8)}`;
    try {
      const lease = await api('/api/sender-leases', {method:'POST', body:JSON.stringify({execution_scope:`${item.account_id}:${item.venue}`, owner_id:owner, lease_seconds:60})});
      await api(`/api/intents/${active.intent_id}/shadow-send`, {method:'POST', body:JSON.stringify({execution_scope:`${item.account_id}:${item.venue}`, owner_id:owner, fencing_token:lease.fencing_token, venue_order_id:document.querySelector('#venue-order-id').value})});
      showToast('已记录 SHADOW send；没有连接交易所'); await route();
    } catch (error) { showApiError(error); }
  }));
  document.querySelectorAll('[data-unknown]').forEach(button => button.addEventListener('click', () => campaignAction(`/api/intents/${active.intent_id}/unknown`, {reason:'operator marked uncertain SHADOW outcome'}, {
    button,
    successMessage:'意图已标记 UNKNOWN；风险保持占用并等待人工对账',
    confirm:{title:'标记为 UNKNOWN？', message:'这会冻结与该意图相关的新增风险，并隐藏重发和释放入口。请只在 SHADOW 结果确实无法确认时继续，随后必须人工对账。', confirmLabel:'标记 UNKNOWN'},
  })));
  document.querySelector('#fill-form')?.addEventListener('submit', event => submitNamedForm(event, `/api/intents/${active.intent_id}/fills`));
  document.querySelector('#position-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction('/api/facts/positions', {...data, account_id:item.account_id, venue:item.venue, instrument_id:item.instrument_id, known:true}, {button:event.submitter, successMessage:'SHADOW 仓位事实已更新'}); });
  document.querySelector('#protection-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction(`/api/campaigns/${item.campaign_id}/protection`, {position_id:item.position.position_id, venue_order_id:data.venue_order_id, quantity:data.quantity, trigger_price:data.trigger_price, fully_covered:data.coverage === 'full', known:data.coverage !== 'unknown'}, {button:event.submitter, successMessage:'保护事实已更新；覆盖状态已重新计算'}); });
  document.querySelector('#target-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction(`/api/campaigns/${item.campaign_id}/managed-reductions`, {target_quantity:data.target_quantity, urgency:data.urgency, reason:data.reason, limit_price:data.limit_price || null, idempotency_key:crypto.randomUUID()}, {button:event.submitter, successMessage:'唯一 reduce-only 目标已生成'}); });
  document.querySelector('[data-auto-exit]')?.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/automatic-exit`, {idempotency_key:crypto.randomUUID(), limit_price:document.querySelector('#target-form')?.elements.limit_price.value || null}, {
    button:event.currentTarget,
    successMessage:'自动退出评估已完成；SHADOW 退出意图已按冻结失效价生成',
    confirm:{title:'评估并自动退出？', message:'确认后会按冻结失效价评估退出条件，并可能生成新的 reduce-only SHADOW 意图。不会连接交易所或发送真实订单。', confirmLabel:'评估并生成退出意图'},
  }));
  document.querySelector('#auto-add-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction(`/api/campaigns/${item.campaign_id}/auto-add`, {candidate_id:data.candidate_id, quantity:data.quantity, idempotency_key:crypto.randomUUID()}, {button:event.submitter, successMessage:'Add 候选已完成最终风控；结果已记录'}); });
  document.querySelector('[data-disable-campaign-add]')?.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/auto-add/disable`, {reason:'operator disabled further Campaign AddUnits', idempotency_key:crypto.randomUUID()}, {
    button:event.currentTarget,
    successMessage:'本 Campaign 的后续 Add 已关闭',
    confirm:{title:'关闭本 Campaign 后续 Add？', message:'确认后，该 Campaign 剩余 AddUnit 将不能继续使用。已有仓位仍可减仓或退出。', confirmLabel:'关闭后续 Add'},
  }));
}

async function campaignAction(path, body, {button = null, pendingLabel = '处理中…', successMessage = 'SHADOW 权威状态已更新', confirm = null} = {}) {
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
    const active = location.pathname === link.getAttribute('href');
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
  event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form)); const candidateId = data.candidate_id; delete data.candidate_id; data.expires_in_minutes = Number(data.expires_in_minutes); data.initial_quantity = data.initial_quantity || null; data.add_trigger_price = data.add_trigger_price || null; data.allow_auto_add = data.allow_auto_add === 'true'; data.requested_adds = Number(data.requested_adds);
  try { const result = await api(`/api/opportunities/${candidateId}/proposals`, {method:'POST', body:JSON.stringify(data)}); dialog.close(); showToast('SYSTEM 提案已冻结并进入审核'); navigate(`/proposals/${result.proposal_id}`); }
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
document.documentElement.dataset.theme = localStorage.getItem('trading-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
syncNavigationMode();
bootstrap().catch((error) => { main.innerHTML = errorView(error, false); });
