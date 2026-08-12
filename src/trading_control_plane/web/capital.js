function partitionCapitalRecords(records) {
  return {
    live: records.filter(item => item.environment === 'LIVE'),
    simulated: records.filter(item => ['SHADOW', 'TESTNET'].includes(item.environment)),
  };
}

function sumCapitalAmounts(values) {
  const parts = values.map(value => {
    const [whole, fraction = ''] = String(value).split('.');
    return {whole, fraction};
  });
  const scale = Math.max(0, ...parts.map(part => part.fraction.length));
  const total = parts.reduce((sum, part) => (
    sum + BigInt(`${part.whole}${part.fraction.padEnd(scale, '0')}`)
  ), 0n);
  if (!scale) return String(total);
  const digits = String(total).padStart(scale + 1, '0');
  return `${digits.slice(0, -scale)}.${digits.slice(-scale)}`;
}

function liveCapitalInTransit(transfers) {
  return sumCapitalAmounts(transfers
    .filter(transfer => transfer.environment === 'LIVE' && OCCUPIED_CAPITAL_TRANSFER_STATUSES.has(transfer.status))
    .map(transfer => transfer.reserved_amount));
}

function formatCapitalIssue(value) {
  const [code, source = ''] = String(value || '').split(':');
  const sourceLabel = ({BINANCE:'Binance',HYPERLIQUID:'Hyperliquid',VAULT:'Vault'}[source] || source);
  return ({
    MISSING_LIVE_SOURCE:`${sourceLabel || '资金来源'}：尚未同步`,
    STALE_LIVE_SOURCE:`${sourceLabel || '资金来源'}：数据已过期`,
    UNKNOWN_USD_VALUE:`${sourceLabel || '资金来源'}：缺少美元估值`,
    CURRENT_VALUE_MISSING:`${sourceLabel || '资金来源'}：没有可采信的当前美元净值`,
    TIME_MISALIGNED_SOURCE:`${sourceLabel || '资金来源'}：与其他来源时间错位`,
  }[code] || '资金数据尚未完整');
}

function formatCapitalUsd(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  const fractionDigits = Math.abs(number) < 100 ? 4 : 2;
  return new Intl.NumberFormat('en-US', {
    style:'currency', currency:'USD', minimumFractionDigits:fractionDigits, maximumFractionDigits:fractionDigits,
  }).format(number);
}

function capitalSourceIssue(issues, source) {
  const match = (issues || []).find(value => String(value).endsWith(`:${source}`));
  return match ? formatCapitalIssue(match) : null;
}

function capitalSourceIssueCode(issues, source) {
  const match = (issues || []).find(value => String(value).endsWith(`:${source}`));
  return match ? String(match).split(':')[0] : null;
}

function capitalFreshnessCopy(observedAt, asOf, maxAgeSeconds) {
  const observed = new Date(observedAt).getTime();
  const reference = new Date(asOf).getTime();
  if (!Number.isFinite(observed) || !Number.isFinite(reference)) return '尚无有效更新时间';
  const ageSeconds = Math.max(0, Math.round((reference - observed) / 1000));
  const age = ageSeconds < 60 ? '刚刚更新' : ageSeconds < 3600
    ? `${Math.floor(ageSeconds / 60)} 分钟前更新`
    : `${Math.floor(ageSeconds / 3600)} 小时前更新`;
  const windowMinutes = Math.max(1, Math.round(Number(maxAgeSeconds || 300) / 60));
  return `${age} · 当前有效窗口 ${windowMinutes} 分钟`;
}

function capitalSourcePresentation(netWorth, source, latestPoint) {
  const issueCode = capitalSourceIssueCode(netWorth.issues, source);
  const currentValue = source === 'VAULT' ? netWorth.vault : netWorth.venues?.[source];
  const individuallyCurrent = currentValue !== null && currentValue !== undefined
    && !['MISSING_LIVE_SOURCE','STALE_LIVE_SOURCE','UNKNOWN_USD_VALUE','CURRENT_VALUE_MISSING'].includes(issueCode);
  const aligned = individuallyCurrent && issueCode !== 'TIME_MISALIGNED_SOURCE';
  const observedAt = individuallyCurrent ? netWorth.source_as_of?.[source] : latestPoint?.time;
  const value = individuallyCurrent ? currentValue : latestPoint?.value;
  const ageSeconds = observedAt && netWorth.as_of
    ? Math.max(0, (new Date(netWorth.as_of).getTime() - new Date(observedAt).getTime()) / 1000)
    : null;
  const nearExpiry = individuallyCurrent && Number.isFinite(ageSeconds)
    && ageSeconds >= Number(netWorth.max_fact_age_seconds || 300) * 2 / 3;
  const state = issueCode === 'TIME_MISALIGNED_SOURCE'
    ? '当前，但未对齐'
    : nearExpiry
      ? '当前，接近过期'
    : individuallyCurrent
      ? '当前可信'
      : capitalSourceIssue(netWorth.issues, source) || '等待数据';
  return {
    aligned, individuallyCurrent, nearExpiry, observedAt, state, value,
    freshness:capitalFreshnessCopy(observedAt, netWorth.as_of, netWorth.max_fact_age_seconds),
  };
}

function renderUnsignedPlanSummary(transfer) {
  const plans = transfer.planned_transactions || [];
  if (!plans.length) return '';
  const actionLabels = {
    approve:'批准资产额度', deposit:'存入资金库', requestWhitelistRelease:'申请释放资金',
    executeWhitelistRelease:'执行资金释放', cancelWhitelistRelease:'取消资金释放',
  };
  const rows = plans.map(plan => `<li><b>${escapeHtml(actionLabels[plan.function_name] || '链上资金操作')}</b><span>链 ${escapeHtml(plan.chain_id)} · 目标 ${escapeHtml(plan.to)} · 原生币金额 ${escapeHtml(plan.value || '0')}</span></li>`).join('');
  return `<details class="wallet-handoff"><summary>${plans.length} 笔交易等待独立钱包确认</summary><ul>${rows}</ul><p class="subtle">资产 ${escapeHtml(transfer.asset)} · 划转 ${fmtNumber(transfer.gross_amount)} · 资金库 ${escapeHtml(transfer.source_id)}</p><button class="secondary" type="button" data-copy-plan="${escapeHtml(transfer.capital_transfer_id)}">复制给独立钱包</button></details>`;
}

function capitalSourceSlots(balances, onchainProvider, onchainConfigured) {
  const liveBalances = partitionCapitalRecords(balances).live;
  const onchainLabel = onchainProvider === 'SAFE_SPENDING_LIMIT'
    ? '链上金库 · Safe Spending Limits'
    : '链上金库 · NoTilt Vault';
  return LIVE_CAPITAL_SOURCES.flatMap(source => {
    const facts = liveBalances.filter(balance => (
      source.key === 'VAULT'
        ? balance.location_type === 'VAULT'
        : balance.location_type === 'VENUE' && balance.venue === source.key
    ));
    if (facts.length) return source.key === 'VAULT'
      ? facts.map(fact => ({...fact, source_label:onchainLabel}))
      : facts;
    return [{
      environment:'LIVE', location_type:source.location_type, location_id:'—', venue:source.key,
      asset:'USD', confirmed_available:null, source_reserved:null, effective_available:null,
      usd_equity:null, control_status:'MISSING', deposit_status:'UNKNOWN', observed_at:null,
      fact_status:'MISSING', missing_detail:source.key === 'VAULT'
        ? (onchainConfigured ? `${onchainLabel} 已配置但未同步` : `${onchainLabel} 尚未配置`)
        : '未同步',
      source_label:source.key === 'VAULT' ? onchainLabel : source.label,
    }];
  });
}

function capitalBalanceRows(balances, issues = []) {
  return balances.map(balance => {
    const missing = balance.fact_status === 'MISSING';
    const source = balance.location_type === 'VAULT' ? 'VAULT' : balance.venue;
    const valuationIssue = capitalSourceIssue(issues, source);
    const historical = !missing && balance.valuation_current === false;
    const sourceLabel = balance.source_label || (balance.location_type === 'VAULT'
      ? 'Vault'
      : ({BINANCE:'Binance', HYPERLIQUID:'Hyperliquid'}[balance.venue] || balance.venue || '交易所'));
    const state = missing
      ? `<b class="capital-status-missing">数据缺失</b><br><span class="subtle">${escapeHtml(balance.missing_detail)}</span>`
      : historical
        ? `<b class="capital-status-missing">历史快照</b><br><span class="subtle">${escapeHtml(valuationIssue || '当前美元估值不可采信')}；不计入当前净值</span><br><span class="subtle">${escapeHtml(fmtStatus(balance.control_status))} / ${escapeHtml(fmtStatus(balance.deposit_status))}</span>`
        : `<b>当前估值</b><br><span class="subtle">${escapeHtml(fmtStatus(balance.control_status))} / ${escapeHtml(fmtStatus(balance.deposit_status))}</span>`;
    const accountScope = balance.location_type === 'VAULT'
      ? (missing ? '等待配置' : '已配置范围')
      : '默认账户';
    return `<tr${missing || historical ? ' class="capital-missing-row"' : ''}><td data-label="资金位置"><b translate="no">${escapeHtml(sourceLabel)}</b></td><td data-label="账户范围">${escapeHtml(accountScope)}</td><td data-label="已确认可用">${fmtNumber(balance.confirmed_available)} ${escapeHtml(balance.asset)}</td><td data-label="美元净值">${balance.usd_equity === null ? '未知' : `${formatCapitalUsd(balance.usd_equity)}`}</td><td data-label="源端预留">${fmtNumber(balance.source_reserved)}</td><td data-label="有效可用"><b>${fmtNumber(balance.effective_available)}</b></td><td data-label="数据状态">${state}</td><td data-label="更新时间">${missing ? '—' : fmtDate(balance.observed_at)}</td></tr>`;
  }).join('');
}

function capitalBalanceTable(rows, emptyMessage) {
  return rows
    ? `<div class="table-scroll-hint capital-balance-scroll-hint">左右滑动查看完整资金数据</div><div class="table-wrap is-scrollable capital-balance-table"><table><thead><tr><th>资金位置</th><th>账户范围</th><th>已确认可用</th><th>美元净值</th><th>源端预留</th><th>有效可用</th><th>数据状态</th><th>更新时间</th></tr></thead><tbody>${rows}</tbody></table></div>`
    : `<div class="callout">${escapeHtml(emptyMessage)}</div>`;
}

