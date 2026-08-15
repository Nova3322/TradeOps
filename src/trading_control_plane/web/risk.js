const riskControlStatusLabel = (value) => ({
  NORMAL:'正常开放', NO_PYRAMID:'禁止加仓', REDUCE_ONLY:'仅允许减仓', KILL_SWITCH:'紧急停止',
  ENABLED:'已启用', DISABLED:'已关闭', PENDING_REVIEW:'等待独立审核', APPROVED:'审核完成待执行',
  REJECTED:'已拒绝', EXPIRED:'已过期', EXECUTED:'已执行',
}[value] || value);

function formatControlBlocker(value) {
  const [code, environment, accountId, venue, detail] = String(value || '').split(':');
  const venueLabel = fmtVenueLabel(venue);
  const scope = accountId ? `（${venueLabel} · ${fmtDefaultAccountLabel(accountId)}）` : '';
  const probeFailure = ({
    BINANCE_RATE_LIMITED:'币安只读接口限流，系统会按计划重试',
    HYPERLIQUID_RATE_LIMITED:'Hyperliquid 只读接口限流，系统会按计划重试',
    BINANCE_AUTH_FAILED:'币安只读鉴权或权限检查失败',
    HYPERLIQUID_AUTH_FAILED:'Hyperliquid 只读鉴权或权限检查失败',
    TEAM_NOT_OPERATIONAL:'团队尚未完成安全配置，暂不执行只读检查',
  }[detail] || (detail ? '只读检查未成功；请在系统状态查看技术详情' : '只读检查未成功'));
  return ({
    LIVE_SCOPE_CONFIGURATION_REQUIRED:'生产账户范围未配置：至少配置一个明确的 LIVE 账户与交易所',
    KILL_SWITCH_MANUAL_RECOVERY_REQUIRED:'系统处于紧急停止：必须先完成人工处置，不能从本页恢复',
    ACTIVE_NEW_RISK_INTENT:'仍有初仓或加仓意图正在处理',
    ORDER_INTENT_UNKNOWN:'订单意图结果未知', VENUE_ORDER_UNKNOWN:'交易所订单结果未知',
    RISK_RESERVATION_UNKNOWN:'风险预留结果未知', CAMPAIGN_UNKNOWN:'交易任务结果未知',
    UNBOUND_OPEN_ORDER:'发现未绑定到受控意图的开放订单',
    READ_ONLY_SOURCE_MISSING:`尚无成功的交易所只读探针${scope}`,
    READ_ONLY_SOURCE_FAILED:`交易所只读探针失败${scope}：${probeFailure}`,
    READ_ONLY_SOURCE_STALE:`交易所只读探针已过期${scope}`,
    ACCOUNT_EQUITY_MISSING:`账户权益事实缺失${scope}`,
    ACCOUNT_EQUITY_UNKNOWN:`账户权益事实未知${scope}`,
    ACCOUNT_EQUITY_STALE:`账户权益事实已过期${scope}`,
    POSITION_FACTS_MISSING:`仓位事实缺失${scope}`,
    POSITION_UNKNOWN:`仓位事实未知${scope}`, POSITION_STALE:`仓位事实已过期${scope}`,
    PROTECTION_INCOMPLETE:`持仓保护不完整${scope}`, PROTECTION_STALE:`持仓保护事实已过期${scope}`,
    COMPUTED_RECONCILIATION_MATCH_REQUIRED:`缺少最新计算型一致对账${scope}`,
    RECONCILIATION_STALE:`计算型对账早于最新事实或已过期${scope}`,
    CONTROL_SCOPE_INVALID:'受控账户范围格式无效',
    RISK_POLICY_MISSING:'当前团队尚未创建风险政策',
    RISK_LIMITS_UNCONFIGURED:'当前团队的单笔、连续亏损与冷却阈值未配置',
  }[code] || `未识别的安全阻断：${value}`);
}

