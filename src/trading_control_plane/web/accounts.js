function latestVenueObservation(facts) {
  const values = [facts.equity?.observed_at, facts.reconciliation?.completed_at]
    .concat(facts.positions.map(item => item.observed_at))
    .concat(facts.orders.map(item => item.observed_at))
    .concat(facts.fills.map(item => item.executed_at))
    .concat(facts.funding.map(item => item.paid_at))
    .filter(Boolean)
    .map(value => new Date(value).getTime())
    .filter(Number.isFinite);
  return values.length ? new Date(Math.max(...values)).toISOString() : null;
}

function exchangeCredentialFields(venue, prefix = '') {
  const name = field => `${prefix}${field}`;
  if (venue === 'HYPERLIQUID') return `<label>账户地址<input name="${name('account_address')}" type="password" autocomplete="new-password" required placeholder="0x…"></label><label>API Wallet 地址（可选）<input name="${name('api_wallet_address')}" type="password" autocomplete="new-password" placeholder="0x…"></label><label>API Wallet 私钥（配置交易身份时必填）<input name="${name('api_wallet_private_key')}" type="password" autocomplete="new-password" placeholder="加密保存且不会回显"></label>`;
  const passphrase = venue === 'OKX' ? `<label>Passphrase<input name="${name('passphrase')}" type="password" autocomplete="new-password" required></label>` : '';
  return `<label>API Key<input name="${name('api_key')}" type="password" autocomplete="new-password" required></label><label>API Secret<input name="${name('api_secret')}" type="password" autocomplete="new-password" required></label>${passphrase}`;
}

const exchangeVenueLabels = {BINANCE:'Binance',HYPERLIQUID:'Hyperliquid',OKX:'OKX',BYBIT:'Bybit'};

function isFixtureExchangeAccount(item) {
  const identity = `${item.account_id || ''} ${item.label || ''}`;
  return /(^|[-_\s])(fixture|test|demo|sample|sandbox)([-_\s]|$)/i.test(identity) || /(测试|样本|演示)/.test(identity);
}

function exchangeAccountRuntimeHealth(runtime, item) {
  return runtime?.data?.source_health?.[`${item.venue}:${item.account_id}`] || null;
}

