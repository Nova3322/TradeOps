async function renderCampaignList() {
  const result = await api('/api/campaigns');
  const environment = currentWorkflowEnvironment();
  const items = (result.data || [])
    .filter(item => item.environment === environment)
    .sort((left, right) =>
      new Date(right.updated_at) - new Date(left.updated_at)
      || String(right.campaign_id).localeCompare(String(left.campaign_id)));
  const modeLabel = fmtCompactEnvironment(environment);
  const modeMeaning = fmtEnvironment(environment, true);
  const environmentBoundaryLabel = localizedText('环境不可跨越');
  const environmentCopy = environment === 'LIVE'
    ? '订单会发送到交易所生产服务器并影响真实资金。'
    : '订单会发送到交易所测试服务器，仅代表交易所测试资产。';
  const historyRows = items.map(item => {
    const symbol = item.symbol || '标的未配置';
    const detailLabel = currentLanguage === 'en' ? `View ${symbol} trade details` : `查看 ${symbol} 交易详情`;
    return `<tr data-href="/campaigns/${item.campaign_id}" data-campaign-row data-search="${escapeHtml(`${item.symbol || ''} ${item.account_id || ''} ${item.venue || ''} ${item.campaign_id}`.toLowerCase())}" data-direction="${escapeHtml(item.direction)}" data-venue="${escapeHtml(item.venue)}" data-status="${escapeHtml(item.status)}"><td class="campaign-instrument-cell" data-label="标的"><div class="campaign-instrument"><b>${escapeHtml(symbol)}</b><a class="row-link campaign-id-link" href="/campaigns/${item.campaign_id}" data-link aria-label="${escapeHtml(detailLabel)}" title="${escapeHtml(item.campaign_id)}">${shortId(item.campaign_id)}</a></div></td><td class="campaign-direction-cell" data-label="方向"><span class="direction-pill ${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(fmtDirection(item.direction))}</span></td><td data-label="账户 / 场所">${escapeHtml(fmtDefaultAccountLabel(item.account_id))}<br><span class="subtle">${escapeHtml(fmtVenueLabel(item.venue))}</span></td><td data-label="仓位目标">${escapeHtml(campaignTargetLabel(item))}</td><td data-label="状态"><b class="status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</b></td><td data-label="最终盈亏">${escapeHtml(campaignPnlLabel(item, item.final_pnl))}</td><td data-label="更新时间">${fmtDate(item.updated_at)}</td></tr>`;
  }).join('');
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow" title="${escapeHtml(modeMeaning)}" aria-label="${escapeHtml(modeMeaning)} · ${escapeHtml(environmentBoundaryLabel)}">${escapeHtml(modeLabel)} · ${escapeHtml(environmentBoundaryLabel)}</p><h1>交易历史</h1><p class="lede"><span>每条交易历史保留从授权、风险占用和下单意图，到成交、保护、减仓、对账与最终结果的完整生命周期。</span> <span>${escapeHtml(environmentCopy)}</span></p></div><div class="toolbar"><a class="secondary" href="/campaigns/alerts" data-link>运行告警</a><a class="secondary" href="/reviews?view=current" data-link>查看提案</a></div></header>
    <div class="stats"><div class="stat"><small>交易历史记录</small><b>${items.length}</b></div><div class="stat"><small>建仓中 / 持仓中</small><b>${items.filter(item => ['OPEN','OPENING'].includes(item.status)).length}</b></div><div class="stat"><small>结果未知</small><b>${items.filter(item => item.status === 'UNKNOWN').length}</b></div></div>
    ${items.length ? `<details class="proposal-filter-disclosure" ${window.matchMedia('(min-width: 781px)').matches ? 'open' : ''}><summary><span><b>筛选交易历史</b><small>按标的、方向、交易所和状态缩小结果</small></span><strong><span><b data-campaign-count>${items.length}</b> 个结果</span><span class="proposal-filter-when-closed">展开</span><span class="proposal-filter-when-open">收起</span></strong></summary><div class="proposal-list-tools"><label>搜索标的或账户<input id="campaign-search" type="search" placeholder="BTCUSDT / acct-1"></label><label>方向<select id="campaign-direction"><option value="">全部方向</option><option value="LONG">做多</option><option value="SHORT">做空</option></select></label><label>交易所<select id="campaign-venue"><option value="">全部交易所</option><option value="BINANCE">币安</option><option value="HYPERLIQUID">Hyperliquid</option><option value="OKX">OKX</option><option value="BYBIT">Bybit</option></select></label><label>状态<select id="campaign-status"><option value="">全部状态</option><option value="OPENING">建仓中</option><option value="OPEN">持仓中</option><option value="REDUCING">减仓中</option><option value="CLOSING">退出中</option><option value="CLOSED">已结束</option><option value="UNKNOWN">结果未知</option></select></label><span role="status" aria-live="polite"><b data-campaign-visible-count>${Math.min(items.length, 50)}</b> / <b data-campaign-count>${items.length}</b> 个结果</span></div></details><div class="table-wrap campaign-list-table"><table><thead><tr><th>标的</th><th>方向</th><th>账户 / 场所</th><th>仓位目标</th><th>状态</th><th>最终盈亏</th><th>更新时间</th></tr></thead><tbody>${historyRows}</tbody></table></div>${recordPaginationMarkup(items.length, '交易历史分页')}<section id="campaign-filter-empty" class="empty-state compact-empty" hidden><div><h2>没有符合条件的交易历史</h2><p>请清除搜索或调整筛选。</p></div></section>` : `<section class="empty-state"><div><h2>当前没有${escapeHtml(modeLabel)}交易历史</h2><p>提案达到独立审核阈值后，系统会自动完成实时风控、短期授权、风险预留和受控执行。</p></div></section>`}</section>`;
  bindLinkedRows();
  if (!items.length) return;
  bindRecordList({
    rowSelector:'[data-campaign-row]',
    filterSelectors:['#campaign-search','#campaign-direction','#campaign-venue','#campaign-status'],
    matches:row => {
      const query = document.querySelector('#campaign-search')?.value.toLowerCase().trim() || '';
      const direction = document.querySelector('#campaign-direction')?.value || '';
      const venue = document.querySelector('#campaign-venue')?.value || '';
      const status = document.querySelector('#campaign-status')?.value || '';
      return (!query || row.dataset.search.includes(query))
        && (!direction || row.dataset.direction === direction)
        && (!venue || row.dataset.venue === venue)
        && (!status || row.dataset.status === status);
    },
    emptySelector:'#campaign-filter-empty',
    visibleCountSelector:'[data-campaign-visible-count]',
    totalCountSelector:'[data-campaign-count]',
  });
}

function accountCredentialFields(venue, prefix = '') {
  if (venue === 'HYPERLIQUID') {
    return `<label>主账户地址<input name="${prefix}account_address" autocomplete="off" required></label><label>API 钱包地址<input name="${prefix}api_wallet_address" autocomplete="off" required></label><label>API 钱包私钥<input name="${prefix}api_wallet_private_key" type="password" autocomplete="new-password" required></label>`;
  }
  const passphrase = ['OKX'].includes(venue) ? `<label>Passphrase<input name="${prefix}passphrase" type="password" autocomplete="new-password" required></label>` : '';
  return `<label>API Key<input name="${prefix}api_key" autocomplete="off" required></label><label>API Secret<input name="${prefix}api_secret" type="password" autocomplete="new-password" required></label>${passphrase}`;
}

function credentialsFromForm(form, prefix = '') {
  const fields = ['api_key','api_secret','passphrase','account_address','api_wallet_address','api_wallet_private_key'];
  return Object.fromEntries(fields.map(name => [name, form.elements[`${prefix}${name}`]?.value?.trim()]).filter(([,value]) => value));
}

function accountCard(item) {
  const modeMeaning = fmtEnvironment(item.environment, true);
  const disabled = !item.active;
  const permissions = item.permissions || {};
  const unsupported = item.environment === 'TESTNET' && item.venue === 'OKX';
  const credentialActionLabel = ['BINANCE','OKX','BYBIT'].includes(item.venue) ? '更新 apikey' : '更新凭据';
  return `<article class="card mode-exchange-account ${disabled ? 'is-muted' : ''}">
    <div class="card-heading"><div><p class="eyebrow" title="${escapeHtml(modeMeaning)}" aria-label="${escapeHtml(modeMeaning)} · ${escapeHtml(fmtVenueLabel(item.venue))}">${escapeHtml(fmtVenueLabel(item.venue))}</p><h3>${escapeHtml(item.label)}</h3><p class="subtle">${escapeHtml(item.account_id)}</p></div><span class="status-pill status-${escapeHtml(item.connection.status)}">${escapeHtml(fmtStatus(item.connection.status))}</span></div>
    ${unsupported ? '<p class="callout is-warning">该交易所测试环境仅支持事实同步；执行保持不可用</p>' : ''}
    <dl class="definition-grid">${definition('凭据', item.credentials.state === 'CONFIGURED' ? `已加密 · v${item.credentials.version}` : '未配置')}${definition('最近验证', item.connection.last_verified_at ? fmtDate(item.connection.last_verified_at) : '尚未验证')}${item.connection.status === 'FAILED' ? definition('连接诊断', fmtBinanceConnectionDiagnostic(item.connection)) : ''}${definition('交易状态', fmtStatus(item.trading.status))}${definition('运行同步', item.runtime_binding.bound ? '已绑定' : '未绑定')}</dl>
    ${permissions.can_manage ? `<details class="operation-toolbox"><summary><span><b>编辑与凭据</b><small>账户 ID 创建后不可修改；账户名称可随时更新</small></span><strong>展开</strong></summary><div class="toolbox-content"><form class="account-label-form" data-account-id="${item.exchange_account_id}" data-version="${item.version}"><div class="field-grid"><label>账户 ID（创建后不可修改）<input value="${escapeHtml(item.account_id)}" readonly aria-readonly="true"></label><label>账户名称<input name="label" value="${escapeHtml(item.label)}" required maxlength="120"></label></div><p class="field-help">资金中心填写账户时使用上方精确账户 ID，不使用可编辑的账户名称。</p><div class="form-error" role="alert"></div><button class="secondary">保存名称</button></form>${permissions.can_manage_credentials ? `<form class="account-credential-form" data-account-id="${item.exchange_account_id}" data-version="${item.version}" data-venue="${item.venue}"><div class="field-grid">${accountCredentialFields(item.venue, 'rotate_')}</div><div class="form-error" role="alert"></div><button class="secondary">${credentialActionLabel}</button></form>` : ''}</div></details>` : ''}
    <div class="form-actions"><a class="secondary" href="/venues/${encodeURIComponent(item.exchange_account_id)}" data-link>查看详情</a>${permissions.can_verify_connection && item.active ? `<button class="secondary" data-account-verify="${item.exchange_account_id}" data-version="${item.version}">连接测试</button>` : ''}${permissions.can_manage ? `<button class="secondary" data-account-state="${item.exchange_account_id}" data-version="${item.version}" data-enabled="${!item.active}">${item.active ? '停用' : '启用'}</button>` : ''}${permissions.can_delete ? `<button class="danger" data-account-delete="${item.exchange_account_id}" data-version="${item.version}" data-confirmation="DELETE:${item.environment}:${escapeHtml(item.account_id)}:${item.venue}">删除</button>` : ''}</div>
  </article>`;
}

async function renderAccountManagement() {
  const activeSpaceName = session?.active_team?.name || '当前团队';
  const params = new URLSearchParams(location.search);
  const [modeResponse, accountResponse] = await Promise.all([
    api('/api/trading-mode'),
    api('/api/exchange-accounts'),
  ]);
  const mode = modeResponse.data;
  const requestedEnvironment = params.get('environment');
  const selectedEnvironment = ['TESTNET', 'LIVE'].includes(requestedEnvironment)
    ? requestedEnvironment
    : mode.execution_mode === 'LIVE' ? 'LIVE' : 'TESTNET';
  const accountData = accountResponse.data;
  const accounts = (accountData.data || []).filter(item => item.environment === selectedEnvironment);
  const canManage = Boolean(accountData.can_manage);
  const venueOptions = (accountData.supported_venues || []).map(venue => `<option value="${venue}">${escapeHtml(fmtVenueLabel(venue))}${selectedEnvironment === 'TESTNET' && venue === 'OKX' ? ' · 仅事实同步' : ''}</option>`).join('');
  const createAccountLabel = selectedEnvironment === 'LIVE' ? '添加生产账户' : '添加测试账户';
  const selectedEnvironmentLabel = fmtCompactEnvironment(selectedEnvironment);
  const selectedEnvironmentMeaning = fmtEnvironment(selectedEnvironment, true);
  const currentModeLabel = fmtCompactEnvironment(mode.execution_mode);
  const currentModeMeaning = fmtEnvironment(mode.execution_mode, true);
  const currentModePrefix = `${localizedText('当前模式')}${currentLanguage === 'en' ? ': ' : '：'}`;
  const configurationCountLabel = currentLanguage === 'en'
    ? `${accounts.length} ${accounts.length === 1 ? 'configuration' : 'configurations'}`
    : `${accounts.length} 个配置`;
  const createForm = canManage ? `<details class="card operation-toolbox"><summary><span><b>${createAccountLabel}</b><small>同时填写精确账户 ID 和账户名称</small></span><strong>展开</strong></summary><form id="exchange-account-create-form" class="toolbox-content"><div class="field-grid"><label>交易所<select name="venue">${venueOptions}</select></label><label>账户 ID（创建后不可修改）<input name="account_id" required maxlength="120" autocomplete="off" autocapitalize="off" spellcheck="false"></label><label>账户名称<input name="label" required maxlength="120" autocomplete="off"></label></div><p class="field-help">账户 ID 必须与服务端运行绑定一致；账户名称仅用于页面识别，可在创建后修改。</p><div class="field-grid" data-create-credentials></div><p class="safety-note">测试凭据只会加载到 TESTNET Adapter；生产凭据只会加载到 LIVE Adapter。</p><div class="form-error" role="alert"></div><button class="primary">添加账户</button></form></details>` : '';
  main.innerHTML = `<section class="page trading-mode-page mode-accounts-page"><header class="page-head"><div><p class="eyebrow">当前空间 · ${escapeHtml(activeSpaceName)}</p><h1>账户管理</h1><p class="lede">提前配置测试和生产账户；实际执行环境始终由服务端读取团队当前模式。</p></div><span class="status-pill ${mode.execution_mode === 'LIVE' ? 'status-ATTENTION' : 'status-APPROVED'}" title="${escapeHtml(currentModeMeaning)}" aria-label="${escapeHtml(currentModePrefix)}${escapeHtml(currentModeMeaning)}">${escapeHtml(currentModePrefix)}${escapeHtml(currentModeLabel)}</span></header>
    <article class="callout"><b>账户配置范围</b><p>这里切换的只是账户配置范围，不会改变团队当前运行模式。实际模式切换请使用页眉“当前模式”。</p><button class="secondary" type="button" data-open-mode-switch>切换当前模式</button></article>
    <nav class="mode-choice-grid" aria-label="账户配置范围"><a class="mode-choice ${selectedEnvironment === 'TESTNET' ? 'is-selected' : ''}" href="/accounts?environment=TESTNET" data-link ${selectedEnvironment === 'TESTNET' ? 'aria-current="page"' : ''}><span class="mode-choice-head"><b>测试账户</b>${selectedEnvironment === 'TESTNET' ? '<span class="mode-choice-current">当前范围</span>' : ''}</span><small>交易所测试环境 API</small></a><a class="mode-choice live-choice ${selectedEnvironment === 'LIVE' ? 'is-selected' : ''}" href="/accounts?environment=LIVE" data-link ${selectedEnvironment === 'LIVE' ? 'aria-current="page"' : ''}><span class="mode-choice-head"><b>生产账户</b>${selectedEnvironment === 'LIVE' ? '<span class="mode-choice-current">当前范围</span>' : ''}</span><small>真实资金环境 API</small></a></nav>
    ${createForm}<section><div class="section-heading"><div><p class="eyebrow" title="${escapeHtml(selectedEnvironmentMeaning)}" aria-label="${escapeHtml(selectedEnvironmentMeaning)} · ${escapeHtml(configurationCountLabel)}">${escapeHtml(selectedEnvironmentLabel)} · ${escapeHtml(configurationCountLabel)}</p><h2>交易所账户</h2></div></div>${accounts.length ? `<div class="mode-account-grid">${accounts.map(accountCard).join('')}</div>` : '<div class="empty-state compact-empty-state mode-account-empty-state"><div><h2>尚未添加此环境账户</h2><p>添加并验证账户后，交易执行才会就绪；账户配置不影响模式选择。</p></div></div>'}</section>
    </section>`;
  const create = document.querySelector('#exchange-account-create-form');
  if (create) {
    const renderFields = () => { create.querySelector('[data-create-credentials]').innerHTML = accountCredentialFields(create.elements.venue.value); };
    create.elements.venue.addEventListener('change', renderFields); renderFields();
    create.addEventListener('submit', async event => { event.preventDefault(); await submitForm(create, () => api('/api/exchange-accounts', {method:'POST', body:JSON.stringify({environment:selectedEnvironment, account_id:create.elements.account_id.value.trim(), venue:create.elements.venue.value, label:create.elements.label.value.trim(), credentials:credentialsFromForm(create), idempotency_key:crypto.randomUUID()})}), {success:'账户已添加', onSuccess:route}); });
  }
  document.querySelectorAll('.account-label-form').forEach(form => form.addEventListener('submit', async event => { event.preventDefault(); await submitForm(form, () => api(`/api/exchange-accounts/${form.dataset.accountId}`, {method:'PUT', body:JSON.stringify({label:form.elements.label.value.trim(), expected_version:Number(form.dataset.version), idempotency_key:crypto.randomUUID()})}), {success:'账户名称已更新', onSuccess:route}); }));
  document.querySelectorAll('.account-credential-form').forEach(form => form.addEventListener('submit', async event => { event.preventDefault(); await submitForm(form, () => api(`/api/exchange-accounts/${form.dataset.accountId}/credentials`, {method:'PUT', body:JSON.stringify({credentials:credentialsFromForm(form,'rotate_'), expected_version:Number(form.dataset.version), idempotency_key:crypto.randomUUID()})}), {success:'凭据已轮换，请重新连接测试', onSuccess:route}); }));
  document.querySelectorAll('[data-account-verify]').forEach(button => button.addEventListener('click', () => withPending(button, '测试中…', async () => { try { const result = await api(`/api/exchange-accounts/${button.dataset.accountVerify}/connection-verifications`, {method:'POST', body:JSON.stringify({expected_version:Number(button.dataset.version), idempotency_key:crypto.randomUUID()})}); const connection = result.connection || {}; showToast(connection.status === 'VERIFIED' ? fmtConnectionVerificationSuccess(result) : `连接测试失败：${fmtBinanceConnectionDiagnostic(connection)}`); await route(); } catch (error) { showApiError(error); } })));
  document.querySelectorAll('[data-account-state]').forEach(button => button.addEventListener('click', async () => { const enabled = button.dataset.enabled === 'true'; const confirmed = await confirmAction({title:`确认${enabled ? '启用' : '停用'}账户？`, message:enabled ? '启用要求凭据已验证并与环境一致。' : '停用会立即关闭运行同步与交易资格。', confirmLabel:`确认${enabled ? '启用' : '停用'}`}); if (!confirmed) return; await withPending(button, '处理中…', async () => { try { await api(`/api/exchange-accounts/${button.dataset.accountState}/state`, {method:'PUT', body:JSON.stringify({enabled, confirmation:enabled ? 'ENABLE_ACCOUNT' : 'DISABLE_ACCOUNT', expected_version:Number(button.dataset.version), idempotency_key:crypto.randomUUID()})}); await route(); } catch (error) { showApiError(error); } }); }));
  document.querySelectorAll('[data-account-delete]').forEach(button => button.addEventListener('click', async () => { const confirmed = await confirmAction({title:'永久删除账户配置？', message:'服务端会检查提案、授权、订单意图、订单、仓位、资金任务和其他引用；存在任何引用都会失败关闭。', confirmLabel:'确认删除'}); if (!confirmed) return; await withPending(button, '检查引用…', async () => { try { await api(`/api/exchange-accounts/${button.dataset.accountDelete}`, {method:'DELETE', body:JSON.stringify({confirmation:button.dataset.confirmation, expected_version:Number(button.dataset.version), idempotency_key:crypto.randomUUID()})}); showToast('账户已删除'); await route(); } catch (error) { showApiError(error); } }); }));
  bindLinkedRows();
}

function closeTeamModeDropdown({restoreFocus = false} = {}) {
  if (teamModeMenu.hidden) return false;
  teamModeDropdownRequestToken += 1;
  teamModeMenu.hidden = true;
  environmentBadge.setAttribute('aria-expanded', 'false');
  if (restoreFocus && !environmentBadge.disabled) environmentBadge.focus({preventScroll:true});
  return true;
}

function teamModeOptionDisabled(data, mode) {
  if (mode === data.execution_mode) return false;
  const readiness = data.target_readiness?.[mode];
  return !data.can_manage || !(readiness?.switch_allowed ?? readiness?.ready);
}

function renderTeamModeDropdown(data, {focus = 'selected'} = {}) {
  teamModeSnapshot = data;
  const modes = [
    {value:'TESTNET', label:'测试模式'},
    {value:'LIVE', label:'生产模式'},
  ];
  teamModeMenu.innerHTML = modes.map(mode => {
    const selected = mode.value === data.execution_mode;
    const disabled = teamModeOptionDisabled(data, mode.value);
    return `<button class="preference-option mode-preference-option" type="button" role="option" tabindex="-1" data-mode-option="${mode.value}" aria-selected="${String(selected)}" ${disabled ? 'disabled aria-disabled="true"' : ''}>${escapeHtml(mode.label)}</button>`;
  }).join('');
  applyLanguageToDocument(teamModeMenu);
  const options = [...teamModeMenu.querySelectorAll('[data-mode-option]:not(:disabled)')];
  const selectedIndex = Math.max(0, options.findIndex(option => option.getAttribute('aria-selected') === 'true'));
  const index = focus === 'first' ? 0 : focus === 'last' ? options.length - 1 : selectedIndex;
  options[index]?.focus({preventScroll:true});
}

async function openTeamModeDropdown({focus = 'selected'} = {}) {
  if (!session || !hasCapability('venue.view') || environmentBadge.disabled) return;
  closeUserMenu();
  closePreferenceDropdowns();
  closeWorkspaceSwitcher();
  const requestToken = ++teamModeDropdownRequestToken;
  teamModeMenu.innerHTML = '<div class="mode-preference-status" role="status">正在读取当前模式…</div>';
  teamModeMenu.hidden = false;
  environmentBadge.setAttribute('aria-expanded', 'true');
  applyLanguageToDocument(teamModeMenu);
  try {
    const response = await api('/api/trading-mode');
    if (requestToken !== teamModeDropdownRequestToken || teamModeMenu.hidden) return;
    renderTeamModeDropdown(response.data, {focus});
  } catch (error) {
    if (requestToken !== teamModeDropdownRequestToken || teamModeMenu.hidden || error.handled) return;
    teamModeMenu.innerHTML = `<div class="mode-preference-status" role="alert">当前模式状态读取失败</div><button class="preference-option" type="button" data-retry-mode-switch>重新检查</button>`;
    applyLanguageToDocument(teamModeMenu);
  }
}

function moveTeamModeFocus(currentOption, key) {
  const options = [...teamModeMenu.querySelectorAll('[data-mode-option]:not(:disabled), [data-retry-mode-switch]')];
  if (!options.length) return;
  const currentIndex = Math.max(0, options.indexOf(currentOption));
  const targetIndex = key === 'Home'
    ? 0
    : key === 'End'
      ? options.length - 1
      : (currentIndex + (key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
  options[targetIndex].focus({preventScroll:true});
}

async function selectTeamMode(mode, button) {
  const data = teamModeSnapshot;
  if (!data || button.disabled) return;
  if (mode === data.execution_mode) {
    closeTeamModeDropdown({restoreFocus:true});
    return;
  }
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = localizedText('处理中…');
  try {
    const result = await api(`/api/teams/${data.team_id}/trading-mode`, {
      method:'PUT',
      body:JSON.stringify({
        mode,
        confirmation:mode === 'LIVE' ? 'I_CONFIRM_LIVE_PRODUCTION_MONEY' : 'SWITCH_TO_TESTNET',
        expected_version:data.version,
        idempotency_key:crypto.randomUUID(),
      }),
    });
    if (result?.session) session = result.session;
    closeTeamModeDropdown();
    updateEnvironmentIndicators();
    setShell(true);
    showToast(`已切换到${fmtExecutionMode(mode)}`);
    await route();
  } catch (error) {
    if (error.handled) return;
    button.textContent = originalLabel;
    button.disabled = false;
    showApiError(error);
    await openTeamModeDropdown();
  }
}

function initializeTeamModeDropdown() {
  environmentBadge.addEventListener('click', () => {
    if (teamModeMenu.hidden) openTeamModeDropdown();
    else closeTeamModeDropdown({restoreFocus:true});
  });
  environmentBadge.addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault();
    openTeamModeDropdown({focus:event.key === 'ArrowUp' ? 'last' : 'selected'});
  });
  teamModeMenu.addEventListener('click', event => {
    const retry = event.target.closest('[data-retry-mode-switch]');
    if (retry) {
      openTeamModeDropdown();
      return;
    }
    const option = event.target.closest('[data-mode-option]');
    if (option) selectTeamMode(option.dataset.modeOption, option);
  });
  teamModeMenu.addEventListener('keydown', event => {
    const option = event.target.closest('[data-mode-option], [data-retry-mode-switch]');
    if (!option) return;
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      moveTeamModeFocus(option, event.key);
    } else if (['Enter', ' '].includes(event.key)) {
      event.preventDefault();
      option.click();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeTeamModeDropdown({restoreFocus:true});
    } else if (event.key === 'Tab') {
      closeTeamModeDropdown();
    }
  });
}

function systemHealthCard({title, status, copy, tone = 'success', meta = ''}) {
  return `<article class="system-health-card tone-${tone}"><div class="system-health-head"><span class="health-indicator" aria-hidden="true"></span><div><small>${escapeHtml(title)}</small><h2>${escapeHtml(status)}</h2></div></div><p>${escapeHtml(copy)}</p>${meta ? `<span class="system-health-meta">${escapeHtml(meta)}</span>` : ''}</article>`;
}

async function renderSystemStatus() {
  const canViewOperations = hasCapability('operations.view');
  const canViewVenues = hasCapability('venue.view');
  const healthRequest = api('/health/ready').then(() => ({ready:true})).catch(error => ({ready:false, error}));
  const opportunityHealthRequest = hasCapability('opportunity.view')
    ? api('/api/opportunities').catch(error => ({error}))
    : Promise.resolve(null);
  const [health, control, campaignsResponse, exceptionsResponse, runtime, freqtrade, opportunityHealth] = await Promise.all([
    healthRequest,
    api('/api/risk-controls').catch(error => ({error})),
    canViewOperations ? api('/api/campaigns') : Promise.resolve({data:[], as_of:null, restricted:true}),
    canViewOperations ? api('/api/campaign-exceptions') : Promise.resolve({data:[], as_of:null, restricted:true}),
    api('/api/runtime/status').catch(error => ({error})),
    api('/api/execution/freqtrade/status').catch(error => ({error})),
    opportunityHealthRequest,
  ]);
  const campaigns = campaignsResponse.data.filter(item => item.status !== 'CLOSED' && item.environment === 'LIVE');
  const details = await Promise.all(campaigns.map(item => api(`/api/campaigns/${item.campaign_id}`)));
  const liveCampaignIds = new Set(campaigns.map(item => item.campaign_id));
  const exceptions = exceptionsResponse.data.filter(item => liveCampaignIds.has(item.campaign_id));
  const codes = new Set(exceptions.map(item => item.code));
  const unknownIntents = details.flatMap(item => item.intents).filter(item => item.status === 'UNKNOWN').length;
  const dispatchingIntents = details.flatMap(item => item.intents).filter(item => item.status === 'DISPATCHING').length;
  const protectionIssues = exceptions.filter(item => item.code.startsWith('PROTECTION_'));
  const exposureIssues = exceptions.filter(item => ['POSITION_UNKNOWN','POSITION_STALE','RISK_RESERVATION_UNKNOWN'].includes(item.code));
  const reconciliationIssues = exceptions.filter(item => item.code.startsWith('RECONCILIATION_'));
  const controlAvailable = !control.error;
  const policy = control.policy || {system_state:'UNKNOWN', version:'—'};
  const gate = control.auto_add_gate || {status:'UNKNOWN', version:'—'};
  const restoreConditions = control.restore_conditions || {ready:false, blockers:[], checks:[]};
  const blockedRiskChecks = (restoreConditions.checks || []).filter(check => check.status === 'BLOCKED');
  const blockedRiskScopes = [...new Map(blockedRiskChecks
    .filter(check => check.scope?.venue)
    .map(check => [`${check.scope.environment}:${check.scope.account_id}:${check.scope.venue}`, check.scope])).values()];
  const blockedRiskScopeLabels = blockedRiskScopes.map(scope => fmtVenueLabel(scope.venue));
  const entryOpen = controlAvailable && policy.system_state === 'NORMAL' && restoreConditions.ready;
  const addOpen = entryOpen && gate.status === 'ENABLED';
  const entryStatus = !controlAvailable
    ? '风险政策未配置'
    : policy.system_state !== 'NORMAL'
      ? riskControlStatusLabel(policy.system_state)
      : !restoreConditions.ready
        ? blockedRiskScopeLabels.length
          ? `${blockedRiskScopeLabels.length} 个生产范围受阻`
          : '实时安全条件未通过'
        : addOpen
          ? '开仓与加仓逐笔检查'
          : '逐笔开仓可检查';
  const entryCopy = !controlAvailable
    ? '缺少当前风险政策或自动加仓控制，系统会阻止新增风险。'
    : policy.system_state !== 'NORMAL'
      ? `风险政策：${riskControlStatusLabel(policy.system_state)}；自动加仓：${riskControlStatusLabel(gate.status)}。`
      : !restoreConditions.ready
        ? `风险政策正常，但${blockedRiskScopeLabels.length ? ` ${blockedRiskScopeLabels.join('、')} 的` : ''}实时安全条件未通过；通过检查的范围仍需逐笔复核。自动加仓${riskControlStatusLabel(gate.status)}。`
        : `实时安全条件全部通过；每笔开仓仍由服务端重新检查。自动加仓${riskControlStatusLabel(gate.status)}。`;
  const perptape = runtime?.data?.external_boundaries?.perptape || {configured:false,status:'NOT_CONFIGURED',candidate_count:0,last_fetched_at:null,contract_version:'—'};
  const notilt = runtime?.data?.external_boundaries?.notilt || {enabled:false,gateway_available:false,configured_chains:[]};
  const telegram = runtime?.data?.external_boundaries?.telegram || {enabled:false,network_configured:false,polling:{state:'DISABLED'}};
  const telegramPolling = telegram.polling || {state:'DISABLED',last_error_code:null,last_success_at:null};
  const telegramDelivery = telegram.delivery || null;
  const telegramHealth = telegram.mode === 'DURABLE_NOTIFICATION_ROUTE' && telegramDelivery
    ? telegramDelivery
    : telegramPolling;
  const telegramHealthy = telegram.enabled && telegram.network_configured && telegramHealth.state === 'HEALTHY';
  const telegramFailureCopy = ({
    TELEGRAM_POLLING_CONFLICT:'另一个 Bot 实例正在使用同一长轮询；只保留一个生产轮询进程后重试。',
    TELEGRAM_BOT_API_CONFLICT:'Telegram 机器人接口报告会话冲突；检查是否存在另一轮询或 webhook 实例。',
    TELEGRAM_AUTH_FAILED:'Telegram 机器人接口拒绝当前凭据；由系统管理员核对机器人配置。',
    TELEGRAM_RATE_LIMITED:'Telegram 机器人接口正在限流；系统会按有界退避自动重试。',
    TELEGRAM_NETWORK_UNAVAILABLE:'当前无法连接 Telegram 机器人接口；网页端审核队列仍可使用。',
    TELEGRAM_RESPONSE_INVALID:'Telegram 机器人接口返回了无法采信的响应；机器人动作保持关闭。',
    TELEGRAM_BOT_API_REJECTED:'Telegram 机器人接口拒绝轮询请求；由系统管理员检查机器人运行实例。',
  })[telegramHealth.last_error_code] || (telegram.mode === 'DURABLE_NOTIFICATION_ROUTE'
    ? '通知路由尚未完成一次成功投递；网页端审核队列仍是权威入口。'
    : '机器人尚未完成一次成功轮询；网页端审核队列仍是权威入口。');
  const telegramStatus = telegramHealthy
    ? '通知可用'
    : telegramHealth.state === 'DEGRADED'
      ? '通知受阻'
      : telegram.enabled
        ? telegram.mode === 'DURABLE_NOTIFICATION_ROUTE' ? '等待首次投递' : '等待首次轮询'
        : '尚未启用';
  const telegramHealthyCopy = telegram.mode === 'DURABLE_NOTIFICATION_ROUTE'
    ? telegramDelivery.interactive_review_ready
      ? 'Telegram 私聊通知最近一次投递成功；批准和拒绝仍需二次确认并写入统一审计。'
      : 'Telegram 通知最近一次投递成功；审核动作仅在绑定审核人身份后提供。'
    : 'Telegram 私聊机器人最近一次长轮询成功；批准和拒绝仍需二次确认并写入统一审计。';
  const connections = runtime?.data?.connections || {};
  const perptapeAvailable = Boolean(connections.PERPTAPE?.available);
  const notiltConfigured = Boolean(connections.NOTILT?.available);
  const perptapeStatus = perptapeAvailable
    ? '数据可用'
    : perptape.status === 'STALE'
      ? '数据已过期'
      : perptape.status === 'WAITING'
        ? '等待首次同步'
        : perptape.status === 'ON_DEMAND'
          ? '按需读取，尚未验证'
          : perptape.configured
            ? '连接状态未知'
            : '尚未配置';
  const perptapeTone = perptapeAvailable ? 'success' : perptape.configured ? 'attention' : 'danger';
  const perptapeTransport = perptape.transport || {};
  const perptapeTransportLabel = ({
    WEBSOCKET_LIVE:'WebSocket 实时流',
    WEBSOCKET_STARTING:'WebSocket 启动中',
    POLLING_FALLBACK:'HTTPS 轮询回退',
    POLLING_ONLY:'HTTPS 定时轮询',
    WEBSOCKET_FAILED:'WebSocket 不可用',
    POLLING_FAILED:'HTTPS 轮询失败',
    WAITING:'等待首次同步',
  })[perptapeTransport.state] || '接入状态未知';
  const perptapeTransportIssue = ({
    PERPTAPE_WEBSOCKET_HEALTH_STALE:'WebSocket 健康检查已过期',
    PERPTAPE_WEBSOCKET_UNAVAILABLE:'WebSocket 当前不可用',
    PERPTAPE_POLLING_FAILED:'HTTPS 轮询失败',
  })[perptapeTransport.error_code] || perptapeTransport.error_code || '';
  const perptapeCopy = perptapeAvailable
    ? `当前接入：${perptapeTransportLabel}。已读取 ${Number(opportunityHealth?.data?.length ?? perptape.candidate_count ?? 0)} 个候选，可用于机会筛选和提案。`
    : perptape.configured
      ? `当前接入：${perptapeTransportLabel}。Perptape 已配置，但最近数据尚未形成可用连接结论；新的外部机会不可用。`
      : 'Perptape 尚未配置；人工提案仍可使用。';
  const accountBoundWorkers = Array.isArray(freqtrade?.account_bindings) ? freqtrade.account_bindings : [];
  const configuredWorkers = accountBoundWorkers.filter(worker => worker.configured);
  const requiredPairsForWorker = worker => details
    .filter(item => item.status !== 'CLOSED' && item.environment === worker.mode && item.account_id === worker.account_id && item.venue === worker.venue)
    .filter(item => item.intents.some(intent => ['READY','DISPATCHING','SENT','PARTIALLY_FILLED','UNKNOWN'].includes(intent.status)))
    .map(item => worker.venue === 'BINANCE' && item.instrument?.symbol?.endsWith('USDT') ? `${item.instrument.symbol.slice(0, -4)}/USDT:USDT` : null)
    .filter(Boolean);
  const verifiedWorkers = configuredWorkers.filter(worker => {
    const requiredPairs = requiredPairsForWorker(worker);
    return worker.status === 'VERIFIED'
      && worker.runtime?.fingerprint_verified === true
      && worker.runtime_health?.status === 'HEALTHY'
      && requiredPairs.every(pair => (worker.runtime?.whitelist || []).includes(pair));
  });
  const executionWorkerHealthy = freqtrade?.execution_worker?.status === 'HEALTHY';
  const liveOrderSendEnabled = freqtrade?.live_order_send === 'ENABLED';
  const workersReady = freqtrade?.backend === 'FREQTRADE'
    && executionWorkerHealthy
    && configuredWorkers.length > 0
    && verifiedWorkers.length === configuredWorkers.length;
  const configuredVenueCounts = configuredWorkers.reduce((counts, worker) => {
    counts[worker.venue] = Number(counts[worker.venue] || 0) + 1;
    return counts;
  }, {});
  const configuredVenueSummary = Object.entries(configuredVenueCounts)
    .map(([venue, count]) => `${fmtVenueLabel(venue)} ${count}`)
    .join('、');
  const executionCopy = workersReady
    ? `${verifiedWorkers.length} 个精确账户 Worker 的运行指纹、模式和最近探针均已验证${configuredVenueSummary ? `：${configuredVenueSummary}` : ''}。`
    : freqtrade?.error
      ? friendlyApiError(freqtrade.error)
      : configuredWorkers.length
        ? `${verifiedWorkers.length} / ${configuredWorkers.length} 个精确账户 Worker 同时满足当前指纹、模式和运行探针；其余绑定禁止执行。`
        : '当前没有可由执行进程加载的精确账户 Freqtrade Worker。';
  const workerScopeRows = configuredWorkers.map(worker => {
    const runtimeState = worker.runtime || {};
    const requiredPairs = requiredPairsForWorker(worker);
    const missingPairs = requiredPairs.filter(pair => !(runtimeState.whitelist || []).includes(pair));
    const conditionReady = worker.status === 'VERIFIED' && runtimeState.fingerprint_verified === true && worker.runtime_health?.status === 'HEALTHY' && !missingPairs.length;
    return `<tr><td data-label="账户 / Venue"><b>${escapeHtml(worker.account_id)} · ${escapeHtml(fmtVenueLabel(worker.venue))}</b><br><span class="subtle">${escapeHtml(worker.name || '未命名 Worker')}</span></td><td data-label="运行条件"><span class="status-pill ${conditionReady ? 'status-APPROVED' : 'status-DENY'}">${conditionReady ? '当前条件通过' : '当前条件阻断'}</span><br><span class="subtle">${escapeHtml(worker.mode)} · ${runtimeState.dry_run === false ? 'LIVE' : '非 LIVE'} · ${escapeHtml(runtimeState.worker_state || 'UNKNOWN')} · 指纹${runtimeState.fingerprint_verified ? '已验证' : '未验证'}</span></td><td data-label="受控能力">Force Entry ${runtimeState.force_entry_enabled ? '已启用' : '未启用'}<br>Position Adjustment ${runtimeState.position_adjustment_enabled ? '已启用' : '未启用'}</td><td data-label="白名单 / 当前任务">${escapeHtml((runtimeState.whitelist || []).join('、') || '尚无已验证白名单')}${requiredPairs.length ? `<br><span class="subtle">当前任务：${escapeHtml(requiredPairs.join('、'))}${missingPairs.length ? `；缺少 ${escapeHtml(missingPairs.join('、'))}` : '；全部允许'}</span>` : '<br><span class="subtle">当前没有待执行任务交易对</span>'}</td><td data-label="最近探针">${fmtDate(worker.runtime_health?.checked_at)}${worker.runtime_health?.error_code ? `<br><code>${escapeHtml(worker.runtime_health.error_code)}</code>` : ''}</td></tr>`;
  }).join('');
  const workerScopePanel = configuredWorkers.length ? `<section><div class="section-heading"><div><p class="eyebrow">精确执行范围</p><h2>账户 Worker 的真实 LIVE 条件</h2></div><span class="status-pill">${verifiedWorkers.length} / ${configuredWorkers.length} 通过</span></div><div class="table-wrap"><table><thead><tr><th>账户 / Venue</th><th>运行条件</th><th>受控能力</th><th>白名单 / 当前任务</th><th>最近探针</th></tr></thead><tbody>${workerScopeRows}</tbody></table></div></section>` : '';
  const tradingConnectionsReady = Boolean(connections.BINANCE?.available && connections.HYPERLIQUID?.available);
  const activeMonitoring = campaigns.length > 0;
  const overallTone = !health.ready || !controlAvailable ? 'danger' : exceptions.length || !entryOpen || !perptapeAvailable || !workersReady || !tradingConnectionsReady || !telegramHealthy ? 'attention' : activeMonitoring ? 'success' : 'neutral';
  const monitoringCards = canViewOperations ? [
    systemHealthCard({title:'减仓与退出', status:!activeMonitoring ? '当前无运行中任务' : unknownIntents ? '部分交易任务需要先对账' : dispatchingIntents ? '原派发等待查询确认' : '路径可用', tone:!activeMonitoring ? 'neutral' : (unknownIntents || dispatchingIntents) ? 'attention' : 'success', copy:!activeMonitoring ? '当前没有需要减仓或退出的交易任务。' : unknownIntents ? `${unknownIntents} 个订单结果未知，相关交易任务禁止重复动作；其他已知仓位仍可减仓或退出。` : dispatchingIntents ? `${dispatchingIntents} 个订单已持久派发，只允许查询原结果，不会再次发送。` : '即使新增风险受限，受控减仓与退出仍然可用。', meta:`${campaigns.length} 个运行中交易任务`}),
    systemHealthCard({title:'止损与保护监控', status:!activeMonitoring ? '当前无监控对象' : protectionIssues.length ? `${protectionIssues.length} 项需要处理` : '监控正常', tone:!activeMonitoring ? 'neutral' : protectionIssues.length ? 'danger' : 'success', copy:!activeMonitoring ? '有交易任务进入持仓后，系统会持续检查止损和保护覆盖。' : protectionIssues.length ? '检测到保护缺失、过期、未知或覆盖不足。' : '运行中的交易任务没有保护异常。', meta:`数据截止 ${fmtDate(exceptionsResponse.as_of)}`}),
    systemHealthCard({title:'风险敞口监控', status:!activeMonitoring ? '当前无监控对象' : exposureIssues.length ? `${exposureIssues.length} 项敞口不确定` : '监控正常', tone:!activeMonitoring ? 'neutral' : exposureIssues.length ? 'danger' : 'success', copy:!activeMonitoring ? '有交易任务进入运行后，系统会检查仓位和风险占用。' : exposureIssues.length ? '仓位或风险占用存在未知或过期数据，系统会阻止新增风险。' : '当前没有仓位未知、仓位过期或风险占用未知。', meta:`${exceptions.length} 项总阻断`}),
    systemHealthCard({title:'对账监控', status:!activeMonitoring ? '暂无对账对象' : reconciliationIssues.length ? `${reconciliationIssues.length} 项未一致` : '对账一致', tone:!activeMonitoring ? 'neutral' : reconciliationIssues.length ? 'attention' : 'success', copy:!activeMonitoring ? '当前没有运行中的交易任务需要对账。' : reconciliationIssues.length ? '至少一个权限范围存在差异、未知、过期或需要人工处理。' : '运行中的交易任务没有派生对账异常。', meta:'只有计算结果为“对账一致”才可作为恢复依据'}),
  ] : [
    systemHealthCard({title:'交易任务监控', status:'当前身份不读取任务详情', tone:'neutral', copy:'系统状态仍展示风险政策、外部连接、执行底座和通知健康；运行任务、保护与对账详情由风险管理人员查看。', meta:'未读取任务数据，不能据此判断任务数量或异常数量'}),
  ];
  const cards = [
    systemHealthCard({title:'核心服务', status:health.ready ? '服务可用' : '服务不可用', tone:health.ready ? 'success' : 'danger', copy:health.ready ? '业务数据库和交易服务运行正常。' : '核心服务检查失败；不能把缺失响应当成正常。', meta:'数据缺失时自动阻止交易'}),
    systemHealthCard({title:'开仓与加仓', status:entryStatus, tone:entryOpen ? (addOpen ? 'success' : 'attention') : 'danger', copy:entryCopy, meta:restoreConditions.ready ? '每笔新增风险仍会重新检查账户、交易所与授权' : `${restoreConditions.blockers?.length || blockedRiskChecks.length} 项实时条件待处理；查看风险控制了解精确原因`}),
    systemHealthCard({title:'自动执行进程', status:executionWorkerHealthy ? 'Execution Worker 健康' : 'Execution Worker 心跳异常', tone:executionWorkerHealthy ? 'success' : 'danger', copy:executionWorkerHealthy ? `最近心跳覆盖 ${Number(freqtrade?.execution_worker?.healthy_binding_count || 0)} 个精确账户绑定。` : '数据库中没有新鲜的 Execution Worker 账户心跳；不会把 API 进程环境变量当成独立进程状态。', meta:'来源：数据库运行心跳'}),
    systemHealthCard({title:'Freqtrade Worker', status:workersReady ? '精确账户运行条件已验证' : '精确账户运行条件未通过', tone:workersReady ? 'success' : 'danger', copy:executionCopy, meta:'身份、LIVE/TESTNET 模式、白名单、Force Entry、仓位调整和运行指纹逐项核验'}),
    systemHealthCard({title:'生产订单 Gate', status:liveOrderSendEnabled ? 'LIVE_ORDER_SEND 已启用' : `LIVE_ORDER_SEND ${fmtStatus(freqtrade?.live_order_send || 'UNKNOWN')}`, tone:liveOrderSendEnabled ? 'attention' : 'danger', copy:liveOrderSendEnabled ? '数据库 Gate 允许已审核 Intent 进入逐笔执行检查；它不会绕过 RBAC、独立审核、风控、对账、租约或 Worker 探针。' : '数据库 Gate 当前不允许发送生产订单。', meta:`来源：${freqtrade?.gate_source === 'DATABASE' ? '数据库' : '未知'} · 更新 ${fmtDate(freqtrade?.live_order_send_updated_at)}`}),
    systemHealthCard({title:'Telegram 审核通知', status:telegramStatus, tone:telegramHealthy ? 'success' : 'attention', copy:telegramHealthy ? telegramHealthyCopy : telegramFailureCopy, meta:telegramHealthy ? `最近成功 ${fmtDate(telegramHealth.last_success_at)}` : '网页端审核队列保持可用；资金、订单、风险开关与权限操作不对 Telegram 机器人开放'}),
    systemHealthCard({title:'Perptape 机会源', status:perptapeStatus, tone:perptapeTone, copy:perptapeCopy, meta:`只读 · 最近数据 ${fmtDate(perptape.last_fetched_at)}${perptapeTransportIssue ? ` · ${perptapeTransportIssue}` : ''}`}),
  ].join('');
  const monitoringIssueCount = protectionIssues.length + exposureIssues.length + reconciliationIssues.length + unknownIntents + dispatchingIntents;
  const monitoringOpen = activeMonitoring || monitoringIssueCount > 0 || !canViewOperations;
  const monitoringSummary = !canViewOperations
    ? '当前身份未读取交易任务详情'
    : monitoringIssueCount
      ? `${monitoringIssueCount} 项监控结果需要处理`
      : activeMonitoring
        ? '运行中的交易任务正在持续监控'
        : '当前无运行中任务，4 项监控检查已收起';
  const monitoringSummaryMarkup = canViewOperations && !activeMonitoring && !monitoringIssueCount
    ? '<small><span class="when-closed">当前无运行中任务，4 项监控检查已收起</span><span class="when-open">当前无运行中任务，正在显示 4 项监控检查</span></small>'
    : `<small>${escapeHtml(monitoringSummary)}</small>`;
  const monitoringDisclosure = `<details class="card create-member-panel system-monitoring-disclosure" ${monitoringOpen ? 'open' : ''}><summary><span><b>仓位、保护与对账监控</b>${monitoringSummaryMarkup}</span><strong><span class="when-closed">查看详情</span><span class="when-open">收起</span></strong></summary><div class="system-health-grid">${monitoringCards.join('')}</div></details>`;
  const connectionLabels = {
    BINANCE:['币安','生产账户','/venues?venue=BINANCE','查看账户数据 →'],
    HYPERLIQUID:['Hyperliquid','生产账户','/venues?venue=HYPERLIQUID','查看账户数据 →'],
    OKX:['OKX','生产账户','/venues?venue=OKX','查看账户数据 →'],
    BYBIT:['Bybit','生产账户','/venues?venue=BYBIT','查看账户数据 →'],
    PERPTAPE:['突破榜单','市场机会','/opportunities','查看机会 →'],
    NOTILT:['链上资金库','生产资金','/capital','查看资金 →'],
  };
  const connectionRows = Object.entries(connectionLabels).map(([key, label]) => {
    const state = connections[key] || {
      available:false,
      category:'NOT_YET_VERIFIED',
      reason:'尚无可核验的只读连接结论。',
      owner_role:key === 'NOTILT' ? '资金管理员' : '系统管理员',
      next_action:'启动只读同步并等待一次探针完成。',
      checked_at:null,
      write_process_enabled:false,
    };
    const destinationCapability = key === 'NOTILT'
      ? 'capital.view'
      : key === 'PERPTAPE'
        ? 'opportunity.view'
        : 'venue.view';
    const restrictedOwner = key === 'NOTILT'
      ? '由资金管理员处理'
      : key === 'PERPTAPE'
        ? '由机会创建者查看'
        : '由风险管理人员查看';
    const action = hasCapability(destinationCapability)
      ? `<a class="text-button" href="${label[2]}" data-link>${label[3]}</a>`
      : `<span class="subtle">${restrictedOwner}</span>`;
    const capability = fmtConnectionCapability(key, state);
    const categoryLabel = fmtConnectionCategory(state.category);
    const errorEvidence = state.error_code ? `<details class="venue-technical-detail"><summary>技术分类</summary><code translate="no">${escapeHtml(state.error_code)}</code></details>` : '';
    const probeEvidence = [
      `${currentLanguage === 'en' ? 'Latest probe: ' : '最近探针：'}${fmtDate(state.checked_at)}`,
      state.last_success_at ? `${currentLanguage === 'en' ? 'Latest success: ' : '最近成功：'}${fmtDate(state.last_success_at)}` : (currentLanguage === 'en' ? 'No successful probe yet' : '尚无成功记录'),
      state.retry_at ? `${currentLanguage === 'en' ? 'Next automatic retry: ' : '下次自动重试：'}${fmtDate(state.retry_at)}` : null,
    ].filter(Boolean).join(' · ');
    const ownership = currentLanguage === 'en'
      ? `Owner: ${translateEnglishText(state.owner_role)} · Next: ${fmtConnectionNextAction(state)}`
      : `负责：${state.owner_role} · 下一步：${fmtConnectionNextAction(state)}`;
    return `<tr><td data-label="数据源"><b>${label[0]}</b>${errorEvidence}</td><td data-label="读取状态与处理建议"><span class="status-pill ${state.available ? 'status-APPROVED' : ''}">${state.available ? (currentLanguage === 'en' ? 'Read-only connected' : '只读已连接') : escapeHtml(categoryLabel)}</span><br><span class="subtle">${escapeHtml(fmtConnectionReason(state))}</span><br><span class="subtle">${escapeHtml(probeEvidence)}</span><br><span class="subtle">${escapeHtml(ownership)}</span></td><td data-label="运行范围">${label[1]}</td><td data-label="可用能力">${escapeHtml(capability)}</td><td data-label="下一步">${action}</td></tr>`;
  }).join('');
  const availableSources = Object.keys(connectionLabels).filter(key => connections[key]?.available).length;
  const connectionSourceCount = Object.keys(connectionLabels).length;
  const executionVerdictTitle = !workersReady
    ? '只读控制台可用，但 Freqtrade 执行底座尚未就绪'
    : 'Freqtrade 执行底座已就绪，但交易所只读连接受限';
  const executionVerdictCopy = !workersReady
    ? `${executionWorkerHealthy ? 'Execution Worker 心跳正常，但至少一个精确账户 Worker 条件未通过' : 'Execution Worker 尚无新鲜数据库心跳'}；${!tradingConnectionsReady ? '至少一个交易所只读连接也受限' : '交易所只读连接正常'}。系统不会把页面可访问误报为可执行交易。`
    : `Execution Worker、精确账户运行指纹和当前任务交易对白名单均已通过；LIVE_ORDER_SEND 数据库 Gate ${liveOrderSendEnabled ? '已启用' : '未启用'}，每笔订单仍需逐项通过审核、风控、对账和租约。`;
  const riskVerdictTitle = policy.system_state === 'NORMAL'
    ? blockedRiskScopeLabels.length
      ? `核心服务可用，但 ${blockedRiskScopeLabels.join('、')} 的实时开仓条件受阻`
      : '核心服务可用，但实时开仓条件未全部通过'
    : `核心服务可用，但风险政策为${riskControlStatusLabel(policy.system_state)}`;
  const blockedRiskReasonCount = blockedRiskChecks.reduce((count, check) => count + new Set((check.reason || []).filter(reason => reason !== 'CURRENT')).size, 0);
  const riskVerdictCopy = blockedRiskChecks.length
    ? `${blockedRiskChecks.length} 个默认账户范围共有 ${blockedRiskReasonCount} 项实时条件未通过；请进入风险控制逐项查看原因、负责人和下一步。通过检查的范围仍需逐笔复核；自动加仓保持关闭。`
    : '风险政策或实时生产事实尚未满足新增风险条件；每笔请求都会继续由服务端拒绝或重新校验。';
  const verdictTitle = !health.ready ? '核心服务未通过就绪检查' : !controlAvailable ? '核心服务可用，但风险政策未配置' : exceptions.length ? '核心服务可用，但存在风险阻断' : !entryOpen ? riskVerdictTitle : !workersReady || !tradingConnectionsReady ? executionVerdictTitle : !telegramHealthy ? '交易管理可用，但 Telegram 审核通知受限' : !perptapeAvailable ? '交易管理可用，但 Perptape 机会源受限' : !canViewOperations ? '核心服务与可见连接状态正常' : activeMonitoring ? '交易系统正在正常监控' : '核心服务可用，当前无运行中交易任务';
  const verdictCopy = !health.ready ? '请先恢复数据库与服务状态，不要继续依赖旧数据。' : !controlAvailable ? `${friendlyApiError(control.error)} 新增风险保持关闭。` : exceptions.length ? `发现 ${exceptions.length} 项安全异常；受影响的新增风险会保持关闭。` : !entryOpen ? riskVerdictCopy : !workersReady || !tradingConnectionsReady ? executionVerdictCopy : !telegramHealthy ? `${telegramFailureCopy} 不影响网页端审核，也不会放宽任何审核或交易边界。` : !perptapeAvailable ? `${perptapeStatus}。现有交易任务仍可管理，但新的 Perptape 机会暂不可用。` : !canViewOperations ? '当前身份未读取交易任务、保护和对账详情；页面仅对已授权的系统事实给出结论。' : activeMonitoring ? '运行中的交易任务没有检测到保护、敞口或对账阻断。' : '当前没有需要监控的交易任务；系统不会把“无监控对象”误报为“监控正常”。';
  const verdictAction = exceptions.length && canViewOperations
    ? '<a class="primary" href="/campaigns/alerts" data-link>查看运行告警</a>'
    : !controlAvailable || !entryOpen
      ? '<a class="secondary" href="/risk" data-link>查看风控中心</a>'
      : (!workersReady || !tradingConnectionsReady) && canViewVenues
        ? '<a class="secondary" href="/accounts" data-link>查看账户管理</a>'
        : !workersReady || !tradingConnectionsReady
          ? '<span class="status-pill">由系统管理员或风险管理人员处理</span>'
          : !telegramHealthy
            ? '<a class="secondary" href="/reviews" data-link>使用网页端审核</a>'
            : !perptapeAvailable && hasCapability('opportunity.view')
              ? '<a class="secondary" href="/opportunities" data-link>查看 Perptape</a>'
              : '';
  main.innerHTML = `<section class="page system-status-page"><header class="page-head"><div><p class="eyebrow">交易系统状态</p><h1>系统状态</h1><p class="lede">这里直接说明系统能否工作、哪些能力受限，以及是否需要处理。绿色表示当前证据正常；黄色表示能力受限；红色表示必须先处理；灰色表示当前没有监控对象。</p></div><div class="toolbar"><button class="secondary" data-refresh>刷新状态</button></div></header>
    <article class="home-status tone-${overallTone}"><div><p class="eyebrow">当前结论</p><h2>${escapeHtml(verdictTitle)}</h2><p>${escapeHtml(verdictCopy)}</p></div>${verdictAction}</article>
    <div class="system-health-grid">${cards}</div>
    ${workerScopePanel}
    ${monitoringDisclosure}
    <section><div class="section-heading"><div><p class="eyebrow">外部数据连接</p><h2>生产数据与资金连接</h2></div><span class="status-pill">${availableSources} / ${connectionSourceCount} 可用</span></div><div class="table-scroll-hint connection-scroll-hint" data-table-hint>左右滑动查看完整连接状态</div><div class="table-wrap connection-status-table"><table><thead><tr><th>数据源</th><th>读取状态与处理建议</th><th>运行范围</th><th>可用能力</th><th></th></tr></thead><tbody>${connectionRows}</tbody></table></div></section>
    ${codes.size ? `<section><div class="section-heading"><div><p class="eyebrow">交易任务运行告警</p><h2>需要处理的问题类型</h2></div><a class="secondary" href="/campaigns/alerts" data-link>查看运行告警</a></div><div class="exception-code-list">${[...codes].sort().map(code => `<span>${escapeHtml(explainException(code).title)}</span>`).join('')}</div></section>` : ''}
  </section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
}

