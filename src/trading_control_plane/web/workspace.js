function renderLogin() {
  main.innerHTML = `<section class="login-page"><div class="login-card">
    <span class="mock-ribbon">账户密码验证</span>
    <p class="eyebrow" style="margin-top:18px">开源交易控制层</p><h1>所有交易行为进入真实账户前的控制层</h1>
    <p class="lede">TradeOps 让交易员、交易 Bot 和 AI Agent 的每笔交易先经过确定性规则、必要审批和完整审计，再发送到交易所。</p>
    ${sessionNotice ? `<div class="callout" role="status">${escapeHtml(sessionNotice)}</div>` : ''}
    <form id="login-form"><label>账户<input name="username" autocomplete="username" required placeholder="请输入账户名"></label><label>密码<input name="password" type="password" autocomplete="current-password" minlength="12" maxlength="128" required placeholder="请输入密码"></label><button class="primary">登录控制台</button><div class="form-error" role="alert"></div></form>
    <p class="microcopy">使用管理员分配的账户登录；当前版本不开放自助注册。</p>
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
      sessionAuthenticationMethod = result.authentication_method || '';
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
      const actionable = result.data.filter(item => item.actionable_for_current_user);
      const workload = reviewerHomeWorkload(actionable.length, riskControl);
      const actions = [
        actionable.length ? '<a class="primary" href="/reviews" data-link>进入审核队列</a>' : '',
        workload.restoreTaskCount ? '<a class="text-link" href="/risk" data-link>查看风险恢复 →</a>' : '',
      ].filter(Boolean).join('');
      const reviewRows = [];
      if (actionable.length) reviewRows.push(`<a class="home-priority attention" href="/reviews" data-link><span class="priority-number">1</span><div><small>独立审核</small><b>${actionable.length} 笔非本人提案等待判断</b><p>只包含未投票、未到期的当前提案；批准不会产生订单。</p></div><strong>打开审核队列</strong></a>`);
      if (workload.restoreTaskCount) reviewRows.push(`<a class="home-priority attention" href="/risk" data-link><span class="priority-number">${reviewRows.length + 1}</span><div><small>风险恢复</small><b>1 项恢复申请等待独立处理</b><p>${escapeHtml(workload.restoreCopy)}；恢复不会沿用旧授权。</p></div><strong>查看恢复条件</strong></a>`);
      if (!reviewRows.length) reviewRows.push('<article class="home-priority clear"><span class="priority-number">—</span><div><small>当前无待办</small><b>审核队列已清空</b><p>自己的提案、已投票、已过期或已结束记录不会出现在待办中。</p></div><strong>无需操作</strong></article>');
      main.innerHTML = `<section class="page home-page"><article class="home-status tone-${workload.needsAttention ? 'attention' : 'success'}"><div><p class="eyebrow">当前任务 · 审核员</p><h1>${escapeHtml(workload.headline)}</h1><p>只处理非本人提案与风险恢复申请；不能发起提案、查看资金或操作交易任务。</p></div>${actions ? `<div class="toolbar">${actions}</div>` : ''}</article><div class="stats home-stats"><div class="stat"><small>提案审核</small><b>${actionable.length}</b><span>${actionable.length ? '非本人 · 未投票 · 未到期' : '当前没有审核待办'}</span></div><div class="stat"><small>风险恢复</small><b>${escapeHtml(workload.restoreStatus)}</b><span>${escapeHtml(workload.restoreCopy)}</span></div><div class="stat"><small>工作范围</small><b class="status-copy">独立判断</b><span>生产与测试始终分开标记</span></div><div class="stat"><small>安全边界</small><b class="status-copy">只读执行</b><span>批准不下单 · 不能自审</span></div></div><section><div class="section-heading"><div><p class="eyebrow">处理顺序</p><h2>按到期与风险优先处理</h2></div><a class="secondary" href="/reviews" data-link>查看完整队列</a></div><div class="home-priority-list">${reviewRows.join('')}</div></section></section>`;
      return;
    }
    if (hasCapability('proposal.create')) {
      const result = await api('/api/proposals');
      const ownedProposals = result.data.filter(item => item.environment === 'LIVE' && item.proposer_id === session.user_id);
      const activeOwnedProposals = ownedProposals.filter(item => ['DRAFT','PENDING_REVIEW'].includes(item.status));
      const pendingOwnedProposals = activeOwnedProposals.filter(item => item.status === 'PENDING_REVIEW');
      main.innerHTML = `<section class="page home-page"><article class="home-status tone-success"><div><p class="eyebrow">当前任务 · 提案发起人</p><h1>从机会开始形成交易判断</h1><p>查看机会、发起提案并跟踪自己的当前提案；不能审核、查看资金或操作交易任务。</p></div><div class="toolbar"><a class="primary" href="/opportunities" data-link>查看机会</a><a class="text-link" href="/reviews?view=current" data-link>查看提案记录 →</a></div></article><div class="stats home-stats"><div class="stat"><small>我的当前提案</small><b>${activeOwnedProposals.length}</b><span>草稿与等待审核</span></div><div class="stat"><small>等待独立审核</small><b>${pendingOwnedProposals.length}</b><span>创建者不能审核本人提案</span></div><div class="stat"><small>交易执行</small><b class="status-copy">未授权</b><span>提案批准后仍需实时风控</span></div><div class="stat"><small>真实下单</small><b class="status-copy">已关闭</b><span>本岗位无订单发送入口</span></div></div><section><div class="section-heading"><div><p class="eyebrow">下一步</p><h2>形成判断，不为操作而操作</h2></div></div><div class="home-priority-list"><a class="home-priority clear" href="/opportunities" data-link><span class="priority-number">1</span><div><small>市场观察</small><b>先核对机会来源与数据状态</b><p>只有数据当前、参数清楚且风险边界完整时才保存提案。</p></div><strong>查看机会</strong></a></div></section></section>`;
      return;
    }
    if (hasCapability('capital.view')) {
      main.innerHTML = `<section class="page home-page"><article class="home-status tone-success"><div><p class="eyebrow">当前任务 · 资金管理员</p><h1>先确认数据可信，再处理资金路径</h1><p>查看资金数据、净值完整性、在途占用和资金对账；交易提案、交易任务与交易所排障不在当前角色范围内。</p></div><a class="primary" href="/capital" data-link>进入资金中心</a></article><div class="stats home-stats"><div class="stat"><small>工作范围</small><b class="status-copy">资金事实</b><span>只看当前工作区与团队</span></div><div class="stat"><small>缺失数据</small><b class="status-copy">阻断汇总</b><span>不会显示为 0 或当前值</span></div><div class="stat"><small>签名 / 广播</small><b class="status-copy">人控</b><span>控制台不读取私钥</span></div><div class="stat"><small>资金操作</small><b class="status-copy">已关闭</b><span>需服务端安全开关双重开启</span></div></div><section><div class="section-heading"><div><p class="eyebrow">处理顺序</p><h2>先总览，再路径，最后核对回执</h2></div></div><div class="home-priority-list"><a class="home-priority clear" href="/capital" data-link><span class="priority-number">1</span><div><small>资金中心</small><b>核对总额、位置和数据可信度</b><p>缺失、过期或时间错位的来源不会进入当前汇总。</p></div><strong>打开资金总览</strong></a><a class="home-priority" href="/capital?view=activity" data-link><span class="priority-number">2</span><div><small>操作与回执</small><b>检查在途阶段与精确阻断</b><p>任何结果未知的操作都保留在日志中，不会被重复提交掩盖。</p></div><strong>查看操作记录</strong></a></div></section></section>`;
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
  const observerOnly = !canReview && !canPropose && !canOperate;
  const pending = pendingResponse.data;
  const approvedAwaitingLaunch = canOperate
    ? approvedResponse.data.filter(item => item.environment === 'LIVE' && proposalAwaitingLaunch(item))
    : [];
  const actionableReviews = canReview ? pending.filter(item => item.actionable_for_current_user) : [];
  const liveReviewCount = actionableReviews.filter(item => item.environment === 'LIVE').length;
  const testnetReviewCount = actionableReviews.filter(item => item.environment === 'TESTNET').length;
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
          : `当前有 ${exceptions.length} 项阻断问题。你可以查看事实，处理与风险动作由风险管理人员负责。`,
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
            title:`${actionableReviews.length} 笔提案等待独立审核`,
            copy:`当前没有运行阻断。生产 ${liveReviewCount} 笔，测试 ${testnetReviewCount} 笔；打开队列逐项判断，批准不会直接产生订单。`,
            href:'/reviews',
            action:'查看审核队列',
          }
        : approvedAwaitingLaunch.length
          ? {
              tone:'attention',
              eyebrow:'交易待启动',
              title:`${approvedAwaitingLaunch.length} 笔已批准提案等待风险检查或启动`,
              copy:'批准不会自动下单。风险管理需要按当前账户事实重新风控、签发短期授权，再创建交易任务。',
              href:'/reviews?view=current',
              action:'查看当前提案',
            }
        : activeCampaigns.length
          ? {
              tone:'success',
              eyebrow:'交易运行中',
              title:`${clearScopeLabel}；${activeCampaigns.length} 个交易任务正在运行`,
              copy:canOperate
                ? '没有派生异常。继续观察仓位、保护、意图和最近对账；需要降险时可随时减仓或退出。'
                : '当前没有派生异常。你可以查看任务事实；减仓、退出和其他操作仍由风险管理人员负责。',
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
  if (exceptions.length) priorityCards.push(`<a class="home-priority danger" href="/campaigns/alerts" data-link><span class="priority-number">1</span><div><small>严重运行告警</small><b>${exceptions.length} 项运行问题</b><p>影响 ${exceptionCampaigns.size} 个交易任务；${canOperate ? '结果未知、保护不足和对账差异不会被自动忽略。' : '当前身份只能查看，处理与风险动作由风险管理人员负责。'}</p></div><strong>查看运行告警 →</strong></a>`);
  if (riskLimited) priorityCards.push(`<a class="home-priority attention" href="/risk" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>新增风险受限</small><b>${escapeHtml(riskControlStatusLabel(riskControl.policy.system_state))}</b><p>${riskControl.restore_conditions.blockers.length ? `${riskControl.restore_conditions.blockers.length} 项恢复条件尚未满足。` : '恢复条件已满足，仍需完成受控审核与执行。'} 减仓和退出不受阻断。</p></div><strong>查看恢复条件 →</strong></a>`);
  if (actionableReviews.length) priorityCards.push(`<a class="home-priority attention" href="/reviews" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>独立审核队列</small><b>${actionableReviews.length} 笔非本人提案等待审核</b><p>${expiringReviews.length ? `${expiringReviews.length} 笔将在 30 分钟内到期。` : `最早一笔到期于 ${fmtDate(nextReview.expires_at)}。`} 生产 ${liveReviewCount} 笔，测试 ${testnetReviewCount} 笔。</p></div><strong>打开审核队列 →</strong></a>`);
  if (approvedAwaitingLaunch.length) priorityCards.push(`<a class="home-priority attention" href="/reviews?view=current" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>交易待启动</small><b>${approvedAwaitingLaunch.length} 笔已批准提案尚未形成交易任务</b><p>审批达标后系统自动运行实时风控；通过后再签发短期授权，缺少事实或安全开关未满足时仍会阻断。</p></div><strong>查看当前提案 →</strong></a>`);
  if (activeCampaigns.length) priorityCards.push(`<a class="home-priority" href="/campaigns" data-link><span class="priority-number">${priorityCards.length + 1}</span><div><small>持续观察</small><b>${activeCampaigns.length} 个运行中交易任务</b><p>${escapeHtml(activeCampaigns.slice(0, 3).map(item => `${item.venue} · ${fmtDirection(item.direction)} · ${fmtStatus(item.status)}`).join('；'))}</p></div><strong>查看运行中任务 →</strong></a>`);
  if (!priorityCards.length) priorityCards.push(observerOnly
    ? '<article class="home-priority clear"><span class="priority-number">✓</span><div><small>当前无待办</small><b>继续观察，不必为了操作而操作</b><p>当前身份可以观察机会，但不能创建提案；如有判断请交由提案发起人保存参数。</p></div><strong>无需操作</strong></article>'
    : `<a class="home-priority clear" href="/opportunities" data-link><span class="priority-number">✓</span><div><small>当前无待办</small><b>继续观察，不必为了操作而操作</b><p>${canPropose ? '机会只是候选；只有形成清楚交易判断时才创建提案。' : '当前身份可以观察机会，但不能创建提案；如有判断请交由提案发起人保存参数。'}</p></div><strong>查看机会 →</strong></a>`);
  const quickStart = observerOnly ? '' : `<article class="card home-quick-start"><p class="eyebrow">${canPropose ? '新的交易判断' : '市场观察'}</p><h2>${canPropose ? '开始新的判断' : '继续观察市场机会'}</h2><p class="subtle">${canPropose ? '先看机会或写交易判断；这两条路径都只会创建提案，并进入独立审核。' : '当前身份只查看候选，不保存交易参数，也不会从这里新增风险。'}</p><div class="stacked-actions"><a class="secondary" href="/opportunities" data-link>查看 Perptape 机会</a>${canPropose ? '<a class="text-link" href="/proposals/new" data-link>创建人工提案 →</a>' : ''}</div></article>`;
  main.innerHTML = `<section class="page home-page"><article class="home-status tone-${safety.tone}"><div><p class="eyebrow">${safety.eyebrow}</p><h1>${escapeHtml(safety.title)}</h1><p>${escapeHtml(safety.copy)}</p></div><a class="primary" href="${safety.href}" data-link>${escapeHtml(safety.action)}</a></article>
    <div class="stats home-stats"><div class="stat"><small>运行告警</small><b class="${exceptions.length ? 'danger-text' : ''}">${exceptionCampaigns.size}</b><span>${exceptions.length ? `${exceptions.length} 项问题` : '无运行告警 / 无需处理'}</span></div><div class="stat"><small>独立审核待办</small><b class="${expiringReviews.length ? 'warning-text' : ''}">${actionableReviews.length}</b><span>${actionableReviews.length ? `生产 ${liveReviewCount} · 测试 ${testnetReviewCount}` : canReview ? '创建者不可审核自己的提案' : '当前身份不是审核人'}</span></div><div class="stat"><small>运行中交易任务</small><b>${activeCampaigns.length}</b><span>${activeCampaigns.length ? '保护与对账需持续有效' : '当前没有活动仓位流程'}</span></div><div class="stat"><small>风险边界</small><b class="${riskLimited ? 'warning-text status-copy' : 'status-copy'}">${escapeHtml(riskPolicySummary)}</b><span>真实下单${escapeHtml(liveOrderSendLabel)} · 自动加仓${escapeHtml(riskControl ? riskControlStatusLabel(riskControl.auto_add_gate.status) : '由管理员控制')}</span></div></div>
    <div class="home-layout"><section><div class="section-heading"><div><p class="eyebrow">处理顺序</p><h2>现在按这个顺序处理</h2></div><button class="secondary" data-refresh>刷新当前数据</button></div><div class="home-priority-list">${priorityCards.join('')}</div></section>
      <aside class="stack">${quickStart}
        <article class="card home-boundary"><p class="eyebrow">系统边界</p><h2>当前控制状态</h2><dl class="definition-grid">${definition('站点环境', fmtEnvironment(authStatus?.environment, true))}${definition('风险政策', riskPolicySummary)}${definition('真实下单', liveOrderSendLabel)}${definition('自动加仓', riskControl ? riskControlStatusLabel(riskControl.auto_add_gate.status) : '由管理员控制')}${definition('安全原则', '数据缺失即阻断')}</dl><p class="safety-note">页面只展示当前事实；任何真实发送仍需通过交易任务、短期授权、交易所配置和服务端安全开关的逐项检查。</p></article></aside></div></section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
}
function workspaceCreationForm({gateway = false} = {}) {
  return `<form class="${gateway ? 'workspace-gateway-create' : 'card scope-create-form'}" id="create-workspace-form" data-destination="/home">
    <div><p class="eyebrow">新的隔离边界</p><h2>${gateway ? '创建工作区' : '创建 Workspace'}</h2><p>个人使用时初始只有创建者，系统仍自动建立同名默认团队。交易所账户始终归属工作区与团队，不存在个人账户旁路。</p></div>
    <label>工作区名称<input name="name" maxlength="120" placeholder="例如 TradeOps APAC" required></label>
    <label>标识（可选）<input name="slug" maxlength="80" pattern="[a-z0-9-]+" placeholder="tradingops-apac"></label>
    <div class="form-error" role="alert"></div><button class="primary">创建并进入</button>
  </form>`;
}

