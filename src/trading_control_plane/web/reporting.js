async function renderRuntimeAlerts() {
  const [result, campaignResponse] = await Promise.all([api('/api/campaign-exceptions'), api('/api/campaigns')]);
  const liveCampaignIds = new Set(campaignResponse.data.filter(item => item.environment === 'LIVE').map(item => item.campaign_id));
  const items = result.data.filter(item => liveCampaignIds.has(item.campaign_id));
  const affectedCampaigns = new Set(items.map(item => item.campaign_id));
  const unknownCount = items.filter(item => item.code.includes('UNKNOWN')).length;
  const staleCount = items.filter(item => item.code.endsWith('_STALE')).length;
  const cards = [...items].sort((left, right) => explainException(left.code).priority - explainException(right.code).priority).map(item => {
    const guidance = explainException(item.code);
    const severity = item.severity === 'CRITICAL' ? '严重' : '高';
    return `<article class="card exception-card"><div class="exception-card-head"><div><p class="eyebrow">${escapeHtml(severity)}运行告警</p><h2>${escapeHtml(guidance.title)}</h2></div><span class="status-pill status-DENY">${escapeHtml(exceptionCategory(item.code))}</span></div><dl class="definition-grid">${definition('受影响交易任务', shortId(item.campaign_id))}${definition('发生 / 首次可判定', fmtDate(item.occurred_at))}${definition('最近检查', fmtDate(item.last_checked_at || result.as_of))}${definition('具体阻断原因', guidance.copy)}${definition('影响', item.impact)}${definition('负责角色', item.owner_role)}${definition('下一步', item.next_action)}${definition('诊断编号', item.code)}</dl>${item.details?.length ? `<p class="subtle">事实详情：${escapeHtml(item.details.map(formatExceptionDetail).join('；'))}</p>` : ''}<p class="safety-note">${escapeHtml(item.action_unavailable_reason || '详情页不提供解除风控动作。')}</p><a class="primary" href="/campaigns/${item.campaign_id}" data-link>打开受影响交易任务</a></article>`;
  }).join('');
  main.innerHTML = `<section class="page exceptions-page"><header class="page-head"><div><p class="eyebrow">交易任务 · 运行告警详情</p><h1>运行告警</h1><p class="lede">只展示运行中生产交易任务需要人工处理的问题；风险恢复、资金异常和系统健康分别保留在各自页面。</p></div><div class="toolbar"><a class="secondary" href="/campaigns" data-link>返回交易任务</a><button class="secondary" data-refresh>刷新</button></div></header>
    <div class="stats exception-stats"><div class="stat"><small>受影响交易任务</small><b class="${affectedCampaigns.size ? 'danger-text' : ''}">${affectedCampaigns.size}</b></div><div class="stat"><small>运行问题</small><b>${items.length}</b></div><div class="stat"><small>结果未知</small><b class="${unknownCount ? 'danger-text' : ''}">${unknownCount}</b></div><div class="stat"><small>数据过期</small><b class="${staleCount ? 'warning-text' : ''}">${staleCount}</b><span>最近检查 ${fmtDate(result.as_of)}</span></div></div>
    ${items.length ? `<div class="exception-grid">${cards}</div>` : `<section class="empty-state"><div><h2>无运行告警 / 当前无需处理</h2><p>检查范围：运行中的生产交易任务；未发现结果未知、数据过期、保护不足或对账差异。已关闭记录不会重新计入当前待办。</p><p class="subtle">最近检查：${fmtDate(result.as_of)}</p><div class="toolbar empty-actions"><a class="secondary" href="/home" data-link>返回当前任务</a><a class="primary" href="/campaigns" data-link>查看交易任务</a></div></div></section>`}</section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
}

function resultDateTimeInput(value) {
  if (!value) return '';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function quantStatsDefaultRange() {
  const now = new Date();
  const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), 23, 59, 59, 999));
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - 30);
  start.setUTCHours(0, 0, 0, 0);
  return {from_time:start.toISOString(), to_time:end.toISOString()};
}

function resizeQuantStatsFrame(frame) {
  try {
    const height = frame.contentDocument?.documentElement?.scrollHeight;
    if (Number.isFinite(height) && height > 0) frame.style.height = `${height + 24}px`;
  } catch (_error) {
    frame.style.height = '12000px';
  }
}

async function renderActualResults() {
  const current = new URLSearchParams(location.search);
  const catalogResponse = await api('/api/results/report-engines');
  const options = catalogResponse.data.options;
  const engines = catalogResponse.data.engines;
  const requestedEngine = current.get('engine') || 'QUANTSTATS';
  const engine = engines.some(item => item.engine === requestedEngine && item.available) ? requestedEngine : engines.find(item => item.available)?.engine;
  const selectedEngine = engines.find(item => item.engine === engine);
  const modeDefault = ['TESTNET','LIVE'].includes(options.current_trading_mode) ? options.current_trading_mode : 'TESTNET';
  const environment = ['TESTNET','LIVE'].includes(current.get('environment')) ? current.get('environment') : modeDefault;
  const defaults = quantStatsDefaultRange();
  const fromTime = current.get('from_time') || defaults.from_time;
  const toTime = current.get('to_time') || defaults.to_time;
  const selectedAccount = current.get('account_id') || '';
  const selectedVenue = current.get('venue') || '';
  const accountOptions = options.accounts.map(item => `<option value="${escapeHtml(item.account_id)}" data-venue="${escapeHtml(item.venue)}" ${selectedAccount === item.account_id && (!selectedVenue || selectedVenue === item.venue) ? 'selected' : ''}>${escapeHtml(item.label)} · ${escapeHtml(fmtVenueLabel(item.venue))}</option>`).join('');
  const engineOptions = engines.map(item => `<option value="${item.engine}" ${engine === item.engine ? 'selected' : ''} ${item.available ? '' : 'disabled'}>${escapeHtml(item.label)} · ${item.available ? escapeHtml(item.version) : escapeHtml(item.error_code)}</option>`).join('');
  main.innerHTML = `<section class="page results-page quantstats-page"><header class="page-head"><div><p class="eyebrow">${escapeHtml(options.scope.workspace_name)} · ${escapeHtml(options.scope.team_name)}</p><h1>绩效报表</h1><p class="lede">QuantStats 与 Pyfolio Reloaded 使用同一份可信净值、收益率、成交、持仓、手续费及基准数据；订单仅用于执行与审计。</p></div><button class="secondary" data-refresh>刷新</button></header>
    <section class="performance-capital-panel" data-capital-performance><div class="loading-card"><span class="spinner"></span><b>正在加载资金绩效曲线</b><p>按当前模式和已授权账户读取可信资金事实。</p></div></section>
    <form id="results-filter-form" class="form-panel compact-form result-filters"><div class="field-grid"><label>报表引擎<select name="engine">${engineOptions}</select></label><label>环境<select name="environment"><option value="TESTNET" ${environment === 'TESTNET' ? 'selected' : ''}>测试模式</option><option value="LIVE" ${environment === 'LIVE' ? 'selected' : ''}>生产历史</option></select></label><label>账户<select name="account_id"><option value="">全部有权限账户</option>${accountOptions}</select></label><label>交易所<select name="venue"><option value="">全部</option>${['BINANCE','HYPERLIQUID','OKX','BYBIT'].map(value => `<option value="${value}" ${selectedVenue === value ? 'selected' : ''}>${escapeHtml(fmtVenueLabel(value))}</option>`).join('')}</select></label><label>起始时间<input name="from_time" type="datetime-local" value="${escapeHtml(resultDateTimeInput(fromTime))}" required></label><label>截止时间<input name="to_time" type="datetime-local" value="${escapeHtml(resultDateTimeInput(toTime))}" required></label></div><div class="form-actions"><button class="primary" type="submit">生成 ${escapeHtml(selectedEngine?.label || engine)} 报表</button></div></form>
    <section class="quantstats-status" data-quantstats-status><div class="loading-card"><span class="spinner"></span><b>正在生成只读报表</b><p>服务端正在验证净值连续性、现金流、估值和环境范围。</p></div></section>
    <section class="quantstats-report-shell" data-quantstats-report hidden><div class="section-heading"><div><p class="eyebrow">${escapeHtml(selectedEngine?.label || engine)} · UTC 24/7 · 365 periods/year</p><h2>完整绩效报表</h2><p data-quantstats-coverage></p></div><div class="form-actions"><a class="secondary" data-report-view target="_blank" rel="noopener">新窗口查看</a><a class="primary" data-report-download>下载 HTML</a></div></div><div class="stats report-common-metrics" data-report-metrics></div><iframe class="quantstats-frame" title="${escapeHtml(selectedEngine?.label || engine)} 完整绩效报表" sandbox="allow-same-origin" referrerpolicy="no-referrer"></iframe></section>
  </section>`;
  applyLanguageToDocument(main);
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  await renderCapitalPerformancePanel();
  applyLanguageToDocument(main);
  document.querySelector('#results-filter-form')?.addEventListener('submit', event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const next = new URLSearchParams({engine:values.engine,environment:values.environment});
    current.getAll('capital_account').forEach(value => next.append('capital_account', value));
    ['venue','account_id'].forEach(key => { if (values[key]) next.set(key, values[key]); });
    ['from_time','to_time'].forEach(key => {
      if (values[key]) next.set(key, new Date(values[key]).toISOString());
    });
    history.replaceState({}, '', `/results?${next.toString()}`);
    route();
  });
  try {
    const body = {engine,environment,from_time:fromTime,to_time:toTime,idempotency_key:crypto.randomUUID()};
    if (selectedAccount) body.account_id = selectedAccount;
    if (selectedVenue) body.venue = selectedVenue;
    const response = await api('/api/results/reports', {method:'POST',body:JSON.stringify(body)});
    const data = response.data;
    const readiness = data.metadata.readiness;
    const status = document.querySelector('[data-quantstats-status]');
    status.innerHTML = `<div class="quantstats-readiness">${Object.entries(readiness).map(([key,value]) => `<span class="status-pill ${value ? 'status-READY' : 'status-DISABLED'}">${escapeHtml(key)} · ${value ? 'READY' : 'NOT READY'}</span>`).join('')}</div>`;
    const shell = document.querySelector('[data-quantstats-report]');
    const frame = shell.querySelector('.quantstats-frame');
    shell.hidden = false;
    shell.querySelector('[data-quantstats-coverage]').textContent = `${data.coverage.nav_point_count} 个净值点 · ${data.coverage.return_count} 个收益周期 · ${data.coverage.transaction_count} 条去重成交 · ${data.library} ${data.library_version} · ${data.chart_count} 张图表`;
    const labels = {total_return:'总收益',annual_return:'年化收益',annual_volatility:'波动率',sharpe:'Sharpe',sortino:'Sortino',max_drawdown:'最大回撤',win_rate:'胜率',fees:'手续费'};
    shell.querySelector('[data-report-metrics]').innerHTML = Object.entries(data.metrics).map(([key,value]) => {
      const display = key === 'fees' ? `${value} U` : ['sharpe','sortino'].includes(key) ? Number(value).toFixed(2) : `${(Number(value) * 100).toFixed(2)}%`;
      return `<article class="stat"><small>${escapeHtml(labels[key] || key)}</small><b>${escapeHtml(display)}</b><span>${escapeHtml(data.engine)}</span></article>`;
    }).join('');
    shell.querySelector('[data-report-view]').href = data.artifact.view_url;
    shell.querySelector('[data-report-download]').href = data.artifact.download_url;
    frame.addEventListener('load', () => resizeQuantStatsFrame(frame), {once:true});
    frame.src = data.artifact.view_url;
  } catch (error) {
    const status = document.querySelector('[data-quantstats-status]');
    status.innerHTML = `<div class="callout tone-attention"><b>报表数据未就绪 · ${escapeHtml(error.code || 'ANALYTICS_NOT_READY')}</b><p>${escapeHtml(error.message || '服务端拒绝使用不完整事实生成收益率。')}</p><p class="subtle">系统没有补零、推测净值或使用成交盈亏伪造收益率。</p></div>`;
  }
  applyLanguageToDocument(main);
}

const notificationChannelLabel = value => ({TELEGRAM:'Telegram',SLACK:'Slack',LARK:'飞书 / Lark',EMAIL:'邮件'}[value] || value);
const notificationEventLabel = value => ({PROPOSAL_REVIEW_REQUIRED:'提案等待独立审核',RISK_DECISION_RECORDED:'风险决策已记录',CAMPAIGN_STATUS_CHANGED:'交易任务状态变化',CAPITAL_STATUS_CHANGED:'资金流程状态变化',SIGNAL_EVENT_RECEIVED:'收到团队信号',CONNECTION_CHECK_FAILED:'账户连接验证失败',TEST_NOTIFICATION:'渠道测试'}[value] || value);

function notificationEventOptions(catalog, selected = []) {
  const selectedEvents = new Set(selected);
  return catalog.filter(item => item.integration_status === 'ACTIVE').map(item => `<label class="check-card"><input type="checkbox" name="event_types" value="${escapeHtml(item.event_type)}" ${selectedEvents.has(item.event_type) ? 'checked' : ''}><span><b>${escapeHtml(notificationEventLabel(item.event_type))}</b><small>${escapeHtml(item.description)}</small></span></label>`).join('');
}

function notificationConfigurationFields(channel, {required = false} = {}) {
  const requiredAttr = required ? 'required' : '';
  const secret = 'type="password" autocomplete="new-password"';
  if (channel === 'TELEGRAM') return `<label>Bot Token<input name="bot_token" ${secret} ${requiredAttr} placeholder="由 BotFather 签发"></label><label>目标 Chat ID<input name="chat_id" ${secret} ${requiredAttr} placeholder="不会回显"></label>`;
  if (channel === 'SLACK') return `<label>Incoming Webhook URL<input name="webhook_url" ${secret} ${requiredAttr} placeholder="https://hooks.slack.com/services/…"></label>`;
  if (channel === 'LARK') return `<label>飞书 / Lark Webhook URL<input name="webhook_url" ${secret} ${requiredAttr} placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…"></label><label>签名密钥（可选）<input name="signing_secret" ${secret} placeholder="启用机器人签名校验时填写"></label>`;
  return `<label>SMTP 主机<input name="smtp_host" ${requiredAttr} placeholder="smtp.example.com"></label><label>加密端口<select name="smtp_port" ${requiredAttr}><option value="587">587 · STARTTLS</option><option value="465">465 · TLS</option></select></label><label>SMTP 用户名<input name="username" ${secret} ${requiredAttr}></label><label>SMTP 密码<input name="password" ${secret} ${requiredAttr}></label><label>发件地址<input name="from_address" type="email" ${requiredAttr}></label><label>收件地址<input name="to_address" type="email" ${requiredAttr}></label>`;
}

