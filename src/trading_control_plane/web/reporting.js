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

const resultRateLabel = value => value === null || value === undefined
  ? '—'
  : `${fmtNumber(Number(value) * 100)}%`;
const resultRatioLabel = value => value === null || value === undefined ? '—' : fmtNumber(value);
const resultEnvironmentLabel = value => fmtEnvironment(value);
const resultEnvironmentNotices = {
  'Synthetic facts; not exchange execution or profit':'这是合成事实，不是交易所成交或真实收益。',
  'Recorded non-production facts; not live profit':'这是已记录的非生产事实，不是真实收益。',
  'Recorded LIVE facts; no profitability guarantee':'这是已记录的真实环境事实，不代表或保证盈利。',
};
const resultEnvironmentNotice = value => currentLanguage === 'en'
  ? String(value || '')
  : (resultEnvironmentNotices[String(value || '')] || fmtOperationalCopy(value));
const resultSourceModeLabel = value => currentLanguage === 'en'
  ? ({PERPTAPE:'Perptape',WEBHOOK:'Webhook',MANUAL:'Manual proposal',SYSTEM:'System proposal'}[value] || value)
  : ({PERPTAPE:'Perptape',WEBHOOK:'Webhook',MANUAL:'人工提案',SYSTEM:'系统提案'}[value] || value);
const resultSignalProviderLabel = value => currentLanguage === 'en'
  ? ({TRADINGVIEW:'TradingView',MODEL:'Custom model',PERPTAPE:'Perptape'}[value] || value)
  : ({TRADINGVIEW:'TradingView',MODEL:'自研模型',PERPTAPE:'Perptape'}[value] || value);
const resultSourceLabel = item => [resultSourceModeLabel(item.signal_source_mode), resultSignalProviderLabel(item.signal_provider)].filter(Boolean).join(' / ') || '未归因';
const resultRiskReasonLabel = reason => riskReasonGuidance[reason]?.label || `待核实（${reason}）`;
const resultStrategyLabel = item => item.strategy_id
  ? `${item.strategy_id} / ${item.strategy_version || '—'}`
  : '人工提案';

function resultDimensionRows(groups) {
  return (groups || []).flatMap(group => {
    const metrics = Object.entries(group.metrics_by_currency || {});
    const risk = group.risk_events_by_result || {};
    const riskLabel = `拒绝 ${risk.DENY || 0} · 缩量 ${risk.SCALE || 0} · 通过 ${risk.ALLOW || 0}`;
    if (!metrics.length) return `<tr><td data-label="范围"><b>${escapeHtml(group.label)}</b></td><td data-label="币种">—</td><td data-label="结果" colspan="5">暂无已记录交易结果</td><td data-label="风险决策">${escapeHtml(riskLabel)}</td></tr>`;
    return metrics.map(([currency, item]) => `<tr><td data-label="范围"><b>${escapeHtml(group.label)}</b></td><td data-label="币种">${escapeHtml(currency)}</td><td data-label="已平仓净收益">${fmtAmount(item.closed_net_pnl, currency)}<br><span class="subtle">未平仓当前值 ${fmtAmount(item.open_current_pnl, currency)}</span></td><td data-label="最大回撤">${fmtAmount(item.maximum_drawdown, currency)}</td><td data-label="胜率">${resultRateLabel(item.win_rate)}</td><td data-label="盈亏比">${resultRatioLabel(item.profit_loss_ratio)}</td><td data-label="已平 / 全部">${item.closed_count} / ${item.campaign_count}</td><td data-label="风险决策">${escapeHtml(riskLabel)}</td></tr>`);
  }).join('');
}