function renderWorkspaceGateway() {
  const workspaces = session.workspaces || [];
  const workspaceCards = workspaces.map(workspace => {
    const teamId = workspaceDefaultTeamId(workspace);
    const active = workspace.workspace_id === session.active_workspace?.workspace_id;
    const memberSummary = `${workspace.member_count ?? 0} 名成员`;
    return `<button class="workspace-gateway-option ${active ? 'is-active' : ''}" type="button" data-enter-workspace="${escapeHtml(workspace.workspace_id)}" data-enter-team="${escapeHtml(teamId || '')}"><span class="workspace-gateway-avatar" aria-hidden="true">${escapeHtml(workspace.name.slice(0, 1).toUpperCase())}</span><span class="workspace-gateway-option-copy"><b>${escapeHtml(workspace.name)}</b><small>${escapeHtml(memberSummary)} · ${workspace.role === 'ADMIN' ? '管理员' : '成员'}</small></span><span class="workspace-gateway-option-state">${teamId ? '进入' : '继续设置'}</span></button>`;
  }).join('');
  const createContent = workspaceCreationForm({gateway:true});
  main.innerHTML = `<section class="workspace-gateway-page" aria-labelledby="workspace-gateway-title">
    <article class="workspace-gateway-card">
      <span class="workspace-gateway-logo" aria-hidden="true"><img src="/assets/tradingops-logo.png" alt=""></span>
      <div class="workspace-gateway-heading"><p class="eyebrow">TradeOps Workspace</p><h1 id="workspace-gateway-title">${workspaces.length ? '选择工作区' : '创建第一个工作区'}</h1><p>${workspaces.length ? '每个工作区拥有独立成员、默认团队、账户、权限与交易数据。' : '个人使用时创建单人工作区；系统仍会建立同名默认团队。'}</p></div>
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
      : '此团队是全新的隔离边界。完成成员、信号源、账户和风险政策后，必须明确进入测试模式；服务端不会自动开放业务能力。';
  main.innerHTML = `<section class="page scope-setup-page"><header class="page-head"><div><p class="eyebrow">Workspace → Team → Account</p><h1>${activeTeam ? escapeHtml(activeTeam.name) : '选择团队边界'}</h1><p class="lede">${escapeHtml(setupReason)}</p></div><span class="status-pill ${activeTeam?.trading_enabled ? 'status-APPROVED' : ''}">${activeTeam ? activeTeam.trading_enabled ? '已启用' : '安全配置中' : '尚未选择团队'}</span></header>
    ${activeWorkspace ? `<article class="card scope-context-card"><div><p class="eyebrow">当前 Workspace</p><h2>${escapeHtml(activeWorkspace.name)}</h2><p>${workspaceAdmin ? '你是此 Workspace 的管理员，可创建隔离团队。' : '你是此 Workspace 的成员，只能进入已加入的团队。'}</p></div><div class="scope-team-list">${teamRows || '<p class="safety-note">你在此 Workspace 中尚未加入任何团队。</p>'}</div></article>` : ''}
    ${activeTeam && !activeTeam.trading_enabled ? `<article class="home-status tone-attention"><div><p class="eyebrow">安全保护</p><h2>团队业务能力尚未开放</h2><p>当前可配置成员、信号源、交易账户、通知和风险政策；生产交易链路保持关闭，交易模式由团队当前状态统一决定。</p></div><div class="toolbar">${hasCapability('access.manage') ? '<a class="secondary" href="/admin/users" data-link>配置团队成员</a>' : ''}${hasCapability('venue.view') ? '<button class="primary" type="button" data-open-mode-switch>切换当前模式</button>' : '<span class="status-pill">由团队管理员处理</span>'}</div></article>` : ''}
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
    if (error?.handled || !session) return;
    setShell(true);
    showApiError(error);
  }
}