async function renderCampaignFacts(mode) {
  const details = mode === 'risk' && !hasCapability('operations.view')
    ? []
    : await loadCampaignDetails();
  let riskControls = null;
  if (mode === 'risk') {
    try {
      riskControls = await api('/api/risk-controls');
    } catch (error) {
      if (error.status !== 403) throw error;
    }
  }
  const titles = {orders:'订单与成交', risk:'风控中心'};
  const visibleDetails = mode === 'risk' ? details.filter(item => item.status !== 'CLOSED') : details;
  let rows = '';
  if (mode === 'orders') rows = visibleDetails.flatMap(item => item.intents.map(intent => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(fmtIntentKind(intent.kind))}${intent.reduce_only ? ' · 只减仓' : ''}</td><td>${escapeHtml(fmtSide(intent.side))} ${fmtNumber(intent.quantity)}</td><td>${escapeHtml(fmtStatus(intent.status))}</td><td>${intent.order ? `${escapeHtml(intent.order.venue_order_id)} · ${escapeHtml(fmtStatus(intent.order.status))}` : '尚未记录交易所回执'}</td><td>${fmtDate(intent.updated_at)}</td></tr>`)).join('');
  if (mode === 'risk') rows = visibleDetails.map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td>${shortId(item.campaign_id)}</td><td>${escapeHtml(fmtStatus(item.status))}</td><td>${item.reservations.map(r => `${escapeHtml(fmtStatus(r.status))} ${fmtNumber(r.amount)}`).join(' · ') || '无预留'}</td><td>${fmtNumber(item.current_target_quantity)} · v${item.target_version}</td><td>${escapeHtml(fmtStatus(item.target_urgency || '—'))}</td><td>${escapeHtml(item.reconciliation ? fmtStatus(item.reconciliation.status) : '未对账')}</td></tr>`).join('');
  const headers = mode === 'orders' ? '<th>交易任务</th><th>意图</th><th>方向 / 数量</th><th>状态</th><th>交易所订单</th><th>更新时间</th>' : '<th>交易任务</th><th>状态</th><th>风险预留</th><th>目标</th><th>紧迫度</th><th>对账</th>';
  const pageLede = mode === 'risk'
    ? '当前团队风险政策与每个生产账户范围分开判断；政策正常不代表所有账户都能新增风险。阻塞项会明确列出原因、负责人和下一步。'
    : '这里显示当前确认的数据；能够重新计算的汇总会按最新数据生成。';
  main.innerHTML = `<section class="page"><header class="page-head"><div><p class="eyebrow">生产交易数据</p><h1>${titles[mode]}</h1><p class="lede">${pageLede}</p></div></header>
    ${mode === 'risk' ? renderRiskControlPanel(riskControls) : ''}
    ${mode === 'risk' && roleNames().includes('SYSTEM_ADMIN') ? `<div class="form-panel compact-form risk-admin-controls"><div class="card-heading"><div><p class="eyebrow">加减仓与全局暂停</p><h2>管理员直接控制</h2></div><span class="status-pill">直接修改</span></div><p class="safety-note">关闭/恢复自动加仓与暂停/解除风险暂停彼此独立。任何恢复都会重新校验服务端状态，不会复活旧交易授权。</p><div class="toolbar"><button class="${riskControls?.auto_add_gate?.status === 'DISABLED' ? 'primary' : 'danger'}" data-${riskControls?.auto_add_gate?.status === 'DISABLED' ? 'enable' : 'disable'}-global-add>${riskControls?.auto_add_gate?.status === 'DISABLED' ? '恢复自动加仓' : '关闭自动加仓'}</button><button class="${riskControls?.policy?.system_state === 'NORMAL' ? 'danger' : 'primary'}" data-${riskControls?.policy?.system_state === 'NORMAL' ? 'pause' : 'unpause'}-new-risk>${riskControls?.policy?.system_state === 'NORMAL' ? '暂停所有风险' : '解除风险暂停'}</button></div></div><div style="height:16px"></div>` : ''}
    ${rows ? `<div class="table-wrap"><table><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>` : mode === 'risk' ? (hasCapability('operations.view') ? '<section class="empty-state compact-empty-state"><div><h2>当前没有运行中的风险任务</h2><p>已结束任务不会占用当前风险工作区；可前往交易历史查看完整记录。</p><a class="secondary" href="/campaigns" data-link>查看交易历史</a></div></section>' : '') : '<section class="empty-state"><div><h2>当前没有可展示的数据</h2></div></section>'}</section>`;
  bindLinkedRows();
  if (mode === 'risk') await bindRiskControlActions();
  document.querySelector('[data-disable-global-add]')?.addEventListener('click', (event) => campaignAction('/api/operations/auto-add/disable', {reason:'administrator disabled AUTO_ADD from Web', idempotency_key:crypto.randomUUID()}, {
    button:event.currentTarget,
    successMessage:'全局自动加仓已关闭；现有仓位与退出能力不受影响',
    confirm:{title:'关闭全局自动加仓？', message:'确认后，所有交易任务都不能继续使用剩余加仓次数。该入口只会收紧风险，无法在此页重新开启。', confirmLabel:'关闭自动加仓'},
  }));
  document.querySelector('[data-pause-new-risk]')?.addEventListener('click', (event) => campaignAction('/api/operations/pause-new-risk', {reason:'administrator paused new risk from Web', idempotency_key:crypto.randomUUID()}, {
    button:event.currentTarget,
    successMessage:'系统已切换为“仅允许减仓”；只能收紧风险和退出',
    confirm:{title:'暂停所有新增风险？', message:'确认后，系统将只允许减仓。已有仓位仍可减仓或退出，但新的初仓和加仓会被拒绝。', confirmLabel:'切换为仅允许减仓'},
  }));
  document.querySelector('[data-enable-global-add]')?.addEventListener('click', (event) => campaignAction('/api/operations/auto-add/enable', {reason:'administrator enabled AUTO_ADD from Web', idempotency_key:crypto.randomUUID()}, {
    button:event.currentTarget,
    successMessage:'自动加仓全局开关已恢复；每次加仓仍需逐笔通过风险、保护与对账检查',
    confirm:{title:'恢复自动加仓？', message:'只恢复全局开关，不会复活旧授权或旧加仓次数；每次加仓仍需重新授权并通过实时检查。', confirmLabel:'确认恢复自动加仓'},
  }));
  document.querySelector('[data-unpause-new-risk]')?.addEventListener('click', async event => {
    const trigger = event.currentTarget;
    let grant;
    try {
      const status = await api('/api/risk-controls');
      const policy = status.policy;
      grant = await confirmStepUpAction({title:'解除风险暂停？', message:'系统会再次验证全部生产账户条件并创建新的 NORMAL 政策；旧交易授权不会恢复。', confirmLabel:'验证身份并解除暂停', action:'risk.restore.direct', objectId:policy.policy_id, objectVersion:policy.revision});
    } catch (error) { showApiError(error); return; }
    if (!grant) return;
    await withPending(trigger, '验证中…', async () => {
      try {
        await api('/api/risk-controls/restore-direct', {method:'POST', body:JSON.stringify({reason:'administrator resumed new risk from Web', idempotency_key:crypto.randomUUID(), action_grant:grant.action_grant})});
        showToast('风险暂停已解除；旧交易授权保持失效');
        await route();
      } catch (error) { showApiError(error); }
    });
  });
}