function resultDimensionSection(title, copy, groups) {
  const rows = resultDimensionRows(groups);
  return `<section class="result-dimension"><div class="section-heading"><div><p class="eyebrow">绩效维度</p><h2>${escapeHtml(title)}</h2><p>${escapeHtml(copy)}</p></div><span class="status-pill">${groups?.length || 0} 个分组</span></div>${rows ? `<div class="table-wrap result-table"><table><thead><tr><th>范围</th><th>币种</th><th>已平仓净收益</th><th>最大回撤</th><th>胜率</th><th>盈亏比</th><th>已平 / 全部</th><th>风险决策</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="callout">当前筛选范围没有可归因记录。</div>'}</section>`;
}

function resultDateTimeInput(value) {
  if (!value) return '';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

async function renderActualResults() {
  const current = new URLSearchParams(location.search);
  const environment = ['SHADOW','TESTNET','LIVE'].includes(current.get('environment'))
    ? current.get('environment')
    : 'SHADOW';
  const allowed = ['venue','account_id','strategy_id','strategy_version','signal_source_mode','signal_provider','from_time','to_time'];
  const request = new URLSearchParams({environment});
  allowed.forEach(key => { if (current.get(key)) request.set(key, current.get(key)); });
  const response = await api(`/api/results?${request.toString()}`);
  const data = response.data;
  const teamGroup = data.dimensions?.team?.[0];
  const teamMetrics = Object.entries(teamGroup?.metrics_by_currency || {});
  const headlineMetrics = teamMetrics.map(([currency, item]) => `<article class="stat result-stat"><small>${escapeHtml(currency)} · 已平仓净收益</small><b>${fmtAmount(item.closed_net_pnl, currency)}</b><span>未平仓当前值 ${fmtAmount(item.open_current_pnl, currency)} · 胜率 ${resultRateLabel(item.win_rate)} · 盈亏比 ${resultRatioLabel(item.profit_loss_ratio)} · 最大回撤 ${fmtAmount(item.maximum_drawdown, currency)}</span></article>`).join('');
  const events = data.risk_events || [];
  const eventRows = events.slice().reverse().map(item => `<tr><td data-label="决策时间">${fmtDate(item.created_at)}</td><td data-label="账户">${escapeHtml(item.account_id)}<br><span class="subtle">${escapeHtml(fmtVenueLabel(item.venue))}</span></td><td data-label="策略 / 来源">${escapeHtml(resultStrategyLabel(item))}<br><span class="subtle">${escapeHtml(resultSourceLabel(item))}</span></td><td data-label="结果"><span class="status-pill status-${escapeHtml(item.result)}">${escapeHtml(fmtStatus(item.result))}</span></td><td data-label="原因">${escapeHtml((item.reasons || []).map(resultRiskReasonLabel).join('、') || '无额外原因')}</td><td data-label="政策版本">${escapeHtml(item.policy_version || '—')} / r${escapeHtml(item.policy_revision || '—')}</td><td data-label="批准风险">${fmtNumber(item.risk_amount)}</td></tr>`).join('');
  const campaignRows = (data.campaigns || []).slice().reverse().map(item => `<tr data-href="/campaigns/${item.campaign_id}"><td data-label="更新时间">${fmtDate(item.updated_at)}</td><td data-label="标的"><b>${escapeHtml(item.symbol || shortId(item.instrument_id))}</b><br><span class="subtle">${escapeHtml(fmtDirection(item.direction))}</span></td><td data-label="账户">${escapeHtml(item.account_id)}<br><span class="subtle">${escapeHtml(fmtVenueLabel(item.venue))}</span></td><td data-label="策略 / 来源">${escapeHtml(resultStrategyLabel(item))}<br><span class="subtle">${escapeHtml(resultSourceLabel(item))}</span></td><td data-label="状态">${escapeHtml(fmtStatus(item.status))}</td><td data-label="总盈亏">${fmtAmount(item.final_pnl, item.currency)}</td></tr>`).join('');
  const stateCopy = data.data_status === 'EMPTY'
    ? '当前团队和筛选范围没有已记录的交易结果或风险决策。'
    : `已读取 ${data.coverage.campaign_count} 个交易任务、${data.coverage.closed_campaign_count} 个已平仓结果和 ${data.coverage.risk_event_count} 条风险决策。`;
  main.innerHTML = `<section class="page results-page"><header class="page-head"><div><p class="eyebrow">${escapeHtml(data.scope.team_name)} · ${resultEnvironmentLabel(environment)} · 已记录历史</p><h1>绩效与风险报表</h1><p class="lede">仅聚合当前 Workspace / Team 及获授权账户的服务端事实。影子、测试网和真实数据严格分开；币种不混算。</p></div><button class="secondary" data-refresh>刷新当前报表</button></header>
    <article class="source-status ${environment === 'LIVE' ? 'tone-attention' : ''}"><div><p class="eyebrow">数据性质</p><h2>${escapeHtml(resultEnvironmentLabel(environment))}</h2><p>${escapeHtml(resultEnvironmentNotice(data.environment_notice))} ${escapeHtml(stateCopy)}</p></div><span class="status-pill">${escapeHtml(fmtStatus(data.data_status))}</span></article>
    <form id="results-filter-form" class="form-panel compact-form result-filters"><div class="field-grid"><label>环境<select name="environment"><option value="SHADOW" ${environment === 'SHADOW' ? 'selected' : ''}>影子模式</option><option value="TESTNET" ${environment === 'TESTNET' ? 'selected' : ''}>测试网</option><option value="LIVE" ${environment === 'LIVE' ? 'selected' : ''}>真实环境</option></select></label><label>交易所<select name="venue"><option value="">全部</option>${['BINANCE','HYPERLIQUID','OKX','BYBIT'].map(value => `<option value="${value}" ${current.get('venue') === value ? 'selected' : ''}>${escapeHtml(fmtVenueLabel(value))}</option>`).join('')}</select></label><label>账户<input name="account_id" value="${escapeHtml(current.get('account_id') || '')}" maxlength="120" placeholder="精确账户 ID"></label><label>策略<input name="strategy_id" value="${escapeHtml(current.get('strategy_id') || '')}" maxlength="120" placeholder="精确策略 ID"></label><label>策略版本<input name="strategy_version" value="${escapeHtml(current.get('strategy_version') || '')}" maxlength="120" placeholder="精确版本"></label><label>信号源<select name="signal_source_mode"><option value="">全部</option>${['PERPTAPE','WEBHOOK','MANUAL','SYSTEM'].map(value => `<option value="${value}" ${current.get('signal_source_mode') === value ? 'selected' : ''}>${escapeHtml(resultSourceModeLabel(value))}</option>`).join('')}</select></label><label>信号提供方<select name="signal_provider"><option value="">全部</option>${['TRADINGVIEW','MODEL','PERPTAPE'].map(value => `<option value="${value}" ${current.get('signal_provider') === value ? 'selected' : ''}>${escapeHtml(resultSignalProviderLabel(value))}</option>`).join('')}</select></label><label>起始时间<input name="from_time" type="datetime-local" value="${escapeHtml(resultDateTimeInput(current.get('from_time')))}"></label><label>截止时间<input name="to_time" type="datetime-local" value="${escapeHtml(resultDateTimeInput(current.get('to_time')))}"></label></div><div class="form-actions"><a class="secondary" href="/results?environment=${environment}" data-link>清除其他筛选</a><button class="primary" type="submit">应用筛选</button></div></form>
    ${headlineMetrics ? `<div class="stats results-stats">${headlineMetrics}</div>` : '<div class="callout"><b>暂无收益指标。</b><p>只有具备完整 Campaign PnL 真源的记录才会进入聚合。</p></div>'}
    <div class="callout tone-attention"><b>百分比收益率与百分比回撤暂不可用。</b><p>当前没有覆盖所有账户、币种和时间边界的可信期初资本，因此只展示结算币种绝对收益和绝对回撤，不伪造百分比。</p></div>
    ${resultDimensionSection('账户', '同一交易所的不同账户独立计算，不跨账户合并风险事件。', data.dimensions?.account)}
    ${resultDimensionSection('策略与版本', 'Webhook 使用冻结 SignalEvent 的策略版本；Perptape 使用冻结提案快照。', data.dimensions?.strategy)}
    ${resultDimensionSection('信号源', 'Perptape、Webhook、直接人工提案和其他系统来源分开归因。', data.dimensions?.signal_source)}
    <section><div class="section-heading"><div><p class="eyebrow">服务端决策</p><h2>风险事件</h2><p>包含通过、缩量和拒绝；即使提案没有形成交易任务，拒绝事件仍会保留。</p></div><span class="status-pill">${events.length} 条</span></div>${eventRows ? `<div class="table-wrap result-table"><table><thead><tr><th>决策时间</th><th>账户</th><th>策略 / 来源</th><th>结果</th><th>原因</th><th>政策版本</th><th>批准风险</th></tr></thead><tbody>${eventRows}</tbody></table></div>` : '<div class="callout">当前范围没有已记录的风险决策。</div>'}</section>
    <section><div class="section-heading"><div><p class="eyebrow">可追溯明细</p><h2>交易任务结果</h2></div><span class="status-pill">${data.campaigns.length} 条</span></div>${campaignRows ? `<div class="table-wrap result-table"><table><thead><tr><th>更新时间</th><th>标的</th><th>账户</th><th>策略 / 来源</th><th>状态</th><th>总盈亏</th></tr></thead><tbody>${campaignRows}</tbody></table></div>` : '<div class="callout">当前范围没有交易任务结果。</div>'}</section>
  </section>`;
  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  document.querySelector('#results-filter-form')?.addEventListener('submit', event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const next = new URLSearchParams({environment:values.environment});
    ['venue','account_id','strategy_id','strategy_version','signal_source_mode','signal_provider'].forEach(key => {
      if (values[key]) next.set(key, values[key]);
    });
    ['from_time','to_time'].forEach(key => {
      if (values[key]) next.set(key, new Date(values[key]).toISOString());
    });
    history.replaceState({}, '', `/results?${next.toString()}`);
    route();
  });
  bindLinkedRows();
}

