function workspaceDefaultTeamId(workspace) {
  if (!workspace) return null;
  return workspace.default_team_id
    || (session?.teams || []).find(team => team.workspace_id === workspace.workspace_id && team.slug === 'default')?.team_id
    || (session?.teams || []).find(team => team.workspace_id === workspace.workspace_id)?.team_id
    || null;
}

function renderWorkspaceSwitcher() {
  if (!session) return;
  const activeWorkspaceId = session.active_workspace?.workspace_id;
  const activeWorkspace = (session.workspaces || []).find(item => item.workspace_id === activeWorkspaceId);
  const nameTarget = scopeSwitcher.querySelector('[data-workspace-name]');
  if (nameTarget) nameTarget.textContent = activeWorkspace?.name || '选择工作区';
  scopeSwitcherMenu.innerHTML = `<div class="workspace-switcher-menu-head"><span>工作区</span><small>${(session.workspaces || []).length} 个可用</small></div>
    <div class="workspace-switcher-options">${(session.workspaces || []).map(workspace => {
      const selected = workspace.workspace_id === activeWorkspaceId;
      const teamId = workspaceDefaultTeamId(workspace);
      return `<button class="workspace-switcher-option ${selected ? 'is-active' : ''}" type="button" role="menuitem" data-switch-workspace="${escapeHtml(workspace.workspace_id)}" data-switch-team="${escapeHtml(teamId || '')}"><span class="workspace-option-avatar" aria-hidden="true">${escapeHtml(workspace.name.slice(0, 1).toUpperCase())}</span><span><b>${escapeHtml(workspace.name)}</b><small>${escapeHtml(`${workspace.member_count ?? 0} 名成员`)}</small></span><strong>${selected ? '当前' : '进入'}</strong></button>`;
    }).join('')}</div>
    <div class="workspace-switcher-menu-actions"><a href="/" data-link role="menuitem">创建新工作区</a><a href="/" data-link role="menuitem">查看所有工作区</a></div>`;
}

function closeWorkspaceSwitcher({restoreFocus = false} = {}) {
  scopeSwitcherMenu.hidden = true;
  scopeSwitcher.setAttribute('aria-expanded', 'false');
  if (restoreFocus && !scopeSwitcher.hidden) scopeSwitcher.focus();
}

function setShell(loggedIn, {workspaceGate = false} = {}) {
  sidebar.hidden = !loggedIn || workspaceGate;
  desktopNavToggle.hidden = !loggedIn || workspaceGate;
  userMenu.hidden = !loggedIn;
  scopeControl.hidden = !loggedIn || workspaceGate;
  mobileNavToggle.hidden = !loggedIn || workspaceGate;
  if (loggedIn) {
    updateEnvironmentIndicators();
    renderWorkspaceSwitcher();
    const rolePriority = ['SYSTEM_ADMIN','TREASURY_ADMIN','OPERATOR','REVIEWER','PROPOSER','OBSERVER'];
    const primaryRole = rolePriority.find(role => roleNames().includes(role));
    const scopeDetail = `${session.active_workspace?.name || '未选择 Workspace'} / ${session.active_team?.name || '未选择团队'}`;
    const identityDetail = `${session.username} · ${scopeDetail} · ${roleNames().map(role => localizedText(fmtRole(role))).join(' / ') || localizedText('未分配角色')}`;
    identityChip.innerHTML = `<strong>${escapeHtml(session.username)}</strong><span>${escapeHtml(localizedText(primaryRole ? fmtRole(primaryRole) : '未分配角色'))}</span>`;
    identityChip.title = identityDetail;
    identityChip.setAttribute('aria-label', identityDetail);
    userMenuName.textContent = session.username;
    userMenuAuth.textContent = sessionAuthenticationMethod === 'password-scrypt' || sessionAuthenticationMethod === 'PASSWORD' ? localizedText('密码登录') : localizedText('内部会话');
    userMenuWorkspace.textContent = session.active_workspace?.name || localizedText('未选择 Workspace');
    userMenuTeam.textContent = session.active_team?.name || localizedText('未选择团队');
    userMenuRole.textContent = localizedText(primaryRole ? fmtRole(primaryRole) : '未分配角色');
    mobileSessionSummary.textContent = scopeDetail;
    mobileSessionSummary.title = identityDetail;
    document.querySelectorAll('[data-nav-capability]').forEach(link => {
      link.hidden = !hasCapability(link.dataset.navCapability);
    });
    document.querySelectorAll('.nav-link-group').forEach(group => {
      group.hidden = ![...group.querySelectorAll('a')].some(link => !link.hidden);
    });
    document.querySelectorAll('[data-nav-section]').forEach(section => {
      section.hidden = ![...section.querySelectorAll('a')].some(link => !link.hidden);
    });
  }
  closeUserMenu();
  closeWorkspaceSwitcher();
  closeMobileNav({restoreFocus:false});
  syncDesktopNavigation();
}