const CAPITAL_CHART_RANGE_MAX = 1000;
const DIRECT_CAPITAL_PATHS = [
  {path:'VAULT_TO_BINANCE', from:'链上金库', to:'币安', badge:'二选一', action:'检查转入币安条件', copy:'选择 NoTilt Vault 或 Safe Spending Limits，再按对应额度规则转入币安。', steps:['选择链上金库','实时额度预检','人控确认','进入币安']},
  {path:'VAULT_TO_HYPERLIQUID', from:'链上金库', to:'Hyperliquid', badge:'二选一', action:'检查转入 Hyperliquid 条件', copy:'选择 NoTilt Vault 或 Safe Spending Limits，先到授权自有地址，再进入 Hyperliquid。', steps:['选择链上金库','额度预检','到达自有地址','合约入金']},
  {path:'BINANCE_TO_VAULT', from:'币安', to:'链上金库', badge:'二选一', action:'检查币安回流条件', copy:'回流到用户选择的 NoTilt Vault 或 Safe Smart Account。', steps:['提现预检','授权地址','目标入金','回执验证']},
  {path:'HYPERLIQUID_TO_VAULT', from:'Hyperliquid', to:'链上金库', badge:'二选一', action:'检查 Hyperliquid 回流条件', copy:'优先使用 Hyperliquid 当前官方 CCTP 路由，固定扣除 0.2 USDC 协议费；仅在官方路由明确回退时使用 Legacy Bridge。', steps:['官方提现','Arbitrum 确认','金库到账','回执验证']},
];
let capitalTrendVisibility = {TOTAL:true};
let capitalChartRangeValue = CAPITAL_CHART_RANGE_MAX;
let capitalChartResizeObserver = null;
let capitalChartOverlayAbortController = null;
const OCCUPIED_CAPITAL_TRANSFER_STATUSES = new Set([
  'SOURCE_RESERVED', 'SUBMITTED', 'IN_FLIGHT', 'DESTINATION_CONFIRMED',
  'UNKNOWN', 'MANUAL_REQUIRED',
]);
async function renderCampaignDetail(id) {
  const item = await api(`/api/campaigns/${id}`);
  if (!['LIVE','TESTNET'].includes(item.environment)) {
    main.innerHTML = '<section class="page"><section class="empty-state"><div><h1>该交易记录不属于当前控制台</h1><p>这里只展示交易所测试环境或生产环境的交易历史。</p><a class="primary" href="/campaigns" data-link>返回交易历史</a></div></section></section>';
    return;
  }
  const canOperate = roleNames().includes('OPERATOR') || roleNames().includes('SYSTEM_ADMIN');
  const canRecordSyntheticFacts = false;
  const active = item.intents.find(intent => ['READY','DISPATCHING','SENT','PARTIALLY_FILLED','UNKNOWN'].includes(intent.status));
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
      addCandidateError = friendlyApiError(error);
    }
  }
  const nextStep = campaignNextStep(item, active, {canOperate, canRecordSyntheticFacts, positionCurrent, hasPosition, flatKnown, protectionReady, reconciliationMatched, exitTerminal, riskClosable});
  const positionTruth = !item.position ? '未同步' : !positionCurrent ? '需要重新同步' : `${fmtStatus(item.position.fact_status)} · ${fmtNumber(item.position.quantity)}`;
  const protectionTruth = !positionCurrent ? '等待仓位同步' : !hasPosition ? '当前无仓位' : protectionReady ? `完整覆盖 · ${fmtNumber(item.protection.quantity)}` : item.protection ? fmtStatus(item.protection.status) : '尚无保护';
  const activeTruth = active ? `${fmtIntentKind(active.kind)} · ${fmtStatus(active.status)}` : '无进行中意图';
  const reconciliationTruth = item.reconciliation ? `${fmtStatus(item.reconciliation.status)} · ${fmtDate(item.reconciliation.completed_at)}` : '尚未运行';
  const environmentTools = '';
  const closedFlat = isClosedFlatCampaign(item);
  const pnlLabel = closedFlat ? '最终盈亏' : '当前总盈亏';
  const positionQuantityLabel = closedFlat ? `0（${localizedText('已平仓')}）` : item.position ? fmtNumber(item.position.quantity) : '未知';
  const averageEntryLabel = flatKnown ? '—（当前无仓位）' : item.position ? fmtNumber(item.position.average_entry_price) : '—';
  const protectionStateLabel = flatKnown ? '保护不适用（当前无仓位）' : item.protection ? fmtStatus(item.protection.status) : '尚无数据';
  const management = closedFlat ? '' : managementPanel(item, addCandidates, addCandidateError, canOperate, canAddNow, active, protectionReady, reconciliationMatched);
  main.innerHTML = `<section class="page campaign-detail"><header class="page-head"><div><p class="eyebrow">${escapeHtml(fmtEnvironment(item.environment, true))} · ${escapeHtml(item.venue)}</p><h1>${escapeHtml(item.instrument?.symbol || '交易记录')} ${shortId(item.campaign_id)}</h1><p class="lede"><b class="status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</b> · ${escapeHtml(fmtDirection(item.direction))} · ${closedFlat ? localizedText('已平仓') : `当前目标 ${fmtNumber(item.current_target_quantity)}`}</p></div><a class="secondary" href="/campaigns" data-link>返回交易历史</a></header>
    <article class="campaign-command tone-${nextStep.tone}"><div><p class="eyebrow">当前唯一推荐动作</p><h2>${escapeHtml(nextStep.title)}</h2><p>${escapeHtml(nextStep.copy)}</p></div><div class="campaign-command-action">${nextStep.action}<div class="form-error" id="campaign-action-error"></div></div></article>
    <div class="campaign-truth-grid"><div class="${item.position && !positionCurrent ? 'truth-danger' : ''}"><small>当前仓位</small><b>${escapeHtml(closedFlat ? localizedText('已平仓') : positionTruth)}</b><span>${item.position ? `${closedFlat ? '确认于' : '上次'} ${fmtDate(item.position.observed_at)}` : '等待交易所仓位数据'}</span></div><div class="${positionCurrent && hasPosition && !protectionReady ? 'truth-danger' : ''}"><small>原生保护</small><b>${escapeHtml(protectionTruth)}</b><span>${!hasPosition ? '当前无仓位，无需保护' : item.protection ? `触发价 ${fmtNumber(item.protection.trigger_price)} · ${fmtDate(item.protection.observed_at)}` : '有仓位时必须确认足额覆盖'}</span></div><div><small>进行中操作</small><b>${escapeHtml(activeTruth)}</b><span>${active ? `${fmtSide(active.side)} ${fmtNumber(active.quantity)} · ${shortId(active.intent_id)}` : '不会与新动作冲突'}</span></div><div class="${item.reconciliation && !reconciliationMatched ? 'truth-danger' : ''}"><small>最近对账</small><b>${escapeHtml(reconciliationTruth)}</b><span>${item.reconciliation?.differences?.length ? `${item.reconciliation.differences.length} 项差异待处理` : reconciliationMatched ? '晚于当前仓位与操作记录' : '需要在最新数据后重跑'}</span></div></div>
    <div class="stats"><div class="stat"><small>已实现盈亏</small><b>${escapeHtml(campaignPnlLabel(item, item.realized_pnl))}</b></div><div class="stat"><small>未实现盈亏</small><b>${escapeHtml(campaignPnlLabel(item, item.unrealized_pnl))}</b></div><div class="stat"><small>${pnlLabel}</small><b>${escapeHtml(campaignPnlLabel(item, item.final_pnl))}</b></div><div class="stat"><small>风险目标</small><b style="font-size:14px">${closedFlat ? localizedText('已平仓') : `${fmtNumber(item.current_target_quantity)} · ${escapeHtml(item.target_urgency ? fmtStatus(item.target_urgency) : '尚未设置')}`}</b></div></div>
    <div class="campaign-command-layout"><div class="stack"><article class="card"><div class="card-heading"><div><p class="eyebrow">执行记录</p><h2>订单操作与成交记录</h2></div><span class="status-pill">${item.intents.length} 个操作 · ${item.fills.length} 笔成交</span></div>${item.intents.length ? item.intents.map(intent => intentCard(intent, item.environment)).join('') : '<p class="subtle">尚无订单操作。</p>'}</article><article class="card"><div class="card-heading"><div><p class="eyebrow">仓位数据</p><h2>仓位与风险保护</h2></div><span class="status-pill ${protectionReady ? 'status-APPROVED' : positionCurrent && hasPosition ? 'status-DENY' : ''}">${!positionCurrent ? '仓位待同步' : hasPosition ? (protectionReady ? '保护完整' : '需要保护') : '当前无仓位'}</span></div><dl class="definition-grid spacious">${definition('仓位数量', positionQuantityLabel)}${definition('平均入场', averageEntryLabel)}${definition('标记价', item.position ? fmtNumber(item.position.mark_price) : '—')}${definition('仓位更新时间', fmtDate(item.position?.observed_at))}${definition('保护状态', protectionStateLabel)}${definition('保护数量', item.protection ? fmtNumber(item.protection.quantity) : '—')}${definition('保护触发价', item.protection ? fmtNumber(item.protection.trigger_price) : '—')}${definition('保护更新时间', fmtDate(item.protection?.observed_at))}</dl></article>${canCreatePositionAction ? `<article class="card risk-reduction-card"><div class="card-heading"><div><p class="eyebrow">降低风险</p><h2>减仓与退出随时可用</h2></div><span class="status-pill">只减险</span></div><p class="subtle">无论新增风险是否暂停，都可以把目标降到更小数量或 0；系统只生成只减仓操作。</p>${targetForm(item)}</article>` : ''}${environmentTools}</div>
      <aside class="stack"><article class="card"><div class="card-heading"><div><p class="eyebrow">风险目标</p><h2>${closedFlat ? '风险预留与平仓结果' : '风险预留与唯一目标'}</h2></div><span class="status-pill">版本 ${item.target_version}</span></div>${item.reservations.map(r => `<div class="callout"><b>${escapeHtml(fmtStatus(r.status))}</b> · ${fmtNumber(r.amount)} ${escapeHtml(item.instrument?.collateral_currency || '')}</div>`).join('') || '<p class="subtle">无风险预留。</p>'}<dl class="definition-grid">${definition(closedFlat ? '最终仓位' : '目标数量', closedFlat ? localizedText('已平仓') : fmtNumber(item.current_target_quantity))}${definition('紧迫度', item.target_urgency ? fmtStatus(item.target_urgency) : '尚未设置')}${definition('目标原因', fmtTargetReason(item.target_reason))}</dl></article>${management}<article class="card"><div class="card-heading"><div><p class="eyebrow">对账</p><h2>对账结论</h2></div><span class="status-pill ${reconciliationMatched ? 'status-APPROVED' : item.reconciliation ? 'status-DENY' : ''}">${escapeHtml(item.reconciliation ? fmtStatus(item.reconciliation.status) : '未运行')}</span></div>${item.reconciliation ? `<p class="subtle">完成于 ${fmtDate(item.reconciliation.completed_at)}</p>${item.reconciliation.differences.length ? `<ul class="exception-list">${item.reconciliation.differences.map(value => `<li>${escapeHtml(value)}</li>`).join('')}</ul>` : '<p class="success-note">订单、成交、仓位和风险保护当前一致。</p>'}` : '<p class="subtle">尚未运行对账；任何不确定结果都必须先对账。</p>'}</article></aside></div></section>`;
  bindCampaignActions(item, active);
}