function capitalHistorySeries(history, alignmentToleranceSeconds = 60, gapToleranceSeconds = 300) {
  const grouped = new Map(LIVE_CAPITAL_SOURCES.map(source => [source.key, new Map()]));
  history.filter(item => item.usd_equity !== null && item.usd_equity !== undefined).forEach(item => {
    const source = item.location_type === 'VAULT' ? 'VAULT' : item.venue;
    const buckets = grouped.get(source);
    const timestamp = new Date(item.observed_at).getTime();
    const value = Number(item.usd_equity);
    if (!buckets || !Number.isFinite(timestamp) || !Number.isFinite(value)) return;
    buckets.set(timestamp, (buckets.get(timestamp) || 0) + value);
  });
  const gapToleranceMs = Math.max(1, Number(gapToleranceSeconds) || 300) * 1000;
  const sourceSeries = LIVE_CAPITAL_SOURCES.map(source => {
    const entries = [...(grouped.get(source.key) || new Map()).entries()]
      .sort((left, right) => left[0] - right[0]);
    const intervals = entries.slice(1).map((entry, index) => entry[0] - entries[index][0])
      .filter(interval => interval > 0).sort((left, right) => left - right);
    const medianInterval = intervals.length >= 3 ? intervals[Math.floor(intervals.length / 2)] : 0;
    const sourceGapTolerance = Math.min(
      Math.max(gapToleranceMs, medianInterval * 3),
      Math.max(gapToleranceMs, 15 * 60 * 1000),
    );
    const points = entries
      .map(([time, value], index, entries) => ({
        time, value,
        breakBefore:index > 0 && time - entries[index - 1][0] > sourceGapTolerance,
      }));
    return {
      source:source.key, label:source.label, points,
      gapToleranceSeconds:Math.round(sourceGapTolerance / 1000),
    };
  });
  const totalTimes = [...new Set(sourceSeries.flatMap(series => series.points.map(point => point.time)))]
    .sort((left, right) => left - right);
  const alignmentToleranceMs = Math.max(1, Number(alignmentToleranceSeconds) || 60) * 1000;
  const closestPoint = (points, time) => {
    let closest = null;
    for (const point of points) {
      if (point.time < time - alignmentToleranceMs) continue;
      if (point.time > time + alignmentToleranceMs) break;
      if (!closest || Math.abs(point.time - time) < Math.abs(closest.time - time)) closest = point;
    }
    return closest;
  };
  const totalPoints = [];
  let lastSignature = '';
  totalTimes.forEach(time => {
    const matches = sourceSeries.map(series => closestPoint(series.points, time));
    if (matches.every(Boolean)) {
      const sourceTimes = matches.map(point => point.time);
      if (Math.max(...sourceTimes) - Math.min(...sourceTimes) > alignmentToleranceMs) return;
      const signature = sourceTimes.join(':');
      if (signature === lastSignature) return;
      lastSignature = signature;
      const totalTime = Math.max(...sourceTimes);
      const previous = totalPoints.at(-1);
      totalPoints.push({
        time:totalTime,
        value:matches.reduce((sum, point) => sum + point.value, 0),
        sourceTimes,
        breakBefore:Boolean(previous && totalTime - previous.time > gapToleranceMs),
      });
    }
  });
  return [...sourceSeries, {
    source:'TOTAL', label:'三方汇总',
    points:totalPoints,
    alignmentToleranceSeconds:Number(alignmentToleranceSeconds) || 60,
    latestCompleteAt:totalPoints.at(-1)?.time || null,
    timeMisaligned:sourceSeries.every(series => series.points.length) && !totalPoints.length,
  }];
}

function capitalHistoryWindow(history, startTime = null, endTime = null) {
  if (startTime === null && endTime === null) return [...history];
  const minimum = startTime === null ? Number.NEGATIVE_INFINITY : Number(startTime);
  const maximum = endTime === null ? Number.POSITIVE_INFINITY : Number(endTime);
  return history.filter(item => {
    const timestamp = new Date(item.observed_at).getTime();
    return Number.isFinite(timestamp) && timestamp >= minimum && timestamp <= maximum;
  });
}

function capitalHistoryRange(history, sliderValue = CAPITAL_CHART_RANGE_MAX) {
  const timestamps = history.map(item => new Date(item.observed_at).getTime()).filter(Number.isFinite);
  if (!timestamps.length) return {history:[], start:null, end:null, duration:0, complete:true};
  const fullStart = Math.min(...timestamps);
  const end = Math.max(...timestamps);
  const fullDuration = Math.max(0, end - fullStart);
  const normalized = Math.min(
    CAPITAL_CHART_RANGE_MAX,
    Math.max(1, Number(sliderValue) || CAPITAL_CHART_RANGE_MAX),
  );
  const duration = normalized >= CAPITAL_CHART_RANGE_MAX
    ? fullDuration
    : Math.min(fullDuration, Math.max(60_000, fullDuration * normalized / CAPITAL_CHART_RANGE_MAX));
  const start = end - duration;
  return {
    history:capitalHistoryWindow(history, start, end),
    start, end, duration, fullStart, fullDuration,
    complete:start <= fullStart,
  };
}