function errorView(error, retry = true) {
  const guidance = errorStateGuidance(error);
  return `<section class="error-state" role="alert" aria-live="assertive" aria-labelledby="error-state-title"><div><p class="eyebrow">运行状态 · 已安全阻断</p><h2 id="error-state-title" data-error-heading tabindex="-1">${escapeHtml(guidance.title)}</h2><p class="error-state-reason">${escapeHtml(guidance.reason)}</p><dl class="error-state-guidance"><div><dt>影响</dt><dd>${escapeHtml(guidance.impact)}</dd></div><div><dt>负责角色</dt><dd>${escapeHtml(guidance.owner)}</dd></div><div><dt>下一步</dt><dd>${escapeHtml(guidance.next)}</dd></div><div><dt>技术状态</dt><dd><code>${escapeHtml(error?.code || `HTTP_${error?.status || 'UNKNOWN'}`)}</code></dd></div></dl><div class="error-state-actions">${retry ? '<button class="primary" data-retry>重新检查</button>' : ''}<a class="secondary" href="/home" data-link>返回当前任务</a></div></div></section>`;
}

function errorStateGuidance(error) {
  const reason = friendlyApiError(error);
  if (error?.code === 'NETWORK_ERROR') return {
    title:'连接已中断', reason,
    impact:'本页事实未更新；离线前画面不得视为当前状态，新增风险与写入操作保持阻断。',
    owner:'平台运维或网络管理员',
    next:'确认服务与网络恢复后重新检查；恢复前不要重复提交任何结果未知的操作。',
  };
  if (error?.code === 'REQUEST_TIMEOUT') return {
    title:'读取超时', reason,
    impact:'本页事实未在时限内确认；新增风险与写入操作保持阻断。',
    owner:'平台运维或网络管理员',
    next:'先确认服务负载与网络状态，再重新检查；写入超时必须先核对权威状态。',
  };
  if (error?.status === 403 || error?.code === 'HTTP_403') return {
    title:'当前范围没有访问权限', reason,
    impact:'当前页面不会加载团队或账户数据，也不会开放任何操作。',
    owner:'团队管理员',
    next:'返回当前任务，或由团队管理员核对岗位、账户与交易所范围。',
  };
  if (error?.code === 'RISK_POLICY_MISSING') return {
    title:'风险政策尚未配置', reason,
    impact:'新增风险、测试启用及相关写入保持阻断；既有减仓与退出边界不因页面提示而改变。',
    owner:'系统管理员',
    next:'保存当前团队的版本化风险政策后重新检查。',
  };
  return {
    title:'当前数据无法读取', reason,
    impact:'本页事实不可用；依赖这些事实的操作保持阻断。',
    owner:'当前功能负责人或系统管理员',
    next:'根据上方原因恢复所需事实后重新检查。',
  };
}

function cancelMobileNavFocus() {
  mobileNavFocusToken += 1;
  if (mobileNavFocusFrame !== null) cancelAnimationFrame(mobileNavFocusFrame);
  mobileNavFocusFrame = null;
}

function openMobileNav() {
  if (!session || !matchMedia('(max-width: 1100px)').matches) return;
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
  const mobile = matchMedia('(max-width: 1100px)').matches;
  sidebar.classList.remove('open');
  navBackdrop.hidden = true;
  mobileNavToggle.setAttribute('aria-expanded', 'false');
  document.body.classList.remove('nav-open');
  main.inert = false;
  sidebar.inert = sidebar.hidden || mobile;
  sidebar.setAttribute('aria-hidden', String(sidebar.hidden || mobile));
  if (restoreFocus && !mobileNavToggle.hidden) mobileNavToggle.focus();
}