function formatControlReason(value) {
  const normalized = String(value || '').trim();
  return ({
    'administrator paused new risk from Web':'管理员暂停了所有新增风险',
    'administrator disabled AUTO_ADD from Web':'管理员关闭了全局自动加仓',
  }[normalized] || (/[\u3400-\u9fff]/.test(normalized) ? normalized : '管理员执行了风险控制变更'));
}

function formatRiskActionReason(value, conditions = {blockers:[]}) {
  const blockerCount = Array.isArray(conditions?.blockers) ? conditions.blockers.length : 0;
  return ({
    READY:'可执行',
    SYSTEM_ADMIN_REQUIRED:'仅最高管理员可以直接恢复',
    SYSTEM_ALREADY_NORMAL:'无需恢复：风险政策当前为正常状态',
    REALTIME_CONDITIONS_BLOCKED:`实时安全条件未全部通过（${blockerCount} 项）`,
    OPERATOR_REQUIRED:'当前身份不是交易运维人员',
    RESTORE_REQUEST_ALREADY_ACTIVE:'已有一条恢复申请正在处理',
    INDEPENDENT_REVIEWER_REQUIRED:'当前身份不是独立审核人员',
    NO_REVIEWABLE_REQUEST:'当前没有可由你独立审核的恢复申请',
    EXECUTION_REQUIREMENTS_NOT_MET:'当前没有满足执行条件的已审核申请',
  }[value] || '当前状态不允许执行；请按实时条件和申请状态处理');
}

function renderRiskPolicyConfiguration(policy, allowed) {
  const canRequest = roleNames().includes('OPERATOR');
  if (!allowed && !canRequest) return policy?.limits_configured ? '' : '<div class="callout"><b>风险限额未配置：</b>管理员可直接填写；普通权限需发起提案并由独立审核人员批准后执行。</div>';
  const value = (name) => policy?.[name] ?? '';
  const title = policy ? (policy.limits_configured ? '日常团队风险政策' : '完成团队风险政策') : '创建团队风险政策';
  const copy = allowed
    ? '管理员可直接修改所有阈值。保存会创建新版本，并使本团队旧交易授权全部失效。'
    : '普通权限提交后只会形成冻结提案；另一名独立审核人员批准并执行后才生效。';
  const status = policy?.limits_configured ? `当前版本 · ${escapeHtml(policy.version || '已配置')}` : '需要完成配置';
  return `<details class="card operation-toolbox risk-policy-editor"><summary aria-labelledby="risk-policy-summary-title risk-policy-summary-copy risk-policy-summary-action"><span><b id="risk-policy-summary-title">日常风控</b> <small id="risk-policy-summary-copy">${status}；点击后修改限额与事实时效</small></span> <strong id="risk-policy-summary-action">修改</strong></summary><form id="risk-policy-form" class="toolbox-content compact-form" data-risk-policy-workflow="${allowed ? 'DIRECT' : 'REVIEWED'}"><div class="card-heading"><div><p class="eyebrow">日常风控设置</p><h2>${title}</h2></div><span class="status-pill">${allowed ? '管理员直接修改' : '提案 · 独立审核'}</span></div><p class="safety-note">${copy}</p><div class="field-grid"><label>政策版本<input name="version" maxlength="120" placeholder="例如 team-risk-2026-08-v1" required></label><label>团队最大总风险<input name="max_total_risk" type="number" step="any" min="0" value="${escapeHtml(value('max_total_risk'))}" required></label><label>单账户最大风险<input name="max_account_risk" type="number" step="any" min="0" value="${escapeHtml(value('max_account_risk'))}" required></label><label>最大单笔亏损<input name="max_single_loss" type="number" step="any" min="0" value="${escapeHtml(value('max_single_loss'))}" required></label><label>最大连续亏损次数<input name="max_consecutive_losses" type="number" step="1" min="1" value="${escapeHtml(value('max_consecutive_losses'))}" required></label><label>亏损冷却期（秒）<input name="loss_cooldown_seconds" type="number" step="1" min="1" value="${escapeHtml(value('loss_cooldown_seconds'))}" required></label><label>事实最大时效（秒）<input name="max_fact_age_seconds" type="number" step="1" min="1" value="${escapeHtml(value('max_fact_age_seconds'))}" required></label></div><label>变更理由<textarea name="reason" rows="3" minlength="10" required>按当前团队已确认的风险授权边界配置版本化政策</textarea></label><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">${allowed ? '直接保存新版本' : '发起政策变更提案'}</button></div></form></details>`;
}

