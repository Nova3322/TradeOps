async function bootstrap() {
  authStatus = await api('/api/auth/status');
  updateEnvironmentIndicators();
  try {
    const result = await api('/api/auth/session');
    session = result.session;
    sessionAuthenticationMethod = result.authentication_method || '';
  } catch (error) {
    if (error.status !== 401) console.error(error);
  }
  setShell(Boolean(session), {workspaceGate:Boolean(session) && ['/', '/workspaces'].includes(location.pathname)});
  await route();
}

let renderedRouteKey = '';

function routeKey() {
  return `${location.pathname}${location.search}`;
}

function restoreRouteViewport(position) {
  requestAnimationFrame(() => window.scrollTo(position.left, position.top));
}

function notFoundView(path) {
  const context = path.startsWith('/campaigns/')
    ? {eyebrow:'交易历史', title:'该交易记录不存在', copy:'链接可能已失效，或记录不属于当前团队与环境。', href:'/campaigns', action:'返回交易历史'}
    : path.startsWith('/proposals/')
      ? {eyebrow:'审核队列', title:'该提案不存在', copy:'链接可能已失效，或记录不属于当前团队与环境。', href:'/reviews?view=current', action:'返回审核队列'}
      : path.startsWith('/venues/')
        ? {eyebrow:'账户管理', title:'账户不存在或不在当前空间', copy:'请返回账户列表，选择当前身份可见的账户。', href:'/accounts', action:'返回账户列表'}
        : {eyebrow:'导航', title:'页面不存在', copy:'链接可能已失效，请从当前任务重新进入。', href:'/home', action:'返回当前任务'};
  return `<section class="access-boundary-state missing-resource-state" role="status" aria-labelledby="missing-resource-title"><div>
    <div class="access-boundary-heading"><div><p class="eyebrow">${context.eyebrow}</p><h2 id="missing-resource-title" data-error-heading tabindex="-1">${context.title}</h2></div><span class="status-pill">记录不可用</span></div>
    <p class="access-boundary-copy">${context.copy}</p>
    <dl class="access-boundary-facts"><div><dt>安全边界</dt><dd>仅保留当前团队与环境范围</dd></div><div><dt>处理结果</dt><dd>未执行任何写入操作</dd></div></dl>
    <div class="access-boundary-actions"><a class="primary" href="${context.href}" data-link>${context.action}</a>${context.href === '/home' ? '' : '<a class="secondary" href="/home" data-link>返回当前任务</a>'}</div>
  </div></section>`;
}

function accessDeniedView(requiredCapability) {
  const roles = roleNames().map(role => localizedText(fmtRole(role)));
  const roleSummary = roles.join(' / ') || localizedText('未分配角色');
  return `<section class="access-boundary-state" role="status" aria-labelledby="access-boundary-title"><div>
    <div class="access-boundary-heading"><div><p class="eyebrow">权限范围</p><h2 id="access-boundary-title" tabindex="-1">当前职责不包含这个页面</h2></div><span class="status-pill">已按岗位限制</span></div>
    <p class="access-boundary-copy">此页面需要“${escapeHtml(capabilityLabel(requiredCapability))}”权限。侧栏只展示当前身份可用入口；直接打开链接也不会绕过服务端权限。</p>
    <dl class="access-boundary-facts"><div><dt>当前身份</dt><dd>${escapeHtml(roleSummary)}</dd></div><div><dt>所需权限</dt><dd>${escapeHtml(localizedText(capabilityLabel(requiredCapability)))}</dd></div><div><dt>权限来源</dt><dd>当前团队岗位与资源范围</dd></div><div><dt>数据处理</dt><dd>未读取受限页面数据</dd></div></dl>
    <div class="access-boundary-actions"><a class="secondary" href="/home" data-link>返回当前任务</a>${hasCapability('opportunity.view') ? '<a class="primary" href="/opportunities" data-link>查看机会</a>' : ''}${hasCapability('capital.view') ? '<a class="primary" href="/capital" data-link>进入资金中心</a>' : ''}</div>
  </div></section>`;
}