function notificationRoutePayload(form) {
  const data = Object.fromEntries(new FormData(form));
  const channel = form.dataset.channel || data.channel;
  const fieldNames = channel === 'TELEGRAM'
    ? ['bot_token','chat_id']
    : channel === 'SLACK'
      ? ['webhook_url']
      : channel === 'LARK'
        ? ['webhook_url','signing_secret']
        : ['smtp_host','smtp_port','username','password','from_address','to_address'];
  const emailCredentialEntered = channel !== 'EMAIL' || ['smtp_host','username','password','from_address','to_address'].some(name => data[name]);
  const configuration = emailCredentialEntered
    ? Object.fromEntries(fieldNames.filter(name => data[name]).map(name => [name, name === 'smtp_port' ? Number(data[name]) : data[name]]))
    : {};
  return {
    environment:data.environment,
    name:data.name,
    channel,
    event_types:new FormData(form).getAll('event_types'),
    enabled:data.enabled === 'true',
    configuration:Object.keys(configuration).length ? configuration : null,
    expected_version:Number(form.dataset.version || 0),
    idempotency_key:crypto.randomUUID(),
  };
}

async function renderNotifications() {
  const data = await api('/api/notifications');
  const routesById = new Map(data.routes.map(item => [item.notification_route_id, item]));
  const defaultEvents = ['PROPOSAL_REVIEW_REQUIRED','RISK_DECISION_RECORDED'];
  const counts = data.delivery_status_counts || {};
  const waiting = (counts.PENDING || 0) + (counts.RETRY_WAIT || 0) + (counts.SENDING || 0);
  const attention = (counts.DEAD_LETTER || 0) + (counts.OUTCOME_UNKNOWN || 0);
  const defaultEnvironment = currentWorkflowEnvironment() === 'LIVE' ? 'LIVE' : 'TESTNET';
  const createForm = data.can_manage ? `<details class="card notification-route-create"><summary><span class="notification-create-summary"><span class="notification-create-icon" aria-hidden="true">＋</span><span><b>新建团队通知路由</b><small>按环境选择渠道与事件，凭据独立加密保存</small></span></span><strong>开始配置</strong></summary><form id="notification-route-create-form" class="toolbox-content notification-create-form" data-version="0" data-channel="TELEGRAM"><section class="notification-form-section"><div class="notification-section-heading"><span>01</span><div><b>基础设置</b><small>路由只在所选环境接收事件</small></div></div><div class="field-grid notification-create-basics"><label>环境<select name="environment"><option value="TESTNET" ${defaultEnvironment === 'TESTNET' ? 'selected' : ''}>测试模式</option><option value="LIVE" ${defaultEnvironment === 'LIVE' ? 'selected' : ''}>生产模式</option></select></label><label>路由名称<input name="name" maxlength="120" required placeholder="例如 提案审核群"></label><label>通知渠道<select name="channel"><option value="TELEGRAM">Telegram</option><option value="SLACK">Slack</option><option value="LARK">飞书 / Lark</option><option value="EMAIL">邮件</option></select></label><label>路由状态<select name="enabled"><option value="true">启用</option><option value="false">停用</option></select></label></div></section><fieldset class="notification-form-section notification-events-section"><legend><span>02</span><span><b>订阅事件</b><small>选择需要进入此路由的团队事件</small></span></legend><div class="notification-event-grid">${notificationEventOptions(data.event_catalog, defaultEvents)}</div></fieldset><fieldset class="notification-form-section notification-credentials-section"><legend><span>03</span><span><b>加密渠道配置</b><small>敏感字段保存后永不回显</small></span></legend><div class="field-grid" data-notification-create-config>${notificationConfigurationFields('TELEGRAM', {required:true})}</div></fieldset><footer class="notification-create-footer"><p class="safety-note">保存只建立通知路由。通知进程没有交易、资金、签名或广播接口；API 和页面只返回脱敏目的地。</p><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" type="submit">加密保存路由</button></div></footer></form></details>` : '';
  const routeCards = data.routes.map(item => {
    const destination = item.configuration_metadata?.destination_hint || '已加密配置';
    const eventTags = item.event_types.map(eventType => `<span class="tag">${escapeHtml(notificationEventLabel(eventType))}</span>`).join('');
    if (!data.can_manage) return `<article class="card notification-route-card"><div class="card-heading"><div><p class="eyebrow">${escapeHtml(fmtExecutionMode(item.environment || 'LIVE'))} · ${escapeHtml(notificationChannelLabel(item.channel))} · ${escapeHtml(destination)}</p><h2>${escapeHtml(item.name)}</h2></div><span class="status-pill ${item.enabled ? 'status-APPROVED' : 'status-DISABLED'}">${item.enabled ? '已启用' : '已停用'}</span></div><div class="tag-row">${eventTags}</div><dl class="definition-grid">${definition('配置状态', 'AES-256-GCM 加密')}${definition('路由 / 凭据版本', `${item.version} / ${item.credential_version}`)}${definition('最近更新', fmtDate(item.updated_at))}</dl></article>`;
    return `<article class="card notification-route-card"><div class="card-heading"><div><p class="eyebrow">${escapeHtml(fmtExecutionMode(item.environment || 'LIVE'))} · ${escapeHtml(notificationChannelLabel(item.channel))} · ${escapeHtml(destination)}</p><h2>${escapeHtml(item.name)}</h2></div><span class="status-pill ${item.enabled ? 'status-APPROVED' : 'status-DISABLED'}">${item.enabled ? '已启用' : '已停用'}</span></div><form class="notification-route-form" data-route-id="${escapeHtml(item.notification_route_id)}" data-route-name="${escapeHtml(item.name)}" data-version="${item.version}" data-channel="${escapeHtml(item.channel)}"><input name="environment" type="hidden" value="${escapeHtml(item.environment || 'LIVE')}"><div class="field-grid"><label>路由名称<input name="name" value="${escapeHtml(item.name)}" maxlength="120" required></label><label>通知渠道<input value="${escapeHtml(notificationChannelLabel(item.channel))}" disabled><input name="channel" type="hidden" value="${escapeHtml(item.channel)}"></label><label>路由状态<select name="enabled"><option value="true" ${item.enabled ? 'selected' : ''}>启用</option><option value="false" ${item.enabled ? '' : 'selected'}>停用</option></select></label></div><fieldset><legend>订阅事件</legend><div class="notification-event-grid">${notificationEventOptions(data.event_catalog, item.event_types)}</div></fieldset><details class="notification-credential-rotate"><summary><span><b>轮换加密渠道配置</b><small>留空则保留当前凭据版本</small></span><strong>展开</strong></summary><div class="field-grid">${notificationConfigurationFields(item.channel)}</div></details><div class="form-error" role="alert"></div><div class="form-actions"><button class="secondary" type="button" data-notification-test ${item.enabled ? '' : 'disabled title="先启用并保存路由"'}>加入测试队列</button><button class="primary" type="submit">保存路由版本</button><button class="danger" type="button" data-delete-notification-route>删除路由</button></div></form></article>`;
  }).join('');
  const deliveryRows = data.deliveries.map(item => {
    const route = routesById.get(item.notification_route_id);
    const timing = item.status === 'RETRY_WAIT' ? `下次 ${fmtDate(item.next_attempt_at)}` : item.sent_at ? fmtDate(item.sent_at) : fmtDate(item.created_at);
    return `<tr><td data-label="事件"><b>${escapeHtml(notificationEventLabel(item.event_type))}</b><br><span class="subtle">${escapeHtml(item.payload?.summary || item.object_type)}</span></td><td data-label="路由">${escapeHtml(route?.name || shortId(item.notification_route_id))}<br><span class="subtle">${escapeHtml(notificationChannelLabel(item.channel))}</span></td><td data-label="范围">${escapeHtml(item.environment || '团队级')}<br><span class="subtle">${escapeHtml([item.account_id,item.venue].filter(Boolean).join(' · ') || '无账户范围')}</span></td><td data-label="状态"><span class="status-pill status-${escapeHtml(item.status)}">${escapeHtml(fmtStatus(item.status))}</span><br><span class="subtle">${item.attempt_count} / ${item.max_attempts} 次</span></td><td data-label="时间">${escapeHtml(timing)}</td><td data-label="错误">${escapeHtml(item.last_error_code || '—')}</td></tr>`;
  }).join('');
  const blockedCatalog = data.event_catalog.filter(item => item.integration_status !== 'ACTIVE');
  main.innerHTML = `<section class="page notification-page"><header class="page-head"><div><p class="eyebrow">${escapeHtml(data.scope.team_name)} · 团队级路由</p><h1>通知中心</h1><p class="lede">统一管理 Telegram、Slack、飞书 / Lark 和邮件。事件先写入当前团队的持久化投递队列；API 不直接外发，由独立最小权限进程发送。</p></div><button class="secondary" data-refresh>刷新投递事实</button></header>
    <div class="stats notification-stats"><article class="stat"><small>已配置路由</small><b>${data.routes.length}</b><span>${data.routes.filter(item => item.enabled).length} 条启用</span></article><article class="stat"><small>等待 / 重试</small><b>${waiting}</b><span>限流失败按退避计划自动重试</span></article><article class="stat"><small>已发送</small><b>${counts.SENT || 0}</b><span>每次投递都有审计记录</span></article><article class="stat"><small>需人工判断</small><b class="${attention ? 'danger-text' : ''}">${attention}</b><span>结果未知不会盲目重发</span></article></div>
    <article class="source-status"><div><p class="eyebrow">权限边界</p><h2>通知渠道不是交易主体</h2><p>交易 ${fmtStatus(data.channel_permissions.trading ? 'ENABLED' : 'DISABLED')} · 资金 ${fmtStatus(data.channel_permissions.funding ? 'ENABLED' : 'DISABLED')} · 签名 ${fmtStatus(data.channel_permissions.signing ? 'ENABLED' : 'DISABLED')} · 广播 ${fmtStatus(data.channel_permissions.broadcast ? 'ENABLED' : 'DISABLED')}。测试通知只入队；独立通知进程发送文本，不触发业务动作。</p></div><span class="status-pill status-READ_ONLY">队列模式</span></article>
    ${blockedCatalog.length ? `<div class="callout tone-attention"><b>资金通知事件尚未开放路由。</b><p>${escapeHtml(blockedCatalog.map(item => item.blocker).filter(Boolean).join(' '))}</p></div>` : ''}
    ${createForm}
    <section><div class="section-heading"><div><p class="eyebrow">脱敏配置</p><h2>团队通知路由</h2><p>修改路由会创建新版本；旧版本待发送任务会取消, 不会使用新凭据发送旧快照。</p></div><span class="status-pill">${data.routes.length} 条</span></div>${routeCards ? `<div class="notification-route-grid">${routeCards}</div>` : '<div class="callout tone-attention"><b>当前团队尚未配置通知路由。</b><p>系统事件仍会写审计；配置路由前不会向外部渠道发送。</p></div>'}</section>
    <section><div class="section-heading"><div><p class="eyebrow">持久化投递</p><h2>最近投递记录</h2><p>网络结果未知时停止自动重试, 避免外部渠道重复消息；明确限流才进入重试等待。</p></div><span class="status-pill">${data.deliveries.length} 条</span></div>${deliveryRows ? `<div class="table-wrap notification-table"><table><thead><tr><th>事件</th><th>路由</th><th>范围</th><th>状态</th><th>时间</th><th>错误</th></tr></thead><tbody>${deliveryRows}</tbody></table></div>` : '<div class="callout">当前团队没有通知投递记录。</div>'}</section>
  </section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  const newRouteForm = document.querySelector('#notification-route-create-form');
  newRouteForm?.elements.channel.addEventListener('change', () => {
    const channel = newRouteForm.elements.channel.value;
    newRouteForm.dataset.channel = channel;
    newRouteForm.querySelector('[data-notification-create-config]').innerHTML = notificationConfigurationFields(channel, {required:true});
  });
  newRouteForm?.addEventListener('submit', event => {
    event.preventDefault();
    const form = event.currentTarget;
    withPending(event.submitter, '保存中…', async () => {
      try {
        await api('/api/notification-routes', {method:'POST', body:JSON.stringify(notificationRoutePayload(form))});
        showToast('通知路由已加密保存');
        await route();
      } catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
  document.querySelectorAll('.notification-route-form').forEach(form => {
    form.addEventListener('submit', event => {
      event.preventDefault();
      withPending(event.submitter, '保存中…', async () => {
        try {
          await api(`/api/notification-routes/${form.dataset.routeId}`, {method:'PUT', body:JSON.stringify(notificationRoutePayload(form))});
          showToast('通知路由新版本已保存');
          await route();
        } catch (error) { showApiError(error, form.querySelector('.form-error')); }
      });
    });
    form.querySelector('[data-notification-test]')?.addEventListener('click', event => withPending(event.currentTarget, '入队中…', async () => {
      try {
        const result = await api(`/api/notification-routes/${form.dataset.routeId}/tests`, {method:'POST', body:JSON.stringify({idempotency_key:crypto.randomUUID()})});
        showToast(result.delivery_status === 'QUEUED' ? '测试通知已加入独立通知队列' : `测试投递状态: ${fmtStatus(result.delivery_status)}`);
        await route();
      } catch (error) { showApiError(error, form.querySelector('.form-error')); }
    }));
    form.querySelector('[data-delete-notification-route]')?.addEventListener('click', async event => {
      const trigger = event.currentTarget;
      const confirmed = await confirmAction({title:`删除通知路由“${form.dataset.routeName}”？`, message:'路由会停止接收新事件，加密渠道凭据会被清除，等待或重试中的投递会取消；已发送与历史投递记录继续保留。正在发送的投递完成前服务端会阻断删除。', confirmLabel:'确认删除路由'});
      if (!confirmed) return;
      await withPending(trigger, '删除中…', async () => {
        try {
          await api(`/api/notification-routes/${form.dataset.routeId}`, {method:'DELETE', body:JSON.stringify({expected_version:Number(form.dataset.version), idempotency_key:crypto.randomUUID()})});
          showToast('通知路由已删除；历史投递记录已保留');
          await route();
        } catch (error) { showApiError(error, form.querySelector('.form-error')); }
      });
    });
  });
}
