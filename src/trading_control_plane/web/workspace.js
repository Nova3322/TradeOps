function renderLogin() {
  main.innerHTML = `<section class="login-page"><div class="login-card">
    <span class="mock-ribbon">账户密码验证</span>
    <p class="eyebrow" style="margin-top:18px">内部访问</p><h1>进入交易控制台</h1>
    <p class="lede">使用管理员分配的账户和密码登录。系统不开放外部注册。</p>
    ${sessionNotice ? `<div class="callout" role="status">${escapeHtml(sessionNotice)}</div>` : ''}
    <form id="login-form"><label>账户<input name="username" autocomplete="username" required placeholder="请输入账户名"></label><label>密码<input name="password" type="password" autocomplete="current-password" minlength="12" maxlength="128" required placeholder="请输入密码"></label><button class="primary">登录</button><div class="form-error" role="alert"></div></form>
  </div></section>`;
  const loginForm = document.querySelector('#login-form');
  loginForm?.querySelectorAll('input').forEach(input => input.addEventListener('input', () => {
    loginForm.querySelector('.form-error').textContent = '';
  }));
  loginForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const data = new FormData(form);
      const result = await api('/api/auth/login', {method:'POST', body: JSON.stringify({username:data.get('username'), password:data.get('password')})});
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

function reviewerHomeWorkload(actionableCount, riskControl) {
  const riskStatusAvailable = !riskControl?.error;
  const reviewRestore = riskStatusAvailable && riskControl.actions?.review_restore?.allowed === true;
  const executeRestore = riskStatusAvailable && riskControl.actions?.execute_restore?.allowed === true;
  const restoreTaskCount = reviewRestore || executeRestore ? 1 : 0;
  const hasReviewWork = actionableCount > 0 || restoreTaskCount > 0;
  const headline = actionableCount && restoreTaskCount
    ? `${actionableCount} 笔提案和 1 项风险恢复待办`
    : actionableCount
      ? `${actionableCount} 笔提案等待你的独立审核`
      : restoreTaskCount
        ? '1 项风险恢复待办等待你的独立处理'
        : !riskStatusAvailable
          ? '提案无待办；风险恢复状态暂不可用'
        : '当前没有需要你处理的审核待办';
  const restoreStatus = riskStatusAvailable ? String(restoreTaskCount) : '—';
  const restoreCopy = !riskStatusAvailable
    ? '风险恢复状态读取失败'
    : restoreTaskCount
      ? reviewRestore ? '等待你的独立审核' : '已审核，等待满足条件后执行'
      : '当前没有风险恢复审核待办';
  const needsAttention = hasReviewWork || !riskStatusAvailable;
  return {riskStatusAvailable, reviewRestore, executeRestore, restoreTaskCount, hasReviewWork, needsAttention, headline, restoreStatus, restoreCopy};
}

async function renderHome() {
  if (!hasCapability('operations.view')) {
    if (hasCapability('proposal.review')) {
      const [result, riskControl] = await Promise.all([
        api('/api/proposals?proposal_status=PENDING_REVIEW'),
        api('/api/risk-controls').catch(error => ({error})),
      ]);
      const actionable = result.data.filter(item => item.environment === 'LIVE' && item.actionable_for_current_user);
      const workload = reviewerHomeWorkload(actionable.length, riskControl);
      const actions = [
        actionable.length ? '<a class="primary" href="/reviews" data-link>进入审核队列</a>' : '',
        workload.restoreTaskCount ? '<a class="secondary" href="/risk" data-link>查看风险恢复</a>' : '',
      ].filter(Boolean).join('');
      main.innerHTML = `<section class="page home-page"><article class="home-status tone-${workload.needsAttention ? 'attention' : 'success'}"><div><p class="eyebrow">审核工作台</p><h1>${escapeHtml(workload.headline)}</h1><p>审核人只处理非本人提案与风险恢复申请；不能发起提案、查看资金或操作交易任务。</p></div>${actions ? `<div class="toolbar">${actions}</div>` : ''}</article><div class="stats home-stats"><div class="stat"><small>提案审核</small><b>${actionable.length}</b><span>${actionable.length ? '只统计非本人、未投票且未到期的提案' : '当前没有需要你审核的提案'}</span></div><div class="stat"><small>风险恢复</small><b>${escapeHtml(workload.restoreStatus)}</b><span>${escapeHtml(workload.restoreCopy)}</span></div></div></section>`;
      return;
    }
    if (hasCapability('proposal.create')) {
      const result = await api('/api/proposals');
      const ownedProposals = result.data.filter(item => item.environment === 'LIVE' && item.proposer_id === session.user_id);
      const activeOwnedProposals = ownedProposals.filter(item => ['DRAFT','PENDING_REVIEW'].includes(item.status));
      const pendingOwnedProposals = activeOwnedProposals.filter(item => item.status === 'PENDING_REVIEW');
      main.innerHTML = `<section class="page home-page"><article class="home-status tone-success"><div><p class="eyebrow">提案工作台</p><h1>从机会开始形成交易判断</h1><p>你可以查看机会、发起提案并跟踪自己的当前提案；不能审核、查看资金或操作交易任务。</p></div><div class="toolbar"><a class="primary" href="/opportunities" data-link>查看机会</a><a class="secondary" href="/proposals" data-link>查看提案记录</a></div></article><div class="stats"><div class="stat"><small>我的当前提案</small><b>${activeOwnedProposals.length}</b><span>只统计草稿和等待审核</span></div><div class="stat"><small>等待独立审核</small><b>${pendingOwnedProposals.length}</b><span>创建者不能审核自己的提案</span></div></div></section>`;
      return;
    }
    if (hasCapability('capital.view')) {
      main.innerHTML = `<section class="page home-page"><article class="home-status tone-success"><div><p class="eyebrow">资金工作台</p><h1>当前任务只显示你的资金职责</h1><p>你可以查看资金数据、净值完整性、在途占用和资金对账；交易提案、交易任务与交易所排障不在当前角色范围内。</p></div><a class="primary" href="/capital" data-link>进入资金中心</a></article></section>`;
      return;
    }
    main.innerHTML = `<section class="page home-page"><article class="home-status tone-neutral"><div><p class="eyebrow">尚未分配职责</p><h1>当前身份尚未分配业务职责</h1><p>请由系统管理员分配明确岗位与权限范围；系统不会把缺少权限显示成空数据。</p></div></article></section>`;
    return;
  }
  const riskControlRequest = api('/api/risk-controls').catch(error => {
    if (error.status === 403) return null;
    throw error;
  });
  const [pendingResponse, approvedResponse, campaignResponse, exceptionResponse, riskControl, runtime] = await Promise.all([
    api('/api/proposals?proposal_status=PENDING_REVIEW'),
    api('/api/proposals?proposal_status=APPROVED'),
    api('/api/campaigns'),
    api('/api/campaign-exceptions'),
    riskControlRequest,
    api('/api/runtime/status'),
  ]);
  const now = Date.now();
  const roles = roleNames();
  const canReview = roles.includes('REVIEWER') || roles.includes('SYSTEM_ADMIN');
  const canPropose = roles.includes('PROPOSER') || roles.includes('SYSTEM_ADMIN');
  const canOperate = roles.includes('OPERATOR') || roles.includes('SYSTEM_ADMIN');
  const pending = pendingResponse.data.filter(item => item.environment === 'LIVE');
  const approvedAwaitingLaunch = canOperate
    ? approvedResponse.data.filter(item => item.environment === 'LIVE' && proposalAwaitingLaunch(item))
    : [];
  const actionableReviews = canReview ? pending.filter(item => item.actionable_for_current_user) : [];
  const systemReviewCount = actionableReviews.filter(item => item.source === 'SYSTEM').length;
  const manualReviewCount = actionableReviews.length - systemReviewCount;
  const expiringReviews = actionableReviews.filter(item => new Date(item.expires_at).getTime() - now < 30 * 60 * 1000);
  const nextReview = [...actionableReviews].sort((left, right) => new Date(left.expires_at) - new Date(right.expires_at))[0];
  const activeCampaigns = campaignResponse.data.filter(item => item.environment === 'LIVE' && item.status !== 'CLOSED');
  const activeCampaignIds = new Set(activeCampaigns.map(item => item.campaign_id));
  const exceptions = exceptionResponse.data.filter(item => activeCampaignIds.has(item.campaign_id));
  const exceptionCampaigns = new Set(exceptions.map(item => item.campaign_id));
  const riskLimited = Boolean(riskControl?.policy && riskControl.policy.system_state !== 'NORMAL');
  const riskPolicySummary = riskControl?.policy?.system_state === 'NORMAL'
    ? '正常'
    : riskControl?.policy ? riskControlStatusLabel(riskControl.policy.system_state) : '未配置';
  const liveOrderSendEnabled = runtime.capability_gates?.LIVE_ORDER_SEND?.status === 'ENABLED'
    && runtime.external_boundaries?.execution?.live_order_send === true;
  const liveOrderSendLabel = liveOrderSendEnabled ? '已开启' : '已关闭';
  const clearScopeLabel = riskControl ? '当前未发现运行阻断' : '当前作用域无运行告警';
  const safety = exceptions.length
    ? {
        tone:'danger',
        eyebrow:canOperate ? '风险提醒' : '只读观察',
        title:canOperate ? `${exceptionCampaigns.size} 个交易任务需要先处理` : `${exceptionCampaigns.size} 个交易任务出现运行告警`,
        copy:canOperate
          ? `当前有 ${exceptions.length} 项阻断问题。相关新增风险保持关闭；先确认结果未知、仓位、保护和对账数据。`
          : `当前有 ${exceptions.length} 项阻断问题。你可以查看事实，处理与风险动作由交易运维人员负责。`,
        href:'/campaigns/alerts',
        action:'查看运行告警',
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
        : approvedAwaitingLaunch.length
          ? {
              tone:'attention',
              eyebrow:'交易待启动',
              title:`${approvedAwaitingLaunch.length} 笔已批准提案等待风险检查或启动`,
              copy:'批准不会自动下单。交易运维需要按当前账户事实重新风控、签发短期授权，再创建交易任务。',
              href:'/proposals',
              action:'查看当前提案',
            }
        : activeCampaigns.length
          ? {
              tone:'success',
              eyebrow:'交易运行中',
              title:`${clearScopeLabel}；${activeCampaigns.length} 个交易任务正在运行`,
              copy:canOperate
                ? '没有派生异常。继续观察仓位、保护、意图和最近对账；需要降险时可随时减仓或退出。'
                : '当前没有派生异常。你可以查看任务事实；减仓、退出和其他操作仍由交易运维人员负责。',
              href:'/campaigns',
              action:'查看运行中交易任务',
            }
          : {
              tone:'success',
              eyebrow:canOperate ? '当前无待办' : '只读观察',
              title:canOperate ? `${clearScopeLabel}，没有必须立即处理的事项` : '当前没有运行告警或运行中交易任务',
              copy:canOperate
                ? `系统没有发现阻断异常、待你审核的提案或运行中交易任务。${riskControl ? '' : '当前团队风险恢复仍由管理员控制。'} 可以继续观察机会。`
                : '当前身份只查看市场与运行事实，不能启动交易、审核提案或改变风险状态。可以继续观察机会。',
              href:'/opportunities',
              action:'查看市场机会',
            };
  const priorityCards = [];
  if (exceptions.length) priorityCards.push(`<a class="home-priority danger" href="/campaigns/alerts" data-link><span class="priority-number">1</span><div><small>严重运行告警</small><b>${exceptions.length} 项运行问题</b><p>影响 ${exceptionCampaigns.size} 个交易任务；${canOperate ? '结果未知、保护不足和对账差异不会被自动忽略。' : '当前身份只能查看，处理与风险动作由交易运维人员负责。'}</p></div><strong>查看运行告警 →</strong></a>`);
  if (riskLimited) priorityCards.push(`<a class="home-priority attention" href="/risk" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>新增风险受限</small><b>${escapeHtml(riskControlStatusLabel(riskControl.policy.system_state))}</b><p>${riskControl.restore_conditions.blockers.length ? `${riskControl.restore_conditions.blockers.length} 项恢复条件尚未满足。` : '恢复条件已满足，仍需完成受控审核与执行。'} 减仓和退出不受阻断。</p></div><strong>查看恢复条件 →</strong></a>`);
  if (actionableReviews.length) priorityCards.push(`<a class="home-priority attention" href="/reviews" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>独立审核队列</small><b>${actionableReviews.length} 笔非本人提案等待审核</b><p>${expiringReviews.length ? `${expiringReviews.length} 笔将在 30 分钟内到期。` : `最早一笔到期于 ${fmtDate(nextReview.expires_at)}。`} 系统机会 ${systemReviewCount} 笔，人工判断 ${manualReviewCount} 笔。</p></div><strong>打开审核队列 →</strong></a>`);
  if (approvedAwaitingLaunch.length) priorityCards.push(`<a class="home-priority attention" href="/proposals" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>交易待启动</small><b>${approvedAwaitingLaunch.length} 笔已批准提案尚未形成交易任务</b><p>先重新运行实时风险检查，再签发短期授权；缺少事实或安全开关未满足时仍会阻断。</p></div><strong>查看当前提案 →</strong></a>`);
  if (activeCampaigns.length) priorityCards.push(`<a class="home-priority" href="/campaigns" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>持续观察</small><b>${activeCampaigns.length} 个运行中交易任务</b><p>${escapeHtml(activeCampaigns.slice(0, 3).map(item => `${item.venue} · ${fmtDirection(item.direction)} · ${fmtStatus(item.status)}`).join('；'))}</p></div><strong>查看运行中任务 →</strong></a>`);
  if (!priorityCards.length) priorityCards.push(`<a class="home-priority clear" href="/opportunities" data-link><span class="priority-number">✓</span><div><small>当前无待办</small><b>继续观察，不必为了操作而操作</b><p>${canPropose ? '机会只是候选；只有形成清楚交易判断时才创建提案。' : '当前身份可以观察机会，但不能创建提案；如有判断请交由提案发起人保存参数。'}</p></div><strong>查看机会 →</strong></a>`);
  main.innerHTML = `<section class="page home-page"><article class="home-status tone-${safety.tone}"><div><p class="eyebrow">${safety.eyebrow}</p><h1>${escapeHtml(safety.title)}</h1><p>${escapeHtml(safety.copy)}</p></div><a class="primary" href="${safety.href}" data-link>${escapeHtml(safety.action)}</a></article>
    <div class="stats home-stats"><div class="stat"><small>运行告警</small><b class="${exceptions.length ? 'danger-text' : ''}">${exceptionCampaigns.size}</b><span>${exceptions.length ? `${exceptions.length} 项问题` : '无运行告警 / 无需处理'}</span></div><div class="stat"><small>非本人待审核</small><b class="${expiringReviews.length ? 'warning-text' : ''}">${actionableReviews.length}</b><span>${expiringReviews.length ? `${expiringReviews.length} 笔即将到期` : canReview ? '创建者不可审核自己的提案' : '当前身份不是审核人'}</span></div><div class="stat"><small>运行中交易任务</small><b>${activeCampaigns.length}</b><span>${activeCampaigns.length ? '保护与对账需持续有效' : '当前没有活动仓位流程'}</span></div><div class="stat"><small>风险政策</small><b class="${riskLimited ? 'warning-text status-copy' : 'status-copy'}">${escapeHtml(riskPolicySummary)}</b><span>真实下单${escapeHtml(liveOrderSendLabel)} · 自动加仓${escapeHtml(riskControl ? riskControlStatusLabel(riskControl.auto_add_gate.status) : '由管理员控制')}</span></div></div>
    <div class="home-layout"><section><div class="section-heading"><div><p class="eyebrow">处理顺序</p><h2>现在按这个顺序处理</h2></div><button class="secondary" data-refresh>刷新当前数据</button></div><div class="home-priority-list">${priorityCards.join('')}</div></section>
      <aside class="stack"><article class="card home-quick-start"><p class="eyebrow">${canPropose ? '新的交易判断' : '市场观察'}</p><h2>${canPropose ? '开始新的判断' : '继续观察市场机会'}</h2><p class="subtle">${canPropose ? '先看机会或写交易判断；这两条路径都只会创建提案，并进入独立审核。' : '当前身份只查看候选，不保存交易参数，也不会从这里新增风险。'}</p><div class="stacked-actions"><a class="primary" href="/opportunities" data-link>查看 Perptape 机会</a>${canPropose ? '<a class="secondary" href="/proposals/new" data-link>创建人工提案</a>' : ''}</div></article>
        <article class="card home-boundary"><p class="eyebrow">系统边界</p><h2>当前控制状态</h2><dl class="definition-grid">${definition('站点环境', fmtEnvironment(authStatus?.environment, true))}${definition('风险政策', riskPolicySummary)}${definition('真实下单', liveOrderSendLabel)}${definition('自动加仓', riskControl ? riskControlStatusLabel(riskControl.auto_add_gate.status) : '由管理员控制')}${definition('安全原则', '数据缺失即阻断')}</dl><p class="safety-note">页面只展示当前事实；任何真实发送仍需通过交易任务、短期授权、交易所配置和服务端安全开关的逐项检查。</p></article></aside></div></section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
}
function workspaceCreationForm({gateway = false} = {}) {
  return `<form class="${gateway ? 'workspace-gateway-create' : 'card scope-create-form'}" id="create-workspace-form" data-destination="/home">
    <div><p class="eyebrow">新的隔离边界</p><h2>${gateway ? '创建工作区' : '创建 Workspace'}</h2><p>创建后自动建立同名默认团队。成员、账户、权限与交易数据不会继承其他工作区。</p></div>
    <label>工作区名称<input name="name" maxlength="120" placeholder="例如 TradingOPS APAC" required></label>
    <label>标识（可选）<input name="slug" maxlength="80" pattern="[a-z0-9-]+" placeholder="tradingops-apac"></label>
    <div class="form-error" role="alert"></div><button class="primary">创建并进入</button>
  </form>`;
}

function renderWorkspaceGateway() {
  const workspaces = session.workspaces || [];
  const workspaceCards = workspaces.map(workspace => {
    const teamId = workspaceDefaultTeamId(workspace);
    const active = workspace.workspace_id === session.active_workspace?.workspace_id;
    const memberSummary = `${workspace.member_count ?? 0} 名成员${workspace.agent_count ? ` · ${workspace.agent_count} 个 Agent` : ''}`;
    return `<button class="workspace-gateway-option ${active ? 'is-active' : ''}" type="button" data-enter-workspace="${escapeHtml(workspace.workspace_id)}" data-enter-team="${escapeHtml(teamId || '')}"><span class="workspace-gateway-avatar" aria-hidden="true">${escapeHtml(workspace.name.slice(0, 1).toUpperCase())}</span><span class="workspace-gateway-option-copy"><b>${escapeHtml(workspace.name)}</b><small>${escapeHtml(memberSummary)} · ${workspace.role === 'ADMIN' ? '管理员' : '成员'}</small></span><span class="workspace-gateway-option-state">${teamId ? '进入' : '继续设置'}</span></button>`;
  }).join('');
  const createContent = workspaceCreationForm({gateway:true});
  main.innerHTML = `<section class="workspace-gateway-page" aria-labelledby="workspace-gateway-title">
    <div class="workspace-gateway-tabs" role="tablist" aria-label="入口类型"><span role="tab" aria-selected="true">工作区</span><span role="tab" aria-selected="false" aria-disabled="true">个人账户</span></div>
    <article class="workspace-gateway-card">
      <span class="workspace-gateway-logo" aria-hidden="true"><img src="/assets/tradingops-logo.png" alt=""></span>
      <div class="workspace-gateway-heading"><p class="eyebrow">TradingOPS Workspace</p><h1 id="workspace-gateway-title">${workspaces.length ? '选择工作区' : '创建第一个工作区'}</h1><p>${workspaces.length ? '每个工作区拥有独立成员、默认团队、账户、权限与交易数据。' : '工作区是成员、默认团队、账户、权限与交易数据的隔离边界。'}</p></div>
      ${workspaces.length ? `<div class="workspace-gateway-list">${workspaceCards}</div><details class="workspace-gateway-create-panel"><summary>创建新工作区</summary>${createContent}</details>` : createContent}
    </article>
    <p class="workspace-gateway-footnote">进入后仍可从侧栏切换工作区。服务端会重新加载对应团队、成员和权限范围。</p>
  </section>`;
  document.querySelectorAll('[data-enter-workspace]').forEach(button => button.addEventListener('click', () => withPending(button, '进入中…', () => selectScope(button.dataset.enterWorkspace, button.dataset.enterTeam || null, {destination:'/home'}))));
  bindScopeCreationForms();
}

function scopeCreationPanels({compact = false} = {}) {
  const workspaceAdmin = currentWorkspaceMembership()?.role === 'ADMIN';
  return `<div class="scope-creation-grid ${compact ? 'is-compact' : ''}">
    ${workspaceCreationForm()}
    ${workspaceAdmin ? `<form class="card scope-create-form" id="create-team-form">
      <div><p class="eyebrow">当前 Workspace</p><h2>创建团队</h2><p>新团队默认处于安全配置阶段，不读取现有团队数据，也不开放交易能力。</p></div>
      <label>团队名称<input name="name" maxlength="120" placeholder="例如 Alpha 策略组" required></label>
      <label>标识（可选）<input name="slug" maxlength="80" pattern="[a-z0-9-]+" placeholder="alpha-desk"></label>
      <div class="form-error" role="alert"></div><button class="primary">创建并进入团队</button>
    </form>` : `<article class="card scope-create-form scope-readonly"><div><p class="eyebrow">Workspace 权限</p><h2>团队由管理员创建</h2><p>你可以进入已经加入的团队；创建团队需要当前 Workspace 的管理员权限。</p></div></article>`}
  </div>`;
}

function bindScopeCreationForms() {
  document.querySelector('#create-workspace-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const payload = {name:data.get('name'), slug:data.get('slug') || null, idempotency_key:crypto.randomUUID()};
    await withPending(event.submitter, '创建中…', async () => {
      try {
        const result = await api('/api/workspaces', {method:'POST', body:JSON.stringify(payload)});
        session = result.session;
        setShell(true);
        history.replaceState({}, '', form.dataset.destination || '/home');
        showToast('工作区与默认团队已创建；交易能力保持关闭');
        await route();
      } catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
  document.querySelector('#create-team-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const payload = {name:data.get('name'), slug:data.get('slug') || null, idempotency_key:crypto.randomUUID()};
    await withPending(event.submitter, '创建中…', async () => {
      try {
        const result = await api('/api/teams', {method:'POST', body:JSON.stringify(payload)});
        session = result.session;
        setShell(true);
        history.replaceState({}, '', '/home');
        showToast('团队已创建并保持交易关闭；请先配置成员与安全边界');
        await route();
      } catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
}

function renderScopeSetup() {
  const activeWorkspace = session.active_workspace;
  const activeTeam = session.active_team;
  const workspaceTeams = (session.teams || []).filter(team => team.workspace_id === activeWorkspace?.workspace_id);
  const workspaceAdmin = currentWorkspaceMembership()?.role === 'ADMIN';
  const teamRows = workspaceTeams.map(team => `<button class="scope-team-row ${team.team_id === activeTeam?.team_id ? 'is-active' : ''}" type="button" data-select-workspace="${escapeHtml(team.workspace_id)}" data-select-team="${escapeHtml(team.team_id)}"><span><b>${escapeHtml(team.name)}</b><small>${team.trading_enabled ? '业务范围已启用' : '安全配置中 · 交易关闭'}</small></span><span class="status-pill ${team.trading_enabled ? 'status-APPROVED' : ''}">${team.team_id === activeTeam?.team_id ? '当前团队' : '进入'}</span></button>`).join('');
  const setupReason = !activeWorkspace
    ? '先创建或选择一个 Workspace。Workspace 是成员与团队的组织边界。'
    : !activeTeam
      ? '请选择已加入的团队；Workspace 管理员也可以创建新团队。'
      : '此团队是全新的隔离边界。完成成员、信号源、账户和风险政策后，必须明确进入影子模式；服务端不会自动开放业务能力。';
  main.innerHTML = `<section class="page scope-setup-page"><header class="page-head"><div><p class="eyebrow">Workspace → Team → Account</p><h1>${activeTeam ? escapeHtml(activeTeam.name) : '选择团队边界'}</h1><p class="lede">${escapeHtml(setupReason)}</p></div><span class="status-pill ${activeTeam?.trading_enabled ? 'status-APPROVED' : ''}">${activeTeam ? activeTeam.trading_enabled ? '已启用' : '安全配置中' : '尚未选择团队'}</span></header>
    ${activeWorkspace ? `<article class="card scope-context-card"><div><p class="eyebrow">当前 Workspace</p><h2>${escapeHtml(activeWorkspace.name)}</h2><p>${workspaceAdmin ? '你是此 Workspace 的管理员，可创建隔离团队。' : '你是此 Workspace 的成员，只能进入已加入的团队。'}</p></div><div class="scope-team-list">${teamRows || '<p class="safety-note">你在此 Workspace 中尚未加入任何团队。</p>'}</div></article>` : ''}
    ${activeTeam && !activeTeam.trading_enabled ? `<article class="home-status tone-attention"><div><p class="eyebrow">Fail closed</p><h2>团队业务能力尚未开放</h2><p>当前可配置成员、信号源、交易账户、通知和风险政策；提案与交易链路保持关闭，直至管理员在影子模式页面完成前置条件并明确启用。</p></div><div class="toolbar">${hasCapability('access.manage') ? '<a class="secondary" href="/admin/users" data-link>配置团队成员</a>' : ''}${hasCapability('venue.view') ? '<a class="primary" href="/shadow" data-link>查看启用条件</a>' : '<span class="status-pill">由团队管理员处理</span>'}</div></article>` : ''}
    ${scopeCreationPanels()}
  </section>`;
  document.querySelectorAll('[data-select-team]').forEach(button => button.addEventListener('click', () => selectScope(button.dataset.selectWorkspace, button.dataset.selectTeam)));
  bindScopeCreationForms();
}

async function selectScope(workspaceId, teamId = null, {destination = '/home'} = {}) {
  try {
    const result = await api('/api/scopes/select', {method:'POST', body:JSON.stringify({workspace_id:workspaceId, team_id:teamId || null, idempotency_key:crypto.randomUUID()})});
    session = result.session;
    setShell(true);
    history.replaceState({}, '', destination);
    showToast(teamId ? '已切换工作区；团队、成员与权限范围已重新加载' : '已切换工作区；请选择团队');
    await route();
  } catch (error) {
    setShell(true);
    showApiError(error);
  }
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
  const venues = ['BINANCE','HYPERLIQUID','OKX','BYBIT'];
  return `<option value="" ${selected ? '' : 'selected'}>全部交易所</option>${venues.map(venue => `<option value="${venue}" ${selected === venue ? 'selected' : ''}>${escapeHtml(fmtVenueLabel(venue))}</option>`).join('')}`;
}

async function renderAccessManagement() {
  const result = await api('/api/admin/users');
  const members = result.data;
  const activeWorkspace = session.active_workspace;
  const activeTeam = session.active_team;
  const cards = members.map(member => {
    const roles = member.roles.map(item => item.role);
    const scope = memberScope(member);
    const roleTags = roles.map(role => `<span>${escapeHtml(fmtRole(role))}</span>`).join('');
    const venueSummary = fmtVenueLabel(scope.venue);
    const scopeSummary = scope.mixed ? '多个账户或交易所范围' : `${scope.account || '全部账户'} · ${scope.venue ? venueSummary : '全部交易所'}`;
    return `<details class="member-access-card ${member.active ? '' : 'is-inactive'}"><summary class="member-access-summary"><span class="member-summary-main"><b>${escapeHtml(member.username)}</b><small>${member.password_configured ? '密码已设置' : '尚未设置密码'} · ${escapeHtml(scopeSummary)}</small><span class="member-role-tags">${roleTags}</span></span><span class="member-summary-actions"><span class="status-pill ${member.active ? 'status-APPROVED' : ''}">${member.active ? '已启用' : '已停用'}</span><strong>${member.is_current_user ? '查看' : '编辑'}</strong></span></summary><form class="member-access-editor" data-user-access="${member.user_id}">
      ${member.is_current_user ? '<p class="safety-note">这是当前账号。为避免误锁死，必须由另一名系统管理员修改。</p>' : ''}
      <div class="permission-grid">${accessRoleOptions(roles, `member-${member.user_id}`, member.is_current_user)}</div>
      <div class="scope-grid"><label>账户范围<input name="account_scope" value="${escapeHtml(scope.account)}" placeholder="留空 = 全部账户" ${member.is_current_user ? 'disabled' : ''}></label><label>交易所范围<select name="venue_scope" ${member.is_current_user ? 'disabled' : ''}>${venueScopeOptions(scope.venue)}</select></label><label>重置密码<input name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" placeholder="留空则不修改" ${member.is_current_user ? 'disabled' : ''}></label><label class="active-toggle"><input name="active" type="checkbox" ${member.active ? 'checked' : ''} ${member.is_current_user ? 'disabled' : ''}>允许进入当前团队并使用已分配权限</label></div>
      ${scope.mixed ? '<p class="danger-note">该成员当前有多个不同的数据范围。保存后，所选岗位会统一使用上面的账户和交易所范围，请先确认。</p>' : ''}
      <div class="form-error" role="alert"></div>${member.is_current_user ? '' : '<div class="form-actions"><button class="secondary">保存权限</button></div>'}</form></details>`;
  }).join('');
  main.innerHTML = `<section class="page access-page"><header class="page-head"><div><p class="eyebrow">${escapeHtml(activeWorkspace?.name || 'Workspace')} · ${escapeHtml(activeTeam?.name || 'Team')}</p><h1>团队成员与权限</h1><p class="lede">岗位、账户范围和交易所范围只在当前团队生效。同一用户加入另一个团队时必须重新分配最小权限。</p></div><span class="status-pill">${members.filter(item => item.active).length} 名启用成员</span></header>
    ${activeTeam && !activeTeam.trading_enabled ? '<article class="home-status tone-attention"><div><p class="eyebrow">安全配置阶段</p><h2>当前仅开放团队管理</h2><p>业务实体尚未完成团队归属，服务端不会让这个新团队读取默认团队的提案、账户、订单或报表。真实下单、资金、签名和广播保持关闭。</p></div><a class="secondary" href="/home" data-link>查看团队状态</a></article>' : ''}
    <article class="card access-principles"><h2>权限分离原则</h2><div class="access-principle-grid"><p><b>审核与发起分开</b><span>审核人不能审核自己的提案；提案发起人不会自动获得执行权限。</span></p><p><b>交易与资金分开</b><span>交易运维人员看不到资金中心；系统管理员拥有最高管理权限，但资金动作仍受实时校验、最终确认和安全开关约束。</span></p><p><b>身份与权限分开</b><span>密码只用于身份验证；岗位和账户范围仍由独立授权控制。</span></p></div></article>
    ${scopeCreationPanels({compact:true})}
    <details class="card create-member-panel"><summary><span><b>加入已有用户</b><small>把现有身份加入当前团队，并独立分配团队岗位</small></span><strong>展开</strong></summary><form id="invite-team-member-form" class="toolbox-content"><div class="field-grid"><label>现有账户名<input name="username" pattern="[A-Za-z0-9._-]+" placeholder="例如 kelly" required></label><label>账户范围<input name="account_scope" placeholder="留空 = 当前团队全部账户"></label><label>交易所范围<select name="venue_scope">${venueScopeOptions()}</select></label></div><div class="permission-grid">${accessRoleOptions([], 'invite')}</div><div class="form-error" role="alert"></div><div class="form-actions"><button class="secondary">加入当前团队</button></div></form></details>
    <details class="card create-member-panel"><summary><span><b>新增内部成员</b><small>创建账户、初始密码和最小必要权限</small></span><strong>展开</strong></summary><form id="create-member-form" class="toolbox-content"><div class="field-grid"><label>账户名<input name="username" pattern="[A-Za-z0-9._-]+" placeholder="例如 reviewer-li" required></label><label>初始密码<input name="password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required placeholder="至少 12 个字符"></label><label>账户范围<input name="account_scope" placeholder="留空 = 全部账户"></label><label>交易所范围<select name="venue_scope">${venueScopeOptions()}</select></label></div><div class="preset-row"><span>常用模板</span><button type="button" class="text-button" data-role-preset="REVIEWER">只审核</button><button type="button" class="text-button" data-role-preset="PROPOSER">只发起提案</button><button type="button" class="text-button" data-role-preset="OPERATOR">交易运维</button></div><div class="permission-grid">${accessRoleOptions([], 'create')}</div><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">创建成员</button></div></form></details>
    <div class="section-heading"><div><p class="eyebrow">当前用户</p><h2>现有成员</h2></div><span class="subtle">截止 ${fmtDate(result.as_of)}</span></div><div class="member-access-list">${cards}</div>
  </section>`;
  document.querySelectorAll('[data-role-preset]').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('#create-member-form input[name="roles"]').forEach(input => { input.checked = input.value === button.dataset.rolePreset; });
  }));
  bindScopeCreationForms();
  document.querySelector('#invite-team-member-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const payload = {username:data.get('username'), roles:data.getAll('roles'), account_scope:data.get('account_scope') || null, venue_scope:(data.get('venue_scope') || '').toUpperCase() || null, idempotency_key:crypto.randomUUID()};
    if (!payload.roles.length) { form.querySelector('.form-error').textContent = '至少选择一个当前团队岗位。'; return; }
    await withPending(event.submitter, '加入中…', async () => {
      try { await api('/api/admin/team-members', {method:'POST', body:JSON.stringify(payload)}); showToast(`${payload.username} 已加入当前团队；其他团队权限未改变`); await route(); }
      catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
  document.querySelector('#create-member-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const payload = {username:data.get('username'), password:data.get('password'), roles:data.getAll('roles'), account_scope:data.get('account_scope') || null, venue_scope:(data.get('venue_scope') || '').toUpperCase() || null};
    if (!payload.roles.length) { form.querySelector('.form-error').textContent = '至少选择一个岗位。'; return; }
    await withPending(event.submitter, '创建中…', async () => {
      try { await api('/api/admin/users', {method:'POST', body:JSON.stringify(payload)}); showToast(`${payload.username} 已创建并可使用账户密码登录`); await route(); }
      catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
  document.querySelectorAll('[data-user-access]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const data = new FormData(form);
    const payload = {roles:data.getAll('roles'), active:data.get('active') === 'on', account_scope:data.get('account_scope') || null, venue_scope:(data.get('venue_scope') || '').toUpperCase() || null, new_password:data.get('new_password') || null};
    if (!payload.roles.length) { form.querySelector('.form-error').textContent = '启用或停用成员时都要保留至少一个岗位记录。'; return; }
    await withPending(event.submitter, '保存中…', async () => {
      try { await api(`/api/admin/users/${form.dataset.userAccess}/access`, {method:'PUT', body:JSON.stringify(payload)}); showToast('成员权限已保存并写入审计'); await route(); }
      catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  }));
}

const agentRoleCatalog = accessRoleCatalog.filter(item => ['OBSERVER','PROPOSER','REVIEWER'].includes(item.role));

function agentRoleOptions(selectedRoles, prefix) {
  const selected = new Set(selectedRoles);
  return agentRoleCatalog.map(item => `<label class="permission-option" for="${prefix}-${item.role}"><input id="${prefix}-${item.role}" name="roles" type="checkbox" value="${item.role}" ${selected.has(item.role) ? 'checked' : ''}><span><b>${escapeHtml(item.label)}</b><small>${escapeHtml(item.copy)}</small></span></label>`).join('');
}

function agentScopeOptions(accounts, selectedAccount = '', selectedVenue = '') {
  return accounts.map(account => {
    const value = `${account.account_id}::${account.venue}`;
    const selected = account.account_id === selectedAccount && account.venue === selectedVenue;
    return `<option value="${escapeHtml(value)}" ${selected ? 'selected' : ''}>${escapeHtml(account.label)} · ${escapeHtml(account.account_id)} · ${escapeHtml(account.venue)}</option>`;
  }).join('');
}

function agentCredentialReveal(result) {
  if (!result) return '';
  if (!result.token) return `<article class="home-status tone-attention agent-token-reveal" role="status"><div><p class="eyebrow">凭据未再次返回</p><h2>该幂等请求已完成</h2><p>服务端只在首次创建或轮换响应中返回明文 Token。若首次响应已丢失，请对该 Agent 执行凭据轮换。</p></div><span class="status-pill">${escapeHtml(result.token_hint || '已保存摘要')}</span></article>`;
  return `<article class="agent-token-reveal" role="status" aria-live="polite"><div class="card-heading"><div><p class="eyebrow">仅显示一次 · 不会持久化到页面存储</p><h2>立即复制 Agent Token</h2></div><span class="status-pill status-APPROVED">版本 ${escapeHtml(result.token_version)}</span></div><p>离开或刷新本页后将不再显示。控制台和 API 列表只保留不可逆摘要与末尾提示。</p><label>Bearer Token<textarea data-agent-plaintext-token readonly rows="3" spellcheck="false" aria-label="仅显示一次的 Agent Bearer Token">${escapeHtml(result.token)}</textarea></label><div class="toolbar"><button class="primary" type="button" data-copy-agent-token>复制 Token</button><span class="subtle">到期 ${fmtDate(result.token_expires_at)}</span></div></article>`;
}

async function renderAgentManagement(credentialResult = null) {
  const [agentResponse, accountResponse] = await Promise.all([
    api('/api/admin/agents'),
    api('/api/exchange-accounts'),
  ]);
  const agents = agentResponse.data;
  const accounts = accountResponse.data.data.filter(item => item.active !== false);
  const defaultScopeOptions = agentScopeOptions(accounts);
  const cards = agents.map(agent => {
    const roles = agent.roles.map(item => item.role);
    const scope = agent.roles[0] || {};
    const selectedOptions = agentScopeOptions(accounts, scope.account_scope, scope.venue_scope);
    const roleTags = roles.map(role => `<span>${escapeHtml(fmtRole(role))}</span>`).join('') || '<span>未分配</span>';
    const token = agent.token;
    return `<details class="member-access-card agent-access-card ${agent.active ? '' : 'is-inactive'}"><summary class="member-access-summary"><span class="member-summary-main"><b>${escapeHtml(agent.username)}</b><small>${escapeHtml(scope.account_scope || '无账户范围')} · ${escapeHtml(scope.venue_scope || '无交易所范围')} · Token ${escapeHtml(token.status)}</small><span class="member-role-tags">${roleTags}</span></span><span class="member-summary-actions"><span class="status-pill ${agent.active && token.status === 'ACTIVE' ? 'status-APPROVED' : ''}">${agent.active ? token.status === 'ACTIVE' ? '可认证' : '凭据到期' : '已停用'}</span><strong>编辑</strong></span></summary><form class="member-access-editor" data-agent-access="${agent.agent_id}" data-auth-version="${agent.auth_version}">
      <div class="agent-fact-strip"><span><b>Token 摘要</b><small>${escapeHtml(token.hint || '—')}</small></span><span><b>版本</b><small>${escapeHtml(token.version)}</small></span><span><b>到期</b><small>${fmtDate(token.expires_at)}</small></span><span><b>最近使用</b><small>${fmtDate(token.last_used_at)}</small></span></div>
      <div class="permission-grid agent-permission-grid">${agentRoleOptions(roles, `agent-${agent.agent_id}`)}</div>
      <div class="scope-grid"><label>唯一账户与交易所范围<select name="scope" required>${selectedOptions}</select></label><label class="active-toggle"><input name="active" type="checkbox" ${agent.active ? 'checked' : ''}>允许此 Token 在当前团队认证</label></div>
      <p class="microcopy">Agent 只能获得观察、提案或独立审核权限；不提供交易执行、风控决策、资金、账户凭据、签名或广播权限。</p><div class="form-error" role="alert"></div><div class="form-actions"><button class="secondary">保存最小权限</button><button class="text-button" type="button" data-rotate-agent="${agent.agent_id}" data-token-version="${token.version}">轮换 Token</button></div></form></details>`;
  }).join('');
  const unavailable = accounts.length === 0;
  main.innerHTML = `<section class="page access-page agent-page"><header class="page-head"><div><p class="eyebrow">${escapeHtml(session.active_workspace?.name || 'Workspace')} · ${escapeHtml(session.active_team?.name || 'Team')}</p><h1>AI Agent 权限</h1><p class="lede">为模型或自动化工作器创建团队固定、账户固定的 API 身份。Agent 与用户共享同一权限、独立审核、幂等和审计真源。</p></div><span class="status-pill">${agents.filter(item => item.active).length} 个启用 Agent</span></header>
    ${agentCredentialReveal(credentialResult)}
    <article class="card access-principles"><h2>不可穿透的 Agent 边界</h2><div class="access-principle-grid"><p><b>最小角色</b><span>仅观察、提案、独立审核；不授予交易、风控、资金或管理岗位。</span></p><p><b>独立审核</b><span>创建者仍不得审核自己的提案；服务端在角色判断前执行同一创建者校验。</span></p><p><b>密钥隔离</b><span>Token 仅首次响应显示；Agent 不读取交易所明文密钥，也不能生成人工动作授权。</span></p></div></article>
    ${unavailable ? '<article class="home-status tone-attention"><div><p class="eyebrow">创建受阻</p><h2>先登记一个团队交易账户</h2><p>Agent 必须绑定现有的精确账户与交易所范围；服务端不会创建通配范围或猜测账户。</p></div><a class="primary" href="/venues" data-link>登记交易账户</a></article>' : ''}
    <details class="card create-member-panel" ${agents.length ? '' : 'open'}><summary><span><b>创建团队 Agent</b><small>Token 只显示一次；创建后默认不具备任何危险能力</small></span><strong>展开</strong></summary><form id="create-agent-form" class="toolbox-content"><div class="field-grid"><label>Agent 名称<input name="username" pattern="[A-Za-z0-9._-]+" placeholder="例如 alpha-model" required></label><label>唯一账户与交易所范围<select name="scope" required ${unavailable ? 'disabled' : ''}>${defaultScopeOptions}</select></label><label>Token 有效天数<input name="expires_in_days" type="number" min="1" max="365" value="90" required></label></div><div class="permission-grid agent-permission-grid">${agentRoleOptions([], 'agent-create')}</div><p class="microcopy">提案 Agent 调用 <code>POST /api/agent/proposals</code>，最多创建已冻结且等待独立审核的 SYSTEM 提案；不会自动风控、授权或下单。</p><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" ${unavailable ? 'disabled title="先登记团队交易账户"' : ''}>创建并显示一次 Token</button></div></form></details>
    <div class="section-heading"><div><p class="eyebrow">当前团队</p><h2>Agent 身份</h2></div><span class="subtle">截止 ${fmtDate(agentResponse.as_of)}</span></div><div class="member-access-list">${cards || '<section class="empty-state"><div><h2>尚未创建 Agent</h2><p>先绑定一个精确账户范围，再只授予当前工作器需要的最小角色。</p></div></section>'}</div>
  </section>`;

  document.querySelector('[data-copy-agent-token]')?.addEventListener('click', async event => {
    const field = document.querySelector('[data-agent-plaintext-token]');
    if (!field) return;
    try { await navigator.clipboard.writeText(field.value); showToast('Token 已复制；请保存到受控的秘密管理器'); }
    catch (_error) { field.focus(); field.select(); showToast('浏览器未允许自动复制；Token 已选中'); }
  });
  document.querySelector('#create-agent-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const [account_scope, venue_scope] = String(data.get('scope') || '').split('::');
    const payload = {username:data.get('username'), roles:data.getAll('roles'), account_scope, venue_scope, expires_in_days:Number(data.get('expires_in_days')), idempotency_key:crypto.randomUUID()};
    if (!payload.roles.length) { form.querySelector('.form-error').textContent = '至少选择一个观察、提案或审核角色。'; return; }
    await withPending(event.submitter, '创建中…', async () => {
      try { const response = await api('/api/admin/agents', {method:'POST', body:JSON.stringify(payload)}); await renderAgentManagement(response.result); enhanceRenderedPage(); }
      catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
  document.querySelectorAll('[data-agent-access]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const data = new FormData(form);
    const active = data.get('active') === 'on';
    const roles = data.getAll('roles');
    const [account_scope, venue_scope] = String(data.get('scope') || '').split('::');
    if (active && !roles.length) { form.querySelector('.form-error').textContent = '启用 Agent 时至少保留一个最小角色。'; return; }
    const payload = {roles, active, account_scope, venue_scope, expected_auth_version:Number(form.dataset.authVersion), idempotency_key:crypto.randomUUID()};
    await withPending(event.submitter, '保存中…', async () => {
      try { await api(`/api/admin/agents/${form.dataset.agentAccess}/access`, {method:'PUT', body:JSON.stringify(payload)}); showToast('Agent 权限已保存；服务端立即按新范围拒绝旧访问'); await renderAgentManagement(); enhanceRenderedPage(); }
      catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  }));
  document.querySelectorAll('[data-rotate-agent]').forEach(button => button.addEventListener('click', async event => {
    const confirmed = await confirmAction({title:'轮换 Agent Token？', message:'当前 Token 会立即失效。新 Token 只在下一次响应中显示一次。', confirmLabel:'确认轮换'});
    if (!confirmed) return;
    await withPending(event.currentTarget, '轮换中…', async () => {
      try { const response = await api(`/api/admin/agents/${event.currentTarget.dataset.rotateAgent}/token-rotations`, {method:'POST', body:JSON.stringify({expected_token_version:Number(event.currentTarget.dataset.tokenVersion), expires_in_days:90, idempotency_key:crypto.randomUUID()})}); await renderAgentManagement(response.result); enhanceRenderedPage(); }
      catch (error) { showApiError(error); }
    });
  }));
}