async function route(options = {}) {
  const requestedRouteKey = routeKey();
  const navigationRequested = focusNextRouteHeading || renderedRouteKey !== requestedRouteKey;
  const preserveView = options?.preserveView ?? !navigationRequested;
  const backgroundRefresh = Boolean(options?.backgroundRefresh);
  const viewport = {left:window.scrollX, top:window.scrollY};
  const finishRender = () => {
    renderedRouteKey = routeKey();
    enhanceRenderedPage();
    if (preserveView) restoreRouteViewport(viewport);
  };
  if (location.pathname !== '/opportunities') stopOpportunityStream();
  capitalChartOverlayAbortController?.abort();
  capitalChartOverlayAbortController = null;
  document.body.classList.remove('capital-chart-expanded');
  if (!preserveView) window.scrollTo(0, 0);
  updateActiveNav();
  closeMobileNav({restoreFocus:false});
  main.setAttribute('aria-busy', 'true');
  if (!session) {
    setShell(false);
    renderLogin();
    finishRender();
    return;
  }
  const path = location.pathname;
  if (path === '/' || path === '/workspaces') {
    setShell(true, {workspaceGate:true});
    renderWorkspaceGateway();
    finishRender();
    return;
  }
  setShell(true);
  if (!session.active_workspace || !session.active_team) {
    renderScopeSetup();
    finishRender();
    return;
  }
  const requiredCapability = routeCapability(path);
  if (requiredCapability && !hasCapability(requiredCapability)) {
    main.innerHTML = accessDeniedView(requiredCapability);
    finishRender();
    return;
  }
  if (!preserveView) {
    main.innerHTML = `<section class="loading-state" role="status" aria-live="polite" aria-label="正在读取当前事实">
      <div class="loading-state-copy"><span class="spinner" aria-hidden="true"></span><div><b>正在读取当前事实…</b><p>正在核对当前空间、权限与服务端数据。</p></div></div>
      <div class="loading-skeleton" aria-hidden="true"><span class="loading-title"></span><span class="loading-lede"></span><div class="loading-stat-row"><span></span><span></span><span></span><span></span></div><span class="loading-panel"></span></div>
    </section>`;
    applyLanguageToDocument(main);
  }
  try {
    if (path === '/home') await renderHome();
    else if (path === '/signals') await renderSignalSources();
    else if (path === '/webhook-signals') await renderWebhookSignals();
    else if (path === '/opportunities') await renderOpportunities();
    else if (path === '/opportunities/defaults') await renderOpportunityDefaults();
    else if (path === '/proposals/new') await renderManualProposal();
    else if (path === '/reviews') {
      const requestedView = new URLSearchParams(location.search).get('view');
      const defaultView = hasCapability('proposal.review') ? 'review' : 'current';
      const view = ['review','current','history'].includes(requestedView) ? requestedView : defaultView;
      if (view === 'review' && !hasCapability('proposal.review')) {
        history.replaceState({}, '', '/reviews?view=current');
        await renderProposalList(null, '当前提案');
      } else if (view === 'history') await renderProposalList(null, '历史记录', true);
      else if (view === 'current') await renderProposalList(null, '当前提案');
      else await renderProposalList('PENDING_REVIEW', '审核队列');
    }
    else if (path === '/proposals') {
      const historyMode = new URLSearchParams(location.search).get('history') === '1';
      history.replaceState({}, '', historyMode ? '/reviews?view=history' : '/reviews?view=current');
      await renderProposalList(null, historyMode ? '历史记录' : '当前提案', historyMode);
    }
    else if (path === '/campaigns') await renderCampaignList();
    else if (path === '/accounts') await renderAccountManagement();
    else if (path === '/team-settings' || path === '/trading-mode') { history.replaceState({}, '', '/home'); await renderHome(); }
    else if (path === '/results') await renderActualResults();
    else if (path === '/notifications') await renderNotifications();
    else if (path === '/campaigns/alerts') await renderRuntimeAlerts();
    else if (path === '/positions') await renderCurrentPositions();
    else if (path === '/system') await renderSystemStatus();
    else if (path === '/orders') await renderCampaignFacts('orders');
    else if (path === '/risk') await renderCampaignFacts('risk');
    else if (path === '/capital') await renderCapitalCenter();
    else if (path === '/exceptions') { history.replaceState({}, '', '/campaigns/alerts'); await renderRuntimeAlerts(); }
    else if (path === '/venues') { history.replaceState({}, '', '/accounts'); await renderAccountManagement(); }
    else if (path === '/venues/binance' || path === '/venues/hyperliquid') {
      const legacyVenue = path.endsWith('/hyperliquid') ? 'HYPERLIQUID' : 'BINANCE';
      history.replaceState({}, '', `/venues?venue=${legacyVenue}`);
      await renderAccountManagement();
    }
    else if (path === '/admin/users') await renderAccessManagement();
    else if (path === '/profile/api-keys' || path === '/profile/api-access' || path === '/admin/agents') await renderApiAccess();
    else {
      const campaignMatch = path.match(/^\/campaigns\/([0-9a-f-]+)$/i);
      const proposalMatch = path.match(/^\/proposals\/([0-9a-f-]+)$/i);
      const venueAccountMatch = path.match(/^\/venues\/([^/]+)$/);
      if (venueAccountMatch) await renderVenueAccountDetail(venueAccountMatch[1]);
      else if (campaignMatch) await renderCampaignDetail(campaignMatch[1]);
      else if (proposalMatch) await renderProposalDetail(proposalMatch[1]);
      else main.innerHTML = notFoundView(path);
    }
    finishRender();
  } catch (error) {
    if (error.status === 401) {
      if (!error.handled) handleUnauthorizedResponse();
      return;
    }
    if (backgroundRefresh && preserveView) {
      main.removeAttribute('aria-busy');
      restoreRouteViewport(viewport);
      return;
    }
    main.innerHTML = error.status === 404 ? notFoundView(path) : errorView(error);
    finishRender();
    main.querySelector('[data-error-heading]')?.focus({preventScroll:true});
  }
}