const notificationChannelLabel = value => ({TELEGRAM:'Telegram',SLACK:'Slack',LARK:'飞书 / Lark',EMAIL:'邮件'}[value] || value);
const notificationEventLabel = value => ({PROPOSAL_REVIEW_REQUIRED:'提案等待独立审核',RISK_DECISION_RECORDED:'风险决策已记录',CAMPAIGN_STATUS_CHANGED:'交易任务状态变化',CAPITAL_STATUS_CHANGED:'资金流程状态变化',SIGNAL_EVENT_RECEIVED:'收到团队信号',CONNECTION_CHECK_FAILED:'账户连接验证失败',TEST_NOTIFICATION:'渠道测试'}[value] || value);

function notificationEventOptions(catalog, selected = []) {
  const enabled = new Set(selected);
  return catalog.map(item => {
    const active = item.integration_status === 'ACTIVE';
    const checked = enabled.has(item.event_type);
    const help = active ? `模板 ${item.template_key} v${item.template_version}` : item.blocker;
    return `<label class="notification-event-option ${active ? '' : 'is-blocked'}"><input type="checkbox" name="event_types" value="${escapeHtml(item.event_type)}" ${checked ? 'checked' : ''} ${active ? '' : 'disabled'}><span><b>${escapeHtml(notificationEventLabel(item.event_type))}</b><small>${escapeHtml(help)}</small></span></label>`;
  }).join('');
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
  const createForm = data.can_manage ? `<details class="card notification-route-create"><summary><span><b>新建团队通知路由</b><small>每条路由只接收所选事件, 凭据独立加密保存</small></span><strong>展开</strong></summary><form id="notification-route-create-form" class="toolbox-content" data-version="0" data-channel="TELEGRAM"><div class="field-grid"><label>路由名称<input name="name" maxlength="120" required placeholder="例如 提案审核群"></label><label>通知渠道<select name="channel"><option value="TELEGRAM">Telegram</option><option value="SLACK">Slack</option><option value="LARK">飞书 / Lark</option><option value="EMAIL">邮件</option></select></label><label>路由状态<select name="enabled"><option value="true">启用</option><option value="false">停用</option></select></label></div><fieldset><legend>订阅事件</legend><div class="notification-event-grid">${notificationEventOptions(data.event_catalog, defaultEvents)}</div></fieldset><fieldset><legend>加密渠道配置</legend><div class="field-grid" data-notification-create-config>${notificationConfigurationFields('TELEGRAM', {required:true})}</div></fieldset><p class="safety-note">保存只建立通知路由。通知进程没有交易、资金、签名或广播接口；API 和页面只返回脱敏目的地。</p><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary" type="submit">加密保存路由</button></div></form></details>` : '';
  const routeCards = data.routes.map(item => {
    const destination = item.configuration_metadata?.destination_hint || '已加密配置';
    const eventTags = item.event_types.map(eventType => `<span class="tag">${escapeHtml(notificationEventLabel(eventType))}</span>`).join('');
    if (!data.can_manage) return `<article class="card notification-route-card"><div class="card-heading"><div><p class="eyebrow">${escapeHtml(notificationChannelLabel(item.channel))} · ${escapeHtml(destination)}</p><h2>${escapeHtml(item.name)}</h2></div><span class="status-pill ${item.enabled ? 'status-APPROVED' : 'status-DISABLED'}">${item.enabled ? '已启用' : '已停用'}</span></div><div class="tag-row">${eventTags}</div><dl class="definition-grid">${definition('配置状态', 'AES-256-GCM 加密')}${definition('路由 / 凭据版本', `${item.version} / ${item.credential_version}`)}${definition('最近更新', fmtDate(item.updated_at))}</dl></article>`;
    return `<article class="card notification-route-card"><div class="card-heading"><div><p class="eyebrow">${escapeHtml(notificationChannelLabel(item.channel))} · ${escapeHtml(destination)}</p><h2>${escapeHtml(item.name)}</h2></div><span class="status-pill ${item.enabled ? 'status-APPROVED' : 'status-DISABLED'}">${item.enabled ? '已启用' : '已停用'}</span></div><form class="notification-route-form" data-route-id="${escapeHtml(item.notification_route_id)}" data-version="${item.version}" data-channel="${escapeHtml(item.channel)}"><div class="field-grid"><label>路由名称<input name="name" value="${escapeHtml(item.name)}" maxlength="120" required></label><label>通知渠道<input value="${escapeHtml(notificationChannelLabel(item.channel))}" disabled><input name="channel" type="hidden" value="${escapeHtml(item.channel)}"></label><label>路由状态<select name="enabled"><option value="true" ${item.enabled ? 'selected' : ''}>启用</option><option value="false" ${item.enabled ? '' : 'selected'}>停用</option></select></label></div><fieldset><legend>订阅事件</legend><div class="notification-event-grid">${notificationEventOptions(data.event_catalog, item.event_types)}</div></fieldset><details class="notification-credential-rotate"><summary><span><b>轮换加密渠道配置</b><small>留空则保留当前凭据版本</small></span><strong>展开</strong></summary><div class="field-grid">${notificationConfigurationFields(item.channel)}</div></details><div class="form-error" role="alert"></div><div class="form-actions"><button class="secondary" type="button" data-notification-test ${item.enabled ? '' : 'disabled title="先启用并保存路由"'}>加入测试队列</button><button class="primary" type="submit">保存路由版本</button></div></form></article>`;
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
  });
}