function campaignNextStep(item, active, truth) {
  const canOperate = Boolean(truth.canOperate);
  const canRecordSyntheticFacts = Boolean(truth.canRecordSyntheticFacts);
  const venueFactsHref = `/venues?venue=${encodeURIComponent(item.venue)}`;
  const filledIntent = item.intents.some(intent => intent.status === 'FILLED');
  if (item.status === 'CLOSED') return {key:'done', tone:'success', title:'交易任务已完成并关闭', copy:'风险预留已释放，成交与对账记录保留在当前交易历史中。', action:'<a class="secondary" href="/campaigns" data-link>返回交易历史</a>'};
  if (active?.status === 'DISPATCHING') return {key:'dispatch', tone:'attention', title:'已持久派发，等待原结果确认', copy:'系统已冻结 Worker、账户版本和发送者范围；现在只查询同一派发，不会再次触发订单写入。', action:'<a class="secondary" href="/campaigns/alerts" data-link>查看派发告警</a><p class="microcopy">不要创建第二个意图；查询超时会转为结果未知并继续占用风险。</p>'};
  if (active?.status === 'UNKNOWN') return {key:'reconcile', tone:'danger', title:'结果不确定，先对账', copy:'风险继续占用，禁止重发、加仓或释放；先核对交易所订单、成交、仓位和保护。', action:canOperate ? '<button class="danger" data-reconcile>立即运行对账</button>' : '<p class="microcopy">等待风险管理人员运行对账。</p>'};
  if (active?.status === 'READY' && active.execution_blocker) {
    const blocker = active.execution_blocker;
    const detail = `<dl class="definition-grid">${definition('阻断码', blocker.code)}${definition('发生时间', fmtDate(blocker.occurred_at))}${definition('最近检查', fmtDate(blocker.last_checked_at))}${definition('负责组件', blocker.component || 'execution-worker')}${definition('下一步', blocker.next_action || '等待系统修复后自动有界重试')}</dl>`;
    return {key:'intent-blocked', tone:'danger', title:blocker.reason || '自动执行预发送检查未通过', copy:'订单尚未发生任何外部发送；系统已持久化真实阻断并按有界间隔重试。', action:`${detail}<a class="secondary" href="/system" data-link>查看执行系统状态</a>`};
  }
  if (active?.status === 'READY') return item.environment === 'LIVE'
    ? {key:'intent', tone:'attention', title:`正在自动推进${fmtIntentKind(active.kind)}`, copy:'审核完成后，系统会自动刷新风险、事实与对账，校验租约和 Worker 后发送；页面不再要求人工点击。', action:'<a class="secondary" href="/system" data-link>查看执行系统状态</a><p class="microcopy">超时或结果不明只查询原结果，不会重复发送。</p>'}
    : {key:'intent', tone:'attention', title:`记录${fmtIntentKind(active.kind)}发送结果`, copy:'当前只有这个意图可以推进；获取发送租约后记录模拟订单，不会连接交易所。', action:canOperate ? operationForm(active, item) : '<p class="microcopy">等待风险管理人员处理待发送意图。</p>'};
  if (active && ['SENT','PARTIALLY_FILLED'].includes(active.status)) return {key:'intent', tone:'attention', title:`确认${fmtIntentKind(active.kind)}成交结果`, copy:'先记录已确认成交，或在确实无法判断时标记为“结果未知”；不要创建第二个意图。', action:canOperate ? operationForm(active, item) : '<p class="microcopy">等待风险管理人员记录成交结果。</p>'};
  if (!truth.positionCurrent && filledIntent) return {key:'position', tone:'attention', title:'同步成交后的当前仓位', copy:'成交已经记录，但仓位数据早于最新成交或尚未确认；在此之前不能判断保护和下一步。', action:canRecordSyntheticFacts ? positionFactForm(item) : `<a class="secondary" href="${venueFactsHref}" data-link>查看交易账户</a><p class="microcopy">生产仓位只能来自交易所只读事实，不能在页面手工补写。</p>`};
  if (truth.hasPosition && !truth.protectionReady) return {key:'protection', tone:'danger', title:'先补齐足额原生保护', copy:'当前有仓位但保护缺失、未知或不足。优先确认保护；若无法保护，使用下方减仓或退出。', action:canRecordSyntheticFacts ? protectionFactForm(item) : '<a class="secondary" href="/campaigns/alerts" data-link>查看保护告警</a><p class="microcopy">生产保护只能来自受控执行与交易所事实，页面不会手工伪造。</p>'};
  if (!truth.reconciliationMatched) return {key:'reconcile', tone:'attention', title:'运行对账确认当前数据', copy:'只有意图、订单、成交、仓位和保护一致后，才适合继续管理或关闭交易任务。', action:canOperate ? '<button class="primary" data-reconcile>运行当前范围对账</button>' : '<p class="microcopy">等待风险管理人员运行对账。</p>'};
  if (truth.flatKnown && truth.exitTerminal && truth.riskClosable) return {key:'close', tone:'success', title:'仓位已清零，可以关闭交易任务', copy:'退出结果终结且对账一致；关闭后会释放剩余风险预留并把结果固定到审计记录。', action:canOperate ? '<button class="primary" data-close-campaign>关闭交易任务</button>' : '<p class="microcopy">等待风险管理人员关闭交易任务。</p>'};
  if (truth.flatKnown) return {key:'close-blocked', tone:'danger', title:'平仓事实仍缺少关闭证据', copy:'仓位虽然为 0，但退出意图或风险预留尚未终结。不要直接释放风险；先查看运行告警确认原因。', action:'<a class="secondary" href="/campaigns/alerts" data-link>查看运行告警</a>'};
  if (truth.hasPosition) return {key:'hold', tone:'success', title:'仓位已确认且保护完整', copy:'当前没有必须处理的异常。继续观察；需要时可使用下方减仓或退出，加仓仍需通过全部门控。', action:''};
  return {key:'reconcile', tone:'attention', title:'确认当前范围数据', copy:'当前没有可确认仓位；先运行对账，避免把缺失数据误认为已经平仓。', action:canOperate ? '<button class="primary" data-reconcile>运行当前范围对账</button>' : '<p class="microcopy">等待风险管理人员运行对账。</p>'};
}