function accessRoleOptions(selectedRoles, prefix, disabled = false) {
  const selected = new Set(selectedRoles);
  return accessRoleCatalog.map(item => `<label class="permission-option" for="${prefix}-${item.role}"><input id="${prefix}-${item.role}" name="roles" type="checkbox" value="${item.role}" ${selected.has(item.role) ? 'checked' : ''} ${disabled ? 'disabled' : ''}><span><b>${escapeHtml(item.label)}</b><small>${escapeHtml(item.copy)}</small></span></label>`).join('');
}

const accessRoleLabel = role => role === 'OPERATOR' ? '风险管理' : fmtRole(role);

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
    const roleTags = roles.map(role => `<span>${escapeHtml(accessRoleLabel(role))}</span>`).join('');
    const venueSummary = fmtVenueLabel(scope.venue);
    const scopeSummary = scope.mixed ? '多个账户或交易所范围' : `${scope.account || '全部账户'} · ${scope.venue ? venueSummary : '全部交易所'}`;
    return `<details class="member-access-card ${member.active ? '' : 'is-inactive'}"><summary class="member-access-summary"><span class="member-summary-main"><b>${escapeHtml(member.username)}</b><small>${member.password_configured ? '密码已设置' : '尚未设置密码'} · ${escapeHtml(scopeSummary)}</small><span class="member-role-tags">${roleTags}</span></span><span class="member-summary-actions"><span class="status-pill ${member.active ? 'status-APPROVED' : ''}">${member.active ? '已启用' : '已停用'}</span><strong>${member.is_current_user ? '查看' : '编辑'}</strong></span></summary><form class="member-access-editor" data-user-access="${member.user_id}">
      ${member.is_current_user ? '<p class="safety-note">这是当前账号。为避免误锁死，必须由另一名系统管理员修改。</p>' : ''}
      <div class="permission-grid">${accessRoleOptions(roles, `member-${member.user_id}`, member.is_current_user)}</div>
      <div class="scope-grid"><label>账户范围<input name="account_scope" value="${escapeHtml(scope.account)}" placeholder="留空 = 全部账户" ${member.is_current_user ? 'disabled' : ''}></label><label>交易所范围<select name="venue_scope" ${member.is_current_user ? 'disabled' : ''}>${venueScopeOptions(scope.venue)}</select></label><label>重置密码<input name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" placeholder="留空则不修改" ${member.is_current_user ? 'disabled' : ''}></label><label class="active-toggle"><input name="active" type="checkbox" ${member.active ? 'checked' : ''} ${member.is_current_user ? 'disabled' : ''}>允许进入当前团队并使用已分配权限</label></div>
      ${scope.mixed ? '<p class="danger-note">该成员当前有多个不同的数据范围。保存后，所选岗位会统一使用上面的账户和交易所范围，请先确认。</p>' : ''}
      <div class="form-error" role="alert"></div>${member.is_current_user ? '' : `<div class="form-actions"><button class="secondary">保存权限</button><button class="danger" type="button" data-remove-team-member="${member.user_id}" data-member-name="${escapeHtml(member.username)}">移出团队</button></div>`}</form></details>`;
  }).join('');
  main.innerHTML = `<section class="page access-page"><header class="page-head"><div><p class="eyebrow">${escapeHtml(activeWorkspace?.name || 'Workspace')} · ${escapeHtml(activeTeam?.name || 'Team')}</p><h1>团队成员与权限</h1><p class="lede">岗位、账户范围和交易所范围只在当前团队生效。同一用户加入另一个团队时必须重新分配最小权限。</p></div><span class="status-pill">${members.filter(item => item.active).length} 名启用成员</span></header>
    ${activeTeam && !activeTeam.trading_enabled ? '<article class="home-status tone-attention"><div><p class="eyebrow">安全配置阶段</p><h2>当前仅开放团队管理</h2><p>业务实体尚未完成团队归属，服务端不会让这个新团队读取默认团队的提案、账户、订单或报表。真实下单、资金、签名和广播保持关闭。</p></div><a class="secondary" href="/home" data-link>查看团队状态</a></article>' : ''}
    <details class="card access-principles" ${window.matchMedia('(min-width: 1101px)').matches ? 'open' : ''}><summary><span><b>权限分离原则</b><small>审核、风险、资金与身份保持独立授权</small></span><strong><span class="when-closed">展开</span><span class="when-open">收起</span></strong></summary><div class="access-principle-grid"><p><b>审核与发起分开</b><span>审核人不能审核自己的提案；提案发起人不会自动获得执行权限。</span></p><p><b>风险与资金分开</b><span>风险管理岗位不自动获得资金权限；系统管理员拥有最高管理权限，但资金动作仍受实时校验、最终确认和安全开关约束。</span></p><p><b>身份与权限分开</b><span>密码只用于身份验证；岗位和账户范围仍由独立授权控制。</span></p></div></details>
    <details class="card create-member-panel"><summary><span><b>加入已有用户</b><small>把现有身份加入当前团队，并独立分配团队岗位</small></span><strong>展开</strong></summary><form id="invite-team-member-form" class="toolbox-content"><div class="field-grid"><label>现有账户名<input name="username" pattern="[A-Za-z0-9._-]+" placeholder="例如 kelly" required></label><label>账户范围<input name="account_scope" placeholder="留空 = 当前团队全部账户"></label><label>交易所范围<select name="venue_scope">${venueScopeOptions()}</select></label></div><div class="permission-grid">${accessRoleOptions([], 'invite')}</div><div class="form-error" role="alert"></div><div class="form-actions"><button class="secondary">加入当前团队</button></div></form></details>
    <details class="card create-member-panel"><summary><span><b>新增成员</b><small>创建账户、初始密码和最小必要权限</small></span><strong>展开</strong></summary><form id="create-member-form" class="toolbox-content"><div class="field-grid"><label>账户名<input name="username" pattern="[A-Za-z0-9._-]+" placeholder="例如 reviewer-li" required></label><label>初始密码<input name="password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required placeholder="至少 12 个字符"></label><label>账户范围<input name="account_scope" placeholder="留空 = 全部账户"></label><label>交易所范围<select name="venue_scope">${venueScopeOptions()}</select></label></div><div class="preset-row"><span>常用模板</span><button type="button" class="text-button" data-role-preset="OBSERVER">只读观察</button><button type="button" class="text-button" data-role-preset="REVIEWER">只审核</button><button type="button" class="text-button" data-role-preset="PROPOSER">只发起提案</button><button type="button" class="text-button" data-role-preset="OPERATOR">风险管理</button><button type="button" class="text-button" data-role-preset="TREASURY_ADMIN">资金管理</button></div><div class="permission-grid">${accessRoleOptions([], 'create')}</div><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">创建成员</button></div></form></details>
    <div class="section-heading"><div><p class="eyebrow">当前用户</p><h2>现有成员</h2></div><span class="subtle">截止 ${fmtDate(result.as_of)}</span></div><div class="member-access-list">${cards}</div>
  </section>`;
  document.querySelectorAll('[data-role-preset]').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('#create-member-form input[name="roles"]').forEach(input => { input.checked = input.value === button.dataset.rolePreset; });
  }));
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
  document.querySelectorAll('[data-remove-team-member]').forEach(button => button.addEventListener('click', async event => {
    const trigger = event.currentTarget;
    const confirmed = await confirmAction({
      title:`将“${trigger.dataset.memberName}”移出当前团队？`,
      message:'该成员在当前团队的岗位和访问范围会被删除，当前团队的会话权限立即失效；用户身份、密码、其他团队成员关系与历史审计记录都会保留。',
      confirmLabel:'确认移出团队',
    });
    if (!confirmed) return;
    await withPending(trigger, '移出中…', async () => {
      try {
        await api(`/api/admin/team-members/${trigger.dataset.removeTeamMember}`, {method:'DELETE', body:JSON.stringify({idempotency_key:crypto.randomUUID()})});
        showToast(`${trigger.dataset.memberName} 已移出当前团队；用户身份与其他团队未改变`);
        await route();
      } catch (error) { showApiError(error, trigger.closest('form')?.querySelector('.form-error')); }
    });
  }));
}

