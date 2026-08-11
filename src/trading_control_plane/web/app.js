function navigate(path) { history.pushState({}, '', path); route(); }
function updateActiveNav() {
  document.querySelectorAll('nav a').forEach((link) => {
    const href = link.getAttribute('href');
    const active = location.pathname === href || (href === '/venues' && location.pathname.startsWith('/venues/'));
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

document.addEventListener('click', (event) => {
  const workspaceOption = event.target.closest('[data-switch-workspace]');
  if (workspaceOption) {
    event.preventDefault();
    closeWorkspaceSwitcher();
    withPending(workspaceOption, '进入中…', () => selectScope(workspaceOption.dataset.switchWorkspace, workspaceOption.dataset.switchTeam || null));
    return;
  }
  const link = event.target.closest('[data-link]');
  if (link) { event.preventDefault(); navigate(link.getAttribute('href')); }
  if (event.target.closest('[data-retry]')) route();
  if (!scopeControl.contains(event.target)) closeWorkspaceSwitcher();
  if (!userMenu.contains(event.target)) closeUserMenu();
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
scopeSwitcher.addEventListener('click', () => {
  const opening = scopeSwitcherMenu.hidden;
  if (opening) {
    renderWorkspaceSwitcher();
    scopeSwitcherMenu.hidden = false;
    scopeSwitcher.setAttribute('aria-expanded', 'true');
    scopeSwitcherMenu.querySelector('[role="menuitem"]')?.focus();
  } else closeWorkspaceSwitcher({restoreFocus:true});
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && sidebar.classList.contains('open')) closeMobileNav();
  if (event.key === 'Escape' && !scopeSwitcherMenu.hidden) closeWorkspaceSwitcher({restoreFocus:true});
  if (event.key === 'Escape' && !userMenuPanel.hidden) closeUserMenu({restoreFocus:true});
});
document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => dialog.close()));
document.querySelector('#system-proposal-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form)); const candidateId = data.candidate_id; delete data.candidate_id; data.environment = currentWorkflowEnvironment(); data.configuration_mode = 'ADVANCED_OVERRIDE'; data.default_config_version = null; data.expires_in_minutes = Number(data.expires_in_hours) * 60; delete data.expires_in_hours; data.initial_quantity = data.initial_quantity || null; data.add_trigger_price = data.add_trigger_price || null; data.allow_auto_add = data.allow_auto_add === 'true'; data.requested_adds = Number(data.requested_adds);
  try { const result = await api(`/api/opportunities/${candidateId}/proposals`, {method:'POST', body:JSON.stringify(data)}); dialog.close(); showToast('系统机会提案已冻结并进入审核'); navigate(`/proposals/${result.proposal_id}`); }
  catch (error) { showApiError(error, form.querySelector('#system-form-error')); }
});
document.querySelector('#logout-button').addEventListener('click', async (event) => withPending(event.currentTarget, '退出中…', async () => {
  cancelMobileNavFocus();
  try {
    await api('/api/auth/logout', {method:'POST'});
    session = null;
    sessionAuthenticationMethod = '';
    setShell(false);
    history.replaceState({}, '', '/');
    await route();
  } catch (error) { showApiError(error); }
}));
function closeUserMenu({restoreFocus = false} = {}) {
  userMenuPanel.hidden = true;
  identityChip.setAttribute('aria-expanded', 'false');
  if (restoreFocus && !userMenu.hidden) identityChip.focus();
}

identityChip.addEventListener('click', () => {
  const opening = userMenuPanel.hidden;
  closeWorkspaceSwitcher();
  userMenuPanel.hidden = !opening;
  identityChip.setAttribute('aria-expanded', String(opening));
  if (opening) userMenuPanel.querySelector('button, input, summary')?.focus({preventScroll:true});
});

passwordChangeForm.addEventListener('input', () => {
  passwordChangeForm.querySelector('.form-error').textContent = '';
});
passwordChangeForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  if (data.get('new_password') !== data.get('confirm_password')) {
    form.querySelector('.form-error').textContent = localizedText('两次输入的新密码不一致。');
    return;
  }
  await withPending(event.submitter, '更新中…', async () => {
    try {
      const result = await api('/api/auth/password', {
        method:'POST',
        body:JSON.stringify({
          current_password:data.get('current_password'),
          new_password:data.get('new_password'),
          expected_auth_version:session.auth_version,
          idempotency_key:crypto.randomUUID(),
        }),
      });
      session = result.session;
      sessionAuthenticationMethod = result.authentication_method || 'PASSWORD';
      form.reset();
      form.closest('details').open = false;
      setShell(true, {workspaceGate:['/', '/workspaces'].includes(location.pathname)});
      showToast('密码已更新；其他旧会话已撤销');
    } catch (error) {
      showApiError(error, form.querySelector('.form-error'));
    }
  });
});

const preferredThemeMedia = matchMedia('(prefers-color-scheme: dark)');
function applyTheme(preference, {persist = false} = {}) {
  const normalizedPreference = ['light', 'dark'].includes(preference) ? preference : 'system';
  const resolved = normalizedPreference === 'system'
    ? (preferredThemeMedia.matches ? 'dark' : 'light')
    : normalizedPreference;
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.themePreference = normalizedPreference;
  themeOptionButtons.forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.themeOption === normalizedPreference));
  });
  if (themeColorMeta) themeColorMeta.content = resolved === 'dark' ? '#0b100f' : '#f4f6f3';
  if (persist) localStorage.setItem(THEME_STORAGE_KEY, normalizedPreference);
}
themeOptionButtons.forEach(button => button.addEventListener('click', () => {
  applyTheme(button.dataset.themeOption, {persist:true});
}));
preferredThemeMedia.addEventListener?.('change', () => {
  if ((localStorage.getItem(THEME_STORAGE_KEY) || 'system') === 'system') applyTheme('system');
});
languageToggle.addEventListener('click', () => {
  currentLanguage = currentLanguage === 'en' ? 'zh-CN' : 'en';
  localStorage.setItem(LANGUAGE_STORAGE_KEY, currentLanguage);
  location.reload();
});
applyTheme(localStorage.getItem(THEME_STORAGE_KEY) || 'system');
applyLanguageToDocument();
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
syncNavigationMode();
bootstrap().catch((error) => {
  main.innerHTML = errorView(error, false);
  enhanceRenderedPage();
  main.querySelector('[data-error-heading]')?.focus();
});