function intentCard(intent, environment = 'TESTNET') { const dispatch = intent.dispatch ? `<p class="subtle">受控派发 · ${escapeHtml(intent.dispatch.backend)} · 账户版本 ${escapeHtml(intent.dispatch.account_version)} · ${fmtDate(intent.dispatch.started_at)}</p>` : ''; const blocker = intent.execution_blocker ? `<div class="callout"><b>${escapeHtml(intent.execution_blocker.code)}</b> · ${escapeHtml(intent.execution_blocker.reason || '预发送检查未通过')}<br><span class="subtle">${fmtDate(intent.execution_blocker.occurred_at)} · 最近检查 ${fmtDate(intent.execution_blocker.last_checked_at)} · ${escapeHtml(intent.execution_blocker.component || 'execution-worker')}</span><br><span class="subtle">下一步：${escapeHtml(intent.execution_blocker.next_action || '等待自动有界重试')}</span></div>` : ''; return `<div class="intent-row"><div><b>${escapeHtml(fmtIntentKind(intent.kind))} · ${escapeHtml(fmtSide(intent.side))} ${fmtNumber(intent.quantity)}</b><br><span class="subtle">${shortId(intent.intent_id)} · ${intent.reduce_only ? '只减仓' : `会增加风险 · ${fmtNumber(intent.leverage)}x`} · ${fmtDate(intent.updated_at)}</span></div><b class="status-${escapeHtml(intent.status)}">${escapeHtml(fmtStatus(intent.status))}</b></div>${blocker}${dispatch}${intent.order ? `<p class="subtle">${escapeHtml(fmtEnvironment(environment, true))}订单 ${escapeHtml(intent.order.venue_order_id)} · 已成交 ${fmtNumber(intent.order.filled_quantity)} / ${fmtNumber(intent.order.ordered_quantity)}</p>` : ''}`; }