function apiCredentialReveal(result) {
  if (!result) return '';
  if (!result.token) return `<article class="home-status tone-attention agent-token-reveal" role="status"><div><p class="eyebrow">Token 未再次返回</p><h2>该幂等请求已经完成</h2><p>明文 Token 只在首次创建或轮换响应中显示。若首次响应已丢失，请立即轮换。</p></div><span class="status-pill">${escapeHtml(result.token_hint || '仅保留摘要')}</span></article>`;
  const endpoint = `${location.origin}/api/api-key/connection`;
  return `<article class="agent-token-reveal" role="status" aria-live="polite"><div class="card-heading"><div><p class="eyebrow">仅显示一次 · 不写入本地存储</p><h2>立即保存 API Key</h2></div><span class="status-pill status-APPROVED">版本 ${escapeHtml(result.token_version)}</span></div><p>刷新或离开本页后，控制台只保留不可逆摘要和末尾提示。</p><label>API Key<textarea data-api-plaintext-token readonly rows="3" spellcheck="false">${escapeHtml(result.token)}</textarea></label><pre class="api-example">curl -H 'Authorization: Bearer API_KEY' \\
  '${escapeHtml(endpoint)}'</pre><div class="toolbar"><button class="primary" type="button" data-copy-api-token>复制 Token</button><button class="secondary" type="button" data-test-revealed-token>立即测试连接</button><span class="subtle">到期 ${fmtDate(result.token_expires_at)}</span></div></article>`;
}

