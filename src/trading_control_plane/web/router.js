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

function notFoundView(path) {
  if (path.startsWith('/campaigns/')) {
    return '<section class="empty-state"><div><p class="eyebrow">交易任务</p><h2>该交易任务不存在</h2><p>链接可能已失效，或记录不属于当前团队与环境。</p><a class="primary" href="/campaigns" data-link>返回交易任务</a></div></section>';
  }
  if (path.startsWith('/proposals/')) {
    return '<section class="empty-state"><div><p class="eyebrow">提案管理</p><h2>该提案不存在</h2><p>链接可能已失效，或记录不属于当前团队与环境。</p><a class="primary" href="/proposals" data-link>返回提案列表</a></div></section>';
  }
  return '<section class="empty-state"><div><p class="eyebrow">导航</p><h2>页面不存在</h2><p>链接可能已失效，请从当前任务重新进入。</p><a class="primary" href="/home" data-link>返回当前任务</a></div></section>';
}

async function route() {
  if (location.pathname !== '/opportunities') stopOpportunityStream();
  window.scrollTo(0, 0);
  updateActiveNav();
  closeMobileNav({restoreFocus:false});
  main.setAttribute('aria-busy', 'true');
  if (!session) {
    setShell(false);
    renderLogin();
    enhanceRenderedPage();
    return;
  }
  const path = location.pathname;
  if (path === '/' || path === '/workspaces') {
    setShell(true, {workspaceGate:true});
    renderWorkspaceGateway();
    enhanceRenderedPage();
    return;
  }
  setShell(true);
  if (!session.active_workspace || !session.active_team) {
    renderScopeSetup();
    enhanceRenderedPage();
    return;
  }
  const requiredCapability = routeCapability(path);
  if (requiredCapability && !hasCapability(requiredCapability)) {
    main.innerHTML = `<section class="empty-state"><div><p class="eyebrow">权限范围</p><h2>当前职责不包含这个页面</h2><p>此页面需要“${escapeHtml(capabilityLabel(requiredCapability))}”权限。侧栏只展示当前身份可用入口；直接打开链接也不会绕过服务端权限。</p><div class="toolbar empty-actions"><a class="secondary" href="/home" data-link>返回当前任务</a>${hasCapability('capital.view') ? '<a class="primary" href="/capital" data-link>进入资金中心</a>' : ''}</div></div></section>`;
    enhanceRenderedPage();
    return;
  }
  main.innerHTML = `<section class="loading-state" role="status" aria-live="polite" aria-label="正在读取当前事实">
    <div class="loading-state-copy"><span class="spinner" aria-hidden="true"></span><div><b>正在读取当前事实…</b><p>正在核对当前空间、权限与服务端数据。</p></div></div>
    <div class="loading-skeleton" aria-hidden="true"><span></span><span></span><span></span></div>
  </section>`;
  applyLanguageToDocument(main);
  try {
    if (path === '/home') await renderHome();
    else if (path === '/signals') await renderSignalSources();
    else if (path === '/webhook-signals') await renderWebhookSignals();
    else if (path === '/opportunities') await renderOpportunities();
    else if (path === '/opportunities/defaults') await renderOpportunityDefaults();
    else if (path === '/proposals/new') await renderManualProposal();
    else if (path === '/reviews') await renderProposalList('PENDING_REVIEW', '审核队列');
    else if (path === '/proposals') {
      const historyMode = new URLSearchParams(location.search).get('history') === '1';
      await renderProposalList(null, historyMode ? '历史提案' : '当前提案', historyMode);
    }
    else if (path === '/campaigns') await renderCampaignList();
    else if (path === '/accounts') await renderAccountManagement();
    else if (path === '/team-settings') await renderTeamSettings();
    else if (path === '/trading-mode') { history.replaceState({}, '', '/accounts'); await renderAccountManagement(); }
    else if (path === '/results') await renderActualResults();
    else if (path === '/notifications') await renderNotifications();
    else if (path === '/campaigns/alerts') await renderRuntimeAlerts();
    else if (path === '/positions') await renderSystemStatus();
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
    enhanceRenderedPage();
  } catch (error) {
    if (error.status === 401) {
      if (!error.handled) handleUnauthorizedResponse();
      return;
    }
    main.innerHTML = errorView(error);
    enhanceRenderedPage();
    main.querySelector('[data-error-heading]')?.focus();
  }
}