function desktopNavigationCollapsedPreference() {
  return localStorage.getItem(DESKTOP_NAV_STORAGE_KEY) === 'true';
}

function setDesktopNavigationCollapsed(collapsed, {persist = false} = {}) {
  const desktop = matchMedia('(min-width: 1101px)').matches;
  const active = Boolean(collapsed && desktop && session && !sidebar.hidden);
  document.querySelector('.app-shell')?.classList.toggle('sidebar-collapsed', active);
  desktopNavToggle.setAttribute('aria-expanded', String(!active));
  const label = localizedText(active ? '展开主导航' : '收起主导航');
  desktopNavToggle.setAttribute('aria-label', label);
  desktopNavToggle.title = label;
  desktopNavToggle.querySelector('span').textContent = active ? '›' : '‹';
  if (persist) localStorage.setItem(DESKTOP_NAV_STORAGE_KEY, String(Boolean(collapsed)));
}

function syncDesktopNavigation() {
  setDesktopNavigationCollapsed(desktopNavigationCollapsedPreference());
}

function syncNavigationMode() {
  closeMobileNav({restoreFocus:false});
  syncDesktopNavigation();
}

function bindLinkedRows() {
  document.querySelectorAll('tr[data-href]').forEach((row) => {
    if (row.dataset.rowBound === 'true') return;
    row.dataset.rowBound = 'true';
    const firstCell = row.querySelector('td');
    if (firstCell && !firstCell.querySelector('.row-link')) {
      const context = firstCell.textContent.trim().replace(/\s+/g, ' ');
      const subject = firstCell.querySelector('b')?.textContent.trim() || context;
      const link = document.createElement('a');
      link.className = 'row-link';
      link.href = row.dataset.href;
      link.dataset.link = '';
      link.textContent = localizedText('查看详情');
      link.setAttribute('aria-label', currentLanguage === 'en' ? `View ${subject}` : `查看 ${subject}`);
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
    if ((wrapper.closest('.risk-condition-details') || wrapper.matches('.connection-status-table')) && matchMedia('(max-width: 780px)').matches) return;
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
  applyLanguageToDocument();
  main.removeAttribute('aria-busy');
  if (focusNextRouteHeading) {
    focusNextRouteHeading = false;
    const heading = main.querySelector('h1, h2');
    if (heading) {
      heading.tabIndex = -1;
      heading.focus({preventScroll:true});
    }
  }
}

function confirmAction({title, message, confirmLabel}) {
  document.querySelector('#confirm-title').textContent = localizedText(title);
  document.querySelector('#confirm-message').textContent = localizedText(message);
  document.querySelector('#confirm-submit').textContent = localizedText(confirmLabel || '确认并继续');
  confirmDialog.returnValue = '';
  confirmDialog.showModal();
  return new Promise((resolve) => {
    const submit = confirmDialog.querySelector('#confirm-submit');
    const cancelButtons = [...confirmDialog.querySelectorAll('[value="cancel"]')];
    let settled = false;
    const finish = confirmed => {
      if (settled) return;
      settled = true;
      submit?.removeEventListener('click', confirm);
      cancelButtons.forEach(button => button.removeEventListener('click', cancel));
      confirmDialog.removeEventListener('cancel', cancel);
      if (confirmDialog.open) confirmDialog.close(confirmed ? 'confirm' : 'cancel');
      resolve(confirmed);
    };
    const confirm = event => { event.preventDefault(); finish(true); };
    const cancel = event => { event.preventDefault(); finish(false); };
    submit?.addEventListener('click', confirm, {once:true});
    cancelButtons.forEach(button => button.addEventListener('click', cancel, {once:true}));
    confirmDialog.addEventListener('cancel', cancel, {once:true});
  });
}

async function withPending(button, pendingLabel, action) {
  if (!button || button.dataset.pending === 'true') return undefined;
  const originalLabel = button.textContent;
  button.dataset.pending = 'true';
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  button.textContent = localizedText(pendingLabel);
  try {
    return await action();
  } finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    delete button.dataset.pending;
    button.textContent = originalLabel;
  }
}