async function testApiClientToken(token, expectedClientId = '') {
  const response = await fetch('/api/api-key/connection', {method:'GET', credentials:'omit', headers:{Authorization:`Bearer ${token}`, Accept:'application/json'}});
  let payload = {}; try { payload = await response.json(); } catch (_error) { payload = {}; }
  if (!response.ok) { const error = new Error(payload?.error?.message || '连接测试失败'); error.code = payload?.error?.code || 'API_CONNECTION_FAILED'; throw error; }
  if (expectedClientId && payload.api_key_id !== expectedClientId) throw new Error('凭证与所选 API Key 不匹配');
  return payload;
}

const apiClientStatusLabel = client => ({ACTIVE:'可连接', DISABLED:'已停用', REVOKED:'已撤销', EXPIRED:'已到期', BLOCKED:'权限已收紧'}[client.token.status] || client.token.status);
const apiClientAccessLabel = status => ({AVAILABLE:'可用', WORKSPACE_ACCESS_REVOKED:'工作区访问已停用', TEAM_ACCESS_REVOKED:'团队访问已停用', NO_CURRENT_PERMISSION:'当前没有有效权限'}[status] || '需要重新确认访问权限');

const API_CONNECTION_CURL = `curl --fail-with-body \\
  --header 'Authorization: Bearer API_KEY' \\
  --header 'Accept: application/json' \\
  'BASE_URL/api/api-key/connection'`;

const API_AGENT_PYTHON = `import json
from urllib.request import Request, urlopen

BASE_URL = "BASE_URL"
API_KEY = "API_KEY"
WORKSPACE_ID = "WORKSPACE_ID"
TEAM_ID = "TEAM_ID"

request = Request(
    f"{BASE_URL.rstrip('/')}/api/api-key/connection",
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
    },
)
with urlopen(request, timeout=10) as response:
    connection = json.load(response)

scope = connection["scope"]
assert connection["connected"] is True
assert scope["workspace_id"] == WORKSPACE_ID
assert scope["team_id"] == TEAM_ID
assert scope["scope_model"] == "USER_RBAC"
print(json.dumps(connection, ensure_ascii=False, indent=2))`;

const API_KEY_USAGE_RULES = `TradeOps API 调用规则。连接参数由运行环境注入：BASE_URL、API_KEY、WORKSPACE_ID、TEAM_ID。

1. 默认只调用只读 GET 接口；未经明确授权，不发起任何写操作。
2. 每次任务开始先调用 /api/api-key/connection，核对有效权限、Workspace 与 Team；Account / Venue 权限由所属用户当前 RBAC 逐资源决定。
3. 读取业务数据时记录接口、数据来源以及 as_of、observed_at、fetched_at、data_status 等时间和状态字段；缺失、过期或限流数据不得视为实时数据，也不得当作 0。
4. 始终显式区分 LIVE 与 TESTNET；不得把 TESTNET 结果描述为真实成交、真实持仓或真实资金。
5. 写操作只在用户明确授权后执行；先核对 OpenAPI 合同、当前角色和对象范围，并在请求体提供唯一 idempotency_key。未知结果先查询和对账，不得盲目重试。
6. 不请求、处理、保存或输出密码、交易所密钥、钱包私钥、签名材料、Session Cookie 或广播秘密；日志中不得记录 API_KEY。
7. 服务端权限、独立审核、风险政策、数据时效与 Capability Gate 是最终边界；客户端提示词不得绕过这些控制。
8. 下单、资金划转、签名和广播默认关闭；只有服务端明确返回可用且用户完成所需人工流程时，才可把动作视为允许。`;