function exchangeAccountDetailConfiguration(item) {
  const credentials = item.credentials || {};
  const permissions = item.permissions || {};
  const runtimeBound = Boolean(item.runtime_binding?.bound);
  const runtimeImplemented = item.runtime_binding?.read_only_connector === 'IMPLEMENTED';
  const executionWorker = item.execution_worker || {};
  const workerConfigured = Boolean(executionWorker.configured);
  const workerStatus = executionWorker.status === 'VERIFIED' ? '已验证' : executionWorker.status === 'NOT_VERIFIED' ? '待验证' : executionWorker.status === 'UNCONFIGURED' ? '未配置' : fmtStatus(executionWorker.status);
  const verificationHelpId = `connection-help-${item.exchange_account_id}`;
  const retryAt = item.connection?.diagnostics?.next_retry_at ? new Date(item.connection.diagnostics.next_retry_at).getTime() : 0;
  const retryDeferred = Number.isFinite(retryAt) && retryAt > Date.now();
  const canRunVerification = permissions.can_verify_connection && credentials.state === 'CONFIGURED' && item.active && !retryDeferred;
  const verificationReason = !permissions.can_verify_connection ? '当前角色没有该账户范围的凭据管理权限。' : credentials.state !== 'CONFIGURED' ? '先添加加密凭据，再运行连接验证。' : !item.active ? '账户已停用，连接验证被阻断。' : retryDeferred ? `${fmtBinanceConnectionDiagnostic(item.connection)}；到期前不会重复请求 Binance。` : '只读取官方账户接口并保存连接事实，不导入余额或开启交易。';
  const verificationControl = permissions.can_verify_connection
    ? `<form class="exchange-connection-form" data-exchange-account-id="${escapeHtml(item.exchange_account_id)}" data-version="${item.version}"><button class="secondary" type="submit" aria-describedby="${verificationHelpId}" ${canRunVerification ? '' : 'disabled'}>验证只读连接</button><small id="${verificationHelpId}">${escapeHtml(verificationReason)}</small><div class="form-error" role="alert"></div></form>`
    : `<p class="subtle">${escapeHtml(verificationReason)}</p>`;
  const runtimeHelpId = `runtime-help-${item.exchange_account_id}`;
  const canConfigureRuntime = permissions.can_manage_credentials && (runtimeBound || (runtimeImplemented && item.active && item.connection?.status === 'VERIFIED'));
  const runtimeReason = !permissions.can_manage_credentials ? '当前角色没有该账户范围的凭据管理权限。' : runtimeBound ? '停用后连续读取立即失效，交易能力保持关闭。' : !runtimeImplemented ? '该交易所尚未实现连续只读适配器。' : !item.active ? '账户已停用，连续读取被阻断。' : item.connection?.status !== 'VERIFIED' ? '先完成当前凭据版本的只读连接验证。' : '使用加密凭据持续同步当前空间内的精确账户事实。';
  const runtimeControl = permissions.can_manage_credentials
    ? `<form class="exchange-runtime-form" data-exchange-account-id="${escapeHtml(item.exchange_account_id)}" data-version="${item.version}" data-enabled="${runtimeBound ? 'true' : 'false'}"><button class="secondary" type="submit" aria-describedby="${runtimeHelpId}" ${canConfigureRuntime ? '' : 'disabled'}>${runtimeBound ? '停用连续只读同步' : '启用连续只读同步'}</button><small id="${runtimeHelpId}">${escapeHtml(runtimeReason)}</small><div class="form-error" role="alert"></div></form>`
    : '';
  const credentialControl = permissions.can_manage_credentials
    ? `<form class="exchange-credential-form" data-exchange-account-id="${escapeHtml(item.exchange_account_id)}" data-venue="${escapeHtml(item.venue)}" data-version="${item.version}"><div class="field-grid">${exchangeCredentialFields(item.venue)}</div><p class="safety-note">凭据写入 AES-256-GCM 加密信封，页面和 API 只返回脱敏元数据；轮换后连接会重置为待验证。</p><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">${credentials.state === 'CONFIGURED' ? '轮换加密凭据' : '添加加密凭据'}</button></div></form>`
    : '<p class="subtle">当前身份只可查看脱敏凭据状态。</p>';
  const workerHelpId = `freqtrade-help-${item.exchange_account_id}`;
  const workerVerifyReason = !permissions.can_manage_worker ? '当前角色没有该账户范围的凭据管理权限。' : !workerConfigured ? '先保存当前账户专属 Worker，再运行无下单验证。' : !item.active ? '账户已停用，Worker 验证被阻断。' : '核对交易所、期货模式与精确账户绑定，不发送订单。';
  const workerVerify = permissions.can_manage_worker && executionWorker.supported
    ? `<form class="freqtrade-worker-verify-form" data-exchange-account-id="${escapeHtml(item.exchange_account_id)}" data-version="${item.version}"><button class="secondary" type="submit" aria-describedby="${workerHelpId}" ${workerConfigured && item.active ? '' : 'disabled'}>验证 Worker</button><small id="${workerHelpId}">${escapeHtml(workerVerifyReason)}</small><div class="form-error" role="alert"></div></form>`
    : '';
  const hip3Field = item.venue === 'HYPERLIQUID' ? `<label>HIP-3 DEX 白名单<input name="hip3_dexes" value="${escapeHtml((executionWorker.hip3_dexes || []).join(','))}" placeholder="例如 xyz（逗号分隔）"></label>` : '';
  const workerEndpoint = executionWorker.endpoint || executionWorker.default_endpoint || '';
  const workerEndpointControl = workerEndpoint
    ? `<input name="base_url" type="hidden" value="${escapeHtml(workerEndpoint)}"><details class="worker-endpoint-override"><summary><span><b>Worker 服务地址</b><small>${executionWorker.endpoint ? '当前绑定地址' : '已按部署配置自动选择'} · 不是工作空间或账户 URL</small></span><strong>修改</strong></summary><label>自定义 Worker 服务地址<input data-worker-endpoint-override type="url" maxlength="2048" value="${escapeHtml(workerEndpoint)}" placeholder="https://worker.example:8080" required></label></details>`
    : `<label>Worker 服务地址<input name="base_url" type="url" maxlength="2048" value="" placeholder="https://worker.example:8080" required><small>这是 Freqtrade Worker 的网络地址，不是工作空间或账户 URL。</small></label>`;
  const workerControl = executionWorker.supported && permissions.can_manage_worker
    ? `<form class="freqtrade-worker-form" data-exchange-account-id="${escapeHtml(item.exchange_account_id)}" data-version="${item.version}"><div class="account-worker-scope"><span>自动绑定当前账户</span><b>${escapeHtml(item.account_id)} · ${escapeHtml(exchangeVenueLabels[item.venue] || item.venue)}</b><small>空间和账户范围由当前页面自动确定，无需填写 URL。</small></div><div class="field-grid"><label>执行模式<select name="mode"><option value="DRY_RUN" ${executionWorker.mode === 'DRY_RUN' ? 'selected' : ''}>DRY_RUN</option><option value="LIVE" ${executionWorker.mode === 'LIVE' ? 'selected' : ''}>LIVE</option></select></label><label>Worker 名称<input name="name" maxlength="120" pattern="[A-Za-z0-9][A-Za-z0-9._-]*" value="${escapeHtml(executionWorker.name || `${item.venue.toLowerCase()}-${item.account_id}`)}" required></label>${workerEndpointControl}<label>控制用户名<input name="username" autocomplete="new-password" maxlength="120" required></label><label>控制密码<input name="password" type="password" autocomplete="new-password" maxlength="2048" required></label>${hip3Field}</div><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">保存 Worker 新版本</button>${workerConfigured ? '<button class="secondary" type="button" data-freqtrade-unconfigure>移除绑定</button>' : ''}</div></form>`
    : '<p class="subtle">当前身份只可查看 Worker 脱敏状态。</p>';
  const tradingEligible = item.trading?.status === 'ELIGIBLE';
  const tradingConfigured = item.trading?.status !== 'DISABLED';
  const spaceLive = session?.active_team?.execution_mode === 'LIVE' && session.active_team?.trading_enabled;
  const tradingReady = item.runtime_binding?.trading_connector === 'FREQTRADE_EXTERNAL' && executionWorker.live_ready && item.active && item.connection?.status === 'VERIFIED' && runtimeBound && spaceLive;
  const canConfigureTrading = permissions.can_manage_trading && (tradingConfigured || tradingReady);
  const tradingHelpId = `trading-help-${item.exchange_account_id}`;
  const tradingReason = !permissions.can_manage_trading ? '当前角色没有该账户范围的账户管理权限。' : tradingConfigured ? '停用会立即撤销当前账户的交易资格。' : !spaceLive ? '当前空间尚未进入真实模式并启用交易。' : item.connection?.status !== 'VERIFIED' ? '先完成只读连接验证。' : !runtimeBound ? '先启用连续只读同步。' : !executionWorker.live_ready ? '先配置并验证当前账户专属的 LIVE Freqtrade Worker。' : '只启用当前空间与账户资格；其他服务端安全开关仍独立生效。';
  const tradingControl = permissions.can_manage_trading
    ? `<form class="exchange-trading-form" data-exchange-account-id="${escapeHtml(item.exchange_account_id)}" data-version="${item.version}" data-enabled="${tradingEligible ? 'true' : 'false'}"><button class="${tradingEligible ? 'secondary' : 'danger'}" type="submit" aria-describedby="${tradingHelpId}" ${canConfigureTrading ? '' : 'disabled'}>${tradingConfigured ? '停用账户交易资格' : '启用账户交易资格'}</button><small id="${tradingHelpId}">${escapeHtml(tradingReason)}</small><div class="form-error" role="alert"></div></form>`
    : '';
  const diagnostics = item.connection?.diagnostics;
  const rateLimitHeaders = diagnostics?.rate_limit_headers || {};
  const diagnosticDetail = diagnostics ? `<details class="venue-technical-detail"><summary>查看 Binance API 失败详情</summary><dl class="definition-grid">${definition('限流类型', binanceLimitCategoryLabels[diagnostics.category] || diagnostics.category)}${definition('HTTP Status', diagnostics.http_status || '未返回')}${definition('Binance error code', diagnostics.binance_error_code ?? '未返回')}${definition('Binance message', diagnostics.binance_error_message || '未返回')}${definition('Retry-After', `${diagnostics.retry_after_seconds || 0} 秒`)}${definition('失败时间', fmtDate(diagnostics.failed_at))}${definition('建议重试时间', fmtDate(diagnostics.next_retry_at))}${definition('限流 / weight headers', Object.entries(rateLimitHeaders).map(([key, value]) => `${key}: ${value}`).join('；') || '未返回')}</dl></details>` : '';
  return `<section class="account-detail-config" aria-labelledby="account-config-title"><div class="section-heading"><div><p class="eyebrow">账户设置</p><h2 id="account-config-title">连接与凭据</h2></div><span class="subtle">账户版本 ${item.version}</span></div><div class="account-detail-config-grid"><article id="credentials" class="card account-config-card"><p class="eyebrow">凭据配置与轮换</p><h2>${credentials.state === 'CONFIGURED' ? '凭据已加密保存' : '凭据尚未配置'}</h2><p class="subtle">${credentials.state === 'CONFIGURED' ? `${credentials.key_hint || '已脱敏'} · 版本 ${credentials.version}` : '配置后仍需单独验证连接。'}</p>${credentialControl}</article><article id="connection" class="card account-config-card"><p class="eyebrow">连接与连续同步</p><h2>${item.connection?.status === 'VERIFIED' ? '只读连接已验证' : '只读连接待验证'}</h2><dl class="definition-grid">${definition('最近检查', fmtDate(item.connection?.checked_at))}${definition('最近验证成功', fmtDate(item.connection?.last_verified_at))}${definition('连接诊断', item.connection?.status === 'FAILED' ? fmtBinanceConnectionDiagnostic(item.connection) : '无')}${definition('连续同步', runtimeBound ? '已绑定' : '未绑定')}${definition('交易资格', item.trading?.enabled ? '账户级已允许' : '保持关闭')}</dl>${diagnosticDetail}${verificationControl}${runtimeControl}</article></div><details class="card account-advanced-settings"><summary><span><b>Worker 与交易资格高级设置</b><small>${escapeHtml(workerStatus)} · ${escapeHtml(fmtStatus(executionWorker.mode || 'UNCONFIGURED'))} · 仅绑定 ${escapeHtml(item.account_id)}</small></span><strong>展开</strong></summary><div class="toolbox-content"><dl class="definition-grid">${definition('绑定范围', `${item.account_id} · ${item.venue}`)}${definition('Worker', executionWorker.name || '未配置')}${definition('端点', executionWorker.endpoint || '未配置或无权查看')}${definition('最近验证', fmtDate(executionWorker.last_verified_at))}${definition('错误代码', executionWorker.error_code || '无')}${definition('真实订单发送', '本页不提供；服务端 Gate 独立控制')}</dl>${workerVerify}${workerControl}${tradingControl}</div></details></section>`;
}

