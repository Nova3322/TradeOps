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
    setShell(false);
    history.replaceState({}, '', '/');
    await route();
  } catch (error) { showApiError(error); }
}));
const preferredThemeMedia = matchMedia('(prefers-color-scheme: dark)');
function applyTheme(theme, {persist = false} = {}) {
  const normalized = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = normalized;
  themeToggle.dataset.theme = normalized;
  themeToggle.setAttribute('aria-pressed', String(normalized === 'dark'));
  const currentLabel = normalized === 'dark'
    ? (currentLanguage === 'en' ? 'Dark' : '深色')
    : (currentLanguage === 'en' ? 'Light' : '浅色');
  const nextLabel = normalized === 'dark'
    ? (currentLanguage === 'en' ? 'light' : '浅色')
    : (currentLanguage === 'en' ? 'dark' : '深色');
  themeToggle.querySelector('[data-theme-label]').textContent = currentLabel;
  themeToggle.setAttribute('aria-label', currentLanguage === 'en'
    ? `${currentLabel} theme; switch to ${nextLabel}`
    : `当前为${currentLabel}主题；切换到${nextLabel}主题`);
  themeToggle.title = themeToggle.getAttribute('aria-label');
  if (themeColorMeta) themeColorMeta.content = normalized === 'dark' ? '#0b100f' : '#f4f6f3';
  if (persist) localStorage.setItem(THEME_STORAGE_KEY, normalized);
}
themeToggle.addEventListener('click', () => {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark', {persist:true});
});
preferredThemeMedia.addEventListener?.('change', event => {
  if (!localStorage.getItem(THEME_STORAGE_KEY)) applyTheme(event.matches ? 'dark' : 'light');
});
languageToggle.addEventListener('click', () => {
  currentLanguage = currentLanguage === 'en' ? 'zh-CN' : 'en';
  localStorage.setItem(LANGUAGE_STORAGE_KEY, currentLanguage);
  location.reload();
});
applyTheme(localStorage.getItem(THEME_STORAGE_KEY) || (preferredThemeMedia.matches ? 'dark' : 'light'));
applyLanguageToDocument();
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
syncNavigationMode();
bootstrap().catch((error) => {
  main.innerHTML = errorView(error, false);
  enhanceRenderedPage();
  main.querySelector('[data-error-heading]')?.focus();
});