function renderRiskControlPanel(control) {
  if (!control) return `<article class="card"><div class="card-heading"><div><p class="eyebrow">团队风险控制</p><h2>当前职责不能查看团队政策</h2></div><span class="status-pill">当前权限范围</span></div><p class="subtle">当前身份只能查看获准访问的账户和交易所风险，不能读取或执行团队风险政策、自动加仓控制和恢复申请。</p><p class="safety-note">这不代表团队风险状态正常。新增风险仍会由服务端强制检查；你仍可使用下表查看风险占用、唯一减仓目标和最近对账。</p><div class="toolbar"><a class="secondary" href="/home" data-link>返回当前任务</a><a class="primary" href="/campaigns/alerts" data-link>查看运行告警</a></div></article>`;
  const policy = control.policy;
  const gate = control.auto_add_gate;
  const canConfigurePolicy = Boolean(control.actions?.configure_policy?.allowed);
  if (!policy) return `<section class="risk-control-overview"><article class="home-status tone-attention"><div><p class="eyebrow">Fail closed</p><h2>当前团队尚无风险政策</h2><p>服务端不会读取其他团队的全局政策，也不会允许提案进入新增风险执行链路。</p></div><span class="status-pill status-DENY">新增风险阻断</span></article>${renderRiskPolicyConfiguration(null, canConfigurePolicy)}</section>`;
  const conditions = control.restore_conditions;
  const restoreGateLabel = conditions.ready ? '实时条件全部通过' : `${conditions.blockers.length} 项阻塞`;
  const blockedScopeCount = new Set((conditions.checks || [])
    .filter(check => check.status === 'BLOCKED' && check.scope)
    .map(check => `${check.scope.environment}:${check.scope.account_id}:${check.scope.venue}`)).size;
  const productionFactLabel = conditions.ready
    ? '全部检查通过'
    : blockedScopeCount
      ? `${blockedScopeCount} 个范围受阻`
      : `${conditions.blockers.length} 项待处理`;
  const isAdmin = roleNames().includes('SYSTEM_ADMIN');
  const isOperator = roleNames().includes('OPERATOR');
  const isReviewer = roleNames().includes('REVIEWER');
  const systemAlreadyNormal = policy.system_state === 'NORMAL';
  const activeRequests = control.requests.filter(item => !item.superseded_by_control_state && ['PENDING_REVIEW','APPROVED'].includes(item.status));
  const historicalRequests = control.requests.filter(item => item.superseded_by_control_state || !['PENDING_REVIEW','APPROVED'].includes(item.status));
  const renderConditionRows = checks => checks.map(check => {
    const venueLabel = fmtVenueLabel(check.scope?.venue);
    const scope = check.scope ? `${venueLabel} · ${fmtDefaultAccountLabel(check.scope.account_id)}` : '全局';
    const reasons = (check.reason || []).map(reason => reason === 'CURRENT' ? '当前检查通过' : formatControlBlocker(reason)).join('；');
    return `<tr><td data-label="条件 / 范围"><b>${escapeHtml(check.label)}</b><br><span class="subtle">${escapeHtml(scope)}</span></td><td data-label="状态"><span class="status-pill status-${check.status === 'PASS' ? 'APPROVED' : 'DENY'}">${check.status === 'PASS' ? '通过' : '阻塞'}</span></td><td data-label="精确原因">${escapeHtml(reasons)}</td><td data-label="处理角色">${escapeHtml(fmtRole(check.role))}</td><td data-label="下一步">${escapeHtml(check.next_action)}</td></tr>`;
  }).join('');
  const blockingChecks = (conditions.checks || []).filter(check => check.status === 'BLOCKED');
  const passingChecks = (conditions.checks || []).filter(check => check.status === 'PASS');
  const blockingRows = renderConditionRows(blockingChecks);
  const passingRows = renderConditionRows(passingChecks);
  const directForm = isAdmin && !systemAlreadyNormal ? (control.actions.direct_restore.allowed
    ? `<form id="risk-direct-restore-form" class="form-panel compact-form"><h2>最高管理员直接恢复</h2><p class="safety-note">所有实时条件已通过。二次确认后，系统会再次校验并创建新的 NORMAL 风险政策；不会开启 AUTO_ADD，旧 TradingAuthorization 继续失效。</p><label>恢复理由<textarea name="reason" rows="3" minlength="10" required>已逐项确认全部实时安全条件，恢复新增风险但保持自动加仓关闭</textarea></label><div class="form-error" role="alert"></div><button class="primary">确认并直接恢复</button></form>`
    : `<div class="callout"><b>最高管理员当前不能直接恢复：</b>${escapeHtml(formatRiskActionReason(control.actions.direct_restore.reason, conditions))}。请先处理上方明确阻断；不会创建占位申请。</div>`) : '';
  const requestForm = isOperator && !systemAlreadyNormal && control.actions.request_restore.allowed
    ? `<form id="risk-restore-form" class="form-panel compact-form"><h2>操作人员发起恢复申请</h2><p class="safety-note">申请冻结当前政策、账户范围和控制版本，由另一名独立审核人员审核。恢复永远不会开启 AUTO_ADD，也不会复活旧授权。</p><label>恢复理由<textarea name="reason" rows="4" minlength="10" required>已完成异常处置，请独立审核人员按实时条件复核恢复</textarea></label><div class="form-error" role="alert"></div><button class="primary">创建恢复申请</button></form>`
    : (isOperator && !systemAlreadyNormal ? `<div class="callout"><b>当前不能创建恢复申请：</b>${escapeHtml(formatRiskActionReason(control.actions.request_restore.reason, conditions))}。</div>` : '');
  const controlProposalForm = isOperator ? `<form id="risk-control-proposal-form" class="form-panel compact-form"><div class="card-heading"><div><p class="eyebrow">加减仓与全局暂停</p><h2>发起风控变更提案</h2></div><span class="status-pill">普通权限 · 独立审核</span></div><p class="safety-note">普通权限的关闭/恢复加仓、暂停/解除风险暂停均不会直接生效；提交后需要另一名独立审核人员批准并执行。</p><label>变更理由<textarea name="reason" rows="3" minlength="10" required>根据当前团队运行情况申请调整加减仓与风险暂停设置</textarea></label><div class="form-error" role="alert"></div><div class="form-actions"><button class="secondary" type="submit" data-risk-change-type="${gate.status === 'ENABLED' ? 'DISABLE_AUTO_ADD' : 'ENABLE_AUTO_ADD'}">${gate.status === 'ENABLED' ? '提案：关闭自动加仓' : '提案：恢复自动加仓'}</button><button class="danger" type="submit" data-risk-change-type="${systemAlreadyNormal ? 'PAUSE_NEW_RISK' : 'RESUME_NEW_RISK'}">${systemAlreadyNormal ? '提案：暂停所有风险' : '提案：解除风险暂停'}</button></div></form>` : '';
  const normalRestoreState = systemAlreadyNormal
    ? conditions.ready
      ? '<div class="success-note"><b>当前无需恢复：</b>风险政策为正常，生产事实也通过实时检查；每笔新增风险仍会在服务端重新校验。自动加仓保持关闭，旧交易授权不会复活。</div>'
      : '<div class="callout"><b>政策正常，但生产事实仍有阻塞条件：</b>当前不需要执行“恢复”；若系统再次进入受限状态，必须先解决这些问题。每笔新增风险仍会按所属账户事实与交易所只读状态重新检查，失败即拒绝。</div>'
    : '';
  const renderRequestCard = item => {
    const changeLabel = ({POLICY_UPDATE:'团队风险政策',DISABLE_AUTO_ADD:'关闭自动加仓',ENABLE_AUTO_ADD:'恢复自动加仓',PAUSE_NEW_RISK:'暂停所有风险',RESUME_NEW_RISK:'解除风险暂停'})[item.change_type || 'RESUME_NEW_RISK'];
    const superseded = Boolean(item.superseded_by_control_state);
    const isRequester = item.requester_id === session.user_id;
    const reviewedByMe = item.reviews.some(review => review.reviewer_id === session.user_id);
    const reviewUi = !superseded && item.status === 'PENDING_REVIEW' && isReviewer && !isRequester && !reviewedByMe
      ? `<label>独立审核理由<textarea id="risk-review-${item.request_id}" rows="3">已核对冻结版本、恢复影响和当前阻塞条件</textarea></label><div class="toolbar"><button class="primary" data-risk-review="${item.request_id}" data-decision="APPROVE" data-version="${item.version}">确认并批准</button><button class="danger" data-risk-review="${item.request_id}" data-decision="REJECT" data-version="${item.version}">拒绝申请</button></div>`
      : superseded ? '' : `<p class="subtle">${isRequester ? '你是申请人，不能审核自己的申请。' : reviewedByMe ? '你已完成本申请的独立审核。' : isReviewer ? '该申请当前不在待审核状态。' : '当前角色不是独立审核人员。'}</p>`;
    const executeUi = !superseded && item.status === 'APPROVED' && (isReviewer || isAdmin) && !isRequester
      ? `<button class="danger" data-risk-execute="${item.request_id}" data-version="${item.version}">确认并执行恢复</button><p class="safety-note">执行时会再次进行安全检查；任何数据、权限范围、风险政策或控制开关发生变化，都会拒绝恢复。</p>`
      : '';
    const statusLabel = superseded ? '已失效（控制状态已变化）' : riskControlStatusLabel(item.status);
    const supersededNote = superseded ? '<div class="callout"><b>无需继续审核：</b>系统已经恢复，或申请冻结的控制版本已被后续操作替代。</div>' : '';
    const policySummary = item.change_type === 'POLICY_UPDATE' ? definition('目标政策版本', item.requested_policy?.version || '—') : '';
    return `<article class="card"><div class="card-head"><div><p class="eyebrow">风控变更提案</p><h2>${escapeHtml(changeLabel)} · ${statusLabel}</h2></div><span class="tag">${shortId(item.request_id)}</span></div>${supersededNote}<dl class="definition-grid">${definition('申请人', item.requester_username || shortId(item.requester_id))}${definition('变更类型', changeLabel)}${policySummary}${definition('最早执行', fmtDate(item.execute_after))}${definition('到期', fmtDate(item.expires_at))}${definition('原自动加仓状态', fmtStatus(item.source_auto_add_status))}</dl><p>${escapeHtml(item.reason)}</p><h3>独立审核记录</h3>${item.reviews.length ? item.reviews.map(review => `<div class="callout"><b>${escapeHtml(review.decision === 'APPROVE' ? '批准' : '拒绝')}</b> · ${escapeHtml(review.reason)}<br><span class="subtle">${escapeHtml(review.reviewer_username || shortId(review.reviewer_id))} · ${fmtDate(review.created_at)}</span></div>`).join('') : `<p class="subtle">${superseded ? '申请已经失效，不再等待审核。' : '等待一名非申请人的独立审核人员。'}</p>`}<div class="review-action-panel">${reviewUi}${executeUi}</div></article>`;
  };
  const requestCards = activeRequests.length
    ? activeRequests.map(renderRequestCard).join('')
    : '<section class="empty-state compact-empty-state"><div><h2>当前没有待处理的恢复申请</h2><p>操作人员可在风险受限时发起申请；最高管理员仅在全部实时条件通过后直接恢复。</p></div></section>';
  const requestHistory = historicalRequests.length
    ? `<details class="operation-toolbox risk-request-history"><summary aria-labelledby="risk-history-summary-title risk-history-summary-copy risk-history-summary-action"><span><b id="risk-history-summary-title">历史恢复申请</b> <small id="risk-history-summary-copy">${historicalRequests.length} 条已结束或已失效记录，不计入当前待办</small></span> <strong id="risk-history-summary-action">查看历史</strong></summary><div class="stack">${historicalRequests.map(renderRequestCard).join('')}</div></details>`
    : '';
  const actionSummary = `<article class="card"><h2>为什么当前能 / 不能操作</h2><dl class="definition-grid">${definition('当前身份', currentRoleSummary())}${definition('管理员直接恢复', formatRiskActionReason(control.actions.direct_restore.reason, conditions))}${definition('操作员发起申请', formatRiskActionReason(control.actions.request_restore.reason, conditions))}${definition('独立审核', formatRiskActionReason(control.actions.review_restore.reason, conditions))}${definition('审核后执行', formatRiskActionReason(control.actions.execute_restore.reason, conditions))}</dl></article>`;
  const conditionTable = rows => `<div class="table-scroll-hint risk-condition-scroll-hint" data-table-hint>左右滑动查看完整安全条件</div><div class="table-wrap"><table class="risk-condition-table"><thead><tr><th>条件 / 范围</th><th>状态</th><th>精确原因</th><th>处理角色</th><th>下一步</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  const passedConditions = passingChecks.length
    ? `<details class="risk-passed-conditions"><summary aria-labelledby="risk-passed-summary-title risk-passed-summary-copy risk-passed-summary-action"><span><b id="risk-passed-summary-title">${passingChecks.length} 项已通过</b> <small id="risk-passed-summary-copy">通过项不占用当前待办；恢复或新增风险时仍会重新校验</small></span> <strong id="risk-passed-summary-action">按需查看</strong></summary>${conditionTable(passingRows)}</details>`
    : '';
  const blockingConditions = blockingChecks.length
    ? `<div class="risk-blocking-conditions"><div class="risk-condition-section-head"><b>当前阻塞</b></div>${conditionTable(blockingRows)}</div>`
    : '<div class="success-note"><b>当前没有实时阻塞：</b>所有生产事实均通过本轮检查；后续操作仍会在服务端重新校验。</div>';
  const conditionDetails = `<details class="operation-toolbox risk-condition-details" ${blockingChecks.length ? 'open' : ''}><summary aria-labelledby="risk-condition-summary-title risk-condition-summary-copy risk-condition-summary-action"><span><b id="risk-condition-summary-title">实时安全条件</b> <small id="risk-condition-summary-copy">${blockingChecks.length ? '阻塞优先展示；已通过条件收起' : '全部条件通过；恢复或新增风险时仍会重新校验'}</small></span> <strong id="risk-condition-summary-action">${blockingChecks.length ? '当前必须处理' : '查看明细'}</strong></summary><div class="risk-condition-content"><p class="subtle">当前团队政策与生产账户事实分开判断。这里只把阻塞项作为当前待办，并逐项说明原因、负责角色和下一步。</p>${blockingConditions}${passedConditions}</div></details>`;
  const policyLabel = systemAlreadyNormal ? '政策正常' : riskControlStatusLabel(policy.system_state);
  return `<section class="risk-control-overview"><div class="stats"><div class="stat"><small>风险政策</small><b>${policy.limits_configured ? policyLabel : '限额未配置'}</b></div><div class="stat"><small>生产范围</small><b>${productionFactLabel}</b></div><div class="stat"><small>自动加仓</small><b>${riskControlStatusLabel(gate.status)}</b></div><div class="stat"><small>实时条件</small><b>${restoreGateLabel}</b></div></div>${renderRiskPolicyConfiguration(policy, canConfigurePolicy)}<div class="detail-layout"><article class="card"><h2>当前团队控制状态</h2><dl class="definition-grid">${definition('团队最大总风险', fmtNumber(policy.max_total_risk))}${definition('单账户最大风险', policy.max_account_risk == null ? '未配置' : fmtNumber(policy.max_account_risk))}${definition('最大单笔亏损', policy.max_single_loss == null ? '未配置' : fmtNumber(policy.max_single_loss))}${definition('连续亏损 / 冷却', policy.max_consecutive_losses == null ? '未配置' : `${policy.max_consecutive_losses} 次 / ${policy.loss_cooldown_seconds} 秒`)}${definition('政策原因', formatControlReason(policy.reason))}${definition('政策更新人', policy.updated_by_username || shortId(policy.updated_by))}${definition('政策更新时间', fmtDate(policy.updated_at))}${definition('控制原因', formatControlReason(gate.reason))}${definition('控制操作人', gate.operator_username || shortId(gate.operator_id))}${definition('控制更新时间', fmtDate(gate.updated_at))}</dl><p class="safety-note">“仅允许减仓”仍允许减仓与退出；暂停新增风险后，本团队旧 TradingAuthorization 永久失效。恢复或开启自动加仓都不会复活旧授权。</p></article>${actionSummary}</div>${normalRestoreState}${conditionDetails}${directForm}${requestForm}${controlProposalForm}<div class="section-head"><div><p class="eyebrow">提案与审核</p><h2>当前风控变更待办</h2></div></div><div class="stack">${requestCards}</div>${requestHistory}</section>`;
}

async function bindRiskControlActions() {
  document.querySelector('#risk-policy-form')?.addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form)); const button = event.submitter || form.querySelector('button');
    await withPending(button, form.dataset.riskPolicyWorkflow === 'DIRECT' ? '保存中…' : '冻结提案中…', async () => { try {
      const requestedPolicy = {version:data.version, max_total_risk:data.max_total_risk, max_account_risk:data.max_account_risk, max_single_loss:data.max_single_loss, max_consecutive_losses:Number(data.max_consecutive_losses), loss_cooldown_seconds:Number(data.loss_cooldown_seconds), max_fact_age_seconds:Number(data.max_fact_age_seconds)};
      if (form.dataset.riskPolicyWorkflow === 'DIRECT') {
        const current = await api('/api/risk-controls');
        await api('/api/risk-controls/policy', {method:'PUT', body:JSON.stringify({...requestedPolicy, expected_revision:Number(current.policy?.revision || 0), reason:data.reason, idempotency_key:crypto.randomUUID()})});
        showToast('团队风险政策已直接保存；旧交易授权已失效');
      } else {
        await api('/api/risk-controls/changes', {method:'POST', body:JSON.stringify({change_type:'POLICY_UPDATE', requested_policy:requestedPolicy, reason:data.reason, idempotency_key:crypto.randomUUID()})});
        showToast('政策变更提案已冻结，等待独立审核');
      }
      await route();
    } catch (error) { showApiError(error, form.querySelector('.form-error')); } });
  });
  document.querySelector('#risk-control-proposal-form')?.addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget; const reason = new FormData(form).get('reason'); const button = event.submitter; const changeType = button.dataset.riskChangeType;
    await withPending(button, '冻结提案中…', async () => { try { await api('/api/risk-controls/changes', {method:'POST', body:JSON.stringify({change_type:changeType, reason, idempotency_key:crypto.randomUUID()})}); showToast('风控变更提案已冻结，等待独立审核'); await route(); } catch (error) { showApiError(error, form.querySelector('.form-error')); } });
  });
  document.querySelector('#risk-restore-form')?.addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const button = event.submitter || form.querySelector('button');
    await withPending(button, '冻结中…', async () => { try { await api('/api/risk-controls/restores', {method:'POST', body:JSON.stringify({reason:data.get('reason'), restore_auto_add:false, idempotency_key:crypto.randomUUID()})}); showToast('恢复申请已冻结，等待独立审核人员'); await route(); } catch (error) { showApiError(error, form.querySelector('.form-error')); } });
  });
  document.querySelector('#risk-direct-restore-form')?.addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget; const reason = new FormData(form).get('reason'); const button = event.submitter || form.querySelector('button');
    const confirmed = await confirmAction({title:'最高管理员直接恢复？', message:'系统会再次验证全部生产账户条件并创建新的 NORMAL 政策。AUTO_ADD 保持关闭，旧 TradingAuthorization 不会恢复。', confirmLabel:'确认并恢复'}); if (!confirmed) return;
    await withPending(button, '验证中…', async () => { try { const status = await api('/api/risk-controls'); const policy = status.policy; const grant = await api('/api/auth/mock/step-up', {method:'POST', body:JSON.stringify({action:'risk.restore.direct', object_id:policy.policy_id, object_version:policy.revision})}); await api('/api/risk-controls/restore-direct', {method:'POST', body:JSON.stringify({reason, idempotency_key:crypto.randomUUID(), action_grant:grant.action_grant})}); showToast('已创建新的正常风险政策；AUTO_ADD 与旧授权保持失效'); await route(); } catch (error) { showApiError(error, form.querySelector('.form-error')); } });
  });
  document.querySelectorAll('[data-risk-review]').forEach(button => button.addEventListener('click', async () => {
    const requestId = button.dataset.riskReview; const version = Number(button.dataset.version); const decision = button.dataset.decision; const reason = document.querySelector(`#risk-review-${requestId}`)?.value || '独立审核拒绝';
    if (decision === 'APPROVE') { const confirmed = await confirmAction({title:'批准这份恢复申请？', message:'本次只记录独立审核票，不会立即恢复。执行时仍会重新验证全部实时条件；AUTO_ADD 保持关闭，旧授权不会恢复。', confirmLabel:'确认批准'}); if (!confirmed) return; }
    await withPending(button, '提交中…', async () => { try { let action_grant = null; if (decision === 'APPROVE') { const grant = await api('/api/auth/mock/step-up', {method:'POST', body:JSON.stringify({action:'risk.restore.review', object_id:requestId, object_version:version})}); action_grant = grant.action_grant; } await api(`/api/risk-controls/restores/${requestId}/reviews`, {method:'POST', body:JSON.stringify({decision, reason, expected_version:version, idempotency_key:crypto.randomUUID(), action_grant})}); showToast(decision === 'APPROVE' ? '独立审核票已记录' : '恢复申请已拒绝'); await route(); } catch (error) { showApiError(error); } });
  }));
  document.querySelectorAll('[data-risk-execute]').forEach(button => button.addEventListener('click', async () => {
    const requestId = button.dataset.riskExecute; const version = Number(button.dataset.version);
    const confirmed = await confirmAction({title:'执行受审核恢复？', message:'系统将重新验证所有受控范围、当前数据、对账结果、未决订单、冷却期和控制版本。只会创建新的正常风险政策；旧授权和旧的可用加仓次数不会恢复。', confirmLabel:'重新验证并执行'}); if (!confirmed) return;
    await withPending(button, '验证中…', async () => { try { const grant = await api('/api/auth/mock/step-up', {method:'POST', body:JSON.stringify({action:'risk.restore.execute', object_id:requestId, object_version:version})}); await api(`/api/risk-controls/restores/${requestId}/execute`, {method:'POST', body:JSON.stringify({expected_version:version, idempotency_key:crypto.randomUUID(), action_grant:grant.action_grant})}); showToast('新的正常风险政策已创建；旧授权保持失效'); await route(); } catch (error) { showApiError(error); } });
  }));
}