function operationForm(intent, item) {
  const label = item.environment === 'LIVE' ? '生产执行' : '测试网执行';
  return `<div class="action-panel"><h3>${label}</h3><p class="microcopy">订单由受控发送进程使用当前环境账户凭据、发送者租约和冻结授权执行；页面不会合成订单或成交事实。</p><a class="secondary" href="/system" data-link>查看运行服务</a></div>`;
}

function targetForm(item) { return `<form id="target-form" class="action-panel"><h3>设定唯一减仓目标</h3><label>减仓后剩余数量<input name="target_quantity" type="number" step="any" min="0" max="${escapeHtml(Math.abs(Number(item.position.quantity)))}" required></label><label>处理速度<select name="urgency"><option value="NORMAL">常规</option><option value="URGENT" selected>紧急</option><option value="IMMEDIATE">立即</option></select></label><label>原因<input name="reason" value="人工降低当前风险" required></label><label>执行限价（Hyperliquid 必填）<input name="limit_price" type="number" step="any" min="0"></label><button class="primary">创建只减仓操作</button><button type="button" class="danger" data-auto-exit>评估失效价并退出</button></form>`; }

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
          ? '全局自动加仓当前关闭。'
          : '';
  const addForm = canOperate && management.allow_auto_add && Number(management.remaining_adds) > 0 && canAddNow
    ? `<form id="auto-add-form" class="action-panel"><h3>Perptape 加仓候选</h3>${candidateError ? `<p class="safety-note">${escapeHtml(candidateError)}</p>` : candidateOptions ? `<label>后续候选<select name="candidate_id">${candidateOptions}</select></label><label>加仓数量<input name="quantity" type="number" step="any" min="0" max="${escapeHtml(management.remaining_quantity)}" required></label><button class="primary" ${management.auto_add_gate !== 'ENABLED' ? 'disabled' : ''}>完成最终风控并创建加仓意图</button>` : '<p class="safety-note">当前没有同交易所、同标的、同方向的后续 Perptape 候选。</p>'}</form>`
    : `<p class="safety-note">${escapeHtml(addBlockedReason || '该交易任务没有剩余的可用加仓次数，或者原提案没有允许自动加仓。')}</p>`;
  const canDisableAdd = canOperate && management.allow_auto_add && Number(management.remaining_adds) > 0;
  return `<article class="card"><div class="card-heading"><div><p class="eyebrow">高级风险选项</p><h2>自动加仓管理</h2></div><span class="status-pill ${management.auto_add_gate === 'ENABLED' ? 'status-APPROVED' : 'status-EXPIRED'}">${escapeHtml(management.auto_add_gate === 'ENABLED' ? '全局已开启' : '全局已关闭')}</span></div><dl class="definition-grid">${definition('已用 / 可用加仓次数', `${item.authorization?.used_adds || 0} / ${item.authorization?.allowed_adds || 0}`)}${definition('剩余数量', fmtNumber(management.remaining_quantity))}${definition('提案触发价', fmtNumber(management.add_trigger_price))}</dl>${addForm}${canDisableAdd ? '<button class="danger" data-disable-campaign-add>永久关闭本交易任务的后续加仓</button>' : ''}<p class="safety-note">加仓是高级可选动作，必须在确认成交、保护足额且对账一致后进行。只有第一笔实际成交会消耗一次加仓次数；结果未知时系统会阻止新增风险。</p></article>`;
}