function formatCapitalRangeDuration(milliseconds) {
  const minutes = Math.max(1, Math.round(Number(milliseconds || 0) / 60_000));
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours < 10 ? hours.toFixed(1) : Math.round(hours)} 小时`;
  const days = hours / 24;
  if (days < 60) return `${days < 10 ? days.toFixed(1) : Math.round(days)} 天`;
  const months = days / 30.4375;
  if (months < 24) return `${months.toFixed(1)} 个月`;
  return `${(days / 365.25).toFixed(1)} 年`;
}

function capitalHistoryCoverage(series, selection) {
  const times = series.flatMap(item => item.points.map(point => point.time));
  if (!times.length || selection.start === null || selection.end === null) {
    return '尚无有效时间范围';
  }
  const gapCount = series.reduce(
    (count, item) => count + item.points.filter(point => point.breakBefore).length,
    0,
  );
  return `${selection.complete ? '完整历史' : `最近 ${formatCapitalRangeDuration(selection.duration)}`} · ${fmtDate(Math.min(...times))} 至 ${fmtDate(Math.max(...times))} · ${gapCount ? `${gapCount} 处断档未连线` : '没有检测到断档'}`;
}

function compactCapitalChartPoints(points, minimumDistance = 2) {
  return points.reduce((result, point, index) => {
    const previous = result.at(-1);
    if (!previous || point.breakBefore || point.x - previous.x >= minimumDistance || index === points.length - 1) {
      result.push(point);
    }
    return result;
  }, []);
}

function capitalAxisDomain(values) {
  const minimumValue = Math.min(...values);
  const maximumValue = Math.max(...values);
  const rawRange = maximumValue - minimumValue;
  const magnitude = Math.max(Math.abs(minimumValue), Math.abs(maximumValue), 1);
  const minimumRange = Math.max(magnitude * .0005, magnitude < 100 ? .001 : .01);
  const range = Math.max(rawRange * 1.36, minimumRange);
  const midpoint = (minimumValue + maximumValue) / 2;
  return {minimum:midpoint - range / 2, maximum:midpoint + range / 2, range};
}

function capitalSeriesChange(series) {
  const points = series?.points || [];
  if (points.length < 2) return null;
  const previous = points.at(-2);
  const latest = points.at(-1);
  if (latest.breakBefore) return null;
  const delta = latest.value - previous.value;
  const percentage = previous.value === 0 ? null : delta / previous.value * 100;
  return {
    delta, percentage, time:latest.time,
    anomalous:(percentage !== null && Math.abs(percentage) >= 1),
  };
}

function capitalSeriesLargestChange(series) {
  const points = series?.points || [];
  let largest = null;
  for (let index = 1; index < points.length; index += 1) {
    if (points[index].breakBefore) continue;
    const previous = points[index - 1];
    const latest = points[index];
    const delta = latest.value - previous.value;
    const percentage = previous.value === 0 ? null : delta / previous.value * 100;
    if (!largest || Math.abs(delta) > Math.abs(largest.delta)) {
      largest = {delta, percentage, time:latest.time};
    }
  }
  return largest;
}

function formatDirectCapitalBlocker(code) {
  return ({
    CAPITAL_VAULT_ID_MISSING:'未配置生产资金库编号',
    CAPITAL_VAULT_ADDRESS_MISSING:'未配置已授权资金库地址',
    CAPITAL_VENUE_ACCOUNT_MISSING:'未配置交易账户',
    CAPITAL_OWNED_ARBITRUM_ADDRESS_MISSING:'未配置已授权 Arbitrum 自有地址',
    CAPITAL_HYPERLIQUID_CONTRACT_MISSING:'未配置 Hyperliquid 合约地址',
    CAPITAL_BINANCE_WHITELIST_ADDRESS_MISSING:'未配置币安白名单充值地址',
    CAPITAL_BINANCE_WITHDRAWAL_ADDRESS_MISSING:'未配置币安受限提现的白名单自有地址',
    BINANCE_DIRECT_TREASURY_WITHDRAWAL_REQUIRED:'币安回流会直接提现到当前链上金库，不再生成第二笔钱包入金',
    CAPITAL_BINANCE_WITHDRAWAL_ADDRESS_SCOPE_MISMATCH:'币安提现白名单地址与当前链上金库地址不一致',
    CAPITAL_AMOUNT_LIMIT_MISSING:'未配置单次金额上限',
    CAPITAL_AMOUNT_LIMIT_EXCEEDED:'金额超过已配置上限',
    CAPITAL_FEE_LIMIT_MISSING:'未配置最大费用上限',
    CAPITAL_MIN_RECEIVED_INVALID:'扣除最大费用后到账金额无效',
    CAPITAL_TRANSFER_GATE_DISABLED:'真实资金划转安全开关当前关闭',
    NOTILT_RELEASE_ADAPTER_UNAVAILABLE:'资金库释放适配器尚未启用',
    HYPERLIQUID_DEPOSIT_ADAPTER_UNAVAILABLE:'Hyperliquid 入金适配器尚未启用',
    HYPERLIQUID_WITHDRAWAL_ADAPTER_UNAVAILABLE:'Hyperliquid 提现适配器尚未启用',
    HYPERLIQUID_HUMAN_WALLET_CONFIRMATION_REQUIRED:'Hyperliquid 已完成协议预检；当前动作须由主钱包或有效多签逐笔确认',
    HUMAN_WALLET_CONFIRMATION_CANCELLED:'用户已取消钱包确认；资金保持原位',
    TREASURY_SOURCE_RECEIPT_REQUIRED:'仍需验证链上金库释放及到账回执',
    TREASURY_DESTINATION_RECEIPT_REQUIRED:'仍需验证链上金库最终入金回执',
    HYPERLIQUID_WITHDRAWAL_REVALIDATION_REQUIRED:'主账户资金归集已确认；须重新读取实时可提现余额后再生成 withdraw3',
    BINANCE_CAPITAL_CREDENTIALS_MISSING:'未加载币安专用资金 API 凭据',
    BINANCE_DEPOSIT_PREFLIGHT_REQUIRED:'需要读取并核对币安实时充值地址与网络状态',
    BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_REQUIRED:'需要核对币安 API 权限、IP 限制、白名单、余额、额度和手续费',
    BINANCE_RESTRICTED_WITHDRAWAL_ADAPTER_UNAVAILABLE:'币安受限提现 API 尚未配置',
    SAFE_ADDRESS_MISSING:'未配置 Safe Smart Account',
    SAFE_DELEGATE_ADDRESS_MISSING:'未配置 Safe 委托地址（delegate）',
    SAFE_SPENDING_LIMIT_NOT_CONFIGURED:'Safe 只读 RPC 或 Spending Limit 范围未配置',
    SAFE_ALLOWANCE_PREFLIGHT_REQUIRED:'必须读取 Safe 当前额度、余额、重置周期与 nonce',
  }[code] || code);
}

function formatDirectCapitalStage(code) {
  return ({
    VAULT_RELEASE_REQUEST:'申请资金库释放', WAIT_10_MINUTES:'等待 10 分钟',
    REVALIDATE_RELEASE:'到期重新校验', TRANSFER_TO_AUTHORIZED_BINANCE_ADDRESS:'转入已授权币安地址',
    VAULT_RELEASE_TO_AUTHORIZED_OWNED_ADDRESS:'释放至已授权自有地址',
    DEPOSIT_TO_HYPERLIQUID_CONTRACT:'存入 Hyperliquid 合约',
    WITHDRAW_FROM_HYPERLIQUID_CONTRACT:'从 Hyperliquid 合约提回',
    RECEIVE_AT_AUTHORIZED_OWNED_ADDRESS:'到达已授权自有地址',
    PREPARE_NOTILT_SDK_DEPOSIT:'由 NoTilt 官方 SDK 构建最低必要无签名入金序列',
    HUMAN_WALLET_CONFIRMATION:'独立人控钱包逐笔核对与确认',
    VERIFY_NOTILT_DEPOSIT_RECEIPT:'校验链、目标、方法、金额与回执',
    RESTRICTED_BINANCE_WITHDRAWAL:'调用受限币安提现 API',
    RESTRICTED_BINANCE_WITHDRAWAL_TO_AUTHORIZED_OWNED_ADDRESS:'币安受限提现至已授权自有地址',
    RESTRICTED_BINANCE_WITHDRAWAL_TO_SELECTED_TREASURY:'币安受限提现至当前链上金库',
    VERIFY_BINANCE_WITHDRAWAL_RECEIPT:'校验币安提现状态与交易哈希',
    VERIFY_SELECTED_TREASURY_CREDIT:'校验当前链上金库到账',
    NOTILT_UNSIGNED_RELEASE_REQUEST_PREVIEW:'NoTilt 释放请求无签名预检',
    NOTILT_UNSIGNED_DEPOSIT_PREVIEW:'NoTilt 入金序列无签名预检',
    READ_SAFE_SPENDING_LIMIT:'读取 Safe Spending Limit',
    VERIFY_SAFE_MODULE_DELEGATE_TOKEN_NONCE:'校验官方模块、delegate、USDC 与 nonce',
    BUILD_SAFE_ALLOWANCE_SIGNATURE_REQUEST:'构建精确的人控签名请求',
    HUMAN_DELEGATE_SIGNATURE_AND_SUBMISSION:'独立 delegate 钱包签名并提交',
    VERIFY_SAFE_TRANSFER_RECEIPT:'校验 Safe 支出回执',
    SAFE_TRANSFER_TO_AUTHORIZED_OWNED_ADDRESS:'Safe 转入已授权自有地址',
    BUILD_EXACT_USDC_TRANSFER_TO_SAFE:'构建精确 USDC 入金请求',
    VERIFY_SAFE_BALANCE_RECEIPT:'校验 Safe 入金与余额变化',
    RESTRICTED_BINANCE_WITHDRAWAL_TO_SAFE:'币安受限提现至 Safe',
    BINANCE_DEPOSIT_PREFLIGHT_READY:'币安充值地址与网络预检已通过',
    BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_READY:'币安受限提现预检已通过',
    BINANCE_RESTRICTED_WITHDRAWAL_SUBMITTED:'币安受限提现已提交，等待回执',
    BINANCE_DEPOSIT_RECEIPT_CONFIRMED:'币安充值到账已确认',
    BINANCE_WITHDRAWAL_RECEIPT_CONFIRMED:'币安提现与 Arbitrum 到账已确认',
    SAFE_ALLOWANCE_SIGNATURE_REQUEST_READY:'Safe 精确签名请求已准备，等待人控钱包',
    SAFE_DEPOSIT_UNSIGNED_TRANSACTION_READY:'Safe 入金无签名请求已准备',
    HYPERLIQUID_DEPOSIT_WALLET_REQUEST_READY:'Hyperliquid 入金请求已准备，等待主钱包或多签',
    HYPERLIQUID_WITHDRAW3_WALLET_REQUEST_READY:'Hyperliquid withdraw3 请求已准备，等待主钱包或多签',
    HYPERLIQUID_CLASS_TRANSFER_WALLET_REQUEST_READY:'Hyperliquid 主账户资金归集请求已准备，等待主钱包或多签',
    HYPERLIQUID_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET:'Hyperliquid 入金已由人控钱包提交',
    HYPERLIQUID_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET:'Hyperliquid 提现已由人控钱包提交',
    HYPERLIQUID_CLASS_TRANSFER_SUBMITTED_BY_HUMAN_WALLET:'Hyperliquid 主账户资金归集已由人控钱包提交',
    TREASURY_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET:'链上金库入金已由人控钱包提交',
    HYPERLIQUID_DEPOSIT_ARBITRUM_CONFIRMED:'Arbitrum 入金交易已验证',
    HYPERLIQUID_DEPOSIT_LEDGER_CONFIRMED:'Hyperliquid 入金账本已验证',
    HYPERLIQUID_WITHDRAWAL_LEDGER_CONFIRMED:'Hyperliquid 提现账本已验证',
    HYPERLIQUID_WITHDRAWAL_ARBITRUM_CONFIRMED:'Arbitrum 提现到账已验证',
    HYPERLIQUID_CLASS_TRANSFER_LEDGER_CONFIRMED:'Hyperliquid 主账户资金归集账本已验证',
    TREASURY_DESTINATION_RECEIPT_CONFIRMED:'链上金库最终到账已验证',
  }[code] || code);
}

function capitalViewSelection(search = location.search) {
  const requested = new URLSearchParams(search).get('view');
  return ['routes','activity'].includes(requested) ? requested : 'overview';
}

function groupCapitalView(nodes, view, activeView) {
  const available = nodes.filter(Boolean);
  if (!available.length) return null;
  const panel = document.createElement('div');
  panel.className = 'capital-view-panel';
  panel.dataset.capitalViewPanel = view;
  panel.setAttribute('aria-labelledby', `capital-view-tab-${view}`);
  panel.hidden = view !== activeView;
  available[0].before(panel);
  available.forEach(node => panel.append(node));
  return panel;
}

async function renderCapitalCenter() {
  const result = await api('/api/capital');
  const item = result.data;
  const directConfiguration = item.direct_configuration || {};
  const netWorth = item.net_worth || {currency:'USD', venues:{}, vault:null, total:null, complete:false, issues:[]};
  const selectedTreasuryProvider = directConfiguration.treasury_provider || 'NOTILT_VAULT';
  const selectedTreasuryProviderLabel = selectedTreasuryProvider === 'SAFE_SPENDING_LIMIT' ? 'Safe Spending Limits' : 'NoTilt Vault';
  const selectedOnchainSeriesLabel = `链上金库 · ${selectedTreasuryProviderLabel}`;
  const balances = partitionCapitalRecords(item.balances);
  const transfers = partitionCapitalRecords(item.transfers);
  const liveInTransit = liveCapitalInTransit(transfers.live);
  const venueNetWorth = Object.entries(netWorth.venues || {}).map(([venue, value]) => `<div class="stat"><small>${escapeHtml(venue)} 净值</small><b>${fmtNumber(value)} ${escapeHtml(netWorth.currency)}</b></div>`).join('');
  const allHistorySeries = capitalHistorySeries(
    item.history || [],
    netWorth.alignment_tolerance_seconds || 60,
    netWorth.history_gap_tolerance_seconds || 300,
  );
  const historySelection = capitalHistoryRange(item.history || [], capitalChartRangeValue);
  const historySeries = capitalHistorySeries(
    historySelection.history,
    netWorth.alignment_tolerance_seconds || 60,
    netWorth.history_gap_tolerance_seconds || 300,
  );
  [...allHistorySeries, ...historySeries].forEach(series => {
    if (series.source === 'VAULT') series.label = selectedOnchainSeriesLabel;
  });
  const visibleHistorySeries = historySeries.filter(series => capitalTrendVisibility[series.source]);
  const hasHistory = historySeries.some(series => series.points.length);
  const chartLegend = historySeries.map(series => {
    const latestPoint = series.points.at(-1);
    const fallbackPoint = allHistorySeries.find(item => item.source === series.source)?.points.at(-1);
    const presentation = series.source === 'TOTAL' ? null : capitalSourcePresentation(netWorth, series.source, latestPoint || fallbackPoint);
    const current = latestPoint && (series.source === 'TOTAL' ? netWorth.complete : presentation.individuallyCurrent);
    const summary = !latestPoint
      ? (fallbackPoint ? `所选范围无数据 · 最后记录 ${fmtDate(fallbackPoint.time)}` : '等待数据')
      : `${formatCapitalUsd(latestPoint.value)} · ${series.source === 'TOTAL' ? (current ? '当前汇总' : '历史汇总') : presentation.state} · ${fmtDate(latestPoint.time)}`;
    const displayLabel = currentLanguage === 'en' && series.source === 'TOTAL' ? 'Combined total' : series.label;
    return `<label class="capital-trend-toggle trend-${escapeHtml(series.source)} ${latestPoint ? '' : 'is-missing'}"><input type="checkbox" data-capital-trend="${escapeHtml(series.source)}" ${latestPoint && capitalTrendVisibility[series.source] ? 'checked' : ''} ${latestPoint ? '' : 'disabled'}><i aria-hidden="true"></i><span><b translate="no">${escapeHtml(displayLabel)}</b><small>${escapeHtml(summary)}</small></span></label>`;
  }).join('');
  const totalSeries = allHistorySeries.find(series => series.source === 'TOTAL');
  const latestCompleteTotal = totalSeries?.points.at(-1) || null;
  const totalHeadline = netWorth.total !== null && netWorth.total !== undefined
    ? formatCapitalUsd(netWorth.total)
    : '当前不可汇总';
  const totalSupporting = netWorth.complete
    ? `三方同一时间口径 · ${fmtDate(netWorth.as_of)}`
    : latestCompleteTotal
      ? `最近完整汇总 ${formatCapitalUsd(latestCompleteTotal.value)} · ${fmtDate(latestCompleteTotal.time)}`
      : '尚无三方同一时间口径的完整记录';
  const sourceCards = [
    {source:'BINANCE', label:'Binance'},
    {source:'HYPERLIQUID', label:'Hyperliquid'},
    {source:'VAULT', label:selectedOnchainSeriesLabel},
  ].map(source => {
    const sourceSeries = historySeries.find(series => series.source === source.source);
    const latestPoint = sourceSeries?.points.at(-1)
      || allHistorySeries.find(series => series.source === source.source)?.points.at(-1);
    const presentation = capitalSourcePresentation(netWorth, source.source, latestPoint);
    const cardState = presentation.aligned ? (presentation.nearExpiry ? 'is-aging' : 'is-current') : 'is-limited';
    return `<article class="capital-worth-card ${cardState}"><div><small translate="no">${escapeHtml(source.label)}</small><b>${formatCapitalUsd(presentation.value)}</b></div><span>${escapeHtml(presentation.state)}</span><p>${presentation.observedAt ? `${escapeHtml(presentation.freshness)} · ${fmtDate(presentation.observedAt)}` : '尚无有效时间'}<br>数据来源：<span translate="no">${escapeHtml(source.label)}</span> 只读账户</p></article>`;
  }).join('');
  const chartCoverage = capitalHistoryCoverage(historySeries, historySelection);
  const issueDetails = [...new Set((netWorth.issues || []).map(issue => {
    const source = String(issue).split(':')[1];
    const lastTime = source && netWorth.source_as_of?.[source];
    return `${formatCapitalIssue(issue)}${lastTime ? `（最后记录 ${fmtDate(lastTime)}）` : ''}`;
  }))];
  const probeDetail = netWorth.onchain_probe?.status === 'FAILED'
    ? ` 当前${selectedOnchainSeriesLabel}只读探针失败（${netWorth.onchain_probe.error_code || '未知错误'}）；页面仅保留仍在有效期内的最近快照。`
    : '';
  const trustCopy = (netWorth.complete
    ? '三方数据完整、新鲜且时间对齐，可以计算当前汇总。'
    : `${issueDetails.join('；') || '资金数据尚未完整'}。影响：当前三方总净值不计算，但其他有效单线继续保留。`) + probeDetail;
  const liveBalanceRows = capitalBalanceRows(
    capitalSourceSlots(
      balances.live,
      selectedTreasuryProvider,
      directConfiguration.selected_onchain_account_configured,
    ),
    netWorth.issues,
  );
  const configuredPlaceholder = value => value ? '已配置；留空保持当前值' : '尚未配置';
  const directConfigurationEditor = directConfiguration.can_manage ? `<details class="card direct-capital-config-editor"><summary><span><b>配置固定资金路径</b><small>${directConfiguration.version ? `版本 ${directConfiguration.version} · ${escapeHtml(directConfiguration.updated_by_username || '系统管理员')} · ${fmtDate(directConfiguration.effective_at)}` : '尚无数据库配置；当前读取安全环境配置'}</small></span><strong>管理员配置</strong></summary><form id="direct-capital-config-form" class="toolbox-content compact-form"><p class="safety-note">先选择一个链上金库，系统只使用该金库创建之后的资金操作。这里只接收公开地址和账户范围；不得输入 API secret、私钥、种子、钱包密码或签名令牌。</p><fieldset class="treasury-provider-choice"><legend>1. 选择链上金库</legend><label><input type="radio" name="treasury_provider" value="NOTILT_VAULT" ${selectedTreasuryProvider === 'NOTILT_VAULT' ? 'checked' : ''}><span><b>NoTilt Vault</b><small>使用 NoTilt 金库预算、延迟与官方 SDK</small></span></label><label><input type="radio" name="treasury_provider" value="SAFE_SPENDING_LIMIT" ${selectedTreasuryProvider === 'SAFE_SPENDING_LIMIT' ? 'checked' : ''}><span><b>Safe Spending Limits</b><small>使用 Safe Allowance Module 的 delegate 额度</small></span></label></fieldset><p class="provider-guidance" data-provider-guidance></p><section class="capital-config-section" data-provider-fields="NOTILT_VAULT"><h3>NoTilt Vault 配置</h3><p>只填写 NoTilt 金库编号和金库地址。</p><div class="field-grid"><label>NoTilt 金库编号<input name="vault_id" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.vault_id_configured)}"></label><label>NoTilt 金库地址<input name="vault_address" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.vault_address_configured)}"></label></div></section><section class="capital-config-section" data-provider-fields="SAFE_SPENDING_LIMIT"><h3>Safe Spending Limits 配置</h3><p>只填写公开的 Safe Smart Account 与 delegate 地址。</p><div class="field-grid"><label>Safe Smart Account<input name="safe_address" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.safe_address_configured)}"></label><label>Safe Spending Limit delegate<input name="safe_delegate_address" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.safe_delegate_configured)}"></label></div></section><section class="capital-config-section common-capital-fields"><h3>2. 共用账户与安全边界</h3><p>无论选择哪个链上金库，币安、Hyperliquid、自有地址和金额限制都共用。</p><div class="field-grid"><label>授权自有 Arbitrum 地址<input name="owned_arbitrum_address" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.owned_arbitrum_address_configured)}"></label><label>币安默认账户<input name="binance_account_id" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.binance_account_configured)}"></label><label>币安白名单入金地址<input name="binance_deposit_address" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.binance_whitelist_destination_configured)}"></label><label>币安受限提现地址<input name="binance_withdrawal_address" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.binance_withdrawal_destination_configured)}"></label><label>Hyperliquid 默认账户<input name="hyperliquid_account_id" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.hyperliquid_account_configured)}"></label><label>Hyperliquid Bridge 地址<input name="hyperliquid_bridge_address" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.hyperliquid_contract_configured)}"></label><label>单次金额上限（USDC）<input name="max_amount" type="number" step="any" min="0.000001" placeholder="留空保持当前值"></label><label>最大费用上限（USDC）<input name="max_fee" type="number" step="any" min="0" placeholder="留空保持当前值"></label></div></section><div class="form-error" role="alert"></div><div class="form-actions"><button class="primary">保存并使用此链上金库</button></div></form></details>` : '';
  const directPathCards = DIRECT_CAPITAL_PATHS.map(path => `<article class="capital-route-card"><div class="capital-route-meta"><span>固定路径</span><strong>${escapeHtml(path.badge)}</strong></div><div class="capital-route-flow"><b>${escapeHtml(path.from)}</b><span aria-hidden="true">→</span><b>${escapeHtml(path.to)}</b></div><p>${escapeHtml(path.copy)}</p><ol>${path.steps.map(step => `<li>${escapeHtml(step)}</li>`).join('')}</ol><button class="secondary capital-route-action" type="button" data-open-capital-path="${escapeHtml(path.path)}">${escapeHtml(path.action)}</button></article>`).join('');
  const directCapitalDialog = `<dialog id="direct-capital-dialog" aria-labelledby="direct-capital-title"><form id="direct-capital-form" class="dialog-form" data-direct-capital-form="" data-treasury-provider="${escapeHtml(selectedTreasuryProvider)}"><div class="dialog-head"><div><p class="eyebrow">资金路径安全预检</p><h2 id="direct-capital-title" data-capital-path-title>选择资金路径</h2></div><button type="button" class="icon-button" data-close-capital-dialog aria-label="关闭">×</button></div><p class="subtle" data-capital-path-copy></p><div class="selected-provider-summary"><small>当前链上金库</small><b>${escapeHtml(selectedTreasuryProviderLabel)}</b><span>如需切换，请由管理员先保存新的资金路径配置。</span></div><input name="path" type="hidden"><label>金额（USDC）<input name="amount" type="number" step="any" min="0.000001" required placeholder="输入划转金额"></label><label class="direct-capital-confirm"><input name="final_confirmed" type="checkbox" required><span>我已核对资金方向与金额</span></label><p class="safety-note">提交只会重新校验地址、网络、资产、额度和实时安全开关。任何条件缺失都会阻断；系统不会签名、广播或发送资金。</p><div class="form-error" role="alert"></div><div class="dialog-actions"><button type="button" class="secondary" data-close-capital-dialog>取消</button><button class="primary" type="submit">最终确认并检查</button></div></form></dialog>`;
  const directRows = (item.direct_operations || []).map(operation => {
    const pathDefinition = DIRECT_CAPITAL_PATHS.find(path => path.path === operation.path);
    const label = pathDefinition ? `${pathDefinition.from} → ${pathDefinition.to}` : operation.path;
    const stages = (operation.stages || []).map(stage => formatDirectCapitalStage(stage.code)).join(' → ');
    const blockers = (operation.blockers || []).map(formatDirectCapitalBlocker);
    const blockerDetails = blockers.length
      ? `<details class="capital-blockers"><summary>${blockers.length} 项阻断，查看详情</summary><p>${escapeHtml(blockers.join('；'))}</p></details>`
      : '<span>无阻断</span>';
    const safeOutbound = operation.path === 'VAULT_TO_BINANCE' || operation.path === 'VAULT_TO_HYPERLIQUID';
    const hyperliquidPath = operation.path === 'VAULT_TO_HYPERLIQUID' || operation.path === 'HYPERLIQUID_TO_VAULT';
    const binancePath = operation.path === 'VAULT_TO_BINANCE' || operation.path === 'BINANCE_TO_VAULT';
    const frozenStages = operation.stages || [];
    const binancePreview = [...frozenStages].reverse().find(stage => stage.artifact?.kind?.startsWith('BINANCE_'));
    const binanceSubmission = [...frozenStages].reverse().find(stage => stage.code === 'BINANCE_RESTRICTED_WITHDRAWAL_SUBMITTED');
    const binanceReceiptConfirmed = frozenStages.some(stage => ['BINANCE_DEPOSIT_RECEIPT_CONFIRMED', 'BINANCE_WITHDRAWAL_RECEIPT_CONFIRMED'].includes(stage.code));
    const hyperliquidPreview = [...frozenStages].reverse().find(stage => stage.artifact?.kind?.startsWith('HYPERLIQUID_'));
    const walletStage = operation.path === 'VAULT_TO_HYPERLIQUID' ? 'HYPERLIQUID_DEPOSIT' : hyperliquidPreview?.artifact?.kind === 'HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST' ? 'HYPERLIQUID_CLASS_TRANSFER' : 'HYPERLIQUID_WITHDRAWAL';
    const walletSubmission = [...frozenStages].reverse().find(stage => stage.code === `${walletStage}_SUBMITTED_BY_HUMAN_WALLET`);
    const providerPreviewReady = frozenStages.some(stage => ['NOTILT_UNSIGNED_DEPOSIT_PREVIEW', 'SAFE_DEPOSIT_UNSIGNED_TRANSACTION_READY'].includes(stage.code));
    const treasurySubmission = [...frozenStages].reverse().find(stage => stage.code === 'TREASURY_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET');
    const providerButton = operation.path === 'BINANCE_TO_VAULT' ? '' : `<button class="text-button" data-capital-preview="${escapeHtml(operation.operation_id)}" data-preview-provider="${escapeHtml(operation.treasury_provider || 'NOTILT_VAULT')}" data-preview-direction="${safeOutbound ? 'OUTBOUND' : 'INBOUND'}" data-operation-version="${Number(operation.version || 1)}">${operation.treasury_provider === 'SAFE_SPENDING_LIMIT' ? (safeOutbound ? '读取 Safe 额度' : '生成 Safe 入金预检') : '生成 NoTilt 预检'}</button>`;
    const hyperliquidButton = hyperliquidPath ? `<button class="text-button" data-hyperliquid-preview="${escapeHtml(operation.operation_id)}" data-operation-version="${Number(operation.version || 1)}">${operation.path === 'VAULT_TO_HYPERLIQUID' ? '生成 Hyperliquid 入金请求' : '生成 Hyperliquid 提现请求'}</button>` : '';
    const binanceButton = binancePath && !binanceReceiptConfirmed ? `<button class="text-button" data-binance-preview="${escapeHtml(operation.operation_id)}" data-operation-version="${Number(operation.version || 1)}">${operation.path === 'VAULT_TO_BINANCE' ? '核对币安充值地址' : '币安受限提现预检'}</button>` : '';
    const binanceBoundary = binancePreview ? `<details class="capital-wallet-handoff"><summary>币安资金链路</summary><div class="wallet-handoff-facts"><b>${operation.path === 'VAULT_TO_BINANCE' ? '币安 Arbitrum 充值地址已实时核对' : '币安受限提现条件已实时核对'}</b><span>网络：${escapeHtml(binancePreview.artifact.network)}</span><span>金额：${escapeHtml(binancePreview.artifact.amount)} USDC</span><span>${operation.path === 'BINANCE_TO_VAULT' ? `手续费：${escapeHtml(binancePreview.artifact.fee)} USDC` : '链上签名：独立人控钱包 / 多签'}</span><code>${escapeHtml(binancePreview.artifact.destination)}</code><p>API Key、Secret、请求签名和钱包私钥均不会显示在页面、操作记录或审计日志中。</p></div>${operation.path === 'VAULT_TO_BINANCE' ? `<form class="binance-receipt-form" data-binance-receipt="${escapeHtml(operation.operation_id)}" data-binance-stage="BINANCE_DEPOSIT" data-operation-version="${Number(operation.version || 1)}"><label>钱包广播后的 Arbitrum 交易哈希<input name="transaction_hash" required pattern="0x[0-9a-fA-F]{64}" autocomplete="off"></label><button class="secondary" type="submit">验证币安充值到账</button><div class="form-error" role="alert"></div></form>` : binanceSubmission ? `<form class="binance-receipt-form" data-binance-receipt="${escapeHtml(operation.operation_id)}" data-binance-stage="BINANCE_WITHDRAWAL" data-operation-version="${Number(operation.version || 1)}"><button class="secondary" type="submit">验证币安提现与链上到账</button><div class="form-error" role="alert"></div></form>` : `<form class="binance-submit-form" data-binance-submit="${escapeHtml(operation.operation_id)}" data-operation-version="${Number(operation.version || 1)}"><label class="direct-capital-confirm"><input name="final_confirmed" type="checkbox" required><span>再次核对币种、网络、白名单地址、金额、手续费和额度</span></label><button class="primary" type="submit" ${directConfiguration.binance_capital_submission_enabled && item.real_transfer_gate === 'ENABLED' ? '' : 'disabled title="币安提现开关或 CAPITAL_TRANSFER 当前关闭"'}>最终确认并提交币安提现</button><p class="subtle">当前按钮受币安专用提现开关与 CAPITAL_TRANSFER 双重控制。</p><div class="form-error" role="alert"></div></form>`}</details>` : '';
    const walletBoundary = hyperliquidPreview ? `<details class="capital-wallet-handoff"><summary>主钱包 / 多签确认</summary><div class="wallet-handoff-facts"><b>${hyperliquidPreview.artifact.kind === 'HYPERLIQUID_WITHDRAW3_TYPED_REQUEST' ? 'withdraw3 人工签名请求' : hyperliquidPreview.artifact.kind === 'HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST' ? '主账户资金归集人工签名请求' : 'Arbitrum USDC 入金请求'}</b><span>网络：${escapeHtml(hyperliquidPreview.artifact.network || 'Hyperliquid Mainnet / Arbitrum')}</span><span>金额：${escapeHtml(hyperliquidPreview.artifact.amount)} USDC</span><span>方法：${escapeHtml(hyperliquidPreview.artifact.method || (walletStage === 'HYPERLIQUID_CLASS_TRANSFER' ? 'HyperliquidTransaction:UsdClassTransfer' : 'HyperliquidTransaction:Withdraw'))}</span><span>费用上限：${escapeHtml(hyperliquidPreview.artifact.maxFee || '链上钱包确认时核对')}</span><code>${escapeHtml(hyperliquidPreview.artifact.destination || hyperliquidPreview.artifact.bridge || '请在钱包中核对目标')}</code><p>API/Agent Wallet 归属已检查；该动作要求主账户或有效多签签名，因此自动进入人控钱包，不属于连接失败。系统不接收签名内容。</p></div>${walletSubmission ? `<p class="subtle">钱包提交已记录，等待逐项验证 Hyperliquid 与 Arbitrum 回执。</p>` : `<form class="wallet-result-form" data-wallet-result="${escapeHtml(operation.operation_id)}" data-wallet-stage="${walletStage}" data-operation-version="${Number(operation.version || 1)}">${walletStage === 'HYPERLIQUID_DEPOSIT' ? '<label>Arbitrum 交易哈希<input name="transaction_hash" required pattern="0x[0-9a-fA-F]{64}" autocomplete="off"></label>' : `<label>Hyperliquid action hash<input name="action_hash" required pattern="0x[0-9a-fA-F]{64}" autocomplete="off"></label><label>签名 nonce<input name="nonce" type="number" required value="${Number(hyperliquidPreview.artifact.nonce || 0)}"></label>`}<label class="direct-capital-confirm"><input name="final_confirmed" type="checkbox" required><span>钱包已显示并由我逐项核对网络、目标、金额、费用与方法</span></label><div class="compact-actions"><button class="primary" type="submit">记录钱包已提交</button><button class="secondary" type="button" data-wallet-cancel>记录取消</button></div><div class="form-error" role="alert"></div></form>`}${walletSubmission ? `<form class="receipt-result-form" data-hl-receipt="${escapeHtml(operation.operation_id)}" data-receipt-path="${walletStage}" data-operation-version="${Number(operation.version || 1)}">${walletStage === 'HYPERLIQUID_DEPOSIT' ? `<input type="hidden" name="recorded_hash" value="${escapeHtml(walletSubmission.transaction_hash || '')}"><label>验证阶段<select name="stage"><option value="HYPERLIQUID_DEPOSIT_ARBITRUM">Arbitrum 入金交易</option><option value="HYPERLIQUID_DEPOSIT_LEDGER">Hyperliquid 入金账本</option></select></label>` : `<input type="hidden" name="action_hash" value="${escapeHtml(walletSubmission.action_hash || '')}"><input type="hidden" name="nonce" value="${Number(walletSubmission.nonce || 0)}"><label>验证阶段<select name="stage">${walletStage === 'HYPERLIQUID_CLASS_TRANSFER' ? '<option value="HYPERLIQUID_CLASS_TRANSFER_LEDGER">主账户资金归集账本</option>' : '<option value="HYPERLIQUID_WITHDRAWAL_LEDGER">Hyperliquid 提现账本</option><option value="HYPERLIQUID_WITHDRAWAL_ARBITRUM">Arbitrum 钱包到账</option>'}</select></label>${walletStage === 'HYPERLIQUID_CLASS_TRANSFER' ? '' : '<label data-arbitrum-hash>Arbitrum 到账交易哈希<input name="transaction_hash" pattern="0x[0-9a-fA-F]{64}" autocomplete="off"></label>'}` }<button class="secondary" type="submit">读取并验证公开回执</button><div class="form-error" role="alert"></div></form>` : ''}</details>` : '';
    const treasuryWallet = operation.path === 'HYPERLIQUID_TO_VAULT' && providerPreviewReady ? `<details class="capital-wallet-handoff"><summary>最终存入${operation.treasury_provider === 'SAFE_SPENDING_LIMIT' ? ' Safe' : ' NoTilt Vault'}</summary>${treasurySubmission ? `<form class="treasury-receipt-form" data-treasury-receipt="${escapeHtml(operation.operation_id)}" data-operation-version="${Number(operation.version || 1)}"><input type="hidden" name="transaction_hash" value="${escapeHtml(treasurySubmission.transaction_hash || '')}"><button class="secondary" type="submit">验证链上金库到账</button><div class="form-error" role="alert"></div></form>` : `<form class="wallet-result-form" data-wallet-result="${escapeHtml(operation.operation_id)}" data-wallet-stage="TREASURY_DEPOSIT" data-operation-version="${Number(operation.version || 1)}"><label>钱包广播后的 Arbitrum 交易哈希<input name="transaction_hash" required pattern="0x[0-9a-fA-F]{64}" autocomplete="off"></label><label class="direct-capital-confirm"><input name="final_confirmed" type="checkbox" required><span>已在钱包中核对链、金库、USDC、金额和方法</span></label><div class="compact-actions"><button class="primary" type="submit">记录金库入金</button><button class="secondary" type="button" data-wallet-cancel>记录取消</button></div><div class="form-error" role="alert"></div></form>`}</details>` : '';
    return `<tr><td data-label="操作">${shortId(operation.operation_id)}<br><span class="subtle">${fmtDate(operation.final_confirmed_at)}</span></td><td data-label="路径 / 金额"><b>${escapeHtml(label)}</b><br><span class="subtle">${operation.treasury_provider === 'SAFE_SPENDING_LIMIT' ? 'Safe Spending Limits' : 'NoTilt Vault'}</span><br><span class="subtle">${fmtNumber(operation.amount)} ${escapeHtml(operation.asset)}</span></td><td data-label="阶段">${escapeHtml(stages || '尚无阶段')}</td><td data-label="状态 / 回执"><b>${escapeHtml(fmtStatus(operation.status))}</b><br><span class="subtle">回执：${escapeHtml(fmtStatus(operation.receipt_status))}</span></td><td data-label="精确阻断">${blockerDetails}<div class="capital-operation-actions">${hyperliquidButton}${binanceButton}${providerButton}</div>${walletBoundary}${binanceBoundary}${treasuryWallet}</td></tr>`;
  }).join('');
  const legacyRows = transfers.live.map(transfer => `<tr><td data-label="记录">${shortId(transfer.capital_transfer_id)}</td><td data-label="方向">${escapeHtml(fmtCapitalDirection(transfer.direction))}</td><td data-label="金额">${fmtNumber(transfer.gross_amount)} ${escapeHtml(transfer.asset)}</td><td data-label="状态">${escapeHtml(fmtStatus(transfer.status))}</td><td data-label="外部回执">${escapeHtml(transfer.external_transfer_id || '未提交')}</td></tr>`).join('');
  const selectedProviderReady = selectedTreasuryProvider === 'SAFE_SPENDING_LIMIT'
    ? directConfiguration.safe_spending_scope_configured
    : directConfiguration.notilt_scope_configured;
  const selectedProviderStatus = selectedProviderReady
    ? '已配置'
    : selectedTreasuryProvider === 'SAFE_SPENDING_LIMIT'
      ? '缺少 Safe、delegate 或可信 RPC'
      : '缺少官方金库或 Agent 范围';
  const capitalRangeText = historySelection.complete ? '全部历史' : `最近 ${formatCapitalRangeDuration(historySelection.duration)}`;
  const capitalHistoryRangeControl = hasHistory ? `<div class="capital-history-range" aria-label="资金曲线时间范围"><div class="capital-history-range-copy"><small>时间范围</small><output for="capital-history-range" data-capital-range-label>${escapeHtml(capitalRangeText)}</output></div><div class="capital-history-range-slider"><input id="capital-history-range" type="range" data-capital-history-range min="1" max="${CAPITAL_CHART_RANGE_MAX}" step="1" value="${Number(capitalChartRangeValue)}" aria-label="拖动选择资金曲线时间范围" aria-valuetext="${escapeHtml(capitalRangeText)}"><div aria-hidden="true"><span>较短</span><span>全部</span></div></div></div>` : '';
  main.innerHTML = `<section class="page capital-page"><header class="page-head"><div><p class="eyebrow">生产资金 · 缺失即阻断 · 不代签不广播</p><h1>资金中心</h1><p class="lede">只保留四条明确资金路径。每次操作都先最终确认，再校验地址、网络、金额、额度和实时安全开关；当前缺少生产参数时只记录阻断与阶段，不会生成订单、签名或发送资金。</p></div></header><div class="stats"><div class="stat"><small>三方总净值</small><b>${fmtNumber(netWorth.total)} ${escapeHtml(netWorth.currency)}</b></div><div class="stat"><small>资金库</small><b>${fmtNumber(netWorth.vault)} ${escapeHtml(netWorth.currency)}</b></div>${venueNetWorth}<div class="stat"><small>净值状态</small><b style="font-size:14px">${escapeHtml(fmtStatus(netWorth.complete ? 'CURRENT' : 'INCOMPLETE'))}</b></div><div class="stat"><small>资金操作</small><b style="font-size:14px">${escapeHtml(fmtStatus(item.real_transfer_gate || 'DISABLED'))}</b></div><div class="stat"><small>在途 / 占用</small><b>${fmtNumber(liveInTransit)}</b></div></div><section class="capital-chart-panel"><div class="chart-head"><div><p class="eyebrow">资金统计</p><h2>资金净值趋势</h2><p class="subtle">固定四条线：币安、Hyperliquid、链上金库和三方汇总。缺失来源显示“等待数据”，不会补成 0；历史曲线不会冒充当前净值。</p></div><b>${fmtNumber(netWorth.total)} <small>${escapeHtml(netWorth.currency)}</small></b></div>${hasHistory ? `<canvas id="capital-chart" height="260" aria-label="币安、Hyperliquid、链上金库和三方汇总四条资金趋势"></canvas>` : '<div class="chart-empty">完成生产资金同步后才显示曲线；缺失数据不会补零。</div>'}<div class="chart-legend" role="group" aria-label="选择显示的资金曲线">${chartLegend}</div></section>${netWorth.complete ? '' : `<div class="callout"><b>净值不完整：</b>${escapeHtml([...new Set((netWorth.issues || []).map(formatCapitalIssue))].join('；') || '尚无资金数据')}</div>`}<section><h2>资金位置</h2><p class="subtle">固定展示三处资金；缺失金额显示为“—”，历史快照不会计入当前净值。</p>${capitalBalanceTable(liveBalanceRows, '尚无生产资金数据。')}</section><article class="card"><div class="card-heading"><div><p class="eyebrow">生产配置预检</p><h2>只显示是否配置，不回显地址或凭据</h2></div><span class="status-pill">单账户模式</span></div><dl class="definition-grid">${definition('网络 / 资产', `${directConfiguration.network || '—'} / ${directConfiguration.asset || '—'}`)}${definition('当前链上金库', selectedTreasuryProviderLabel)}${definition('链上金库状态', selectedProviderStatus)}${definition('自有钱包', directConfiguration.owned_arbitrum_address_configured ? '已配置' : '未配置')}${definition('币安账户与地址', directConfiguration.binance_account_configured && directConfiguration.binance_whitelist_destination_configured && directConfiguration.binance_withdrawal_destination_configured ? '已配置' : '不完整')}${definition('币安专用资金 API', directConfiguration.binance_capital_credentials_configured ? '已加载（不回显）' : '未加载')}${definition('币安真实提现提交', directConfiguration.binance_capital_submission_enabled && item.real_transfer_gate === 'ENABLED' ? '已启用' : '关闭；仅可预检')}${definition('Hyperliquid 路径', directConfiguration.hyperliquid_account_configured && directConfiguration.hyperliquid_contract_configured ? '已配置' : '不完整')}${definition('金额 / 费用上限', directConfiguration.limits_configured ? '已配置' : '未配置')}${definition('签名 / 广播', '币安只使用专用受限 API；链上签名交给人控钱包或有效多签，服务端不读取私钥')}</dl></article><section class="capital-routes-section"><div class="card-heading"><div><p class="eyebrow">四条直达路径</p><h2>选择资金从哪里到哪里</h2><p class="subtle">先选路径，再在一个确认窗口里填写金额；安全说明不再重复四遍。</p></div><span class="status-pill ${item.real_transfer_gate === 'ENABLED' ? 'status-APPROVED' : 'status-DISABLED'}">${escapeHtml(fmtStatus(item.real_transfer_gate || 'DISABLED'))}</span></div>${directConfigurationEditor}<div class="callout direct-capital-boundary" data-provider-boundary><b>统一安全边界：</b>系统只使用当前选定的 ${escapeHtml(selectedTreasuryProviderLabel)}；每条路径都重新校验地址、网络、资产、额度、实时状态与安全开关。链上私钥不进入控制台；真实币安提现还需专用开关和 CAPITAL_TRANSFER 双重开启。</div><div class="capital-route-grid">${directPathCards}</div></section>${directCapitalDialog}<section><h2>操作日志、阶段与回执</h2>${directRows ? `<div class="table-wrap is-scrollable capital-operation-table"><table><thead><tr><th>操作</th><th>路径 / 金额</th><th>阶段</th><th>状态 / 回执</th><th>精确阻断</th></tr></thead><tbody>${directRows}</tbody></table></div>` : '<div class="callout">尚无直达资金操作。提交一次最终确认后，会在这里记录校验结果。</div>'}</section><section><h2>历史资金划转</h2><p class="subtle">旧流程只保留为只读审计记录，不再是四条直达操作的必经界面。</p>${legacyRows ? `<div class="table-wrap is-scrollable capital-history-table"><table><thead><tr><th>记录</th><th>方向</th><th>金额</th><th>状态</th><th>外部回执</th></tr></thead><tbody>${legacyRows}</tbody></table></div>` : '<div class="callout">尚无历史资金划转。</div>'}</section></section>`;
  const pageHead = main.querySelector('.capital-page > .page-head');
  pageHead.classList.add('capital-page-head');
  pageHead.innerHTML = `<div><p class="eyebrow">生产资金 · 只读事实</p><h1>资金中心</h1><p class="lede">先看总额、资金位置和数据可信度，再处理资金路径。缺失、过期或时间错位的数据不会补零，也不会参与汇总。</p></div><div class="capital-gate-summary"><small>资金操作</small><b>${escapeHtml(fmtStatus(item.real_transfer_gate || 'DISABLED'))}</b><span>在途 / 占用 ${fmtNumber(liveInTransit)} USDC</span></div>`;
  const legacyStats = main.querySelector('.capital-page > .stats');
  legacyStats.outerHTML = `<section class="capital-overview" aria-label="当前资金净值"><article class="capital-total-card ${netWorth.complete ? 'is-current' : 'is-limited'}"><small>当前三方总净值</small><b>${escapeHtml(totalHeadline)}</b><p>${escapeHtml(totalSupporting)}</p></article><div class="capital-source-cards">${sourceCards}</div></section><section class="capital-trust-panel ${netWorth.complete ? 'is-current' : 'is-limited'}"><div><b>${netWorth.complete ? '数据可信，可用于当前汇总' : '当前汇总已阻断'}</b><p>${escapeHtml(trustCopy)}</p></div><span>${netWorth.complete ? '完整' : '需关注'}</span></section>`;
  const chartPanel = main.querySelector('.capital-chart-panel');
  chartPanel.innerHTML = `<div class="chart-head"><div><p class="eyebrow">资金净值趋势</p><h2>四条固定资金曲线</h2><p class="subtle">Binance、Hyperliquid、${escapeHtml(selectedOnchainSeriesLabel)} 与三方汇总。汇总只使用 ${Number(netWorth.alignment_tolerance_seconds || 60)} 秒内对齐的三方事实；缺失、过期、错位和断档都不会补零或强行连线。</p></div></div><div class="capital-chart-meta"><span data-capital-range-coverage>${escapeHtml(chartCoverage)}</span><span>断档按实际采样节奏的 3 倍判定（至少 ${Number(netWorth.history_gap_tolerance_seconds || 300)} 秒）；纵轴至少保留 0.05% 观察范围</span></div><div class="chart-legend" role="group" aria-label="选择显示的资金曲线">${chartLegend}</div>${hasHistory ? `<div class="capital-chart-wrap"><canvas id="capital-chart" height="300" aria-label="Binance、Hyperliquid、${escapeHtml(selectedOnchainSeriesLabel)} 和三方汇总四条 USD 资金趋势"></canvas><div class="capital-chart-tooltip" role="status" hidden></div></div>` : '<div class="chart-empty">尚无可绘制的资金历史；缺失数据不会补零。</div>'}${capitalHistoryRangeControl}`;
  const legacyIncomplete = chartPanel.nextElementSibling?.classList.contains('callout') ? chartPanel.nextElementSibling : null;
  legacyIncomplete?.remove();
  const capitalPositionsHeading = [...main.querySelectorAll('section > h2')].find(heading => heading.textContent === '资金位置');
  if (capitalPositionsHeading) {
    capitalPositionsHeading.textContent = '资金位置明细';
    const detail = capitalPositionsHeading.nextElementSibling;
    if (detail?.classList.contains('subtle')) detail.textContent = 'USD 金额统一精度：小额四位、大额两位；原资产余额保留在明细中。历史快照不会计入当前净值。';
  }
  const activeCapitalView = capitalViewSelection();
  const capitalNavigation = document.createElement('nav');
  capitalNavigation.className = 'section-tabs capital-section-tabs';
  capitalNavigation.setAttribute('aria-label', '资金中心页面');
  capitalNavigation.innerHTML = `<a id="capital-view-tab-overview" class="${activeCapitalView === 'overview' ? 'active' : ''}" href="/capital" data-link ${activeCapitalView === 'overview' ? 'aria-current="page"' : ''}><span>资金总览</span><b>${netWorth.complete ? '完整' : '需关注'}</b></a><a id="capital-view-tab-routes" class="${activeCapitalView === 'routes' ? 'active' : ''}" href="/capital?view=routes" data-link ${activeCapitalView === 'routes' ? 'aria-current="page"' : ''}><span>资金路径</span><b>${escapeHtml(fmtStatus(item.real_transfer_gate || 'DISABLED'))}</b></a><a id="capital-view-tab-activity" class="${activeCapitalView === 'activity' ? 'active' : ''}" href="/capital?view=activity" data-link ${activeCapitalView === 'activity' ? 'aria-current="page"' : ''}><span>操作与回执</span><b>${Number(item.direct_operations?.length || 0)}</b></a>`;
  pageHead.after(capitalNavigation);
  const overviewPanel = groupCapitalView([
    main.querySelector('.capital-overview'),
    main.querySelector('.capital-trust-panel'),
    chartPanel,
    capitalPositionsHeading?.closest('section'),
  ], 'overview', activeCapitalView);
  const configurationCard = [...main.querySelectorAll('.capital-page > article.card')].find(card => card.querySelector('h2')?.textContent === '只显示是否配置，不回显地址或凭据');
  const routesPanel = groupCapitalView([
    configurationCard,
    main.querySelector('.capital-routes-section'),
  ], 'routes', activeCapitalView);
  const operationHeading = [...main.querySelectorAll('.capital-page > section > h2')].find(heading => heading.textContent === '操作日志、阶段与回执');
  const historyHeading = [...main.querySelectorAll('.capital-page > section > h2')].find(heading => heading.textContent === '历史资金划转');
  const activityPanel = groupCapitalView([
    operationHeading?.closest('section'),
    historyHeading?.closest('section'),
  ], 'activity', activeCapitalView);
  [overviewPanel, routesPanel, activityPanel].filter(Boolean).forEach(panel => {
    panel.setAttribute('role', 'region');
  });
  if (activeCapitalView === 'overview') drawCapitalChart(visibleHistorySeries);
  bindCapitalActions(historySeries, {
    history:item.history || [],
    alignmentToleranceSeconds:netWorth.alignment_tolerance_seconds || 60,
    gapToleranceSeconds:netWorth.history_gap_tolerance_seconds || 300,
    selectedOnchainSeriesLabel,
  });
}

function drawCapitalChart(series) {
  const canvas = document.querySelector('#capital-chart');
  if (!canvas) return;
  const allPoints = series.flatMap(item => item.points);
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = width < 520 ? 270 : 300;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const context = canvas.getContext('2d'); context.scale(ratio, ratio);
  const styles = getComputedStyle(document.documentElement);
  const line = styles.getPropertyValue('--line').trim();
  const panel = styles.getPropertyValue('--panel').trim();
  const muted = styles.getPropertyValue('--muted').trim();
  if (!allPoints.length) {
    context.fillStyle = muted;
    context.font = '11px system-ui';
    context.textAlign = 'center';
    context.fillText('请至少选择一条有数据的曲线', width / 2, height / 2);
    return;
  }
  const colors = ['--chart-source-1','--chart-source-2','--chart-source-3','--chart-total'].map(name => styles.getPropertyValue(name).trim());
  const times = allPoints.map(point => point.time);
  const minimumTime = Math.min(...times);
  const maximumTime = Math.max(...times);
  const axisSamples = allPoints.map(point => formatCapitalUsd(point.value));
  context.font = width < 520 ? '9px system-ui' : '10px system-ui';
  const widestAxisLabel = Math.max(44, ...axisSamples.map(label => context.measureText(label).width));
  const left = Math.ceil(widestAxisLabel + (width < 520 ? 14 : 18));
  const right = width - 10, top = 18, bottom = height - 34;
  const x = time => maximumTime === minimumTime ? (left + right) / 2 : left + ((right - left) * (time - minimumTime) / (maximumTime - minimumTime));
  const total = series.find(item => item.source === 'TOTAL' && item.points.length);
  const sources = series.filter(item => item.source !== 'TOTAL' && item.points.length);
  const bands = total && sources.length
    ? [{items:[total], top, bottom:top + (bottom - top) * .39}, {items:sources, top:top + (bottom - top) * .53, bottom}]
    : [{items:series.filter(item => item.points.length), top, bottom}];
  const hitPoints = [];
  context.textBaseline = 'middle';
  bands.forEach(band => {
    const values = band.items.flatMap(item => item.points.map(point => point.value));
    const domain = capitalAxisDomain(values);
    const range = domain.range;
    band.minimum = domain.minimum;
    band.maximum = domain.maximum;
    band.y = value => band.bottom - ((band.bottom - band.top) * (value - band.minimum) / range);
    context.fillStyle = muted;
    context.textAlign = 'left';
    context.textBaseline = 'bottom';
    context.fillText(band.items.some(item => item.source === 'TOTAL') ? '三方汇总 · USD' : '单项资金 · USD', left, band.top - 4);
    context.textBaseline = 'middle';
    for (let index = 0; index < 3; index += 1) {
      const gridY = band.top + ((band.bottom - band.top) * index / 2);
      const tickValue = band.maximum - ((band.maximum - band.minimum) * index / 2);
      context.strokeStyle = line; context.lineWidth = 1;
      context.beginPath(); context.moveTo(left, gridY); context.lineTo(right, gridY); context.stroke();
      context.fillStyle = muted; context.textAlign = 'right';
      context.fillText(formatCapitalUsd(tickValue), left - 7, gridY);
    }
  });
  series.forEach((item, seriesIndex) => {
    if (!item.points.length) return;
    const band = bands.find(candidate => candidate.items.includes(item));
    if (!band) return;
    const colorIndex = ({BINANCE:0, HYPERLIQUID:1, VAULT:2, TOTAL:3})[item.source];
    const color = colors[colorIndex ?? seriesIndex] || colors[0];
    const projected = item.points.map(point => ({...point, x:x(point.time), y:band.y(point.value)}));
    const points = compactCapitalChartPoints(projected);
    context.beginPath();
    points.forEach((point, index) => {
      if (!index || point.breakBefore) context.moveTo(point.x, point.y);
      else context.lineTo(point.x, point.y);
      hitPoints.push({...point, source:item.source, label:item.label, color});
    });
    context.strokeStyle = color;
    context.lineWidth = item.source === 'TOTAL' ? 3 : 2.25;
    context.lineJoin = 'round';
    context.lineCap = 'round';
    if (item.source === 'TOTAL') context.setLineDash([7, 5]);
    context.stroke();
    context.setLineDash([]);
    const latest = points.at(-1);
    context.beginPath();
    context.arc(latest.x, latest.y, 4, 0, Math.PI * 2);
    context.fillStyle = panel;
    context.fill();
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.stroke();
  });
  context.fillStyle = muted; context.textBaseline = 'bottom';
  const timeFormatter = new Intl.DateTimeFormat('zh-CN', {month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit'});
  const tickCount = width < 520 ? 3 : 5;
  for (let index = 0; index < tickCount; index += 1) {
    const ratio = tickCount === 1 ? 0 : index / (tickCount - 1);
    const tickTime = minimumTime + (maximumTime - minimumTime) * ratio;
    const tickX = left + (right - left) * ratio;
    context.textAlign = index === 0 ? 'left' : index === tickCount - 1 ? 'right' : 'center';
    context.fillText(timeFormatter.format(new Date(tickTime)), tickX, height - 4);
  }
  const tooltip = document.querySelector('.capital-chart-tooltip');
  canvas.onpointermove = event => {
    const rect = canvas.getBoundingClientRect();
    const pointerX = event.clientX - rect.left;
    const pointerY = event.clientY - rect.top;
    const nearest = hitPoints.reduce((best, point) => {
      const distance = Math.hypot(point.x - pointerX, point.y - pointerY);
      return !best || distance < best.distance ? {point, distance} : best;
    }, null);
    if (!tooltip || !nearest || nearest.distance > 30) {
      if (tooltip) tooltip.hidden = true;
      return;
    }
    tooltip.hidden = false;
    tooltip.innerHTML = `<b translate="no">${escapeHtml(nearest.point.label)}</b><span>${escapeHtml(formatCapitalUsd(nearest.point.value))}</span><small>${escapeHtml(fmtDate(nearest.point.time))}</small>`;
    tooltip.style.left = `${Math.min(width - 145, Math.max(6, nearest.point.x + 10))}px`;
    tooltip.style.top = `${Math.max(6, nearest.point.y - 64)}px`;
  };
  canvas.onpointerleave = () => { if (tooltip) tooltip.hidden = true; };
}

function bindCapitalActions(historySeries = [], rangeContext = null) {
  let activeHistorySeries = historySeries;
  const redrawCapitalHistory = () => {
    drawCapitalChart(activeHistorySeries.filter(series => capitalTrendVisibility[series.source]));
  };
  const rangeInput = document.querySelector('[data-capital-history-range]');
  rangeInput?.addEventListener('input', event => {
    capitalChartRangeValue = Number(event.currentTarget.value);
    const selection = capitalHistoryRange(rangeContext?.history || [], capitalChartRangeValue);
    activeHistorySeries = capitalHistorySeries(
      selection.history,
      rangeContext?.alignmentToleranceSeconds || 60,
      rangeContext?.gapToleranceSeconds || 300,
    );
    activeHistorySeries.forEach(series => {
      if (series.source === 'VAULT') {
        series.label = rangeContext?.selectedOnchainSeriesLabel || '链上金库';
      }
    });
    const rangeLabel = document.querySelector('[data-capital-range-label]');
    const rangeText = selection.complete
      ? '全部历史'
      : `最近 ${formatCapitalRangeDuration(selection.duration)}`;
    if (rangeLabel) {
      rangeLabel.textContent = rangeText;
    }
    event.currentTarget.setAttribute('aria-valuetext', rangeText);
    const coverage = document.querySelector('[data-capital-range-coverage]');
    if (coverage) coverage.textContent = capitalHistoryCoverage(activeHistorySeries, selection);
    redrawCapitalHistory();
  });
  document.querySelectorAll('[data-capital-trend]').forEach(input => input.addEventListener('change', event => {
    capitalTrendVisibility[event.currentTarget.dataset.capitalTrend] = event.currentTarget.checked;
    redrawCapitalHistory();
  }));
  capitalChartResizeObserver?.disconnect();
  const chartCanvas = document.querySelector('#capital-chart');
  if (chartCanvas && typeof ResizeObserver !== 'undefined') {
    let previousWidth = Math.round(chartCanvas.clientWidth);
    capitalChartResizeObserver = new ResizeObserver(entries => {
      const nextWidth = Math.round(entries[0]?.contentRect?.width || chartCanvas.clientWidth);
      if (!nextWidth || nextWidth === previousWidth) return;
      previousWidth = nextWidth;
      redrawCapitalHistory();
    });
    capitalChartResizeObserver.observe(chartCanvas);
  }
  document.querySelectorAll('[data-capital-preview]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    const isSafe = target.dataset.previewProvider === 'SAFE_SPENDING_LIMIT';
    const safeOutbound = target.dataset.previewDirection === 'OUTBOUND';
    const safeMessage = safeOutbound ? '只会读取官方 Allowance Module、当前 USDC 额度、余额、重置周期与 nonce，并构建待人控 delegate 钱包签名的精确哈希。' : '只会构建从已授权自有地址到配置 Safe 的精确 Arbitrum USDC 无签名交易。';
    const confirmed = await confirmAction({title:isSafe ? (safeOutbound ? '读取 Safe Spending Limit？' : '生成 Safe 入金预检？') : '生成 NoTilt SDK 无签名预检？', message:isSafe ? `${safeMessage} 不会读取私钥、签名或广播。` : '只会调用官方可信部署和资产目录构建固定用途的无签名交易。不会签名或广播；当前仍有任何阻断时，不得交给钱包执行。', confirmLabel:'确认并生成预检'});
    if (!confirmed) return;
    await withPending(target, '预检中…', async () => {
      try {
        const suffix = isSafe ? 'safe-spending-preview' : 'notilt-unsigned-preview';
        const result = await api(`/api/capital/direct-operations/${target.dataset.capitalPreview}/${suffix}`, {method:'POST', body:JSON.stringify({expected_version:Number(target.dataset.operationVersion), final_confirmed:true, idempotency_key:crypto.randomUUID()})});
        showToast(result.execution_blocked ? `已生成无签名预检，但仍被 ${result.blockers.length} 项条件阻断` : '无签名计划已生成；仍需独立人控钱包逐笔确认');
        await route();
      } catch (error) { showApiError(error); }
    });
  }));
  document.querySelectorAll('[data-binance-preview]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    const confirmed = await confirmAction({title:'执行币安资金预检？', message:'只读取官方 Wallet API 的实时权限、IP 限制、USDC Arbitrum 网络、白名单、余额、额度、手续费或充值地址。不会提交提现，也不会回显 API Key、Secret 或签名。', confirmLabel:'确认并预检'});
    if (!confirmed) return;
    await withPending(target, '币安预检中…', async () => {
      try {
        await api(`/api/capital/direct-operations/${target.dataset.binancePreview}/binance-preview`, {method:'POST', body:JSON.stringify({expected_version:Number(target.dataset.operationVersion), final_confirmed:true, idempotency_key:crypto.randomUUID()})});
        showToast('币安实时预检已完成；尚未提交任何资金操作');
        await route();
      } catch (error) { showApiError(error); }
    });
  }));
  document.querySelectorAll('.binance-submit-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const current = event.currentTarget;
    const confirmed = await confirmAction({title:'最终提交币安提现？', message:'这是实际资金操作。系统会使用已核对的专用受限 API，提交后只能通过固定 withdrawOrderId 查询状态，不会自动重试未知结果。', confirmLabel:'确认提交提现'});
    if (!confirmed) return;
    await withPending(event.submitter, '提交中…', async () => {
      try {
        await api(`/api/capital/direct-operations/${current.dataset.binanceSubmit}/binance-submit`, {method:'POST', body:JSON.stringify({expected_version:Number(current.dataset.operationVersion), final_confirmed:true, confirmation_phrase:'CONFIRM_BINANCE_WITHDRAWAL', idempotency_key:crypto.randomUUID()})});
        showToast('币安提现已提交；等待币安与 Arbitrum 双重回执');
        await route();
      } catch (error) { showApiError(error, current.querySelector('.form-error')); }
    });
  }));
  document.querySelectorAll('.binance-receipt-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const current = event.currentTarget;
    const transactionHash = new FormData(current).get('transaction_hash');
    const payload = {expected_version:Number(current.dataset.operationVersion), stage:current.dataset.binanceStage, idempotency_key:crypto.randomUUID()};
    if (transactionHash) payload.transaction_hash = transactionHash;
    await withPending(event.submitter, '核对回执中…', async () => {
      try {
        await api(`/api/capital/direct-operations/${current.dataset.binanceReceipt}/binance-receipt`, {method:'POST', body:JSON.stringify(payload)});
        showToast('币安与链上回执已按冻结范围核对');
        await route();
      } catch (error) { showApiError(error, current.querySelector('.form-error')); }
    });
  }));
  document.querySelectorAll('[data-hyperliquid-preview]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    const confirmed = await confirmAction({title:'生成 Hyperliquid 人控钱包请求？', message:'系统会先验证 API/Agent Wallet 与主账户归属、当前余额、官方 Bridge2、白名单、金额和费用。withdraw3 或链上入金不接受代理钱包时会自动转为主钱包/有效多签确认，不会记录私钥、签名或广播。', confirmLabel:'确认并生成'});
    if (!confirmed) return;
    await withPending(target, '协议预检中…', async () => {
      try {
        const result = await api(`/api/capital/direct-operations/${target.dataset.hyperliquidPreview}/hyperliquid-preview`, {method:'POST', body:JSON.stringify({expected_version:Number(target.dataset.operationVersion), final_confirmed:true, idempotency_key:crypto.randomUUID()})});
        showToast(result.automatic_fallback ? '协议权限已核对：已自动切换为主钱包 / 多签逐笔确认' : 'Hyperliquid 预检已完成');
        await route();
      } catch (error) { showApiError(error); }
    });
  }));
  document.querySelectorAll('.wallet-result-form').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const current = event.currentTarget;
      const values = Object.fromEntries([...new FormData(current).entries()].filter(([, value]) => String(value).trim() && value !== 'on'));
      if (values.nonce) values.nonce = Number(values.nonce);
      const confirmed = await confirmAction({title:'记录人控钱包提交结果？', message:'只记录公开交易哈希或 action hash 与 nonce，不上传签名内容。系统随后仍会独立验证 Hyperliquid、Arbitrum 与链上金库回执。', confirmLabel:'确认记录'});
      if (!confirmed) return;
      await withPending(event.submitter, '记录中…', async () => {
        try {
          await api(`/api/capital/direct-operations/${current.dataset.walletResult}/wallet-submission`, {method:'POST', body:JSON.stringify({...values, expected_version:Number(current.dataset.operationVersion), stage:current.dataset.walletStage, outcome:'SUBMITTED', final_confirmed:true, idempotency_key:crypto.randomUUID()})});
          showToast('钱包提交已记录；资金状态仍等待公开回执验证');
          await route();
        } catch (error) { showApiError(error, current.querySelector('.form-error')); }
      });
    });
    form.querySelector('[data-wallet-cancel]')?.addEventListener('click', async event => {
      const current = event.currentTarget.closest('.wallet-result-form');
      const confirmed = await confirmAction({title:'记录钱包取消？', message:'取消后系统保持 fail-closed，不会将其记为协议故障，也不会继续资金流程。', confirmLabel:'确认取消'});
      if (!confirmed) return;
      try {
        await api(`/api/capital/direct-operations/${current.dataset.walletResult}/wallet-submission`, {method:'POST', body:JSON.stringify({expected_version:Number(current.dataset.operationVersion), stage:current.dataset.walletStage, outcome:'CANCELLED', final_confirmed:true, idempotency_key:crypto.randomUUID()})});
        showToast('已记录钱包取消；资金未由系统发送');
        await route();
      } catch (error) { showApiError(error, current.querySelector('.form-error')); }
    });
  });
  document.querySelectorAll('.receipt-result-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const current = event.currentTarget;
    const values = Object.fromEntries([...new FormData(current).entries()].filter(([, value]) => String(value).trim()));
    const stage = values.stage;
    const payload = {expected_version:Number(current.dataset.operationVersion), stage, idempotency_key:crypto.randomUUID()};
    if (stage === 'HYPERLIQUID_DEPOSIT_ARBITRUM') payload.transaction_hash = values.recorded_hash;
    if (stage === 'HYPERLIQUID_DEPOSIT_LEDGER') payload.action_hash = values.recorded_hash;
    if (stage === 'HYPERLIQUID_WITHDRAWAL_LEDGER' || stage === 'HYPERLIQUID_CLASS_TRANSFER_LEDGER') { payload.action_hash = values.action_hash; payload.nonce = Number(values.nonce); }
    if (stage === 'HYPERLIQUID_WITHDRAWAL_ARBITRUM') payload.transaction_hash = values.transaction_hash;
    await withPending(event.submitter, '验证中…', async () => {
      try {
        await api(`/api/capital/direct-operations/${current.dataset.hlReceipt}/hyperliquid-receipt`, {method:'POST', body:JSON.stringify(payload)});
        showToast('公开回执已验证；其他阶段仍按状态逐项等待');
        await route();
      } catch (error) { showApiError(error, current.querySelector('.form-error')); }
    });
  }));
  document.querySelectorAll('.treasury-receipt-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const current = event.currentTarget;
    const transactionHash = new FormData(current).get('transaction_hash');
    await withPending(event.submitter, '验证金库到账…', async () => {
      try {
        await api(`/api/capital/direct-operations/${current.dataset.treasuryReceipt}/treasury-receipt`, {method:'POST', body:JSON.stringify({expected_version:Number(current.dataset.operationVersion), transaction_hash:transactionHash, idempotency_key:crypto.randomUUID()})});
        showToast('链上金库到账已验证；仅在全部回执一致后标记完成');
        await route();
      } catch (error) { showApiError(error, current.querySelector('.form-error')); }
    });
  }));
  const directCapitalConfigForm = document.querySelector('#direct-capital-config-form');
  const syncTreasuryProviderFields = () => {
    if (!directCapitalConfigForm) return;
    const provider = directCapitalConfigForm.elements.treasury_provider.value;
    directCapitalConfigForm.querySelectorAll('[data-provider-fields]').forEach(section => {
      const active = section.dataset.providerFields === provider;
      section.hidden = !active;
      section.querySelectorAll('input').forEach(input => { input.disabled = !active; });
    });
    const guidance = directCapitalConfigForm.querySelector('[data-provider-guidance]');
    if (guidance) guidance.textContent = provider === 'SAFE_SPENDING_LIMIT'
      ? '当前选择 Safe Spending Limits：只需配置 Safe Smart Account 和 delegate；NoTilt 字段不会保存，也不会参与资金操作。'
      : '当前选择 NoTilt Vault：只需配置 NoTilt 金库编号和地址；Safe 字段不会保存，也不会参与资金操作。';
    const boundary = document.querySelector('[data-provider-boundary]');
    if (boundary) boundary.innerHTML = `<b>统一安全边界：</b>系统只使用当前选定的 ${provider === 'SAFE_SPENDING_LIMIT' ? 'Safe Spending Limits' : 'NoTilt Vault'}；每条路径都重新校验地址、网络、资产、额度、实时状态与安全开关，且不在服务内签名或广播。`;
  };
  directCapitalConfigForm?.querySelectorAll('input[name="treasury_provider"]').forEach(input => input.addEventListener('change', syncTreasuryProviderFields));
  syncTreasuryProviderFields();
  directCapitalConfigForm?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries([...new FormData(form).entries()].filter(([, value]) => String(value).trim()));
    values.network = 'ARBITRUM'; values.asset = 'USDC'; values.idempotency_key = crypto.randomUUID();
    const confirmed = await confirmAction({title:'保存新的资金路径配置版本？', message:'系统只保存公开账户范围、地址和额度，不接收任何密钥或签名材料。新版本只影响之后创建的资金操作；现有操作继续使用冻结引用。', confirmLabel:'确认保存配置'});
    if (!confirmed) return;
    await withPending(event.submitter, '保存中…', async () => {
      try {
        await api('/api/capital/direct-configuration', {method:'PUT', body:JSON.stringify(values)});
        showToast('资金路径配置新版本已保存并写入审计');
        await route();
      } catch (error) { showApiError(error, form.querySelector('.form-error')); }
    });
  });
  const directCapitalDialog = document.querySelector('#direct-capital-dialog');
  const directCapitalForm = document.querySelector('#direct-capital-form');
  document.querySelectorAll('[data-open-capital-path]').forEach(button => button.addEventListener('click', event => {
    const path = DIRECT_CAPITAL_PATHS.find(item => item.path === event.currentTarget.dataset.openCapitalPath);
    if (!path || !directCapitalDialog || !directCapitalForm) return;
    directCapitalForm.reset();
    directCapitalForm.dataset.directCapitalForm = path.path;
    directCapitalForm.elements.path.value = path.path;
    directCapitalForm.querySelector('[data-capital-path-title]').textContent = `${path.from} → ${path.to}`;
    directCapitalForm.querySelector('[data-capital-path-copy]').textContent = path.copy;
    directCapitalForm.querySelector('.form-error').textContent = '';
    directCapitalDialog.showModal();
  }));
  document.querySelectorAll('[data-close-capital-dialog]').forEach(button => button.addEventListener('click', () => directCapitalDialog?.close()));
  document.querySelectorAll('[data-direct-capital-form]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const currentForm = event.currentTarget;
    const amount = new FormData(currentForm).get('amount');
    const treasuryProvider = currentForm.dataset.treasuryProvider || 'NOTILT_VAULT';
    const path = currentForm.dataset.directCapitalForm;
    const pathDefinition = DIRECT_CAPITAL_PATHS.find(item => item.path === path);
    const pathLabel = pathDefinition ? `${pathDefinition.from} → ${pathDefinition.to}` : path;
    directCapitalDialog?.close();
    const providerLabel = treasuryProvider === 'SAFE_SPENDING_LIMIT' ? 'Safe Spending Limits' : 'NoTilt Vault';
    const confirmed = await confirmAction({title:'最终确认资金路径？', message:`使用 ${providerLabel} 检查 ${pathLabel}，金额 ${amount} USDC。系统会重新校验地址、网络、额度和安全开关；任何缺失都会阻断，不会发送资金。`, confirmLabel:'确认并执行安全检查'});
    if (!confirmed) { directCapitalDialog?.showModal(); return; }
    const button = currentForm.querySelector('button[type="submit"], button:not([type])');
    await withPending(button, '正在安全校验…', async () => {
      try {
        const result = await api('/api/capital/direct-operations', {
          method:'POST',
          body:JSON.stringify({path, amount, final_confirmed:true, idempotency_key:crypto.randomUUID()}),
        });
        const blockers = (result.blockers || []).map(formatDirectCapitalBlocker).join('；');
        showToast(result.status === 'BLOCKED' ? `已安全阻断：${blockers}` : '资金操作已记录');
        await route();
      } catch (error) {
        showApiError(error, currentForm.querySelector('.form-error'));
        directCapitalDialog?.showModal();
      }
    });
  }));
}
