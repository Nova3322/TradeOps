function positionPnlBucket(item) {
  if (item.unrealized_pnl === null || item.unrealized_pnl === undefined) return 'UNKNOWN';
  const pnl = Number(item.unrealized_pnl);
  if (pnl > 0) return 'PROFIT';
  if (pnl < 0) return 'LOSS';
  return 'FLAT';
}

function positionAccountScope(item) {
  return JSON.stringify([item.venue, item.account_id]);
}

function positionPnlClass(value) {
  if (value === null || value === undefined) return 'warning-text';
  if (Number(value) > 0) return 'direction-long';
  if (Number(value) < 0) return 'direction-short';
  return '';
}

async function renderCurrentPositions() {
  const environment = currentWorkflowEnvironment();
  if (!['LIVE','TESTNET'].includes(environment)) {
    main.innerHTML = '<section class="page"><header class="page-head"><div><p class="eyebrow">账户只读事实</p><h1>当前持仓</h1><p class="lede">先完成团队交易模式配置，再读取对应环境的账户持仓。</p></div></header><section class="empty-state"><div><h2>当前模式尚未配置</h2><p>页面没有读取或推测任何账户持仓。</p><a class="primary" href="/accounts" data-link>前往账户管理</a></div></section></section>';
    return;
  }

  const result = await api(`/api/positions?environment=${encodeURIComponent(environment)}`);
  const payload = result.data || {};
  const positions = payload.positions || [];
  const accounts = payload.accounts || [];
  const summary = payload.summary || {};
  const accountOptions = accounts.map(item => `<option value="${escapeHtml(positionAccountScope(item))}">${escapeHtml(item.label)} · ${escapeHtml(fmtVenueLabel(item.venue))}${item.active ? '' : ' · 已停用'}</option>`).join('');
  const rows = positions.map(item => {
    const pnl = item.unrealized_pnl;
    return `<tr data-position-row data-venue="${escapeHtml(item.venue)}" data-account="${escapeHtml(positionAccountScope(item))}" data-direction="${escapeHtml(item.direction)}" data-pnl="${positionPnlBucket(item)}">
      <td data-label="交易所"><b>${escapeHtml(fmtVenueLabel(item.venue))}</b><br><span class="subtle">${escapeHtml(fmtEnvironment(item.environment, true))}</span></td>
      <td data-label="账户"><a class="text-link" href="/venues/${escapeHtml(item.exchange_account_id)}" data-link>${escapeHtml(item.account_label)}</a><br><span class="subtle">${escapeHtml(item.account_id)}${item.account_active ? '' : ' · 已停用'}</span></td>
      <td data-label="标的"><b>${escapeHtml(item.symbol)}</b></td>
      <td data-label="方向 / 数量"><span class="direction-pill ${item.direction === 'LONG' ? 'direction-long' : 'direction-short'}">${escapeHtml(fmtDirection(item.direction))}</span><br><span class="subtle">${fmtNumber(item.quantity)}</span></td>
      <td data-label="入场价 / 标记价"><b>${fmtNumber(item.average_entry_price)}</b><br><span class="subtle">${fmtNumber(item.mark_price)}</span></td>
      <td data-label="未实现盈亏"><b class="${positionPnlClass(pnl)}">${pnl === null || pnl === undefined ? '结果未知' : escapeHtml(fmtAmount(pnl, item.collateral_currency))}</b></td>
      <td data-label="数据状态"><span class="status-pill status-${escapeHtml(item.fact_status)}">${escapeHtml(factStatusLabel(item.fact_status))}</span><br><span class="subtle">${fmtDate(item.observed_at)}</span></td>
    </tr>`;
  }).join('');
  const emptyCopy = Number(summary.unknown_count || 0) > 0
    ? '现有仓位事实包含未知状态，不能据此确认账户空仓。'
    : '当前可见账户没有非零持仓。';
  const filters = positions.length ? `<details class="proposal-filter-disclosure position-filter-disclosure" ${window.matchMedia('(min-width: 781px)').matches ? 'open' : ''}>
      <summary><span><b>筛选持仓</b><small>交易所、账户、方向和未实现盈亏</small></span><strong><span><b data-position-visible-count>${positions.length}</b> / ${positions.length} 个结果</span><span class="proposal-filter-when-closed">展开</span><span class="proposal-filter-when-open">收起</span></strong></summary>
      <div class="position-filter-panel">
        <div class="position-filter-heading"><div><b>筛选持仓</b><small>交易所、账户、方向和未实现盈亏</small></div><div class="position-filter-actions"><span role="status" aria-live="polite"><b data-position-visible-count>${positions.length}</b> / ${positions.length} 个结果</span><button class="text-button" type="button" data-position-reset disabled>清除筛选</button></div></div>
        <div class="position-list-tools"><label><span>交易所</span><select id="position-venue" autocomplete="off"><option value="">全部交易所</option>${['BINANCE','HYPERLIQUID','OKX','BYBIT'].map(venue => `<option value="${venue}">${escapeHtml(fmtVenueLabel(venue))}</option>`).join('')}</select></label><label><span>账户</span><select id="position-account" autocomplete="off"><option value="">全部账户</option>${accountOptions}</select></label><label><span>多空</span><select id="position-direction" autocomplete="off"><option value="">全部方向</option><option value="LONG">做多</option><option value="SHORT">做空</option></select></label><label><span>未实现盈亏</span><select id="position-pnl" autocomplete="off"><option value="">全部</option><option value="PROFIT">盈利</option><option value="LOSS">亏损</option><option value="FLAT">持平</option><option value="UNKNOWN">结果未知</option></select></label><button class="text-button position-filter-reset-mobile" type="button" data-position-reset disabled>清除筛选</button></div>
      </div>
    </details>` : '';

  main.innerHTML = `<section class="page current-positions-page"><header class="page-head"><div><p class="eyebrow">${escapeHtml(fmtEnvironment(environment, true))} · 多账户只读事实</p><h1>当前持仓</h1><p class="lede">统一展示当前团队内有权限账户的非零持仓；可按交易所、账户、方向和未实现盈亏筛选。本页只读，不提供手动平仓或订单操作。</p></div><div class="toolbar"><button class="secondary" type="button" data-refresh>刷新持仓</button></div></header>
    <div class="stats position-stats"><div class="stat"><small>当前持仓</small><b>${Number(summary.position_count || 0)}</b><span>仅统计非零仓位事实</span></div><div class="stat"><small>涉及账户</small><b>${Number(summary.account_count || 0)} / ${accounts.length}</b><span>持仓账户 / 可见账户</span></div><div class="stat"><small>多头 / 空头</small><b>${Number(summary.long_count || 0)} / ${Number(summary.short_count || 0)}</b><span>按仓位数量正负判断</span></div><div class="stat"><small>结果未知</small><b class="${Number(summary.unknown_count || 0) ? 'warning-text' : ''}">${Number(summary.unknown_count || 0)}</b><span>未知事实不计为实时盈亏</span></div></div>
    ${filters}
    ${positions.length ? `<div id="position-list" class="table-wrap position-table"><table><thead><tr><th>交易所</th><th>账户</th><th>标的</th><th>方向 / 数量</th><th>入场价 / 标记价</th><th>未实现盈亏</th><th>数据状态</th></tr></thead><tbody>${rows}</tbody></table></div><section id="position-filter-empty" class="empty-state compact-empty" hidden><div><h2>没有符合筛选的持仓</h2><p>请调整或清除筛选条件。</p></div></section>` : `<section class="empty-state"><div><h2>当前没有可展示的持仓</h2><p>${escapeHtml(emptyCopy)}</p><a class="secondary" href="/accounts" data-link>查看账户状态</a></div></section>`}
    <p class="safety-note position-read-only-note">数据来自服务端最近保存的账户事实；请结合数据状态与更新时间判断时效。未知事实不会被当作 0，本页没有平仓按钮。</p></section>`;

  document.querySelector('[data-refresh]')?.addEventListener('click', route);
  if (!positions.length) return;
  const filterControls = ['#position-venue','#position-account','#position-direction','#position-pnl']
    .map(selector => document.querySelector(selector));
  const resetButtons = document.querySelectorAll('[data-position-reset]');
  filterControls.forEach(control => { control.value = ''; });
  const applyFilters = () => {
    const venue = document.querySelector('#position-venue').value;
    const account = document.querySelector('#position-account').value;
    const direction = document.querySelector('#position-direction').value;
    const pnl = document.querySelector('#position-pnl').value;
    const active = Boolean(venue || account || direction || pnl);
    let visible = 0;
    document.querySelectorAll('[data-position-row]').forEach(row => {
      const matches = (!venue || row.dataset.venue === venue)
        && (!account || row.dataset.account === account)
        && (!direction || row.dataset.direction === direction)
        && (!pnl || row.dataset.pnl === pnl);
      row.hidden = !matches;
      if (matches) visible += 1;
    });
    document.querySelectorAll('[data-position-visible-count]').forEach(node => { node.textContent = visible; });
    document.querySelector('.position-filter-panel').classList.toggle('has-active-filters', active);
    resetButtons.forEach(button => { button.disabled = !active; });
    document.querySelector('#position-list').hidden = visible === 0;
    document.querySelector('#position-filter-empty').hidden = visible !== 0;
  };
  filterControls.forEach(control => control.addEventListener('input', applyFilters));
  resetButtons.forEach(button => {
    button.addEventListener('click', () => {
      filterControls.forEach(control => { control.value = ''; });
      applyFilters();
    });
  });
  applyFilters();
}