function bindCampaignActions(item, active) {
  document.querySelectorAll('[data-pnl]').forEach(button => button.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/pnl`, {}, {button:event.currentTarget, pendingLabel:'刷新中…', successMessage:'盈亏已按当前交易所事实重新计算'})));
  document.querySelectorAll('[data-reconcile]').forEach(button => button.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/reconcile`, {execution_scope:`${item.environment}:${item.account_id}:${item.venue}`}, {button:event.currentTarget, pendingLabel:'对账中…', successMessage:'对账已完成；结果已写入审计事实'})));
  document.querySelector('[data-close-campaign]')?.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/close`, {}, {
    button:event.currentTarget,
    pendingLabel:'关闭中…',
    successMessage:'交易任务已关闭，剩余风险预留已释放',
    confirm:{title:'关闭这个交易任务？', message:'系统会再次确认仓位已清零、没有进行中意图且最近对账一致。关闭后会释放剩余风险预留，历史记录仍可审计。', confirmLabel:'确认关闭'},
  }));
  document.querySelectorAll('[data-unknown]').forEach(button => button.addEventListener('click', () => campaignAction(`/api/intents/${active.intent_id}/unknown`, {reason:'operator marked uncertain exchange outcome'}, {
    button,
    successMessage:'意图已标记为结果未知；风险保持占用并等待人工对账',
    confirm:{title:'标记为结果未知？', message:'这会阻止与该意图相关的新增风险，并隐藏重发和释放入口。请只在交易所结果确实无法确认时继续，随后必须人工对账。', confirmLabel:'标记为结果未知'},
  })));
  document.querySelector('#target-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction(`/api/campaigns/${item.campaign_id}/managed-reductions`, {target_quantity:data.target_quantity, urgency:data.urgency, reason:data.reason, limit_price:data.limit_price || null, idempotency_key:crypto.randomUUID()}, {button:event.submitter, successMessage:'唯一只减仓目标已生成'}); });
  document.querySelector('[data-auto-exit]')?.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/automatic-exit`, {idempotency_key:crypto.randomUUID(), limit_price:document.querySelector('#target-form')?.elements.limit_price.value || null}, {
    button:event.currentTarget,
    successMessage:'自动退出评估已完成；只减仓退出意图已按提案失效价生成',
    confirm:{title:'评估并自动退出？', message:'确认后会按提案失效价评估退出条件，并可能生成新的只减仓意图。仍由对应环境 Adapter 受控执行。', confirmLabel:'评估并生成退出意图'},
  }));
  document.querySelector('#auto-add-form')?.addEventListener('submit', event => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); campaignAction(`/api/campaigns/${item.campaign_id}/auto-add`, {candidate_id:data.candidate_id, quantity:data.quantity, idempotency_key:crypto.randomUUID()}, {button:event.submitter, successMessage:'加仓候选已完成最终风控；结果已记录'}); });
  document.querySelector('[data-disable-campaign-add]')?.addEventListener('click', (event) => campaignAction(`/api/campaigns/${item.campaign_id}/auto-add/disable`, {reason:'operator disabled further Campaign AddUnits', idempotency_key:crypto.randomUUID()}, {
    button:event.currentTarget,
    successMessage:'本交易任务的后续加仓已关闭',
    confirm:{title:'关闭本交易任务的后续加仓？', message:'确认后，该交易任务剩余的可用加仓次数将不能继续使用。已有仓位仍可减仓或退出。', confirmLabel:'关闭后续加仓'},
  }));
}

async function campaignAction(path, body, {button = null, pendingLabel = '处理中…', successMessage = '模拟状态已更新', confirm = null} = {}) {
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
