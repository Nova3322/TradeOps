function navigate(path) { focusNextRouteHeading = true; history.pushState({}, '', path); route(); }
function updateActiveNav() {
  const proposalSource = new URLSearchParams(location.search).get('from');
  document.querySelectorAll('#sidebar nav a').forEach((link) => {
    const href = link.getAttribute('href');
    const active = location.pathname === href
      || (href === '/venues' && location.pathname.startsWith('/venues/'))
      || (href === '/reviews' && location.pathname.startsWith('/proposals/') && proposalSource === 'reviews')
      || (href === '/proposals' && location.pathname.startsWith('/proposals/') && proposalSource !== 'reviews')
      || (href === '/campaigns' && location.pathname.startsWith('/campaigns/'));
    link.classList.toggle('active', active);
    if (active) {
      link.setAttribute('aria-current', 'page');
    }
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
  if (link) { event.preventDefault(); closeUserMenu(); navigate(link.getAttribute('href')); }
  if (event.target.closest('[data-retry]')) route();
  if (!scopeControl.contains(event.target)) closeWorkspaceSwitcher();
  preferenceSelects.forEach((select, kind) => {
    if (!select.contains(event.target)) closePreferenceDropdown(kind);
  });
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
  if (event.key === 'Escape' && closePreferenceDropdowns({restoreFocus:true})) {
    event.preventDefault();
    return;
  }
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
  closePreferenceDropdowns();
  closeWorkspaceSwitcher();
  userMenuPanel.hidden = !opening;
  identityChip.setAttribute('aria-expanded', String(opening));
  if (opening) userMenuPanel.querySelector('button, input, summary')?.focus({preventScroll:true});
});

function preferenceElements(kind) {
  const select = preferenceSelects.get(kind);
  return {
    select,
    trigger:select?.querySelector('.preference-trigger'),
    value:select?.querySelector('[data-preference-value]'),
    menu:select?.querySelector('.preference-menu'),
  };
}

function preferenceOptionLabel(kind, option) {
  return kind === 'language' ? option.label : localizedText(option.label);
}

function activePreferenceValue(kind) {
  return kind === 'language' ? currentLanguage : currentThemePreference;
}

function renderPreferenceDropdown(kind) {
  const {value, menu} = preferenceElements(kind);
  const options = PREFERENCE_OPTIONS[kind] || [];
  const selectedValue = activePreferenceValue(kind);
  menu.innerHTML = options.map(option => `<button class="preference-option" type="button" role="option" tabindex="-1" data-preference-option="${escapeHtml(option.value)}" aria-selected="${String(option.value === selectedValue)}">${escapeHtml(preferenceOptionLabel(kind, option))}</button>`).join('');
  const selected = options.find(option => option.value === selectedValue) || options[0];
  value.textContent = selected ? preferenceOptionLabel(kind, selected) : '—';
}

function updatePreferenceDropdown(kind, selectedValue) {
  const {value, menu} = preferenceElements(kind);
  const options = PREFERENCE_OPTIONS[kind] || [];
  const selected = options.find(option => option.value === selectedValue) || options[0];
  value.textContent = selected ? preferenceOptionLabel(kind, selected) : '—';
  menu.querySelectorAll('[data-preference-option]').forEach(option => {
    option.setAttribute('aria-selected', String(option.dataset.preferenceOption === selectedValue));
  });
}

function closePreferenceDropdown(kind, {restoreFocus = false} = {}) {
  const {trigger, menu} = preferenceElements(kind);
  if (!menu || menu.hidden) return false;
  menu.hidden = true;
  trigger.setAttribute('aria-expanded', 'false');
  if (restoreFocus) trigger.focus({preventScroll:true});
  return true;
}

function closePreferenceDropdowns({except = null, restoreFocus = false} = {}) {
  let closed = false;
  preferenceSelects.forEach((_select, kind) => {
    if (kind !== except) closed = closePreferenceDropdown(kind, {restoreFocus}) || closed;
  });
  return closed;
}

function openPreferenceDropdown(kind, {focus = 'selected'} = {}) {
  const {trigger, menu} = preferenceElements(kind);
  closePreferenceDropdowns({except:kind});
  menu.hidden = false;
  trigger.setAttribute('aria-expanded', 'true');
  const options = [...menu.querySelectorAll('[data-preference-option]')];
  const selectedIndex = Math.max(0, options.findIndex(option => option.getAttribute('aria-selected') === 'true'));
  const index = focus === 'first' ? 0 : focus === 'last' ? options.length - 1 : selectedIndex;
  options[index]?.focus({preventScroll:true});
}

function selectPreference(kind, value) {
  if (!(PREFERENCE_OPTIONS[kind] || []).some(option => option.value === value)) return;
  if (kind === 'theme') {
    applyTheme(value, {persist:true});
    closePreferenceDropdown(kind, {restoreFocus:true});
    return;
  }
  const nextLanguage = normalizeLanguagePreference(value);
  if (nextLanguage === currentLanguage) {
    closePreferenceDropdown(kind, {restoreFocus:true});
    return;
  }
  currentLanguage = nextLanguage;
  localStorage.setItem(LANGUAGE_STORAGE_KEY, currentLanguage);
  updatePreferenceDropdown(kind, currentLanguage);
  closePreferenceDropdown(kind, {restoreFocus:true});
  location.reload();
}

function movePreferenceFocus(menu, currentOption, key) {
  const options = [...menu.querySelectorAll('[data-preference-option]')];
  if (!options.length) return;
  const currentIndex = Math.max(0, options.indexOf(currentOption));
  const targetIndex = key === 'Home'
    ? 0
    : key === 'End'
      ? options.length - 1
      : (currentIndex + (key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
  options[targetIndex].focus({preventScroll:true});
}

function initializePreferenceDropdowns() {
  preferenceSelects.forEach((select, kind) => {
    renderPreferenceDropdown(kind);
    const {trigger, menu} = preferenceElements(kind);
    trigger.addEventListener('click', () => {
      if (menu.hidden) openPreferenceDropdown(kind);
      else closePreferenceDropdown(kind, {restoreFocus:true});
    });
    trigger.addEventListener('keydown', event => {
      if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
      event.preventDefault();
      openPreferenceDropdown(kind, {focus:event.key === 'ArrowUp' ? 'last' : 'selected'});
    });
    menu.addEventListener('click', event => {
      const option = event.target.closest('[data-preference-option]');
      if (option) selectPreference(kind, option.dataset.preferenceOption);
    });
    menu.addEventListener('keydown', event => {
      const option = event.target.closest('[data-preference-option]');
      if (!option) return;
      if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        movePreferenceFocus(menu, option, event.key);
      } else if (['Enter', ' '].includes(event.key)) {
        event.preventDefault();
        selectPreference(kind, option.dataset.preferenceOption);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closePreferenceDropdown(kind, {restoreFocus:true});
      } else if (event.key === 'Tab') {
        closePreferenceDropdown(kind);
      }
    });
  });
}

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
  const normalizedPreference = normalizeThemePreference(preference);
  const resolved = normalizedPreference === 'system'
    ? (preferredThemeMedia.matches ? 'dark' : 'light')
    : normalizedPreference;
  currentThemePreference = normalizedPreference;
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.themePreference = normalizedPreference;
  updatePreferenceDropdown('theme', normalizedPreference);
  if (themeColorMeta) themeColorMeta.content = resolved === 'dark' ? '#0d1110' : '#f5f4f0';
  if (persist) localStorage.setItem(THEME_STORAGE_KEY, normalizedPreference);
}
preferredThemeMedia.addEventListener?.('change', () => {
  if (currentThemePreference === 'system') applyTheme('system');
});
const storedLanguagePreference = localStorage.getItem(LANGUAGE_STORAGE_KEY);
if (storedLanguagePreference && storedLanguagePreference !== currentLanguage) {
  localStorage.setItem(LANGUAGE_STORAGE_KEY, currentLanguage);
}
const storedThemePreference = localStorage.getItem(THEME_STORAGE_KEY);
currentThemePreference = normalizeThemePreference(storedThemePreference);
initializePreferenceDropdowns();
applyTheme(currentThemePreference, {
  persist:Boolean(storedThemePreference && storedThemePreference !== currentThemePreference),
});
applyLanguageToDocument();
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
syncNavigationMode();
bootstrap().catch((error) => {
  main.innerHTML = errorView(error, false);
  enhanceRenderedPage();
  main.querySelector('[data-error-heading]')?.focus();
});