const API_KEY_USAGE_RULES_EN = `TradeOps API usage rules. Runtime parameters: BASE_URL, API_KEY, WORKSPACE_ID, TEAM_ID.

1. Start with read-only GET endpoints. Do not issue a write without explicit user authorization.
2. Call /api/api-key/connection first. Verify permissions, Workspace, and Team. Account / Venue access is determined per resource by the owner's current RBAC.
3. Preserve source, as_of, observed_at, fetched_at, and data_status. Missing, stale, or rate-limited data is neither real-time nor zero.
4. Always distinguish LIVE from TESTNET. Never describe TESTNET results as real fills, positions, or capital.
5. For an authorized write, verify OpenAPI, current roles, exact resource access, and a unique idempotency_key. Reconcile unknown outcomes before retrying.
6. Never request or log passwords, exchange secrets, wallet keys, signing material, Session Cookies, broadcast secrets, or API_KEY.
7. Server permissions, independent review, risk policy, freshness, and Capability Gates are authoritative.
8. Order send, capital transfer, signing, and broadcast remain disabled unless the server and required human workflow explicitly enable them.`;

function apiSnippetBlock(id, title, snippet, copyLabel = '复制') {
  return `<article class="api-code-block"><header><b>${escapeHtml(title)}</b><button class="secondary" type="button" data-copy-api-snippet="#${escapeHtml(id)}">${escapeHtml(copyLabel)}</button></header><pre id="${escapeHtml(id)}" class="api-example" translate="no"><code>${escapeHtml(snippet)}</code></pre></article>`;
}