function credentialPayload(form, venue) {
  const data = Object.fromEntries(new FormData(form));
  const fields = venue === 'HYPERLIQUID'
    ? ['account_address', 'api_wallet_address', 'api_wallet_private_key']
    : venue === 'OKX' ? ['api_key', 'api_secret', 'passphrase'] : ['api_key', 'api_secret'];
  return Object.fromEntries(fields.filter(field => data[field]).map(field => [field, data[field]]));
}

function bindExchangeAccountForms() {
  const createForm = document.querySelector('#exchange-account-form');
  const venueSelect = createForm?.elements.venue;
  venueSelect?.addEventListener('change', () => {
    createForm.querySelector('[data-create-credential-fields]').innerHTML = exchangeCredentialFields(venueSelect.value);
  });
  createForm?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = Object.fromEntries(new FormData(form));
    const body = {account_id:data.account_id, venue:data.venue, label:data.label || null, credentials:credentialPayload(form, data.venue), idempotency_key:crypto.randomUUID()};
    try { await withPending(event.submitter, '加密保存中…', () => api('/api/exchange-accounts', {method:'POST', body:JSON.stringify(body)})); showToast('账户已登记；连接待验证，交易保持关闭'); await route(); }
    catch (error) { showApiError(error, form.querySelector('.form-error')); }
  });
  document.querySelectorAll('.exchange-credential-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const venue = form.dataset.venue;
    const body = {credentials:credentialPayload(form, venue), expected_version:Number(form.dataset.version), idempotency_key:crypto.randomUUID()};
    try { await withPending(event.submitter, '加密保存中…', () => api(`/api/exchange-accounts/${form.dataset.exchangeAccountId}/credentials`, {method:'PUT', body:JSON.stringify(body)})); showToast('凭据版本已轮换；连接重置为待验证，交易保持关闭'); await route(); }
    catch (error) { showApiError(error, form.querySelector('.form-error')); }
  }));
  document.querySelectorAll('.exchange-connection-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const body = {expected_version:Number(form.dataset.version), idempotency_key:crypto.randomUUID()};
    try {
      const result = await withPending(event.submitter, '只读验证中…', () => api(`/api/exchange-accounts/${form.dataset.exchangeAccountId}/connection-verifications`, {method:'POST', body:JSON.stringify(body)}));
      showToast(result.connection?.status === 'VERIFIED' ? '只读连接验证成功；交易能力仍保持关闭' : `连接验证失败：${fmtBinanceConnectionDiagnostic(result.connection)}`);
      await route();
    } catch (error) { showApiError(error, form.querySelector('.form-error')); }
  }));
  document.querySelectorAll('.exchange-runtime-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const enabled = form.dataset.enabled !== 'true';
    const body = {enabled, expected_version:Number(form.dataset.version), idempotency_key:crypto.randomUUID()};
    try {
      await withPending(event.submitter, enabled ? '绑定中…' : '停用中…', () => api(`/api/exchange-accounts/${form.dataset.exchangeAccountId}/runtime-sync`, {method:'PUT', body:JSON.stringify(body)}));
      showToast(enabled ? '连续只读同步已绑定；交易能力仍保持关闭' : '连续只读同步已停用');
      await route();
    } catch (error) { showApiError(error, form.querySelector('.form-error')); }
  }));
  document.querySelectorAll('.exchange-trading-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const enabled = form.dataset.enabled !== 'true';
    const confirmed = await confirmAction({
      title: enabled ? '启用当前账户的交易资格？' : '停用当前账户的交易资格？',
      message: enabled ? '只启用当前空间与账户的资格。全局真实发送、发送者租约、风控、任务和进程安全开关仍会独立阻断；本操作不会下单、签名或广播。' : '当前账户会立即失去交易资格；连接与只读同步保持不变，任何后续真实发送都由服务端拒绝。',
      confirmLabel: enabled ? '确认启用账户资格' : '确认停用账户资格',
    });
    if (!confirmed) return;
    const body = {enabled, expected_version:Number(form.dataset.version), idempotency_key:crypto.randomUUID()};
    try {
      await withPending(event.submitter, enabled ? '启用中…' : '停用中…', () => api(`/api/exchange-accounts/${form.dataset.exchangeAccountId}/trading-eligibility`, {method:'PUT', body:JSON.stringify(body)}));
      showToast(enabled ? '账户交易资格已启用；其他真实执行安全开关保持不变' : '账户交易资格已停用');
      await route();
    } catch (error) { showApiError(error, form.querySelector('.form-error')); }
  }));
  document.querySelectorAll('.freqtrade-worker-form').forEach(form => {
    form.querySelector('[data-worker-endpoint-override]')?.addEventListener('input', event => {
      form.elements.base_url.value = event.currentTarget.value;
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(form));
      const body = {
        mode:data.mode,
        name:data.name,
        base_url:data.base_url,
        username:data.username,
        password:data.password,
        hip3_dexes:String(data.hip3_dexes || '').split(',').map(value => value.trim()).filter(Boolean),
        expected_version:Number(form.dataset.version),
        idempotency_key:crypto.randomUUID(),
      };
      try {
        await withPending(event.submitter, '加密保存中…', () => api(`/api/exchange-accounts/${form.dataset.exchangeAccountId}/freqtrade-worker`, {method:'PUT', body:JSON.stringify(body)}));
        showToast('账户专属 Worker 已保存为待验证；没有发送订单');
        await route();
      } catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
    form.querySelector('[data-freqtrade-unconfigure]')?.addEventListener('click', async event => {
      const confirmed = await confirmAction({title:'移除当前账户的 Worker 绑定？', message:'移除后该账户的 Freqtrade LIVE 执行会由服务端拒绝；账户连接与只读同步保持不变。', confirmLabel:'确认移除绑定'});
      if (!confirmed) return;
      const body = {mode:'UNCONFIGURED', hip3_dexes:[], expected_version:Number(form.dataset.version), idempotency_key:crypto.randomUUID()};
      try {
        await withPending(event.currentTarget, '移除中…', () => api(`/api/exchange-accounts/${form.dataset.exchangeAccountId}/freqtrade-worker`, {method:'PUT', body:JSON.stringify(body)}));
        showToast('Worker 绑定已移除；该账户的 Freqtrade LIVE 执行保持阻断');
        await route();
      } catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
  document.querySelectorAll('.freqtrade-worker-verify-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const body = {expected_version:Number(form.dataset.version), idempotency_key:crypto.randomUUID()};
    try {
      const result = await withPending(event.submitter, '验证中…', () => api(`/api/exchange-accounts/${form.dataset.exchangeAccountId}/freqtrade-worker/verifications`, {method:'POST', body:JSON.stringify(body)}));
      showToast(result.worker?.status === 'VERIFIED' ? 'Worker 范围验证成功；真实发送 Gate 保持不变' : `Worker 验证失败：${result.worker?.error_code || 'FREQTRADE_WORKER_PROBE_FAILED'}`);
      await route();
    } catch (error) { showApiError(error, form.querySelector('.form-error')); }
  }));
  document.querySelectorAll('[data-delete-exchange-account]').forEach(button => button.addEventListener('click', async event => {
    const trigger = event.currentTarget;
    const confirmed = await confirmAction({
      title:`删除实盘账户“${trigger.dataset.accountLabel}”？`,
      message:'账户会从当前空间移除，并立即停用连续同步、交易资格、Worker 和加密凭据。历史订单、成交、资金记录与审计仍会保留；有运行中交易任务或未决资金操作时服务端会阻断删除。',
      confirmLabel:'确认删除账户',
    });
    if (!confirmed) return;
    const body = {expected_version:Number(trigger.dataset.version), idempotency_key:crypto.randomUUID()};
    try {
      await withPending(trigger, '删除中…', () => api(`/api/exchange-accounts/${trigger.dataset.deleteExchangeAccount}`, {method:'DELETE', body:JSON.stringify(body)}));
      showToast('实盘账户已删除；历史交易与审计记录已保留');
      history.pushState({}, '', '/accounts?environment=LIVE');
      await route();
    } catch (error) { showApiError(error); }
  }));
}

async function renderVenueAccountDetail(requestedAccountId) {
  const accountResult = await api('/api/exchange-accounts');
  const registry = accountResult.data;
  const decodedAccountId = decodeURIComponent(requestedAccountId);
  const account = (registry.data || []).find(item => item.account_id === decodedAccountId || item.exchange_account_id === decodedAccountId);
  if (!account) {
    main.innerHTML = notFoundView(location.pathname);
    main.querySelector('[data-error-heading]')?.focus();
    return;
  }
  const venue = account.venue;
  const [runtime, factsResult, health] = await Promise.all([
    api('/api/runtime/status').catch(error => [403, 409].includes(error.status) ? null : Promise.reject(error)),
    api(`/api/exchange-accounts/${encodeURIComponent(account.exchange_account_id)}/facts`),
    api(`/api/exchange-accounts/${encodeURIComponent(account.exchange_account_id)}/fact-health`),
  ]);
  const accountId = account.account_id;
  const facts = factsResult.data;
  const persistedHistoryIncomplete = Boolean(
    health?.data?.error_code && String(health.data.error_code).includes('HISTORY_INCOMPLETE')
  );
  const persistedFactsCurrent = Boolean(
    health?.data?.data_status === 'CURRENT'
      && (health.data.runtime_status === 'SUCCESS' || persistedHistoryIncomplete)
  );
  const status = {
    venue,
    execution_backend:'FREQTRADE',
    worker_configured:false,
    automatic_sync_enabled:Boolean(persistedFactsCurrent && account?.runtime_binding?.bound),
    automatic_sync_interval_seconds:runtime?.data?.external_boundaries?.fact_adapter?.reconciliation_seconds || 0,
    default_account_id:accountId,
    fact_environment:account.environment,
  };
  status.automatic_sync_enabled = Boolean(persistedFactsCurrent && account.runtime_binding?.bound);
  status.default_account_id = accountId;
  const aggregateConnection = runtime?.data?.connections?.[venue] || null;
  const persistedHealth = health?.data
    ? {
        ...health.data,
        status:health.data.runtime_status || (health.data.data_status === 'CURRENT' ? 'SUCCESS' : 'FAILED'),
      }
    : null;
  const exactHealth = persistedHealth || exchangeAccountRuntimeHealth(runtime, account);
  const connection = exactHealth
    ? {
        ...aggregateConnection,
        available:(exactHealth.status === 'SUCCESS' || String(exactHealth.error_code || '').includes('HISTORY_INCOMPLETE')) && status.automatic_sync_enabled,
        category:String(exactHealth.error_code || '').includes('HISTORY_INCOMPLETE')
          ? 'READ_ONLY_CONNECTED_HISTORY_INCOMPLETE'
          : exactHealth.status === 'SUCCESS'
          ? status.automatic_sync_enabled ? 'READ_ONLY_CONNECTED' : 'EXPLICITLY_DISABLED'
          : exactHealth.status === 'SKIPPED' ? 'PROBE_SKIPPED' : 'READ_ONLY_PROBE_FAILED',
        error_code:exactHealth.error_code,
        checked_at:exactHealth.checked_at,
        last_success_at:exactHealth.last_success_at,
        retry_at:exactHealth.retry_at,
        consecutive_failures:exactHealth.consecutive_failures,
        reason:String(exactHealth.error_code || '').includes('HISTORY_INCOMPLETE')
          ? '当前余额、仓位与订单只读事实已连接；历史成交或资金费补全暂不完整。'
          : exactHealth.status === 'SUCCESS' && !status.automatic_sync_enabled
          ? '最近只读探针成功，但连续同步当前关闭，因此不标记为实时连接。'
          : exactHealth.status === 'SUCCESS'
            ? '最近一次精确账户只读同步成功。'
            : aggregateConnection?.reason || '该账户最近一次只读同步没有形成可用实时事实。',
        owner_role:aggregateConnection?.owner_role || '系统管理员',
        next_action:String(exactHealth.error_code || '').includes('HISTORY_INCOMPLETE')
          ? '等待上游历史接口恢复；新增风险继续保持阻断。'
          : exactHealth.status === 'SUCCESS' && !status.automatic_sync_enabled
          ? '在连接设置中启用连续只读同步。'
          : aggregateConnection?.next_action || '检查精确账户错误代码并等待下一次有界重试。',
      }
    : account.connection?.status === 'VERIFIED'
      ? {available:false, category:account.runtime_binding?.bound ? 'NOT_YET_VERIFIED' : 'EXPLICITLY_DISABLED', checked_at:account.connection.checked_at, last_success_at:account.connection.last_verified_at, reason:account.runtime_binding?.bound ? '凭据连接验证已通过，但连续同步尚未产出可用探针。' : '凭据连接验证已通过，但连续同步当前关闭。', owner_role:'系统管理员', next_action:account.runtime_binding?.bound ? '等待首次有界同步，期间仅使用已保存快照。' : '在连接设置中启用连续只读同步。'}
      : aggregateConnection;
  const connected = Boolean(accountId && connection?.available);
  const probeSucceededWhileClosed = exactHealth?.status === 'SUCCESS' && !status.automatic_sync_enabled;
  const connectionLabel = probeSucceededWhileClosed ? '探针成功 · 同步关闭' : fmtConnectionCategory(connection?.category);
  const historyIncomplete = connection?.category === 'READ_ONLY_CONNECTED_HISTORY_INCOMPLETE';
  const connectionEvidence = connection?.error_code
    ? `<details class="venue-technical-detail"><summary>查看技术分类</summary><code translate="no">${escapeHtml(connection.error_code)}</code></details>`
    : '';
  const connectionReason = connection
    ? (currentLanguage === 'en' ? `${fmtConnectionReason(connection)} Owner: ${translateEnglishText(connection.owner_role)}. Next: ${fmtConnectionNextAction(connection)}` : `${fmtConnectionReason(connection)} 负责：${connection.owner_role}；下一步：${fmtConnectionNextAction(connection)}`)
    : (currentLanguage === 'en' ? 'This identity cannot read the unified connection probe. The page shows saved facts only and does not claim a live connection.' : '当前身份无法读取统一连接探针；页面仅展示已保存账户事实，不能据此声称实时已连接。');
  const connectionProbeEvidence = connection
    ? [
        `${currentLanguage === 'en' ? 'Latest probe: ' : '最近探针：'}${fmtDate(connection.checked_at)}`,
        connection.last_success_at ? `${currentLanguage === 'en' ? 'Latest success: ' : '最近成功：'}${fmtDate(connection.last_success_at)}` : (currentLanguage === 'en' ? 'No successful probe yet' : '尚无成功记录'),
        connection.retry_at ? `${currentLanguage === 'en' ? 'Next automatic retry: ' : '下次自动重试：'}${fmtDate(connection.retry_at)}` : null,
        Number(connection.consecutive_failures || 0) > 0 ? `${currentLanguage === 'en' ? 'Consecutive failures: ' : '连续失败：'}${Number(connection.consecutive_failures)}${currentLanguage === 'en' ? '' : ' 次'}` : null,
      ].filter(Boolean).join(' · ')
    : (currentLanguage === 'en' ? 'Probe time is unavailable for this identity' : '当前身份无法读取探针时间');
  const lastSync = latestVenueObservation(facts);
  const snapshotMode = !connected && Boolean(lastSync);
  const hip3Dexes = Array.isArray(status.hip3_dexes) ? status.hip3_dexes : [];
  const venueDetail = currentLanguage === 'en'
    ? venue === 'BINANCE'
      ? ({PORTFOLIO_MARGIN:'Unified account',MAIN_ACCOUNT:'Main account',SUBACCOUNT:'Subaccount'}[status.account_mode] || 'Unknown account mode')
      : venue === 'HYPERLIQUID'
        ? `Core markets${status.hip3_available ? ` + HIP-3${hip3Dexes.length ? ` (${hip3Dexes.join(', ')})` : ''}` : ''}`
        : venue === 'OKX' ? 'USDT linear SWAP scope' : 'Unified USDT linear perpetual scope'
    : venue === 'BINANCE'
      ? (accountModeLabels[status.account_mode] || '账户模式未知')
      : venue === 'HYPERLIQUID'
        ? `核心市场${status.hip3_available ? ` + HIP-3${hip3Dexes.length ? `（${hip3Dexes.join('、')}）` : ''}` : ''}`
        : venue === 'OKX' ? 'USDT 线性永续范围' : '统一账户 USDT 线性永续范围';
  const executionWorker = account?.execution_worker || null;
  const executionDetail = executionWorker?.live_ready
    ? (currentLanguage === 'en' ? 'Exact-account LIVE Freqtrade worker verified; order actions remain in Trading' : '精确账户 LIVE Freqtrade Worker 已验证；下单操作仍只在交易任务中进行')
    : executionWorker?.configured
      ? (currentLanguage === 'en' ? `Exact-account Freqtrade worker: ${executionWorker.status}; this page cannot place orders` : `精确账户 Freqtrade Worker：${fmtStatus(executionWorker.status)}；本页不能下单`)
      : executionWorker?.supported
        ? (currentLanguage === 'en' ? 'Exact-account Freqtrade worker is not configured; order sending remains blocked' : '精确账户 Freqtrade Worker 尚未配置；订单发送保持阻断')
        : (currentLanguage === 'en' ? 'No supported execution worker is available; order sending remains blocked' : '没有可用的受控执行 Worker；订单发送保持阻断');
  const syncInterval = Number(status.automatic_sync_interval_seconds || 0);
  const syncConfigured = Boolean(account.runtime_binding?.bound);
  const automaticSyncCopyLocalized = status.automatic_sync_enabled && connected
    ? historyIncomplete
      ? localizedText(`系统约每 ${syncInterval} 秒更新余额、仓位与当前委托；历史成交和资金费仍在等待上游补全。`)
      : localizedText(`系统约每 ${syncInterval} 秒读取一次完整账户；新出现的持仓和委托会自动纳入，最近成交与资金费同步保存。`)
    : status.automatic_sync_enabled
      ? localizedText(`同步服务约每 ${syncInterval} 秒检查一次；上游失败时按有界退避计划重试。连接恢复前不会把旧快照标记为实时。`)
      : syncConfigured
        ? localizedText('账户已绑定连续同步，但当前同步进程关闭；以下仅展示已保存数据。')
        : localizedText('当前只展示已经保存的生产数据；配置连续读取服务后会自动更新。');
  const automaticSyncCopy = currentLanguage === 'en'
    ? status.automatic_sync_enabled && connected
      ? historyIncomplete
        ? `Balances, positions, and open orders update about every ${syncInterval} seconds. Historical fills and funding are still waiting for upstream backfill.`
        : `The system reads the complete account about every ${syncInterval} seconds. New positions and orders are included automatically, with recent fills and funding saved together.`
      : status.automatic_sync_enabled
        ? `The reader checks about every ${syncInterval} seconds and follows a bounded backoff after upstream failures. Saved snapshots will not be presented as live until the connection recovers.`
      : syncConfigured
        ? 'Continuous sync is bound to this account, but the reader process is off. Only saved data is shown.'
        : 'Only saved production data is shown. Configure the continuous reader to update it automatically.'
    : automaticSyncCopyLocalized;
  const connectionSummary = currentLanguage === 'en'
    ? probeSucceededWhileClosed
      ? 'The probe succeeded, but continuous sync is off; the data below is not marked live'
      : historyIncomplete
        ? 'Current account facts are available; history is incomplete'
        : connected
          ? 'The latest read-only probe succeeded'
          : connection
            ? 'Live account facts are unavailable; only the last snapshot is shown'
            : 'The live connection could not be verified; only saved facts are shown'
    : probeSucceededWhileClosed
      ? '探针成功，但连续同步关闭；以下数据不标记为实时'
      : historyIncomplete
        ? '当前账户事实可用；历史记录待补全'
        : connected
          ? '最近只读检查成功'
          : connection
            ? '实时账户事实不可用；仅展示最后快照'
            : '无法验证实时连接；仅展示已保存事实';
  const fixtureBadge = isFixtureExchangeAccount(account) ? '<span class="status-pill status-RETRY_WAIT">测试 Fixture</span>' : '';
  const deleteAction = account.permissions?.can_delete ? `<button class="danger" type="button" data-delete-exchange-account="${escapeHtml(account.exchange_account_id)}" data-account-label="${escapeHtml(account.label)}" data-version="${account.version}">删除实盘账户</button>` : '';
  main.innerHTML = `<section class="page venue-facts-page venue-account-detail-page"><div class="detail-back-row"><a class="row-link" href="/accounts?environment=LIVE" data-link>← 返回账户列表</a>${fixtureBadge}</div><header class="page-head"><div><p class="eyebrow">${escapeHtml(exchangeVenueLabels[venue] || venue)} · ${escapeHtml(accountId)}</p><h1>${escapeHtml(account.label)}</h1><p class="lede">当前空间内的精确账户配置、连接状态与历史快照。</p></div><div class="toolbar"><button class="secondary" data-refresh>刷新当前状态</button>${deleteAction}</div></header>${exchangeAccountDetailConfiguration(account)}<section id="status" class="account-status-history"><div class="section-heading"><div><p class="eyebrow">账户状态及历史快照</p><h2>连接与数据</h2></div><span class="subtle">最近保存 ${fmtDate(lastSync)}</span></div><div class="stats venue-status-stats"><div class="stat"><small>连接状态</small><b class="${connected ? 'direction-long' : 'warning-text'}">${escapeHtml(connectionLabel)}</b><span>${escapeHtml(connectionSummary)}</span></div><div class="stat"><small>运行模式</small><b>${currentLanguage === 'en' ? 'Production account · read-only' : '生产账户 · 只读'}</b><span>${escapeHtml(venueDetail)} · ${escapeHtml(executionDetail)}</span></div><div class="stat"><small>账户范围</small><b>当前空间</b><span>${escapeHtml(exchangeVenueLabels[venue])} · ${escapeHtml(accountId)} · 精确空间/账户范围</span></div><div class="stat"><small>${snapshotMode ? '最后快照' : '事实新鲜度'}</small><b>${fmtDate(lastSync)}</b><span>${lastSync ? snapshotMode ? '连接受限；以下数据不是实时事实' : '最近保存时间；连接探针另行校验' : '尚无已保存事实'}</span></div></div>
    <article class="account-sync-note ${connected ? 'is-active' : ''}"><span class="status-dot"></span><div><b>${currentLanguage === 'en' ? 'Connection check' : '连接检查'}</b><p>${escapeHtml(connectionReason)}</p><span class="system-health-meta">${escapeHtml(connectionProbeEvidence)}</span>${connectionEvidence}</div></article>
    <article class="account-sync-note ${status.automatic_sync_enabled && connected ? 'is-active' : ''}"><span class="status-dot"></span><div><b>${status.automatic_sync_enabled && connected ? '账户数据自动同步' : status.automatic_sync_enabled ? '自动同步等待连接恢复' : '账户自动更新尚未启用'}</b><p>${escapeHtml(automaticSyncCopy)}</p></div></article>
    ${snapshotMode ? `<article class="danger-note venue-snapshot-warning"><b>当前连接不可用，以下仅为最后一次保存快照</b><p>这些余额、仓位、订单与成交不能作为实时交易依据。恢复只读连接并完成新一轮同步后，页面才会重新标记为当前事实。</p></article>` : ''}
    <div id="history">${venueFactSections(facts, {snapshotMode, historyIncomplete})}</div></section></section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  bindExchangeAccountForms();
  const detailTarget = location.hash ? document.getElementById(location.hash.slice(1)) : null;
  detailTarget?.scrollIntoView({block:'start'});
}

function venueFactSections(facts, {snapshotMode = false, historyIncomplete = false} = {}) {
  const positionRows = facts.positions.filter(item => Number(item.quantity) !== 0);
  const activeOrderRows = facts.orders.filter(item => !['FILLED','CANCELLED','REJECTED','EXPIRED'].includes(item.status));
  const historicalOrderRows = facts.orders.filter(item => ['FILLED','CANCELLED','REJECTED','EXPIRED'].includes(item.status));
  const positions = positionRows.map(item => `<tr><td data-label="标的">${escapeHtml(item.symbol)}</td><td data-label="数量 / 入场">${fmtNumber(item.quantity)} @ ${fmtNumber(item.average_entry_price)}</td><td data-label="标记价">${fmtNumber(item.mark_price)}</td><td data-label="数据状态">${escapeHtml(snapshotMode ? '历史快照' : factStatusLabel(item.fact_status))}</td><td data-label="保护">${item.protection ? `${escapeHtml(fmtStatus(item.protection.status))} · ${item.protection.fully_covered ? '足额' : '不足'}` : '无保护数据'}</td><td data-label="更新时间">${fmtDate(item.observed_at)}</td></tr>`).join('');
  const renderOrderRows = items => items.map(item => `<tr><td data-label="交易所订单">${escapeHtml(item.venue_order_id)}</td><td data-label="标的">${escapeHtml(item.symbol)}</td><td data-label="状态">${escapeHtml(fmtStatus(item.status))}</td><td data-label="成交 / 委托">${fmtNumber(item.filled_quantity)} / ${fmtNumber(item.ordered_quantity)}</td><td data-label="关联操作">${item.intent_id ? shortId(item.intent_id) : '外部未关联'}</td><td data-label="更新时间">${fmtDate(item.observed_at)}</td></tr>`).join('');
  const orders = renderOrderRows(activeOrderRows);
  const orderHistory = renderOrderRows(historicalOrderRows);
  const fills = facts.fills.map(item => `<tr><td data-label="成交编号">${escapeHtml(item.venue_fill_id)}</td><td data-label="标的">${escapeHtml(item.symbol)}</td><td data-label="方向 / 数量">${escapeHtml(fmtSide(item.side))} ${fmtNumber(item.quantity)}</td><td data-label="价格">${fmtNumber(item.price)}</td><td data-label="手续费">${fmtNumber(item.fee)} ${escapeHtml(item.fee_currency)}</td><td data-label="成交时间">${fmtDate(item.executed_at)}</td></tr>`).join('');
  const funding = facts.funding.map(item => `<tr><td data-label="支付编号">${escapeHtml(item.venue_payment_id)}</td><td data-label="标的">${escapeHtml(item.symbol)}</td><td data-label="金额">${fmtNumber(item.amount)} ${escapeHtml(item.currency)}</td><td data-label="支付时间">${fmtDate(item.paid_at)}</td></tr>`).join('');
  const reconciliation = facts.reconciliation;
  const positionTitle = snapshotMode ? '最后快照中的仓位与风险保护' : '当前仓位与风险保护';
  const positionEmpty = snapshotMode ? '最后一次保存快照中没有持仓；这不能确认当前账户仍为空仓。' : '当前账户没有持仓；零仓位行情不会冒充当前仓位。';
  const orderTitle = snapshotMode ? '最后快照中的委托' : '当前委托';
  const orderEmpty = snapshotMode ? '最后一次保存快照中没有未完成委托；这不能确认当前仍无挂单。' : '当前账户没有未完成委托。';
  const orderHistoryDescription = currentLanguage === 'en'
    ? `${historicalOrderRows.length} filled, cancelled, rejected, or expired records${snapshotMode ? ' from the last snapshot; they do not confirm current open orders' : '; they are not current open orders'}`
    : `${historicalOrderRows.length} 条已成交、取消、拒绝或过期记录；${snapshotMode ? '仅表示最后快照，不代表当前挂单' : '不计入当前委托'}`;
  const fillTitle = snapshotMode ? '最后快照中的成交记录' : historyIncomplete ? '已保存成交' : '最近成交';
  const fillEmpty = snapshotMode ? '最后一次保存快照中没有成交记录；这不代表连接中断后没有成交。' : historyIncomplete ? '当前没有已保存的成交；这不代表交易所没有历史成交。' : '当前没有已保存的成交记录。';
  const fundingTitle = snapshotMode ? '最后快照中的资金费' : historyIncomplete ? '已保存资金费' : '资金费';
  const fundingEmpty = snapshotMode ? '最后一次保存快照中没有资金费记录；这不代表连接中断后没有资金费。' : historyIncomplete ? '当前没有已保存的资金费；这不代表交易所没有历史资金费。' : '当前没有已保存的资金费记录。';
  return `<div class="stats"><div class="stat"><small>权益</small><b>${fmtNumber(facts.equity?.equity)} ${escapeHtml(facts.equity?.currency || '')}</b></div><div class="stat"><small>可用余额</small><b>${fmtNumber(facts.equity?.available_balance)} ${escapeHtml(facts.equity?.currency || '')}</b></div><div class="stat"><small>权益状态</small><b style="font-size:14px">${escapeHtml(snapshotMode ? '历史快照' : factStatusLabel(facts.equity?.fact_status))}</b></div><div class="stat"><small>最近对账</small><b style="font-size:14px" class="${reconciliation?.status === 'MATCH' && !snapshotMode ? 'direction-long' : reconciliation ? 'warning-text' : ''}">${escapeHtml(reconciliation ? snapshotMode ? '历史结果' : fmtStatus(reconciliation.status) : '未运行')}</b><span>${fmtDate(reconciliation?.completed_at)}</span></div></div>
    ${reconciliation?.differences?.length ? `<article class="danger-note"><b>${snapshotMode ? '最后快照的对账差异' : '对账差异'}</b><ul>${reconciliation.differences.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul></article>` : ''}
    ${factTable(positionTitle, '<th>标的</th><th>数量 / 入场</th><th>标记价</th><th>数据状态</th><th>保护</th><th>更新时间</th>', positions, positionEmpty, snapshotMode)}
    ${factTable(orderTitle, '<th>交易所订单</th><th>标的</th><th>状态</th><th>成交 / 委托</th><th>关联操作</th><th>更新时间</th>', orders, orderEmpty, snapshotMode)}
    ${orderHistory ? `<details class="operation-toolbox venue-order-history"><summary><span><b>${snapshotMode ? '最后快照中的订单记录' : '最近订单记录'}</b><small>${escapeHtml(orderHistoryDescription)}</small></span><strong>查看记录</strong></summary><div class="toolbox-content"><div class="table-scroll-hint venue-fact-scroll-hint">左右滑动查看完整订单记录</div><div class="table-wrap is-scrollable venue-fact-table"><table><thead><tr><th>交易所订单</th><th>标的</th><th>状态</th><th>成交 / 委托</th><th>关联操作</th><th>更新时间</th></tr></thead><tbody>${orderHistory}</tbody></table></div></div></details>` : ''}
    ${historyIncomplete ? '<article class="callout venue-history-warning"><b>历史记录尚未补全</b><p>以下成交与资金费只代表已经保存的记录，不能据此判断完整历史；余额、仓位和当前委托不受影响。</p></article>' : ''}
    ${factTable(fillTitle, '<th>成交编号</th><th>标的</th><th>方向 / 数量</th><th>价格</th><th>手续费</th><th>成交时间</th>', fills, fillEmpty, snapshotMode || historyIncomplete)}
    ${factTable(fundingTitle, '<th>支付编号</th><th>标的</th><th>金额</th><th>支付时间</th>', funding, fundingEmpty, snapshotMode || historyIncomplete)}`;
}

function factTable(title, headers, rows, emptyCopy = '当前没有已保存的数据。', emptyAttention = false) {
  return `<section><h2>${escapeHtml(title)}</h2>${rows ? `<div class="table-scroll-hint venue-fact-scroll-hint">左右滑动查看完整${escapeHtml(title)}</div><div class="table-wrap is-scrollable venue-fact-table"><table><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="callout ${emptyAttention ? 'tone-attention' : ''}">${escapeHtml(emptyCopy)}</div>`}</section>`;
}