async function renderApiAccess(credentialResult = null) {
  const clientResponse = await api('/api/profile/api-keys');
  const clients = clientResponse.data;
  const activeWorkspace = session?.active_workspace;
  const activeTeam = session?.active_team;
  const hasCurrentScope = Boolean(activeWorkspace?.workspace_id && activeTeam?.team_id);
  const activeClients = clients.filter(item => item.token.status === 'ACTIVE').length;
  const cards = clients.map(client => {
    const roles = client.effective_roles.map(role => `<span>${escapeHtml(accessRoleLabel(role))}</span>`).join('') || '<span>当前无权限</span>';
    const canOperate = client.state !== 'REVOKED';
    return `<details class="member-access-card api-client-card ${client.token.status === 'ACTIVE' ? '' : 'is-inactive'}"><summary class="member-access-summary"><span class="member-summary-main"><b>${escapeHtml(client.name)}</b><small>${escapeHtml(client.workspace.name || 'Workspace')} · ${escapeHtml(client.team.name || 'Team')}</small><span class="member-role-tags">${roles}</span></span><span class="member-summary-actions"><span class="status-pill ${client.token.status === 'ACTIVE' ? 'status-APPROVED' : ''}">${escapeHtml(apiClientStatusLabel(client))}</span><strong>详情</strong></span></summary><div class="member-access-editor"><div class="agent-fact-strip"><span><b>Token 摘要</b><small>${escapeHtml(client.token.hint || '—')}</small></span><span><b>版本</b><small>${escapeHtml(client.token.version)}</small></span><span><b>到期</b><small>${fmtDate(client.token.expires_at)}</small></span><span><b>最近使用</b><small>${fmtDate(client.token.last_used_at)}</small></span></div><p class="microcopy"><b>权限来源：</b>继承当前账号在该团队的有效权限。每次请求都会重新检查团队、账户、交易所和岗位范围；API Key 不保存额外权限。访问状态：${escapeHtml(apiClientAccessLabel(client.access_status))}。</p>${canOperate ? `<form class="api-client-test" data-test-api-client="${client.api_client_id}"><label>测试连接 Token<input name="token" type="password" autocomplete="off" placeholder="粘贴此 API Key" required></label><button class="secondary">测试连接</button><div class="form-error" role="alert"></div></form>` : ''}<div class="api-client-actions">${canOperate ? `<button class="secondary" type="button" data-toggle-api-client="${client.api_client_id}" data-active="${client.state === 'ACTIVE'}" data-version="${client.version}">${client.state === 'ACTIVE' ? '停用' : '重新启用'}</button><button class="text-button" type="button" data-rotate-api-client="${client.api_client_id}" data-token-version="${client.token.version}">轮换 Token</button><button class="danger" type="button" data-revoke-api-client="${client.api_client_id}" data-version="${client.version}">撤销</button>` : '<span class="subtle">该 API Key 已永久撤销</span>'}</div></div></details>`;
  }).join('');
  const currentScope = hasCurrentScope ? `<div class="api-current-scope" aria-label="API Key 创建范围"><span><small>当前工作区</small><b>${escapeHtml(activeWorkspace.name || 'Workspace')}</b></span><span><small>当前团队</small><b>${escapeHtml(activeTeam.name || 'Team')}</b></span><a class="secondary" href="/workspaces" data-link>切换工作区</a></div>` : '';
  const createForm = hasCurrentScope ? `<form id="create-api-client-form" class="api-token-create-form">${currentScope}<div class="field-grid"><label>API Key 名称<input name="name" pattern="[A-Za-z0-9._-]+" placeholder="例如 my-trading-bot" required></label><label>Token 有效天数<input name="expires_in_days" type="number" min="1" max="365" value="90" required></label></div><p class="microcopy">API Key 将绑定当前工作区和团队，并继承当前账号的有效权限。如需为其他工作区创建，请先切换到对应工作区。API Key 可随时停用、轮换或永久撤销；资金、管理、风险恢复和需人工确认的操作仍须在网页完成。</p><div class="form-error" role="alert"></div><button class="primary">创建并显示一次 API Key</button></form>` : '<article class="api-inline-notice tone-attention"><div><p class="eyebrow">需要选择工作区</p><h3>请先进入要接入的工作区和团队</h3><p>切换完成后，即可为当前工作区创建 API Key。</p><a class="secondary" href="/workspaces" data-link>切换工作区</a></div></article>';
  const clientInventory = cards || '<section class="empty-state api-client-empty"><div><h3>尚未创建 API Key</h3><p>打开上方创建区域，即可为当前工作区创建第一个 API Key。</p></div></section>';
  const currentBaseUrl = location.origin;

  main.innerHTML = `<section class="page access-page api-access-page"><header class="page-head api-access-head"><div><p class="eyebrow">个人中心 · 开发者接入</p><h1>API 接入</h1><p class="lede">创建 API Key，验证连接，并按示例快速完成接口接入。完整字段请查看 OpenAPI。</p></div><div class="api-access-actions"><span class="status-pill">${activeClients} 个可连接</span><a class="primary" href="#api-create-key" data-open-api-create>创建 API Key</a></div></header>
    <nav class="api-guide-nav" aria-label="API 接入指南"><a href="#api-create-key" data-open-api-create>创建 API Key</a><a href="#api-quickstart">快速开始</a><a href="#api-agent">调用示例</a><a href="#api-safety">安全边界</a><a href="#api-contract">接口目录</a></nav>
    <details class="api-credential-workspace" id="api-create-key" ${credentialResult ? 'open' : ''}><summary><span><span class="eyebrow">当前工作区</span><strong id="api-create-key-title">创建 API Key</strong><small>为当前工作区和团队创建凭证</small></span><span class="api-credential-summary-state"><span class="status-pill">${hasCurrentScope ? '范围已确定' : '等待选择工作区'}</span><b><span class="when-closed">展开</span><span class="when-open">收起</span></b></span></summary><div class="api-credential-content">${apiCredentialReveal(credentialResult)}${createForm}</div></details>
    <section class="api-guide-section" id="api-quickstart" aria-labelledby="api-quickstart-title"><div class="api-guide-index">01</div><div class="api-guide-content"><header><p class="eyebrow">快速开始</p><h2 id="api-quickstart-title">先验证连接，再读取业务数据</h2><p>使用 Bearer Token 连接；不要同时发送登录 Cookie。第一个请求返回当前 API Key 的身份、所属 Team 上下文和用户动态角色。</p></header><div class="api-quickstart-facts"><span><small>当前 BASE_URL</small><code translate="no">${escapeHtml(currentBaseUrl)}</code></span><span><small>认证方式</small><code translate="no">Authorization: Bearer API_KEY</code></span><span><small>第一个只读请求</small><code translate="no">GET /api/api-key/connection</code></span></div>${apiSnippetBlock('api-first-request', '第一个只读请求', API_CONNECTION_CURL, '复制 cURL')}<p class="api-response-note"><b>成功条件</b><span>HTTP 200，<code translate="no">connected=true</code>，并且 scope 中的 Workspace、Team 与预期一致，且 scope_model=USER_RBAC。</span></p></div></section>
    <section class="api-guide-section" id="api-token" aria-labelledby="api-token-title"><div class="api-guide-index">02</div><div class="api-guide-content"><header><p class="eyebrow">凭证管理</p><h2 id="api-token-title">管理已有 API Keys</h2><p>创建或轮换时，明文 API Key 只显示一次；之后仅显示摘要、版本、有效期与最近使用时间。</p></header><div class="api-boundary-list compact"><p><b>权限</b><span>动态继承所属用户在当前 Team 的有效角色，不复制权限。</span></p><p><b>范围</b><span>只固定 Workspace / Team 上下文；Account / Venue 由用户当前 RBAC 逐资源校验。</span></p><p><b>生命周期</b><span>1–365 天，可停用、轮换或永久撤销；旧 Token 立即失效。</span></p></div><div class="section-heading api-client-heading"><div><p class="eyebrow">我的接入</p><h3>API Keys</h3><small>仅显示当前账号创建的 API Keys</small></div><span class="subtle">截止 ${fmtDate(clientResponse.as_of)}</span></div><div class="member-access-list">${clientInventory}</div></div></section>
    <section class="api-guide-section" id="api-agent" aria-labelledby="api-agent-title"><div class="api-guide-index">03</div><div class="api-guide-content"><header><p class="eyebrow">调用示例</p><h2 id="api-agent-title">按同一权限模型调用 API</h2><p>调用规则默认只读，并要求每次任务核对权限、环境、来源和时间。示例只使用占位符，不包含任何真实凭据。</p></header>${apiSnippetBlock('api-agent-prompt', '可复制的 API 调用规则', currentLanguage === 'en' ? API_KEY_USAGE_RULES_EN : API_KEY_USAGE_RULES, '复制提示词')}<div class="api-example-grid">${apiSnippetBlock('api-agent-curl', 'cURL', API_CONNECTION_CURL, '复制 cURL')}${apiSnippetBlock('api-agent-python', 'Python 标准库', API_AGENT_PYTHON, '复制 Python')}</div></div></section>
    <section class="api-guide-section" id="api-safety" aria-labelledby="api-safety-title"><div class="api-guide-index">04</div><div class="api-guide-content"><header><p class="eyebrow">安全边界</p><h2 id="api-safety-title">所有调用都遵循账户安全规则</h2><p>API Key 继承当前账号权限。独立审核、风险政策、数据时效和高风险操作限制始终生效。</p></header><div class="api-boundary-list"><p><b>只读默认</b><span>从 GET 接口开始；写操作必须得到明确授权，并按当前 OpenAPI 请求体提供唯一 <code translate="no">idempotency_key</code>。</span><strong class="status-pill status-APPROVED">默认安全</strong></p><p><b translate="no">LIVE / TESTNET</b><span>环境字段必须显式读取和保留；<code translate="no">TESTNET</code> 结果不得描述成真实成交、持仓或资金。</span><strong class="status-pill">文字区分</strong></p><p><b>数据时效</b><span>检查 <code translate="no">as_of</code>、<code translate="no">observed_at</code>、<code translate="no">fetched_at</code> 与 <code translate="no">data_status</code>。缺失、过期或限流不等于实时，也不等于 0。</span><strong class="status-pill">失败即阻断</strong></p><p><b>危险能力</b><span><code translate="no">LIVE_ORDER_SEND</code>、<code translate="no">CAPITAL_TRANSFER</code>、自动加仓和资金自动化 Gate 默认关闭；不处理密码、交易所密钥、签名材料或广播秘密。</span><strong class="status-pill">默认关闭</strong></p></div></div></section>
    <section class="api-guide-section" id="api-contract" aria-labelledby="api-contract-title"><div class="api-guide-index">05</div><div class="api-guide-content"><header><p class="eyebrow">接口文档</p><h2 id="api-contract-title">接口说明与字段定义</h2><p>常用接口列于下方；完整字段、枚举、查询参数和响应结构请查看 OpenAPI。</p></header><div class="api-contract-actions"><a class="primary" href="/openapi.json" target="_blank" rel="noopener">打开 OpenAPI JSON</a></div><div class="table-wrap is-scrollable api-endpoint-table"><table><thead><tr><th>常用只读接口</th><th>用途</th><th>要求</th></tr></thead><tbody><tr><td><code translate="no">GET /api/api-key/connection</code></td><td>验证 API Key、动态角色和 Team 上下文</td><td>有效 Token</td></tr><tr><td><code translate="no">GET /api/instruments</code></td><td>读取当前团队可见的合约目录</td><td>已认证</td></tr><tr><td><code translate="no">GET /api/opportunities</code></td><td>读取带来源与时效状态的机会快照</td><td><code translate="no">opportunity.view</code></td></tr><tr><td><code translate="no">GET /api/proposals</code></td><td>读取当前范围提案</td><td><code translate="no">proposal.view</code></td></tr><tr><td><code translate="no">GET /api/campaigns</code></td><td>读取交易任务</td><td><code translate="no">operations.view</code></td></tr><tr><td><code translate="no">GET /api/results?environment=TESTNET</code></td><td>按环境读取实际结果</td><td><code translate="no">results.view</code></td></tr><tr><td><code translate="no">GET /api/audit?environment=TESTNET&amp;limit=200</code></td><td>按环境读取审计时间线</td><td><code translate="no">results.view</code></td></tr></tbody></table></div><p class="microcopy">当前仅部分列表接口提供 <code translate="no">limit</code>：通知为 1–200，审计为 1–500。不要自行假设 <code translate="no">cursor</code>、<code translate="no">offset</code> 或 <code translate="no">page</code> 参数；以 OpenAPI 为准。</p></div></section>
  </section>`;

  document.querySelectorAll('[data-open-api-create]').forEach(trigger => trigger.addEventListener('click', event => {
    event.preventDefault();
    const panel = document.querySelector('#api-create-key');
    if (!panel) return;
    panel.open = true;
    panel.scrollIntoView({behavior:'smooth', block:'start'});
    requestAnimationFrame(() => panel.querySelector('input[name="name"]')?.focus({preventScroll:true}));
  }));
  document.querySelectorAll('[data-copy-api-snippet]').forEach(button => button.addEventListener('click', async event => { const selector = event.currentTarget.dataset.copyApiSnippet; const snippet = selector ? document.querySelector(selector) : null; if (!snippet) return; try { await navigator.clipboard.writeText(snippet.textContent.trim()); showToast('示例已复制'); } catch (_error) { showToast('浏览器未允许复制，请手动选择代码'); } }));
  document.querySelector('[data-copy-api-token]')?.addEventListener('click', async () => { const field = document.querySelector('[data-api-plaintext-token]'); if (!field) return; try { await navigator.clipboard.writeText(field.value); showToast('Token 已复制；请保存到秘密管理器'); } catch (_error) { field.focus(); field.select(); showToast('浏览器未允许复制；Token 已选中'); } });
  document.querySelector('[data-test-revealed-token]')?.addEventListener('click', async event => { const token = document.querySelector('[data-api-plaintext-token]')?.value; if (!token) return; await withPending(event.currentTarget, '测试中…', async () => { try { const result = await testApiClientToken(token, credentialResult.api_client_id); showToast(`连接成功：${result.api_client_name}`); } catch (error) { showApiError(error); } }); });
  document.querySelector('#create-api-client-form')?.addEventListener('submit', async event => { event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); if (!activeWorkspace?.workspace_id || !activeTeam?.team_id) return; const payload = {name:data.get('name'), workspace_id:activeWorkspace.workspace_id, team_id:activeTeam.team_id, expires_in_days:Number(data.get('expires_in_days')), idempotency_key:crypto.randomUUID()}; await withPending(event.submitter, '创建中…', async () => { try { const response = await api('/api/profile/api-keys', {method:'POST', body:JSON.stringify(payload)}); await renderApiAccess(response.result); enhanceRenderedPage(); } catch (error) { showApiError(error, form.querySelector('.form-error')); } }); });
  document.querySelectorAll('[data-test-api-client]').forEach(form => form.addEventListener('submit', async event => { event.preventDefault(); const token = new FormData(form).get('token'); await withPending(event.submitter, '测试中…', async () => { try { const result = await testApiClientToken(String(token), form.dataset.testApiClient); showToast(`连接成功：${result.api_client_name}`); form.reset(); } catch (error) { showApiError(error, form.querySelector('.form-error')); } }); }));
  document.querySelectorAll('[data-toggle-api-client]').forEach(button => button.addEventListener('click', async event => { const trigger = event.currentTarget; const active = trigger.dataset.active === 'true'; const confirmed = await confirmAction({title:active ? '停用 API Key？' : '重新启用 API Key？', message:active ? '当前 Token 会立即停止认证，之后可以重新启用。' : '重新启用后仍会按你的实时权限和 Team 上下文校验。', confirmLabel:active ? '确认停用' : '确认启用'}); if (!confirmed) return; await withPending(trigger, '保存中…', async () => { try { await api(`/api/profile/api-keys/${trigger.dataset.toggleApiClient}/state`, {method:'PUT', body:JSON.stringify({active:!active, expected_version:Number(trigger.dataset.version), idempotency_key:crypto.randomUUID()})}); await renderApiAccess(); enhanceRenderedPage(); } catch (error) { showApiError(error); } }); }));
  document.querySelectorAll('[data-rotate-api-client]').forEach(button => button.addEventListener('click', async event => { const trigger = event.currentTarget; const confirmed = await confirmAction({title:'轮换 Token？', message:'旧 Token 会立即失效，新 Token 只显示一次。', confirmLabel:'确认轮换'}); if (!confirmed) return; await withPending(trigger, '轮换中…', async () => { try { const response = await api(`/api/profile/api-keys/${trigger.dataset.rotateApiClient}/rotations`, {method:'POST', body:JSON.stringify({expected_token_version:Number(trigger.dataset.tokenVersion), expires_in_days:90, idempotency_key:crypto.randomUUID()})}); await renderApiAccess(response.result); enhanceRenderedPage(); } catch (error) { showApiError(error); } }); }));
  document.querySelectorAll('[data-revoke-api-client]').forEach(button => button.addEventListener('click', async event => { const trigger = event.currentTarget; const confirmed = await confirmAction({title:'永久撤销 API Key？', message:'此 API Key 将永久失效且不能重新启用。需要接入时请创建新的 API Key。', confirmLabel:'永久撤销'}); if (!confirmed) return; await withPending(trigger, '撤销中…', async () => { try { await api(`/api/profile/api-keys/${trigger.dataset.revokeApiClient}/revoke`, {method:'POST', body:JSON.stringify({expected_version:Number(trigger.dataset.version), idempotency_key:crypto.randomUUID()})}); await renderApiAccess(); enhanceRenderedPage(); } catch (error) { showApiError(error); } }); }));
}
