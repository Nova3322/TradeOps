let capitalSeriesColorIndex = {TOTAL:6};
let capitalOperationsPage = 1;
let capitalOperationsPageSize = 50;

function partitionCapitalRecords(records) {
  return {
    live: records.filter(item => item.environment === 'LIVE'),
    testnet: records.filter(item => item.environment === 'TESTNET'),
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
  const [sourceVenue, sourceAccount] = source.split('|');
  const venueLabel = ({BINANCE:'Binance',HYPERLIQUID:'Hyperliquid',VAULT:'Vault'}[sourceVenue] || sourceVenue);
  const sourceLabel = sourceAccount ? `${venueLabel} · ${sourceAccount}` : venueLabel;
  return ({
    MISSING_LIVE_SOURCE:`${sourceLabel || '资金来源'}：尚未同步`,
    STALE_LIVE_SOURCE:`${sourceLabel || '资金来源'}：数据已过期`,
    UNKNOWN_USD_VALUE:`${sourceLabel || '资金来源'}：缺少美元估值`,
    CURRENT_VALUE_MISSING:`${sourceLabel || '资金来源'}：没有可采信的当前美元净值`,
    MISSING_ACCOUNT_SOURCE:`${sourceLabel || '所选账户'}：尚未同步资金事实`,
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
  const currentValue = netWorth.accounts?.[source] ?? (source === 'VAULT' ? netWorth.vault : netWorth.venues?.[source]);
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

function capitalHistorySeries(history, alignmentToleranceSeconds = 60, gapToleranceSeconds = 300) {
  const sourceDefinitions = [...new Map(history.map(item => {
    const key = item.source || (item.location_type === 'VAULT' ? 'VAULT' : item.venue);
    return [key, {key, label:item.source_label || (item.location_type === 'VAULT' ? '链上金库' : `${item.venue} · ${item.location_id}`)}];
  })).values()];
  const grouped = new Map(sourceDefinitions.map(source => [source.key, new Map()]));
  history.filter(item => item.usd_equity !== null && item.usd_equity !== undefined).forEach(item => {
    const source = item.source || (item.location_type === 'VAULT' ? 'VAULT' : item.venue);
    const buckets = grouped.get(source);
    const timestamp = new Date(item.observed_at).getTime();
    const value = Number(item.usd_equity);
    if (!buckets || !Number.isFinite(timestamp) || !Number.isFinite(value)) return;
    buckets.set(timestamp, (buckets.get(timestamp) || 0) + value);
  });
  const gapToleranceMs = Math.max(1, Number(gapToleranceSeconds) || 300) * 1000;
  const sourceSeries = sourceDefinitions.map(source => {
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
    source:'TOTAL', label:localizedText('所选账户汇总'),
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
    HYPERLIQUID_WITHDRAWAL_REVALIDATION_REQUIRED:'主账户资金归集已确认；须重新读取实时可提现余额与官方 USDC 路由后再生成提现请求',
    BINANCE_CAPITAL_CREDENTIALS_MISSING:'未加载所选币安账户的已验证资金 API 凭据',
    CAPITAL_ACCOUNT_CREDENTIALS_NOT_READY:'所选币安账户凭据尚未通过连接验证',
    CAPITAL_ACCOUNT_ENVIRONMENT_MISMATCH:'所选币安账户与当前生产环境不一致',
    BINANCE_CAPITAL_TIME_SYNC_FAILED:'币安签名请求前的服务器时间同步失败',
    BINANCE_INTERNAL_TRANSFER_PERMISSION_DISABLED:'币安 Universal Transfer 实际端点拒绝访问，请核对现货/合约划转权限、IP 白名单和账户范围',
    BINANCE_DEPOSIT_PREFLIGHT_REQUIRED:'需要读取并核对币安实时充值地址与网络状态',
    BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_REQUIRED:'需要核对币安 API 权限、IP 限制、白名单、余额、额度和手续费',
    BINANCE_RESTRICTED_WITHDRAWAL_ADAPTER_UNAVAILABLE:'币安受限提现 API 尚未配置',
    SAFE_ADDRESS_MISSING:'未配置 Safe Smart Account',
    SAFE_DELEGATE_ADDRESS_MISSING:'未配置 Safe 委托地址（delegate）',
    SAFE_SPENDING_LIMIT_NOT_CONFIGURED:'Safe 只读 RPC 或 Spending Limit 范围未配置',
    SAFE_GATEWAY_UNAVAILABLE:'Safe Gateway 运行时未就绪',
    SAFE_PREFLIGHT_REJECTED:'Safe 链上实时预检未通过',
    SAFE_RESPONSE_INVALID:'Safe RPC 返回的数据无效',
    SAFE_ALLOWANCE_PREFLIGHT_REQUIRED:'必须读取 Safe 当前额度、余额、重置周期与 nonce',
  }[code] || code);
}

const DIRECT_CAPITAL_PREFLIGHT_BLOCKERS = new Set([
  'SAFE_ALLOWANCE_PREFLIGHT_REQUIRED',
  'BINANCE_DEPOSIT_PREFLIGHT_REQUIRED',
  'BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_REQUIRED',
]);

const ARBITRUM_WALLET_CHAIN = {
  chainId:'0xa4b1',
  chainName:'Arbitrum One',
  nativeCurrency:{name:'Ether', symbol:'ETH', decimals:18},
  rpcUrls:['https://arb1.arbitrum.io/rpc'],
  blockExplorerUrls:['https://arbiscan.io'],
};
let directCapitalWalletActions = new Map();
let directCapitalAssuranceRecords = new Map();
const directCapitalReceiptReconciliations = new Map();
const directCapitalOutboundContinuations = new Map();
let capitalBackgroundRefreshTimer = null;
const BINANCE_RECEIPT_BROWSER_LEASE_MS = 60_000;

function capitalPageHasActiveInteraction() {
  if (document.querySelector('.capital-page dialog[open]')) return true;
  const active = document.activeElement;
  return Boolean(active?.closest?.('.capital-page input, .capital-page select, .capital-page textarea, .capital-page [contenteditable="true"]'));
}

function scheduleCapitalBackgroundRefresh(delayMs = 0) {
  if (capitalBackgroundRefreshTimer) clearTimeout(capitalBackgroundRefreshTimer);
  capitalBackgroundRefreshTimer = setTimeout(async () => {
    capitalBackgroundRefreshTimer = null;
    if (location.pathname !== '/capital') return;
    if (capitalPageHasActiveInteraction()) {
      scheduleCapitalBackgroundRefresh(3000);
      return;
    }
    await route({preserveView:true, backgroundRefresh:true});
  }, Math.max(0, Number(delayMs) || 0));
}

async function refreshCapitalPage() {
  if (location.pathname !== '/capital') return;
  await route({preserveView:true, backgroundRefresh:true});
}

async function withBinanceReceiptBrowserSingleflight(reconciliationKey, action) {
  const lockName = `tradeops:binance-receipt:${reconciliationKey}`;
  const browserLocks = globalThis.navigator?.locks;
  if (browserLocks?.request) {
    return browserLocks.request(lockName, {mode:'exclusive', ifAvailable:true}, lock => (
      lock ? action() : {pending:true, active_reconciliation:true}
    ));
  }
  const storage = globalThis.localStorage;
  if (!storage) return action();
  const owner = crypto.randomUUID();
  const renew = () => storage.setItem(lockName, JSON.stringify({owner, expiresAt:Date.now() + BINANCE_RECEIPT_BROWSER_LEASE_MS}));
  try {
    const active = JSON.parse(storage.getItem(lockName) || 'null');
    if (active?.owner && Number(active.expiresAt) > Date.now()) return {pending:true, active_reconciliation:true};
    renew();
    const claimed = JSON.parse(storage.getItem(lockName) || 'null');
    if (claimed?.owner !== owner) return {pending:true, active_reconciliation:true};
  } catch (_error) {
    return action();
  }
  const heartbeat = setInterval(renew, BINANCE_RECEIPT_BROWSER_LEASE_MS / 2);
  try {
    return await action();
  } finally {
    clearInterval(heartbeat);
    try {
      const active = JSON.parse(storage.getItem(lockName) || 'null');
      if (active?.owner === owner) storage.removeItem(lockName);
    } catch (_error) { /* the server-side lease remains authoritative */ }
  }
}

function directCapitalCurrentPhase(operation) {
  const stages = operation?.stages || [];
  const confirmed = new Set(stages.filter(stage => stage.status === 'CONFIRMED').map(stage => stage.code));
  const submitted = stages.some(stage => String(stage.code || '').endsWith('_SUBMITTED_BY_HUMAN_WALLET')
    || stage.code === 'BINANCE_RESTRICTED_WITHDRAWAL_SUBMITTED');
  if (operation?.status === 'SETTLED') return '资金路径已完成，公开回执已确认';
  if (confirmed.has('HYPERLIQUID_WITHDRAWAL_ARBITRUM_CONFIRMED')) return 'Safe 已到账，正在完成最终状态同步';
  if (confirmed.has('HYPERLIQUID_WITHDRAWAL_LEDGER_CONFIRMED')) return 'Hyperliquid 提现已确认，正在等待 Arbitrum / Safe 到账';
  if (submitted && operation?.path === 'HYPERLIQUID_TO_VAULT') return '钱包提交成功，正在核对 Hyperliquid 账本';
  if (submitted) return '资金操作已提交，正在核对公开回执';
  if (operation?.status === 'BLOCKED') return '安全预检尚未通过，未提交资金操作';
  return '资金操作已创建，正在准备下一步';
}

function directCapitalReceiptHash(operation) {
  const confirmed = [...(operation?.stages || [])].reverse().find(stage =>
    ['HYPERLIQUID_WITHDRAWAL_ARBITRUM_CONFIRMED','TREASURY_DESTINATION_RECEIPT_CONFIRMED','BINANCE_WITHDRAWAL_RECEIPT_CONFIRMED','BINANCE_DEPOSIT_RECEIPT_CONFIRMED'].includes(stage.code));
  return confirmed?.evidence?.transactionHash || confirmed?.evidence?.transaction_hash || '';
}

function renderDirectCapitalLiveProgress(operations) {
  const operation = (operations || [])[0];
  if (!operation) return '<div data-capital-live-progress></div>';
  const path = DIRECT_CAPITAL_PATHS.find(item => item.path === operation.path);
  const label = path ? `${path.from} → ${path.to}` : operation.path;
  const tone = operation.status === 'SETTLED' ? 'is-success' : operation.status === 'BLOCKED' ? 'is-warning' : '';
  const hash = directCapitalReceiptHash(operation);
  const receiptLink = /^0x[0-9a-fA-F]{64}$/.test(hash)
    ? `<a href="https://arbiscan.io/tx/${escapeHtml(hash)}" target="_blank" rel="noopener">查看 Arbitrum 回执</a>`
    : '';
  return `<div class="callout capital-live-progress ${tone}" data-capital-live-progress data-operation-id="${escapeHtml(operation.operation_id)}"><div><small>最近一次资金操作</small><b>${escapeHtml(label)} · ${fmtNumber(operation.amount)} ${escapeHtml(operation.asset)}</b><span>${escapeHtml(directCapitalCurrentPhase(operation))}</span></div><div><span>操作 ${shortId(operation.operation_id)} · ${escapeHtml(fmtDate(operation.updated_at || operation.final_confirmed_at))}</span>${receiptLink}</div></div>`;
}

function setDirectCapitalLiveProgress(message, operationId = '') {
  const host = document.querySelector('[data-capital-live-progress]');
  if (!host) return;
  if (operationId && host.dataset.operationId !== String(operationId)) return;
  const status = host.querySelector('div:first-child span');
  if (status) status.textContent = message;
}

const announcedCapitalWalletProviders = [];
window.addEventListener('eip6963:announceProvider', event => {
  const provider = event?.detail?.provider;
  if (!provider?.request || announcedCapitalWalletProviders.some(item => item.provider === provider)) return;
  announcedCapitalWalletProviders.push({provider, info:event.detail.info || {}});
});
window.dispatchEvent(new Event('eip6963:requestProvider'));

function capitalWalletError(code, message, cause = null) {
  const error = new Error(message);
  error.code = code;
  if (cause) error.cause = cause;
  return error;
}

function capitalWalletProviders() {
  const providers = [];
  const add = (provider, info = {}) => {
    if (provider?.request && !providers.some(item => item.provider === provider)) providers.push({provider, info});
  };
  (Array.isArray(window.ethereum?.providers) ? window.ethereum.providers : []).forEach(provider => add(provider));
  add(window.ethereum);
  announcedCapitalWalletProviders.forEach(item => add(item.provider, item.info));
  return providers;
}

function isWalletCancellation(error) {
  const message = String(error?.message || '').toLowerCase();
  return Number(error?.code) === 4001 || error?.code === 'ACTION_REJECTED'
    || message.includes('user rejected') || message.includes('user denied');
}

function normalizeCapitalWalletError(error) {
  if (error?.code && ['WALLET_', 'HYPERLIQUID_'].some(prefix => String(error.code).startsWith(prefix))) return error;
  const message = String(error?.message || '').toLowerCase();
  if (message.includes('insufficient funds') || message.includes('insufficient balance')) {
    return capitalWalletError('WALLET_GAS_INSUFFICIENT', '当前钱包没有足够的 Arbitrum ETH 支付 Gas。', error);
  }
  if (Number(error?.code) === -32002 || message.includes('already pending')) {
    return capitalWalletError('WALLET_REQUEST_PENDING', '钱包中已有待处理请求，请先在钱包里完成或取消。', error);
  }
  if (Number(error?.code) === 4100 || message.includes('unauthorized')) {
    return capitalWalletError('WALLET_ACCOUNT_ACCESS_DENIED', '钱包尚未授权 TradeOps 读取当前账户。', error);
  }
  return capitalWalletError('WALLET_REQUEST_FAILED', '钱包没有接受本次请求，请打开钱包查看具体提示后重试。', error);
}

async function walletProvider(expectedAddress) {
  const candidates = capitalWalletProviders();
  if (!candidates.length) {
    throw capitalWalletError(
      'WALLET_PROVIDER_NOT_AVAILABLE',
      '当前标签页没有检测到钱包。请允许钱包扩展访问 127.0.0.1，并刷新后重试。',
    );
  }
  if (expectedAddress) {
    for (const candidate of candidates) {
      try {
        const accounts = await candidate.provider.request({method:'eth_accounts'});
        if (String(accounts?.[0] || '').toLowerCase() === String(expectedAddress).toLowerCase()) {
          return candidate.provider;
        }
      } catch (_error) { /* The interactive request below remains authoritative. */ }
    }
  }
  if (candidates.length > 1) {
    throw capitalWalletError(
      'WALLET_PROVIDER_SELECTION_REQUIRED',
      '检测到多个钱包，但没有钱包连接到本次要求的账户。请在目标钱包中连接当前站点后重试。',
    );
  }
  return candidates[0].provider;
}

async function connectedArbitrumWallet(expectedAddress) {
  const provider = await walletProvider(expectedAddress);
  let accounts;
  try {
    accounts = await provider.request({method:'eth_requestAccounts'});
  } catch (error) {
    if (isWalletCancellation(error)) throw error;
    throw normalizeCapitalWalletError(error);
  }
  const account = String(accounts?.[0] || '');
  if (!/^0x[0-9a-fA-F]{40}$/.test(account)) {
    throw capitalWalletError('WALLET_ACCOUNT_INVALID', '钱包没有返回有效的账户地址。');
  }
  if (expectedAddress && account.toLowerCase() !== String(expectedAddress).toLowerCase()) {
    throw capitalWalletError('WALLET_ACCOUNT_MISMATCH', `请在钱包中切换到本次操作要求的账户 ${expectedAddress}。`);
  }
  let currentChain;
  try {
    currentChain = await provider.request({method:'eth_chainId'});
  } catch (error) {
    throw normalizeCapitalWalletError(error);
  }
  if (String(currentChain).toLowerCase() !== ARBITRUM_WALLET_CHAIN.chainId) {
    try {
      await provider.request({method:'wallet_switchEthereumChain', params:[{chainId:ARBITRUM_WALLET_CHAIN.chainId}]});
    } catch (error) {
      if (isWalletCancellation(error)) throw error;
      if (Number(error?.code) !== 4902) throw normalizeCapitalWalletError(error);
      try {
        await provider.request({method:'wallet_addEthereumChain', params:[ARBITRUM_WALLET_CHAIN]});
      } catch (addError) {
        if (isWalletCancellation(addError)) throw addError;
        throw capitalWalletError('WALLET_NETWORK_SWITCH_FAILED', '钱包未能切换到 Arbitrum One。', addError);
      }
    }
  }
  return {provider, account};
}

function walletHexValue(value) {
  const amount = BigInt(String(value ?? '0'));
  return `0x${amount.toString(16)}`;
}

async function sendWalletTransaction(transaction, expectedAddress) {
  const expected = expectedAddress || transaction.from || transaction.sender;
  const {provider, account} = await connectedArbitrumWallet(expected);
  const hash = await provider.request({
    method:'eth_sendTransaction',
    params:[{
      from:account,
      to:transaction.to,
      data:transaction.data || '0x',
      value:walletHexValue(transaction.value),
    }],
  });
  if (!/^0x[0-9a-fA-F]{64}$/.test(String(hash))) throw new Error('钱包没有返回有效的交易哈希。');
  return String(hash);
}

function splitWalletSignature(signature) {
  const value = String(signature || '');
  if (!/^0x[0-9a-fA-F]{130}$/.test(value)) throw new Error('钱包没有返回有效的签名。');
  const rawV = Number.parseInt(value.slice(130, 132), 16);
  return {r:`0x${value.slice(2, 66)}`, s:`0x${value.slice(66, 130)}`, v:rawV < 27 ? rawV + 27 : rawV};
}

function hyperliquidSubmissionReason(body) {
  const candidate = body?.response?.data?.message
    || body?.response?.message
    || body?.response
    || body?.message
    || body?.msg
    || body?.error;
  if (candidate == null) return '官方端点未返回可读原因';
  const text = typeof candidate === 'string' ? candidate : JSON.stringify(candidate);
  return String(text).replace(/\s+/g, ' ').slice(0, 240);
}

async function submitHyperliquidWalletAction(artifact) {
  if (artifact.kind === 'HYPERLIQUID_ARBITRUM_DEPOSIT_UNSIGNED_TRANSACTION') {
    return {transaction_hash:await sendWalletTransaction(artifact, artifact.from)};
  }
  const {provider, account} = await connectedArbitrumWallet(artifact.account);
  let signature;
  try {
    signature = await provider.request({
      method:'eth_signTypedData_v4',
      params:[account, JSON.stringify(artifact.typedData)],
    });
  } catch (error) {
    if (isWalletCancellation(error)) throw error;
    throw normalizeCapitalWalletError(error);
  }
  let response;
  try {
    response = await fetch(artifact.exchangeEndpoint, {
      method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({...artifact.exchangeRequestTemplate, signature:splitWalletSignature(signature)}),
    });
  } catch (error) {
    throw capitalWalletError(
      'HYPERLIQUID_BROWSER_SUBMISSION_UNAVAILABLE',
      '钱包签名已完成，但浏览器未能连接 Hyperliquid 官方提交端点。请先核对 Hyperliquid 记录，确认未提交后再重试。',
      error,
    );
  }
  let body = null;
  try { body = await response.json(); } catch (_error) { body = null; }
  if (!response.ok || body?.status === 'err' || body?.response?.type === 'error') {
    const reason = hyperliquidSubmissionReason(body);
    const rejection = capitalWalletError(
      response.status === 429 ? 'HYPERLIQUID_RATE_LIMITED' : 'HYPERLIQUID_SUBMISSION_REJECTED',
      `Hyperliquid 官方端点拒绝了已签名请求：${reason}（HTTP ${response.status}）。资金记录未标记为已提交。`,
    );
    rejection.details = {http_status:response.status, hyperliquid_response:body};
    throw rejection;
  }
  return {nonce:Number(artifact.nonce), action_hash:body?.response?.data?.hash || body?.data?.hash || undefined};
}

async function recordDirectWalletOutcome({operationId, version, stage, outcome, evidence = {}}) {
  return api(`/api/capital/direct-operations/${operationId}/wallet-submission`, {
    method:'POST',
    body:JSON.stringify({
      ...evidence,
      expected_version:Number(version),
      stage,
      outcome,
      final_confirmed:true,
      idempotency_key:crypto.randomUUID(),
    }),
  });
}

async function executeDirectWalletAction(action) {
  let evidence;
  const submittedHashes = [];
  try {
    if (action.kind === 'HYPERLIQUID') {
      evidence = await submitHyperliquidWalletAction(action.artifact);
    } else {
      const transactions = action.transactions || [action.transaction];
      let transactionHash = null;
      for (const transaction of transactions) {
        transactionHash = await sendWalletTransaction(transaction, action.expectedWallet);
        submittedHashes.push(transactionHash);
      }
      evidence = {transaction_hash:transactionHash};
    }
  } catch (error) {
    if (isWalletCancellation(error)) {
      if (!submittedHashes.length) {
        await recordDirectWalletOutcome({...action, outcome:'CANCELLED'});
        throw capitalWalletError('WALLET_CONFIRMATION_CANCELLED', '已取消钱包确认，资金未发送。');
      }
      throw capitalWalletError('WALLET_CONFIRMATION_PARTIALLY_CANCELLED', `已取消后续钱包确认；前序交易 ${submittedHashes.at(-1)} 已提交，流程保持暂停并等待公开回执。`);
    }
    throw normalizeCapitalWalletError(error);
  }
  const recorded = await recordDirectWalletOutcome({...action, outcome:'SUBMITTED', evidence});
  return {recorded, evidence};
}

function walletActionFromTreasuryPreview({operationId, path, preview}) {
  const stage = path.startsWith('VAULT_TO_') ? 'TREASURY_WITHDRAWAL' : 'TREASURY_DEPOSIT';
  if (preview.artifact) {
    return {
      operationId, version:preview.version, stage:'NOTILT_DESTINATION_TRANSFER',
      kind:'TRANSACTION', transaction:preview.artifact,
      expectedWallet:preview.artifact.from,
    };
  }
  if (preview.signature_request) {
    return {
      operationId, version:preview.version, stage, kind:'TRANSACTION',
      transaction:preview.signature_request,
      expectedWallet:preview.signature_request.from || preview.signature_request.sender,
    };
  }
  return {
    operationId, version:preview.version, stage, kind:'TRANSACTIONS',
    transactions:preview.transactions,
    expectedWallet:preview.wallet_address,
  };
}

function walletActionFromHyperliquidPreview({operationId, preview}) {
  const artifact = preview.artifact;
  return {
    operationId,
    version:preview.version,
    kind:'HYPERLIQUID',
    artifact,
    stage:artifact.kind === 'HYPERLIQUID_ARBITRUM_DEPOSIT_UNSIGNED_TRANSACTION'
      ? 'HYPERLIQUID_DEPOSIT'
      : artifact.kind === 'HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST'
        ? 'HYPERLIQUID_CLASS_TRANSFER'
        : 'HYPERLIQUID_WITHDRAWAL',
  };
}

function assertDirectWalletExecutionReady(preview, operationId, stage) {
  const operation = (preview.data?.direct_operations || []).find(item => item.operation_id === operationId);
  if (preview.data?.real_transfer_gate !== 'ENABLED') {
    throw new Error('CAPITAL_TRANSFER 当前未启用，本次钱包请求保持阻断。');
  }
  const allowed = new Set([
    'HYPERLIQUID_HUMAN_WALLET_CONFIRMATION_REQUIRED',
    // A prior wallet cancellation proves that nothing was submitted.  A fresh,
    // explicit click must be able to reopen the exact same unsigned request.
    'HUMAN_WALLET_CONFIRMATION_CANCELLED',
  ]);
  if (stage === 'TREASURY_DEPOSIT') allowed.add('TREASURY_DESTINATION_RECEIPT_REQUIRED');
  const blockers = (operation?.blockers || preview.blockers || []).filter(code => !allowed.has(code));
  if (blockers.length) {
    const error = new Error(`当前仍有 ${blockers.length} 项安全阻断：${blockers.map(formatDirectCapitalBlocker).join('；')}`);
    error.code = blockers[0];
    throw error;
  }
}

async function prepareTreasuryWalletAction({operationId, version, path, treasuryProvider}) {
  const suffix = treasuryProvider === 'SAFE_SPENDING_LIMIT'
    ? 'safe-spending-preview'
    : 'notilt-unsigned-preview';
  const preview = await api(`/api/capital/direct-operations/${operationId}/${suffix}`, {
    method:'POST',
    timeoutMs:treasuryProvider === 'SAFE_SPENDING_LIMIT' ? 45_000 : REQUEST_TIMEOUT_MS,
    body:JSON.stringify({expected_version:Number(version), final_confirmed:true, idempotency_key:crypto.randomUUID()}),
  });
  const action = walletActionFromTreasuryPreview({operationId, path, preview});
  assertDirectWalletExecutionReady(preview, operationId, action.stage);
  return action;
}

async function prepareNoTiltReleaseExecution({operationId, version}) {
  const preview = await api(`/api/capital/direct-operations/${operationId}/notilt-release-execution-preview`, {
    method:'POST',
    body:JSON.stringify({expected_version:Number(version), final_confirmed:true, idempotency_key:crypto.randomUUID()}),
  });
  const action = {
    operationId,
    version:preview.version,
    stage:'NOTILT_RELEASE_EXECUTION',
    kind:'TRANSACTIONS',
    transactions:preview.transactions,
    expectedWallet:preview.wallet_address,
  };
  assertDirectWalletExecutionReady(preview, operationId, action.stage);
  return action;
}

async function prepareNoTiltDestinationTransfer({operationId, version}) {
  const preview = await api(`/api/capital/direct-operations/${operationId}/notilt-destination-preview`, {
    method:'POST',
    body:JSON.stringify({expected_version:Number(version), final_confirmed:true, idempotency_key:crypto.randomUUID()}),
  });
  const action = walletActionFromTreasuryPreview({operationId, path:'VAULT_TO_BINANCE', preview});
  assertDirectWalletExecutionReady(preview, operationId, action.stage);
  return action;
}

async function prepareHyperliquidWalletAction({operationId, version}) {
  const preview = await api(`/api/capital/direct-operations/${operationId}/hyperliquid-preview`, {
    method:'POST',
    timeoutMs:45_000,
    body:JSON.stringify({expected_version:Number(version), final_confirmed:true, idempotency_key:crypto.randomUUID()}),
  });
  const action = walletActionFromHyperliquidPreview({operationId, preview});
  assertDirectWalletExecutionReady(preview, operationId, action.stage);
  return action;
}

async function prepareAndSubmitBinanceWithdrawal({operationId, version}) {
  const preview = await api(`/api/capital/direct-operations/${operationId}/binance-preview`, {
    method:'POST',
    timeoutMs:60_000,
    body:JSON.stringify({expected_version:Number(version), final_confirmed:true, idempotency_key:crypto.randomUUID()}),
  });
  return api(`/api/capital/direct-operations/${operationId}/binance-submit`, {
    method:'POST',
    body:JSON.stringify({
      expected_version:Number(preview.version),
      final_confirmed:true,
      confirmation_phrase:'CONFIRM_BINANCE_WITHDRAWAL',
      idempotency_key:crypto.randomUUID(),
    }),
  });
}

const PUBLIC_RECEIPT_PENDING_CODES = new Set([
  'ARBITRUM_RECEIPT_NOT_CONFIRMED',
  'ARBITRUM_RECEIPT_UNAVAILABLE',
  'HYPERLIQUID_RECEIPT_NOT_CONFIRMED',
  'BINANCE_CAPITAL_RECEIPT_NOT_FOUND',
  'BINANCE_CAPITAL_WITHDRAWAL_PENDING',
  'BINANCE_CAPITAL_DEPOSIT_PENDING',
  'BINANCE_INTERNAL_TRANSFER_PENDING',
  'BINANCE_CAPITAL_RATE_LIMITED',
  'BINANCE_CAPITAL_WEIGHT_HEADROOM_DEFERRED',
  'BINANCE_RECEIPT_CHECK_IN_PROGRESS',
]);

const waitForPublicReceipt = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function retryDirectPublicReceipt(request, {attempts = 20, delayMs = 3000, delaysMs = null} = {}) {
  let lastPendingError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await request();
    } catch (error) {
      if (!PUBLIC_RECEIPT_PENDING_CODES.has(error?.code)) throw error;
      lastPendingError = error;
      if (error?.code === 'BINANCE_RECEIPT_CHECK_IN_PROGRESS') break;
      if (attempt + 1 < attempts) {
        const nextRetryAt = Date.parse(error?.details?.next_retry_at || '');
        const retryAfterMs = Number(error?.details?.retry_after_seconds || 0) * 1000;
        const serverDelayMs = Number.isFinite(nextRetryAt) ? Math.max(0, nextRetryAt - Date.now()) : 0;
        const scheduledDelayMs = delaysMs?.[Math.min(attempt, delaysMs.length - 1)] ?? delayMs;
        await waitForPublicReceipt(Math.max(scheduledDelayMs, retryAfterMs, serverDelayMs));
      }
    }
  }
  return {pending:true, error:lastPendingError};
}

async function verifyTreasuryWithdrawalReceipt({operationId, version, transactionHash}) {
  return retryDirectPublicReceipt(() => api(`/api/capital/direct-operations/${operationId}/treasury-withdrawal-receipt`, {
    method:'POST',
    timeoutMs:60_000,
    body:JSON.stringify({
      expected_version:Number(version),
      transaction_hash:transactionHash,
      idempotency_key:crypto.randomUUID(),
    }),
  }), {attempts:200, delayMs:3000});
}

async function verifyHyperliquidDepositReceipts({operationId, version, transactionHash}) {
  const chain = await retryDirectPublicReceipt(() => api(`/api/capital/direct-operations/${operationId}/hyperliquid-receipt`, {
    method:'POST',
    timeoutMs:30_000,
    body:JSON.stringify({
      expected_version:Number(version),
      stage:'HYPERLIQUID_DEPOSIT_ARBITRUM',
      transaction_hash:transactionHash,
      idempotency_key:crypto.randomUUID(),
    }),
  }));
  if (chain.pending) return chain;
  return retryDirectPublicReceipt(() => api(`/api/capital/direct-operations/${operationId}/hyperliquid-receipt`, {
    method:'POST',
    timeoutMs:30_000,
    body:JSON.stringify({
      expected_version:Number(chain.version),
      stage:'HYPERLIQUID_DEPOSIT_LEDGER',
      action_hash:transactionHash,
      idempotency_key:crypto.randomUUID(),
    }),
  }), {attempts:24, delayMs:5000});
}

async function verifyRecordedHyperliquidDeposit(operation) {
  const stages = operation.stages || [];
  const submission = [...stages].reverse().find(stage => stage.code === 'HYPERLIQUID_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET');
  if (!submission?.transaction_hash) return {pending:true};
  const chainConfirmed = stages.some(stage => stage.code === 'HYPERLIQUID_DEPOSIT_ARBITRUM_CONFIRMED');
  const ledgerConfirmed = stages.some(stage => stage.code === 'HYPERLIQUID_DEPOSIT_LEDGER_CONFIRMED');
  if (chainConfirmed && ledgerConfirmed) return {pending:false, version:Number(operation.version)};
  if (!chainConfirmed) {
    return verifyHyperliquidDepositReceipts({
      operationId:operation.operation_id,
      version:Number(operation.version),
      transactionHash:submission.transaction_hash,
    });
  }
  return retryDirectPublicReceipt(() => api(`/api/capital/direct-operations/${operation.operation_id}/hyperliquid-receipt`, {
    method:'POST',
    timeoutMs:30_000,
    body:JSON.stringify({
      expected_version:Number(operation.version),
      stage:'HYPERLIQUID_DEPOSIT_LEDGER',
      action_hash:submission.transaction_hash,
      idempotency_key:crypto.randomUUID(),
    }),
  }), {attempts:24, delayMs:5000});
}

async function verifyHyperliquidWithdrawalReceipts({operationId, version, actionHash, nonce}) {
  setDirectCapitalLiveProgress('钱包提交成功，正在核对 Hyperliquid 账本', operationId);
  const ledger = await retryDirectPublicReceipt(() => api(`/api/capital/direct-operations/${operationId}/hyperliquid-receipt`, {
    method:'POST',
    timeoutMs:30_000,
    body:JSON.stringify({
      expected_version:Number(version),
      stage:'HYPERLIQUID_WITHDRAWAL_LEDGER',
      action_hash:actionHash || undefined,
      nonce:Number(nonce),
      idempotency_key:crypto.randomUUID(),
    }),
  }), {attempts:30, delayMs:2000});
  if (ledger.pending) return ledger;
  setDirectCapitalLiveProgress('Hyperliquid 提现已确认，正在等待 Arbitrum / Safe 到账', operationId);
  const chain = await retryDirectPublicReceipt(() => api(`/api/capital/direct-operations/${operationId}/hyperliquid-receipt`, {
    method:'POST',
    timeoutMs:30_000,
    body:JSON.stringify({
      expected_version:Number(ledger.version),
      stage:'HYPERLIQUID_WITHDRAWAL_ARBITRUM',
      idempotency_key:crypto.randomUUID(),
    }),
  }), {attempts:80, delayMs:5000});
  if (!chain.pending) setDirectCapitalLiveProgress('Safe 已到账，公开回执已确认', operationId);
  return chain;
}

function reconcilePendingHyperliquidWithdrawals(operations) {
  (operations || []).filter(operation => (
    operation.path === 'HYPERLIQUID_TO_VAULT'
    && operation.status === 'AWAITING_RECEIPT'
  )).slice(0, 3).forEach(operation => {
    if (directCapitalReceiptReconciliations.has(operation.operation_id)) return;
    const stages = operation.stages || [];
    const submission = [...stages].reverse().find(stage => stage.code === 'HYPERLIQUID_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET');
    const settled = stages.some(stage => stage.code === 'HYPERLIQUID_WITHDRAWAL_ARBITRUM_CONFIRMED');
    if (!submission?.nonce || settled) return;
    const job = verifyHyperliquidWithdrawalReceipts({
      operationId:operation.operation_id,
      version:Number(operation.version),
      actionHash:submission.action_hash,
      nonce:Number(submission.nonce),
    }).then(async receipt => {
      if (receipt.pending) {
        setDirectCapitalLiveProgress('公开回执仍在确认；页面会保持当前操作，稍后自动继续核对', operation.operation_id);
        scheduleCapitalBackgroundRefresh(15_000);
        return;
      }
      showToast('Safe 已到账，Hyperliquid 与 Arbitrum 回执均已确认');
      await refreshCapitalPage();
    }).catch(async error => {
      if (error?.code === 'VERSION_CONFLICT') scheduleCapitalBackgroundRefresh(0);
      else {
        setDirectCapitalLiveProgress(`Hyperliquid 提现已提交，回执核对暂缓（${error?.code || 'RECEIPT_UNAVAILABLE'}）；系统不会重复发送`, operation.operation_id);
        scheduleCapitalBackgroundRefresh(15_000);
      }
    }).finally(() => directCapitalReceiptReconciliations.delete(operation.operation_id));
    directCapitalReceiptReconciliations.set(operation.operation_id, job);
  });
}

function reconcilePendingHyperliquidDeposits(operations) {
  (operations || []).filter(operation => (
    operation.path === 'VAULT_TO_HYPERLIQUID'
    && operation.status === 'AWAITING_RECEIPT'
    && (operation.stages || []).some(stage => stage.code === 'HYPERLIQUID_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET')
  )).slice(0, 3).forEach(operation => {
    const reconciliationKey = `${operation.operation_id}:HYPERLIQUID_DEPOSIT`;
    if (directCapitalReceiptReconciliations.has(reconciliationKey)) return;
    const job = verifyRecordedHyperliquidDeposit(operation).then(async receipt => {
      if (receipt.pending) {
        setDirectCapitalLiveProgress('Hyperliquid 入金交易已提交；链上或交易所账本仍在确认，系统不会重复发送', operation.operation_id);
        scheduleCapitalBackgroundRefresh(15_000);
        return;
      }
      showToast('Hyperliquid 入金已确认，Arbitrum 与交易所账本回执一致');
      await refreshCapitalPage();
    }).catch(async error => {
      if (error?.code === 'VERSION_CONFLICT') scheduleCapitalBackgroundRefresh(0);
      else {
        setDirectCapitalLiveProgress(`Hyperliquid 入金已提交，回执核对暂缓（${error?.code || 'RECEIPT_UNAVAILABLE'}）；不会重复发送`, operation.operation_id);
        scheduleCapitalBackgroundRefresh(15_000);
      }
    }).finally(() => directCapitalReceiptReconciliations.delete(reconciliationKey));
    directCapitalReceiptReconciliations.set(reconciliationKey, job);
  });
}

async function verifyBinanceReceipt({operationId, version, stage, transactionHash}) {
  const idempotencyKey = crypto.randomUUID();
  return withBinanceReceiptBrowserSingleflight(`${operationId}:${stage}`, () => (
    retryDirectPublicReceipt(() => api(`/api/capital/direct-operations/${operationId}/binance-receipt`, {
      method:'POST',
      timeoutMs:60_000,
      body:JSON.stringify({
        expected_version:Number(version),
        stage,
        transaction_hash:transactionHash || undefined,
        idempotency_key:idempotencyKey,
      }),
    }), {attempts:6, delaysMs:[3000, 5000, 10000, 20000, 30000]})
  ));
}

function reconcilePendingBinanceReceipts(operations) {
  (operations || []).filter(operation => (
    ['VAULT_TO_BINANCE', 'BINANCE_TO_VAULT'].includes(operation.path)
    && operation.status === 'AWAITING_RECEIPT'
  )).slice(0, 1).forEach(operation => {
    const stages = operation.stages || [];
    const receiptStage = operation.path === 'VAULT_TO_BINANCE' ? 'BINANCE_DEPOSIT' : 'BINANCE_WITHDRAWAL';
    const reconciliationKey = `${operation.operation_id}:${receiptStage}`;
    if (directCapitalReceiptReconciliations.has(reconciliationKey)) return;
    const confirmed = stages.some(stage => stage.code === `${receiptStage}_RECEIPT_CONFIRMED`);
    const sourceSubmission = operation.path === 'VAULT_TO_BINANCE'
      ? [...stages].reverse().find(stage => ['NOTILT_DESTINATION_TRANSFER_SUBMITTED_BY_HUMAN_WALLET', 'TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET'].includes(stage.code))
      : [...stages].reverse().find(stage => stage.code === 'BINANCE_RESTRICTED_WITHDRAWAL_SUBMITTED');
    if (confirmed || !sourceSubmission) return;
    const transactionHash = sourceSubmission.transaction_hash || sourceSubmission.submission?.transactionHash || '';
    const job = verifyBinanceReceipt({
      operationId:operation.operation_id,
      version:Number(operation.version),
      stage:receiptStage,
      transactionHash,
    }).then(async receipt => {
      if (receipt.pending) {
        setDirectCapitalLiveProgress('币安回执仍在确认；刷新页面后会从当前阶段继续，不会并行重复核对', operation.operation_id);
        return;
      }
      showToast('币安与链上公开回执已核对');
      await refreshCapitalPage();
    }).catch(async error => {
      if (error?.code === 'VERSION_CONFLICT') scheduleCapitalBackgroundRefresh(0);
      else {
        setDirectCapitalLiveProgress(`币安资金已提交，公开回执核对暂缓（${error?.code || 'RECEIPT_UNAVAILABLE'}）；系统不会重复提交`, operation.operation_id);
        scheduleCapitalBackgroundRefresh(30_000);
      }
    }).finally(() => directCapitalReceiptReconciliations.delete(reconciliationKey));
    directCapitalReceiptReconciliations.set(reconciliationKey, job);
  });
}

async function continueSafeOutboundExecution({operationId, version, path, transactionHash}) {
  setDirectCapitalLiveProgress(path === 'VAULT_TO_HYPERLIQUID'
    ? '第一笔钱包交易已提交，正在等待授权地址到账；到账后会自动打开 Hyperliquid 入金确认'
    : '链上金库转出已提交，正在等待公开回执', operationId);
  const treasuryReceipt = await verifyTreasuryWithdrawalReceipt({
    operationId, version, transactionHash,
  });
  if (treasuryReceipt.pending) {
    setDirectCapitalLiveProgress('链上回执仍在确认，页面会保留操作并自动续接；请保持当前页面打开', operationId);
    return {kind:'WALLET', result:treasuryReceipt, receiptPending:Boolean(treasuryReceipt.pending)};
  }
  if (path === 'VAULT_TO_BINANCE') {
    const binanceReceipt = await verifyBinanceReceipt({
      operationId,
      version:treasuryReceipt.version,
      stage:'BINANCE_DEPOSIT',
      transactionHash,
    });
    return {kind:'BINANCE', result:binanceReceipt, receiptPending:Boolean(binanceReceipt.pending)};
  }
  setDirectCapitalLiveProgress('授权地址已到账，正在自动准备 Hyperliquid 入金钱包确认', operationId);
  const hyperliquidAction = await prepareHyperliquidWalletAction({
    operationId, version:treasuryReceipt.version,
  });
  showToast('授权地址已到账，正在打开 Hyperliquid 入金钱包确认');
  const hyperliquidSubmission = await executeDirectWalletAction(hyperliquidAction);
  setDirectCapitalLiveProgress('Hyperliquid 入金已由钱包提交，正在核对 Arbitrum 与交易所账本', operationId);
  let hyperliquidReceipt;
  try {
    hyperliquidReceipt = await verifyHyperliquidDepositReceipts({
      operationId,
      version:hyperliquidSubmission.recorded.version,
      transactionHash:hyperliquidSubmission.evidence.transaction_hash,
    });
  } catch (error) {
    // The wallet transaction is already public at this point. A transient or
    // concurrent receipt read must never be presented as a failed transfer or
    // trigger a duplicate submission; the reconciler resumes from the frozen hash.
    setDirectCapitalLiveProgress(`Hyperliquid 入金已提交，回执核对暂缓（${error?.code || 'RECEIPT_UNAVAILABLE'}）；系统不会重复发送`, operationId);
    hyperliquidReceipt = {pending:true, error};
  }
  return {
    kind:'WALLET',
    result:hyperliquidSubmission,
    receiptPending:Boolean(hyperliquidReceipt.pending),
  };
}

function reconcilePendingSafeHyperliquidDeposits(operations) {
  (operations || []).filter(operation => (
    operation.path === 'VAULT_TO_HYPERLIQUID'
    && operation.treasury_provider === 'SAFE_SPENDING_LIMIT'
    && operation.status !== 'SETTLED'
  )).slice(0, 1).forEach(operation => {
    if (directCapitalOutboundContinuations.has(operation.operation_id)) return;
    const stages = operation.stages || [];
    const sourceSubmission = [...stages].reverse().find(stage => stage.code === 'TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET');
    const targetSubmission = stages.some(stage => stage.code === 'HYPERLIQUID_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET');
    const walletCancelled = stages.some(stage => stage.code?.endsWith('_WALLET_CANCELLED'));
    if (!sourceSubmission?.transaction_hash || targetSubmission || walletCancelled) return;
    const job = continueSafeOutboundExecution({
      operationId:operation.operation_id,
      version:Number(operation.version),
      path:operation.path,
      transactionHash:sourceSubmission.transaction_hash,
    }).then(async result => {
      showToast(result.receiptPending
        ? '链上回执仍在确认，系统会继续自动核对'
        : 'Hyperliquid 入金已提交并进入公开回执确认');
      await refreshCapitalPage();
    }).catch(async error => {
      if (error?.code === 'VERSION_CONFLICT') scheduleCapitalBackgroundRefresh(0);
      else if (error?.code === 'WALLET_REJECTED') showApiError(error);
      else {
        setDirectCapitalLiveProgress(`链上转出已提交，Hyperliquid 入金续接暂缓（${error?.code || 'RECEIPT_UNAVAILABLE'}）；系统不会重复发送`, operation.operation_id);
        scheduleCapitalBackgroundRefresh(15_000);
      }
    }).finally(() => directCapitalOutboundContinuations.delete(operation.operation_id));
    directCapitalOutboundContinuations.set(operation.operation_id, job);
  });
}

async function startDirectCapitalExecution({operationId, version, path, treasuryProvider}) {
  if (path === 'BINANCE_TO_VAULT') {
    const submitted = await prepareAndSubmitBinanceWithdrawal({operationId, version});
    if (submitted.pending) {
      return {kind:'BINANCE', result:submitted, receiptPending:true};
    }
    const receipt = await verifyBinanceReceipt({
      operationId,
      version:submitted.version,
      stage:'BINANCE_WITHDRAWAL',
    });
    return {kind:'BINANCE', result:submitted, receiptPending:Boolean(receipt.pending)};
  }
  if (path === 'HYPERLIQUID_TO_VAULT') {
    const action = await prepareHyperliquidWalletAction({operationId, version});
    const submitted = await executeDirectWalletAction(action);
    setDirectCapitalLiveProgress('钱包提交成功，正在核对 Hyperliquid 账本', operationId);
    return {kind:'WALLET', result:submitted, receiptPending:true};
  }
  let currentVersion = Number(version);
  if (path === 'VAULT_TO_BINANCE') {
    const binancePreview = await api(`/api/capital/direct-operations/${operationId}/binance-preview`, {
      method:'POST', timeoutMs:60_000,
      body:JSON.stringify({expected_version:currentVersion, final_confirmed:true, idempotency_key:crypto.randomUUID()}),
    });
    currentVersion = Number(binancePreview.version);
  }
  const action = await prepareTreasuryWalletAction({
    operationId, version:currentVersion, path, treasuryProvider,
  });
  const treasurySubmission = await executeDirectWalletAction(action);
  if (path === 'VAULT_TO_HYPERLIQUID') {
    setDirectCapitalLiveProgress('第一笔钱包交易已提交，正在等待授权地址到账；无需再次点击页面按钮', operationId);
  }
  if (treasuryProvider !== 'SAFE_SPENDING_LIMIT') {
    return {kind:'WALLET', result:treasurySubmission};
  }
  if (path === 'VAULT_TO_HYPERLIQUID') {
    return {kind:'WALLET', result:treasurySubmission, receiptPending:true, continuationScheduled:true};
  }
  return continueSafeOutboundExecution({
    operationId,
    version:treasurySubmission.recorded.version,
    path,
    transactionHash:treasurySubmission.evidence.transaction_hash,
  });
}

function directCapitalExecutionSuccessMessage(execution) {
  if (execution?.continuationScheduled) return '第一笔链上交易已提交；到账后会自动打开 Hyperliquid 入金钱包确认';
  return execution?.kind === 'BINANCE'
    ? '币安提现已直接提交，正在等待公开回执'
    : '钱包已提交，公开交易信息已自动记录';
}

function formatDirectCapitalStage(code) {
  return ({
    VAULT_RELEASE_REQUEST:'申请资金库释放', WAIT_10_MINUTES:'等待 10 分钟',
    REVALIDATE_RELEASE:'到期重新校验', TRANSFER_TO_AUTHORIZED_BINANCE_ADDRESS:'转入已授权币安地址',
    VAULT_RELEASE_TO_AUTHORIZED_OWNED_ADDRESS:'释放至已授权自有地址',
    DEPOSIT_TO_HYPERLIQUID_CONTRACT:'存入 Hyperliquid 合约',
    WITHDRAW_FROM_HYPERLIQUID_CONTRACT:'从 Hyperliquid 合约提回',
    WITHDRAW_DIRECTLY_TO_SAFE:'Hyperliquid 直接提回 Safe',
    RECEIVE_AT_AUTHORIZED_OWNED_ADDRESS:'到达已授权自有地址',
    PREPARE_NOTILT_SDK_DEPOSIT:'由 NoTilt 官方 SDK 构建最低必要无签名入金序列',
    HUMAN_WALLET_CONFIRMATION:'独立人控钱包逐笔核对与确认',
    VERIFY_NOTILT_DEPOSIT_RECEIPT:'校验链、目标、方法、金额与回执',
    RESTRICTED_BINANCE_WITHDRAWAL:'调用受限币安提现 API',
    RESTRICTED_BINANCE_WITHDRAWAL_TO_AUTHORIZED_OWNED_ADDRESS:'币安受限提现至已授权自有地址',
    RESTRICTED_BINANCE_WITHDRAWAL_TO_SELECTED_TREASURY:'币安受限提现至当前链上金库',
    VERIFY_BINANCE_WITHDRAWAL_RECEIPT:'校验币安提现状态与交易哈希',
    TRANSFER_BINANCE_USDM_TO_SPOT:'币安合约账户划转至现货账户',
    TRANSFER_BINANCE_SPOT_TO_USDM:'币安现货入账后划转至合约账户',
    VERIFY_SELECTED_TREASURY_CREDIT:'校验当前链上金库到账',
    NOTILT_UNSIGNED_RELEASE_REQUEST_PREVIEW:'NoTilt 释放请求无签名预检',
    NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED:'NoTilt 释放请求回执已验证',
    NOTILT_UNSIGNED_RELEASE_EXECUTION_PREVIEW:'NoTilt 释放执行请求已准备',
    NOTILT_RELEASE_EXECUTION_SUBMITTED_BY_HUMAN_WALLET:'NoTilt 释放执行已由钱包提交',
    NOTILT_RELEASE_EXECUTION_RECEIPT_CONFIRMED:'NoTilt 释放执行回执已验证',
    NOTILT_DESTINATION_TRANSFER_PREVIEW:'NoTilt 释放资金的目标转账已准备',
    NOTILT_DESTINATION_TRANSFER_SUBMITTED_BY_HUMAN_WALLET:'NoTilt 释放资金已由钱包转向目标',
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
    HYPERLIQUID_CCTP_WITHDRAWAL_WALLET_REQUEST_READY:'Hyperliquid CCTP 提现请求已准备，固定协议费 0.2 USDC，等待主钱包或多签',
    HYPERLIQUID_CLASS_TRANSFER_WALLET_REQUEST_READY:'Hyperliquid 主账户资金归集请求已准备，等待主钱包或多签',
    HYPERLIQUID_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET:'Hyperliquid 入金已由人控钱包提交',
    HYPERLIQUID_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET:'Hyperliquid 提现已由人控钱包提交',
    HYPERLIQUID_CLASS_TRANSFER_SUBMITTED_BY_HUMAN_WALLET:'Hyperliquid 主账户资金归集已由人控钱包提交',
    TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET:'链上金库转出已由人控钱包提交',
    TREASURY_WITHDRAWAL_RECEIPT_CONFIRMED:'链上金库转出到账已验证',
    TREASURY_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET:'链上金库入金已由人控钱包提交',
    HYPERLIQUID_DEPOSIT_ARBITRUM_CONFIRMED:'Arbitrum 入金交易已验证',
    HYPERLIQUID_DEPOSIT_LEDGER_CONFIRMED:'Hyperliquid 入金账本已验证',
    HYPERLIQUID_WITHDRAWAL_LEDGER_CONFIRMED:'Hyperliquid 提现账本已验证',
    HYPERLIQUID_WITHDRAWAL_ARBITRUM_CONFIRMED:'Arbitrum 提现到账已验证',
    HYPERLIQUID_CLASS_TRANSFER_LEDGER_CONFIRMED:'Hyperliquid 主账户资金归集账本已验证',
    TREASURY_DESTINATION_RECEIPT_CONFIRMED:'链上金库最终到账已验证',
  }[code] || code);
}

async function renderCapitalPerformancePanel() {
  const host = document.querySelector('[data-capital-performance]');
  if (!host) return;
  const displayEnvironment = currentWorkflowEnvironment();
  if (!['TESTNET','LIVE'].includes(displayEnvironment)) {
    host.innerHTML = '<div class="callout"><b>团队尚未选择运行模式</b><p>选择测试或生产模式后，这里会按当前环境加载账户净值曲线。</p></div>';
    return;
  }
  const searchParams = new URLSearchParams(location.search);
  const selectionStorageKey = `tradingops.performance.capital.accounts.${session?.active_team?.team_id || 'active'}.${displayEnvironment}`;
  let requestedAccounts = searchParams.getAll('capital_account');
  if (!requestedAccounts.length) {
    try { requestedAccounts = JSON.parse(localStorage.getItem(selectionStorageKey) || '[]'); } catch (_error) { requestedAccounts = []; }
  }
  const accountQuery = requestedAccounts.length ? `&accounts=${encodeURIComponent(requestedAccounts.join(','))}` : '';
  let result;
  try {
    result = await api(`/api/capital?environment=${displayEnvironment}${accountQuery}`);
  } catch (error) {
    host.innerHTML = `<div class="callout tone-attention"><b>资金曲线暂未就绪</b><p>${escapeHtml(friendlyApiError(error))}</p><button class="secondary" type="button" data-retry-capital-performance>重新加载</button></div>`;
    host.querySelector('[data-retry-capital-performance]')?.addEventListener('click', renderCapitalPerformancePanel);
    return;
  }
  const item = result.data;
  const accountOptions = item.account_options || [];
  capitalSeriesColorIndex = Object.fromEntries(accountOptions.map((option, index) => [option.key, index % 6]));
  capitalSeriesColorIndex.TOTAL = 6;
  const selectedAccountKeys = new Set(item.selected_account_keys || []);
  selectedAccountKeys.forEach(key => { if (capitalTrendVisibility[key] === undefined) capitalTrendVisibility[key] = true; });
  const netWorth = item.net_worth || {currency:'USD', venues:{}, vault:null, total:null, complete:false, issues:[]};
  const directConfiguration = item.direct_configuration || {};
  const selectedTreasuryProvider = directConfiguration.treasury_provider || 'NOTILT_VAULT';
  const selectedOnchainSeriesLabel = `链上金库 · ${selectedTreasuryProvider === 'SAFE_SPENDING_LIMIT' ? 'Safe' : 'Vault'}`;
  const allHistorySeries = capitalHistorySeries(item.history || [], netWorth.alignment_tolerance_seconds || 60, netWorth.history_gap_tolerance_seconds || 300);
  const historySelection = capitalHistoryRange(item.history || [], capitalChartRangeValue);
  const historySeries = capitalHistorySeries(historySelection.history, netWorth.alignment_tolerance_seconds || 60, netWorth.history_gap_tolerance_seconds || 300);
  [...allHistorySeries, ...historySeries].forEach(series => { if (series.source === 'VAULT') series.label = selectedOnchainSeriesLabel; });
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
    return `<label class="capital-trend-toggle series-color-${capitalSeriesColorIndex[series.source] ?? 0} ${latestPoint ? '' : 'is-missing'}"><input type="checkbox" data-capital-trend="${escapeHtml(series.source)}" ${latestPoint && capitalTrendVisibility[series.source] ? 'checked' : ''} ${latestPoint ? '' : 'disabled'}><i aria-hidden="true"></i><span><b translate="no">${escapeHtml(series.label)}</b><small>${escapeHtml(summary)}</small></span></label>`;
  }).join('');
  const totalSeries = allHistorySeries.find(series => series.source === 'TOTAL');
  const latestCompleteTotal = totalSeries?.points.at(-1) || null;
  const totalHeadline = netWorth.total !== null && netWorth.total !== undefined ? formatCapitalUsd(netWorth.total) : '当前不可汇总';
  const totalSupporting = netWorth.complete
    ? `所选账户同一时间口径 · ${fmtDate(netWorth.as_of)}`
    : latestCompleteTotal
      ? `最近完整汇总 ${formatCapitalUsd(latestCompleteTotal.value)} · ${fmtDate(latestCompleteTotal.time)}`
      : '尚无所选账户同一时间口径的完整记录';
  const sourceCards = accountOptions.filter(option => selectedAccountKeys.has(option.key)).map(option => ({
    source:option.key,
    label:option.location_type === 'VAULT' ? option.label : `${fmtVenueLabel(option.venue)} · ${option.label}`,
  })).map(source => {
    const sourceSeries = historySeries.find(series => series.source === source.source);
    const latestPoint = sourceSeries?.points.at(-1) || allHistorySeries.find(series => series.source === source.source)?.points.at(-1);
    const presentation = capitalSourcePresentation(netWorth, source.source, latestPoint);
    const cardState = presentation.aligned ? (presentation.nearExpiry ? 'is-aging' : 'is-current') : 'is-limited';
    return `<article class="capital-worth-card ${cardState}"><div><small translate="no">${escapeHtml(source.label)}</small><b>${formatCapitalUsd(presentation.value)}</b></div><span>${escapeHtml(presentation.state)}</span><p>${presentation.observedAt ? `${escapeHtml(presentation.freshness)} · ${fmtDate(presentation.observedAt)}` : '尚无有效时间'}</p></article>`;
  }).join('');
  const issueDetails = [...new Set((netWorth.issues || []).map(formatCapitalIssue))];
  const trustCopy = netWorth.complete
    ? '所选账户数据完整、新鲜且时间对齐，可以计算当前汇总。'
    : `${issueDetails.join('；') || '资金数据尚未完整'}。当前汇总保持关闭，可信的单账户曲线仍保留。`;
  const accountFilters = accountOptions.map((option, index) => {
    const accountId = String(option.account_id || '');
    const displayAccountId = accountId.length > 18 ? `${accountId.slice(0, 10)}…${accountId.slice(-6)}` : accountId;
    const scopeLabel = option.location_type === 'VAULT' ? localizedText('链上金库') : fmtVenueLabel(option.venue);
    return `<label class="capital-account-option ${option.selectable ? '' : 'is-disabled'}"><input type="checkbox" name="capital_account" value="${escapeHtml(option.key)}" ${selectedAccountKeys.has(option.key) ? 'checked' : ''} ${option.selectable ? '' : 'disabled'}><i class="capital-account-color color-${index % 6}" aria-hidden="true"></i><span><b title="${escapeHtml(option.label)}">${escapeHtml(option.label)}</b><small title="${escapeHtml(accountId)}">${escapeHtml(scopeLabel)} · ${escapeHtml(displayAccountId)} · ${escapeHtml(fmtStatus(option.connection_status))}</small>${option.disabled_reason ? `<em>${escapeHtml(option.disabled_reason)}</em>` : ''}</span></label>`;
  }).join('');
  const selectedTags = accountOptions.filter(option => selectedAccountKeys.has(option.key)).map(option => `<button class="capital-account-tag color-${capitalSeriesColorIndex[option.key]}" type="button" data-remove-capital-account="${escapeHtml(option.key)}"><span>${escapeHtml(option.label)}</span><b aria-hidden="true">×</b><span class="sr-only">移除 ${escapeHtml(option.label)}</span></button>`).join('');
  const capitalRangeText = historySelection.complete ? '全部历史' : `最近 ${formatCapitalRangeDuration(historySelection.duration)}`;
  const rangeControl = hasHistory ? `<div class="capital-history-range" aria-label="资金曲线时间范围"><div class="capital-history-range-copy"><small>时间范围</small><output for="capital-history-range" data-capital-range-label>${escapeHtml(capitalRangeText)}</output></div><div class="capital-history-range-slider"><input id="capital-history-range" type="range" data-capital-history-range min="1" max="${CAPITAL_CHART_RANGE_MAX}" step="1" value="${Number(capitalChartRangeValue)}" aria-label="拖动选择资金曲线时间范围" aria-valuetext="${escapeHtml(capitalRangeText)}"><div aria-hidden="true"><span>较短</span><span>全部</span></div></div></div>` : '';
  host.innerHTML = `<div class="section-heading"><div><p class="eyebrow">${escapeHtml(fmtExecutionMode(displayEnvironment))} · 账户净值</p><h2>资金绩效曲线</h2><p class="subtle">账户选择只影响绩效展示，不改变团队模式、交易权限或资金能力。</p></div><span class="status-pill">${selectedAccountKeys.size} 个账户</span></div>
    <details class="capital-account-picker"><summary><span>选择要叠加的账户曲线</span><small>已选 ${selectedAccountKeys.size} 个，可多选</small></summary><div class="capital-account-picker-panel"><div class="capital-account-picker-tools"><input type="search" data-capital-account-search placeholder="搜索账户名称或 ID" aria-label="搜索账户"><select data-capital-venue-filter aria-label="按交易所筛选"><option value="">全部交易所</option>${[...new Set(accountOptions.map(option => option.venue))].map(venue => `<option value="${escapeHtml(venue)}">${escapeHtml(venue === 'VAULT' ? '链上金库' : fmtVenueLabel(venue))}</option>`).join('')}</select><button class="secondary" type="button" data-capital-select-all>全选</button><button class="secondary" type="button" data-capital-clear>清空</button></div><div class="capital-account-options">${accountFilters || '<p class="subtle">当前模式尚未添加有效账户。</p>'}</div></div></details><div class="capital-account-tags" aria-label="已选账户">${selectedTags || '<span class="subtle">尚未选择账户</span>'}</div>
    <section class="capital-overview" aria-label="当前资金净值"><article class="capital-total-card ${netWorth.complete ? 'is-current' : 'is-limited'}"><small>当前所选账户总净值</small><b>${escapeHtml(totalHeadline)}</b><p>${escapeHtml(totalSupporting)}</p></article><div class="capital-source-cards">${sourceCards}</div></section>
    <section class="capital-trust-panel ${netWorth.complete ? 'is-current' : 'is-limited'}"><div><b>${netWorth.complete ? '所选账户数据可信，可用于当前汇总' : '当前汇总已阻断'}</b><p>${escapeHtml(trustCopy)}</p></div><span>${netWorth.complete ? '完整' : '需关注'}</span></section>
    <section class="capital-chart-panel" tabindex="-1"><div class="chart-head"><div><p class="eyebrow">净值趋势</p><h3>所选账户独立曲线与可信汇总</h3><p class="subtle">缺失、过期、错位和断档不会补零或强行连线。</p></div>${hasHistory ? '<button class="secondary capital-chart-expand" type="button" data-capital-chart-expand aria-pressed="false" aria-label="放大资金曲线"><span data-expand-label>全屏查看</span></button>' : ''}</div><div class="capital-chart-meta"><span data-capital-range-coverage>${escapeHtml(capitalHistoryCoverage(historySeries, historySelection))}</span><span>数据来自当前模式对应交易所只读事实</span></div><div class="chart-legend" role="group" aria-label="选择显示的资金曲线">${chartLegend}</div>${hasHistory ? '<div class="capital-chart-wrap"><canvas id="capital-chart" height="300" aria-label="所选账户及汇总 USD 资金趋势"></canvas><div class="capital-chart-tooltip" role="status" hidden></div></div>' : '<div class="chart-empty">尚无可绘制的资金历史；缺失数据不会补零。</div>'}${rangeControl}</section>`;
  drawCapitalChart(visibleHistorySeries);
  const applyAccountSelection = selected => {
    localStorage.setItem(selectionStorageKey, JSON.stringify(selected));
    const next = new URLSearchParams(location.search);
    next.delete('capital_account');
    (selected.length ? selected : ['__NONE__']).forEach(value => next.append('capital_account', value));
    history.replaceState({}, '', `/results?${next.toString()}`);
    renderCapitalPerformancePanel();
  };
  host.querySelectorAll('.capital-account-option input').forEach(input => input.addEventListener('change', () => applyAccountSelection([...host.querySelectorAll('.capital-account-option input:checked')].map(option => option.value))));
  host.querySelectorAll('[data-remove-capital-account]').forEach(button => button.addEventListener('click', () => applyAccountSelection([...selectedAccountKeys].filter(key => key !== button.dataset.removeCapitalAccount))));
  host.querySelector('[data-capital-select-all]')?.addEventListener('click', () => applyAccountSelection(accountOptions.filter(option => option.selectable).map(option => option.key)));
  host.querySelector('[data-capital-clear]')?.addEventListener('click', () => applyAccountSelection([]));
  const filterAccounts = () => {
    const query = (host.querySelector('[data-capital-account-search]')?.value || '').toLowerCase();
    const venue = host.querySelector('[data-capital-venue-filter]')?.value || '';
    host.querySelectorAll('.capital-account-option').forEach((row, index) => {
      const option = accountOptions[index];
      row.hidden = !(`${option.label} ${option.account_id}`.toLowerCase().includes(query) && (!venue || option.venue === venue));
    });
  };
  host.querySelector('[data-capital-account-search]')?.addEventListener('input', filterAccounts);
  host.querySelector('[data-capital-venue-filter]')?.addEventListener('change', filterAccounts);
  bindCapitalPerformanceChart(historySeries, {
    history:item.history || [],
    alignmentToleranceSeconds:netWorth.alignment_tolerance_seconds || 60,
    gapToleranceSeconds:netWorth.history_gap_tolerance_seconds || 300,
    selectedOnchainSeriesLabel,
  });
}

function renderDirectCapitalConfigurationEditor(directConfiguration, selectedTreasuryProvider) {
  if (!directConfiguration.can_manage) return '';
  const configuredPlaceholder = value => value ? '已配置；留空保持当前值' : '尚未配置';
  const fieldValue = value => escapeHtml(value == null ? '' : String(value));
  const exchangeAccountInput = (name, label, value, configured) => `<label>${label}<span class="field-help">直接填写与服务端生产运行绑定完全一致的 account_id；账户名称不参与校验。</span><input name="${name}" value="${fieldValue(value)}" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${configuredPlaceholder(configured)}"></label>`;
  const binanceAccountInput = exchangeAccountInput('binance_account_id', '币安账户 ID', directConfiguration.binance_account_id, directConfiguration.binance_account_configured);
  const hyperliquidAccountInput = exchangeAccountInput('hyperliquid_account_id', 'Hyperliquid 账户 ID', directConfiguration.hyperliquid_account_id, directConfiguration.hyperliquid_account_configured);
  const version = directConfiguration.version
    ? `版本 ${directConfiguration.version} · ${escapeHtml(directConfiguration.updated_by_username || '系统管理员')} · ${fmtDate(directConfiguration.effective_at)}`
    : '尚无数据库配置；当前读取安全环境配置';
  return `<details class="card direct-capital-config-editor">
    <summary><span><b>配置 Vault、Safe 与资金路径</b><small>${version}</small></span><strong>管理员配置</strong></summary>
    <form id="direct-capital-config-form" class="toolbox-content compact-form">
      <p class="safety-note">NoTilt Vault 与 Safe 可以同时接入并分别保存。选择项只决定之后新建的资金操作使用哪个金库，未选中的金库配置会继续保留。这里只接收公开地址和账户范围；不得输入 API secret、私钥、种子、钱包密码或签名令牌。</p>
      <fieldset class="treasury-provider-choice"><legend>1. 选择当前使用的链上金库</legend>
        <label><input type="radio" name="treasury_provider" value="NOTILT_VAULT" ${selectedTreasuryProvider === 'NOTILT_VAULT' ? 'checked' : ''}><span><b>NoTilt Vault</b><small>使用 NoTilt 金库预算、延迟与官方 SDK</small></span></label>
        <label><input type="radio" name="treasury_provider" value="SAFE_SPENDING_LIMIT" ${selectedTreasuryProvider === 'SAFE_SPENDING_LIMIT' ? 'checked' : ''}><span><b>Safe Spending Limits</b><small>使用 Safe Allowance Module 的 delegate 额度</small></span></label>
      </fieldset>
      <p class="provider-guidance" data-provider-guidance></p>
      <div class="capital-provider-config-grid"><section class="capital-config-section" data-provider-fields="NOTILT_VAULT"><div class="capital-config-heading"><div><h3>NoTilt Vault 配置</h3><p>填写 NoTilt 金库编号和金库地址。</p></div><span data-provider-role>已接入备用</span></div><div class="field-grid"><label>NoTilt 金库编号<input name="vault_id" value="${fieldValue(directConfiguration.vault_id)}" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.vault_id_configured)}"></label><label>NoTilt 金库地址<input name="vault_address" value="${fieldValue(directConfiguration.vault_address)}" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.vault_address_configured)}"></label></div>${directConfiguration.notilt_provider_configured && selectedTreasuryProvider !== 'NOTILT_VAULT' ? '<label class="direct-capital-confirm"><input type="checkbox" name="clear_notilt_configuration"><span>清除未使用的 NoTilt 配置</span></label>' : ''}</section>
      <section class="capital-config-section" data-provider-fields="SAFE_SPENDING_LIMIT"><div class="capital-config-heading"><div><h3>Safe Spending Limits 配置</h3><p>填写公开的 Safe Smart Account 与 delegate 地址。</p></div><span data-provider-role>已接入备用</span></div><div class="field-grid"><label>Safe Smart Account<input name="safe_address" value="${fieldValue(directConfiguration.safe_address)}" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.safe_address_configured)}"></label><label>Safe Spending Limit delegate<input name="safe_delegate_address" value="${fieldValue(directConfiguration.safe_delegate_address)}" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.safe_delegate_configured)}"></label></div>${directConfiguration.safe_provider_configured && selectedTreasuryProvider !== 'SAFE_SPENDING_LIMIT' ? '<label class="direct-capital-confirm"><input type="checkbox" name="clear_safe_configuration"><span>清除未使用的 Safe 配置</span></label>' : ''}</section></div>
      <section class="capital-config-section common-capital-fields"><h3>2. 共用账户与安全边界</h3><p>两种链上金库共用币安、Hyperliquid、自有地址和金额限制；直接填写与服务端生产运行绑定一致的精确 account_id，不填写可编辑的账户名称。</p><div class="field-grid"><label>授权自有 Arbitrum 地址<span class="field-help">用于 Arbitrum One 原生 USDC 充值和接收资金的钱包地址。钱包需准备少量 ETH 支付链上 Gas。</span><input name="owned_arbitrum_address" value="${fieldValue(directConfiguration.owned_arbitrum_address)}" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.owned_arbitrum_address_configured)}"></label>${binanceAccountInput}<label>币安白名单入金地址<input name="binance_deposit_address" value="${fieldValue(directConfiguration.binance_deposit_address)}" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.binance_whitelist_destination_configured)}"></label><label>币安受限提现地址<input name="binance_withdrawal_address" value="${fieldValue(directConfiguration.binance_withdrawal_address)}" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.binance_withdrawal_destination_configured)}"><small data-withdrawal-scope-help></small></label>${hyperliquidAccountInput}<label>Hyperliquid 充值桥地址（Bridge）<span class="field-help">仅用于 Arbitrum One 原生 USDC。生产环境必须使用 Hyperliquid 官方 Bridge2 地址。</span><input name="hyperliquid_bridge_address" value="${fieldValue(directConfiguration.hyperliquid_bridge_address)}" autocomplete="off" placeholder="${configuredPlaceholder(directConfiguration.hyperliquid_contract_configured)}"></label><label>单次金额上限（USDC）<input name="max_amount" value="${fieldValue(directConfiguration.max_amount)}" type="number" step="any" min="0.000001" placeholder="留空保持当前值"></label><label>最大费用上限（USDC）<span class="field-help">仅交易所或 Hyperliquid 提现会从 USDC 到账额扣除费用；Safe 发起的链上转账使用 ETH 支付 Gas，不再误减 USDC 到账额。</span><input name="max_fee" value="${fieldValue(directConfiguration.max_fee)}" type="number" step="any" min="0" placeholder="留空保持当前值"><small>最大费用上限必须低于单次金额上限。</small></label></div></section>
      <div class="form-error" role="alert" tabindex="-1"></div><div class="form-actions"><button class="primary">保存配置并切换当前金库</button></div>
    </form>
  </details>`;
}

function renderCapitalHandoffCard({eyebrow, title, facts = [], note = ''}) {
  return `<article class="capital-handoff-card"><header><span>${escapeHtml(eyebrow)}</span><b>${escapeHtml(title)}</b></header><dl>${facts.map(fact => `<div><dt>${escapeHtml(fact.label)}</dt><dd class="${fact.mono ? 'is-mono' : ''}">${escapeHtml(fact.value ?? '—')}</dd></div>`).join('')}</dl>${note ? `<p>${escapeHtml(note)}</p>` : ''}</article>`;
}

function renderCapitalOperationAssurance({operationId, operationLabel, amount, asset, blockers, pendingPreflights, hardBlockers, handoffCards}) {
  const cards = handoffCards.filter(Boolean);
  const blockerItems = blockers.map(blocker => `<li>${escapeHtml(blocker)}</li>`).join('');
  const healthLabel = hardBlockers.length
    ? `${hardBlockers.length} 项阻断`
    : pendingPreflights.length
      ? `${pendingPreflights.length} 项预检`
      : cards.length
        ? '签名摘要'
        : '校验通过';
  const healthClass = hardBlockers.length ? 'is-blocked' : pendingPreflights.length ? 'is-pending' : 'is-ready';
  const summaryCopy = hardBlockers.length
    ? '需要先处理阻断条件'
    : pendingPreflights.length
      ? '提交前将自动完成实时校验'
      : cards.length
        ? '查看钱包、金额与收款范围'
        : '当前没有待处理项';
  if (!blockers.length && !cards.length) {
    return `<div class="capital-operation-assurance ${healthClass}"><span class="capital-assurance-state">${healthLabel}</span><small>${summaryCopy}</small></div>`;
  }
  directCapitalAssuranceRecords.set(String(operationId), {
    title:operationLabel || '资金操作',
    meta:`${fmtNumber(amount)} ${asset || 'USDC'} · 操作 ${shortId(operationId)}`,
    healthLabel, healthClass, summaryCopy,
    body:`${blockers.length ? `<section class="capital-operation-blocker-panel"><b>${hardBlockers.length ? '需要处理' : '实时预检'}</b><ul>${blockerItems}</ul></section>` : ''}${cards.length ? `<div class="capital-handoff-grid">${cards.join('')}</div>` : ''}`,
  });
  return `<div class="capital-operation-assurance ${healthClass}"><span class="capital-assurance-state">${escapeHtml(healthLabel)}</span><small>${escapeHtml(summaryCopy)}</small><button class="secondary capital-assurance-trigger" type="button" data-capital-assurance-record="${escapeHtml(operationId)}">查看记录</button></div>`;
}

async function renderCapitalCenter() {
  const displayEnvironment = currentWorkflowEnvironment();
  if (!['TESTNET','LIVE'].includes(displayEnvironment)) {
    main.innerHTML = `<section class="page"><div class="callout"><b>团队尚未选择运行模式</b><p>请由管理员使用页眉“当前模式”选择测试模式或生产模式。</p>${hasCapability('venue.view') ? '<button class="primary" type="button" data-open-mode-switch>切换当前模式</button>' : '<span class="status-pill">由团队管理员处理</span>'}</div></section>`;
    return;
  }
  const result = await api(`/api/capital?environment=${displayEnvironment}`, {
    timeoutMs:45_000,
  });
  const item = result.data;
  directCapitalWalletActions = new Map();
  directCapitalAssuranceRecords = new Map();
  const directConfiguration = item.direct_configuration || {};
  const selectedTreasuryProvider = directConfiguration.treasury_provider || 'NOTILT_VAULT';
  const selectedTreasuryProviderLabel = selectedTreasuryProvider === 'SAFE_SPENDING_LIMIT' ? 'Safe Spending Limits' : 'NoTilt Vault';
  const transfers = partitionCapitalRecords(item.transfers);
  const liveInTransit = liveCapitalInTransit(transfers.live);
  const directConfigurationEditor = renderDirectCapitalConfigurationEditor(directConfiguration, selectedTreasuryProvider);
  const directPathCards = DIRECT_CAPITAL_PATHS.map(path => {
    const resumable = (item.direct_operations || []).find(operation => {
      if (operation.path !== path.path || operation.treasury_provider !== 'SAFE_SPENDING_LIMIT' || operation.status === 'SETTLED') return false;
      const stages = operation.stages || [];
      const walletCancelled = stages.some(stage => stage.code?.endsWith('_WALLET_CANCELLED'));
      const sourceSubmission = [...stages].reverse().find(stage => stage.code === 'TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET');
      const targetSubmission = stages.some(stage => stage.code === 'HYPERLIQUID_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET');
      return Boolean(sourceSubmission?.transaction_hash && !targetSubmission && !walletCancelled && ['VAULT_TO_BINANCE','VAULT_TO_HYPERLIQUID'].includes(path.path));
    });
    const sourceSubmission = resumable && [...(resumable.stages || [])].reverse().find(stage => stage.code === 'TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET');
    const autoContinuing = Boolean(resumable && path.path === 'VAULT_TO_HYPERLIQUID');
    const resumeAttributes = resumable && !autoContinuing ? ` data-resume-capital-operation="${escapeHtml(resumable.operation_id)}" data-operation-version="${Number(resumable.version)}" data-transaction-hash="${escapeHtml(sourceSubmission.transaction_hash)}"` : '';
    const actionLabel = autoContinuing ? '已提交，正在自动续接入金' : resumable ? '核对已提交链上划转' : path.action;
    const disabled = autoContinuing ? ' disabled aria-busy="true"' : '';
    return `<article class="capital-route-card"><div class="capital-route-meta"><span>固定路径</span><strong>${escapeHtml(path.badge)}</strong></div><div class="capital-route-flow"><b>${escapeHtml(path.from)}</b><span aria-hidden="true">→</span><b>${escapeHtml(path.to)}</b></div><p>${escapeHtml(path.copy)}</p><ol>${path.steps.map(step => `<li>${escapeHtml(step)}</li>`).join('')}</ol><button class="secondary capital-route-action" type="button" data-open-capital-path="${escapeHtml(path.path)}"${resumeAttributes}${disabled}>${escapeHtml(actionLabel)}</button></article>`;
  }).join('');
  const directCapitalDialog = `<dialog id="direct-capital-dialog" aria-labelledby="direct-capital-title"><form id="direct-capital-form" class="dialog-form" data-direct-capital-form="" data-treasury-provider="${escapeHtml(selectedTreasuryProvider)}"><div class="dialog-head"><div><p class="eyebrow">资金路径确认</p><h2 id="direct-capital-title" data-capital-path-title>选择资金路径</h2></div><button type="button" class="icon-button" data-close-capital-dialog aria-label="关闭">×</button></div><p class="subtle" data-capital-path-copy></p><div class="selected-provider-summary"><small>当前链上金库</small><b>${escapeHtml(selectedTreasuryProviderLabel)}</b><span>如需切换，请由管理员先保存新的资金路径配置。</span></div><input name="path" type="hidden"><label>金额（USDC）<input name="amount" type="number" step="any" min="0.000001" required placeholder="输入划转金额"></label><p class="safety-note">点击“确认并继续”即确认当前资金方向与金额。系统先完成实时预检：链上操作直接唤起钱包由你签名；币安转出通过受限 API 直接提交。任何条件缺失仍会阻断。</p><div class="form-error" role="alert"></div><div class="dialog-actions"><button type="button" class="secondary" data-close-capital-dialog>取消</button><button class="primary" type="submit">确认并继续</button></div></form></dialog>`;
  const capitalAssuranceDialog = `<dialog id="capital-assurance-dialog" class="capital-assurance-dialog" aria-labelledby="capital-assurance-title"><div class="capital-assurance-dialog-shell"><header class="capital-assurance-dialog-head"><div><p class="eyebrow">操作记录</p><h2 id="capital-assurance-title" data-capital-assurance-title>校验与签名摘要</h2><span data-capital-assurance-meta></span></div><button type="button" class="icon-button" data-close-capital-assurance aria-label="关闭">×</button></header><div class="capital-assurance-dialog-status" data-capital-assurance-status></div><div class="capital-assurance-dialog-content" data-capital-assurance-content></div><footer><button class="secondary" type="button" data-close-capital-assurance>关闭</button></footer></div></dialog>`;
  const directOperations = item.direct_operations || [];
  const directOperationTotal = directOperations.length;
  const directOperationTotalPages = Math.max(1, Math.ceil(directOperationTotal / capitalOperationsPageSize));
  capitalOperationsPage = Math.min(Math.max(1, capitalOperationsPage), directOperationTotalPages);
  const directOperationPageItems = directOperations.slice(
    (capitalOperationsPage - 1) * capitalOperationsPageSize,
    capitalOperationsPage * capitalOperationsPageSize,
  );
  const directRows = directOperationPageItems.map(operation => {
    const pathDefinition = DIRECT_CAPITAL_PATHS.find(path => path.path === operation.path);
    const label = pathDefinition ? `${pathDefinition.from} → ${pathDefinition.to}` : operation.path;
    const stages = directCapitalCurrentPhase(operation);
    const blockerCodes = operation.blockers || [];
    const blockers = blockerCodes.map(formatDirectCapitalBlocker);
    const pendingPreflights = blockerCodes.filter(code => DIRECT_CAPITAL_PREFLIGHT_BLOCKERS.has(code));
    const hardBlockers = blockerCodes.filter(code => !DIRECT_CAPITAL_PREFLIGHT_BLOCKERS.has(code));
    const safeOutbound = operation.path === 'VAULT_TO_BINANCE' || operation.path === 'VAULT_TO_HYPERLIQUID';
    const frozenStages = operation.stages || [];
    const binancePreview = [...frozenStages].reverse().find(stage => stage.artifact?.kind?.startsWith('BINANCE_'));
    const safePreview = [...frozenStages].reverse().find(stage => stage.signature_request?.kind?.startsWith('SAFE_'));
    const notiltPreview = [...frozenStages].reverse().find(stage => ['NOTILT_UNSIGNED_RELEASE_REQUEST_PREVIEW','NOTILT_UNSIGNED_DEPOSIT_PREVIEW'].includes(stage.code));
    const notiltExecutionPreview = [...frozenStages].reverse().find(stage => stage.code === 'NOTILT_UNSIGNED_RELEASE_EXECUTION_PREVIEW');
    const notiltDestinationPreview = [...frozenStages].reverse().find(stage => stage.code === 'NOTILT_DESTINATION_TRANSFER_PREVIEW');
    const notiltRequestReceipt = [...frozenStages].reverse().find(stage => stage.code === 'NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED');
    const notiltExecutionReceipt = [...frozenStages].reverse().find(stage => stage.code === 'NOTILT_RELEASE_EXECUTION_RECEIPT_CONFIRMED');
    const safeWithdrawalReceipt = [...frozenStages].reverse().find(stage => stage.code === 'TREASURY_WITHDRAWAL_RECEIPT_CONFIRMED');
    const binanceSubmission = [...frozenStages].reverse().find(stage => stage.code === 'BINANCE_RESTRICTED_WITHDRAWAL_SUBMITTED');
    const binanceReceiptConfirmed = frozenStages.some(stage => ['BINANCE_DEPOSIT_RECEIPT_CONFIRMED', 'BINANCE_WITHDRAWAL_RECEIPT_CONFIRMED'].includes(stage.code));
    const hyperliquidPreview = [...frozenStages].reverse().find(stage => stage.artifact?.kind?.startsWith('HYPERLIQUID_'));
    const walletStage = operation.path === 'VAULT_TO_HYPERLIQUID' ? 'HYPERLIQUID_DEPOSIT' : hyperliquidPreview?.artifact?.kind === 'HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST' ? 'HYPERLIQUID_CLASS_TRANSFER' : 'HYPERLIQUID_WITHDRAWAL';
    const walletSubmission = [...frozenStages].reverse().find(stage => stage.code === `${walletStage}_SUBMITTED_BY_HUMAN_WALLET`);
    const hyperliquidDepositConfirmed = frozenStages.some(stage => stage.code === 'HYPERLIQUID_DEPOSIT_ARBITRUM_CONFIRMED') && frozenStages.some(stage => stage.code === 'HYPERLIQUID_DEPOSIT_LEDGER_CONFIRMED');
    const hyperliquidWithdrawalConfirmed = frozenStages.some(stage => stage.code === 'HYPERLIQUID_WITHDRAWAL_LEDGER_CONFIRMED') && frozenStages.some(stage => stage.code === 'HYPERLIQUID_WITHDRAWAL_ARBITRUM_CONFIRMED');
    const hyperliquidClassTransferConfirmed = frozenStages.some(stage => stage.code === 'HYPERLIQUID_CLASS_TRANSFER_LEDGER_CONFIRMED');
    const providerPreviewReady = frozenStages.some(stage => ['NOTILT_UNSIGNED_DEPOSIT_PREVIEW', 'SAFE_DEPOSIT_UNSIGNED_TRANSACTION_READY'].includes(stage.code));
    const treasuryWithdrawalSubmission = [...frozenStages].reverse().find(stage => stage.code === 'TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET');
    const notiltExecutionSubmission = [...frozenStages].reverse().find(stage => stage.code === 'NOTILT_RELEASE_EXECUTION_SUBMITTED_BY_HUMAN_WALLET');
    const notiltDestinationSubmission = [...frozenStages].reverse().find(stage => stage.code === 'NOTILT_DESTINATION_TRANSFER_SUBMITTED_BY_HUMAN_WALLET');
    const treasurySubmission = [...frozenStages].reverse().find(stage => stage.code === 'TREASURY_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET');
    const treasuryReceiptConfirmed = frozenStages.some(stage => stage.code === 'TREASURY_DESTINATION_RECEIPT_CONFIRMED');
    const treasuryPreview = safePreview || notiltPreview;
    const treasuryStage = safeOutbound ? 'TREASURY_WITHDRAWAL' : 'TREASURY_DEPOSIT';
    const treasuryAlreadySubmitted = safeOutbound ? treasuryWithdrawalSubmission : treasurySubmission;
    const treasuryPreviewExecutable = safePreview
      ? Boolean(safePreview.signature_request?.to && safePreview.signature_request?.data)
      : Boolean(notiltPreview?.wallet_address && notiltPreview?.transactions?.length);
    const treasuryActionKey = treasuryPreview && treasuryPreviewExecutable && !treasuryAlreadySubmitted ? `${operation.operation_id}:${treasuryStage}` : '';
    if (treasuryActionKey) {
      directCapitalWalletActions.set(treasuryActionKey, safePreview ? {
        operationId:operation.operation_id, version:operation.version, stage:treasuryStage,
        kind:'TRANSACTION', transaction:safePreview.signature_request,
        expectedWallet:safePreview.signature_request.from || safePreview.signature_request.sender,
        path:operation.path, treasuryProvider:operation.treasury_provider,
      } : {
        operationId:operation.operation_id, version:operation.version, stage:treasuryStage,
        kind:'TRANSACTIONS', transactions:notiltPreview.transactions,
        expectedWallet:notiltPreview.wallet_address,
        path:operation.path, treasuryProvider:operation.treasury_provider,
      });
    }
    const notiltExecutionActionKey = notiltExecutionPreview && !notiltExecutionSubmission ? `${operation.operation_id}:NOTILT_RELEASE_EXECUTION` : '';
    if (notiltExecutionActionKey) directCapitalWalletActions.set(notiltExecutionActionKey, {
      operationId:operation.operation_id, version:operation.version, stage:'NOTILT_RELEASE_EXECUTION',
      kind:'TRANSACTIONS', transactions:notiltExecutionPreview.transactions,
      expectedWallet:notiltExecutionPreview.wallet_address,
    });
    const notiltDestinationActionKey = notiltDestinationPreview && !notiltDestinationSubmission ? `${operation.operation_id}:NOTILT_DESTINATION_TRANSFER` : '';
    if (notiltDestinationActionKey) directCapitalWalletActions.set(notiltDestinationActionKey, {
      operationId:operation.operation_id, version:operation.version, stage:'NOTILT_DESTINATION_TRANSFER',
      kind:'TRANSACTION', transaction:notiltDestinationPreview.artifact,
      expectedWallet:notiltDestinationPreview.artifact?.from,
    });
    const hyperliquidActionKey = hyperliquidPreview && !walletSubmission ? `${operation.operation_id}:${walletStage}` : '';
    if (hyperliquidActionKey) directCapitalWalletActions.set(hyperliquidActionKey, {
      operationId:operation.operation_id, version:operation.version, stage:walletStage,
      kind:'HYPERLIQUID', artifact:hyperliquidPreview.artifact,
      path:operation.path, treasuryProvider:operation.treasury_provider,
    });
    const startButton = !treasuryActionKey && !hyperliquidActionKey && !binanceSubmission && !(operation.path === 'BINANCE_TO_VAULT' && binancePreview) && !treasuryWithdrawalSubmission && !walletSubmission
      ? `<button class="primary" type="button" data-direct-capital-start="${escapeHtml(operation.operation_id)}" data-operation-version="${Number(operation.version || 1)}" data-operation-path="${escapeHtml(operation.path)}" data-treasury-provider="${escapeHtml(operation.treasury_provider || 'NOTILT_VAULT')}">${operation.path === 'BINANCE_TO_VAULT' ? '继续并提交币安提现' : '继续并打开钱包'}</button>` : '';
    const treasuryWalletButton = treasuryActionKey ? `<button class="primary" type="button" data-direct-wallet-action="${escapeHtml(treasuryActionKey)}">打开钱包确认</button>` : '';
    const notiltExecutionWalletButton = notiltExecutionActionKey ? `<button class="primary" type="button" data-direct-wallet-action="${escapeHtml(notiltExecutionActionKey)}">执行释放并打开钱包</button>` : '';
    const notiltDestinationWalletButton = notiltDestinationActionKey ? `<button class="primary" type="button" data-direct-wallet-action="${escapeHtml(notiltDestinationActionKey)}">转入币安并打开钱包</button>` : '';
    const hyperliquidWalletButton = hyperliquidActionKey ? `<button class="primary" type="button" data-direct-wallet-action="${escapeHtml(hyperliquidActionKey)}">打开钱包确认</button>` : '';
    const binanceSubmitButton = operation.path === 'BINANCE_TO_VAULT' && binancePreview && !binanceSubmission ? `<button class="primary" type="button" data-binance-submit-direct="${escapeHtml(operation.operation_id)}" data-operation-version="${Number(operation.version || 1)}" ${directConfiguration.binance_capital_submission_enabled && item.real_transfer_gate === 'ENABLED' ? '' : 'disabled title="币安提现开关或 CAPITAL_TRANSFER 当前关闭"'}>直接提交币安提现</button>` : '';
    const binanceDepositSubmission = operation.treasury_provider === 'NOTILT_VAULT' ? notiltDestinationSubmission : treasuryWithdrawalSubmission;
    const binanceReceiptButton = binancePreview && !binanceReceiptConfirmed && ((operation.path === 'VAULT_TO_BINANCE' && binanceDepositSubmission?.transaction_hash) || (operation.path === 'BINANCE_TO_VAULT' && binanceSubmission)) ? `<button class="secondary" type="button" data-binance-receipt-direct="${escapeHtml(operation.operation_id)}" data-binance-stage="${operation.path === 'VAULT_TO_BINANCE' ? 'BINANCE_DEPOSIT' : 'BINANCE_WITHDRAWAL'}" data-operation-version="${Number(operation.version || 1)}" data-transaction-hash="${escapeHtml(binanceDepositSubmission?.transaction_hash || '')}">自动核对到账</button>` : '';
    const notiltReceiptSubmission = notiltExecutionSubmission && !notiltExecutionReceipt ? notiltExecutionSubmission : treasuryWithdrawalSubmission;
    const notiltReceiptButton = operation.treasury_provider === 'NOTILT_VAULT' && notiltReceiptSubmission && !(notiltExecutionSubmission ? notiltExecutionReceipt : notiltRequestReceipt) ? `<button class="secondary" type="button" data-notilt-receipt-direct="${escapeHtml(operation.operation_id)}" data-operation-version="${Number(operation.version || 1)}" data-transaction-hash="${escapeHtml(notiltReceiptSubmission.transaction_hash || '')}">${notiltExecutionSubmission ? '验证释放执行回执' : '验证释放请求回执'}</button>` : '';
    const safeWithdrawalReceiptButton = operation.path === 'VAULT_TO_HYPERLIQUID' && operation.treasury_provider === 'SAFE_SPENDING_LIMIT' && treasuryWithdrawalSubmission && !safeWithdrawalReceipt ? `<button class="secondary" type="button" data-treasury-withdrawal-receipt-direct="${escapeHtml(operation.operation_id)}" data-operation-version="${Number(operation.version || 1)}" data-transaction-hash="${escapeHtml(treasuryWithdrawalSubmission.transaction_hash || '')}">验证 Safe 转出到账</button>` : '';
    const walletReceiptConfirmed = walletStage === 'HYPERLIQUID_DEPOSIT' ? hyperliquidDepositConfirmed : walletStage === 'HYPERLIQUID_CLASS_TRANSFER' ? hyperliquidClassTransferConfirmed : hyperliquidWithdrawalConfirmed;
    const hyperliquidReceiptButton = walletSubmission && !walletReceiptConfirmed ? `<button class="secondary" type="button" data-hl-receipt-direct="${escapeHtml(operation.operation_id)}" data-wallet-stage="${walletStage}" data-operation-version="${Number(operation.version || 1)}" data-transaction-hash="${escapeHtml(walletSubmission.transaction_hash || '')}" data-action-hash="${escapeHtml(walletSubmission.action_hash || '')}" data-nonce="${Number(walletSubmission.nonce || 0)}">自动读取公开回执</button>` : '';
    const treasuryReceiptButton = treasurySubmission && !treasuryReceiptConfirmed ? `<button class="secondary" type="button" data-treasury-receipt-direct="${escapeHtml(operation.operation_id)}" data-operation-version="${Number(operation.version || 1)}" data-transaction-hash="${escapeHtml(treasurySubmission.transaction_hash || '')}">自动验证金库到账</button>` : '';
    const nextAction = operation.path === 'VAULT_TO_HYPERLIQUID' && operation.treasury_provider === 'SAFE_SPENDING_LIMIT' && safeWithdrawalReceipt && !walletSubmission
      ? 'HYPERLIQUID'
      : operation.treasury_provider === 'NOTILT_VAULT' && safeOutbound && notiltRequestReceipt && !notiltExecutionPreview && !notiltExecutionSubmission && !notiltExecutionReceipt
        ? 'NOTILT_EXECUTION'
        : operation.path === 'VAULT_TO_BINANCE' && operation.treasury_provider === 'NOTILT_VAULT' && notiltExecutionReceipt && !notiltDestinationPreview && !notiltDestinationSubmission
          ? 'NOTILT_DESTINATION'
          : operation.path === 'VAULT_TO_HYPERLIQUID' && operation.treasury_provider === 'NOTILT_VAULT' && notiltExecutionReceipt && !walletSubmission
            ? 'HYPERLIQUID'
      : operation.path === 'HYPERLIQUID_TO_VAULT' && walletStage === 'HYPERLIQUID_CLASS_TRANSFER' && hyperliquidClassTransferConfirmed
        ? 'HYPERLIQUID'
        : operation.path === 'HYPERLIQUID_TO_VAULT' && walletStage === 'HYPERLIQUID_WITHDRAWAL' && hyperliquidWithdrawalConfirmed && !treasurySubmission && !treasuryActionKey
          ? 'TREASURY_DEPOSIT'
          : '';
    const nextActionLabel = nextAction === 'NOTILT_EXECUTION'
      ? '协议解锁后执行并打开钱包'
      : nextAction === 'NOTILT_DESTINATION'
        ? '继续转入币安并打开钱包'
        : nextAction === 'HYPERLIQUID'
          ? (walletStage === 'HYPERLIQUID_CLASS_TRANSFER' ? '继续提现并打开钱包' : '到账后继续并打开钱包')
          : '继续存入金库并打开钱包';
    const nextActionButton = nextAction ? `<button class="primary" type="button" data-direct-capital-next="${escapeHtml(operation.operation_id)}" data-next-action="${nextAction}" data-operation-version="${Number(operation.version || 1)}" data-operation-path="${escapeHtml(operation.path)}" data-treasury-provider="${escapeHtml(operation.treasury_provider || 'NOTILT_VAULT')}">${nextActionLabel}</button>` : '';
    const binanceBoundary = binancePreview ? renderCapitalHandoffCard({
      eyebrow:'交易所链路',
      title:operation.path === 'VAULT_TO_BINANCE' ? '币安充值范围已核对' : binanceSubmission ? '币安提现已提交' : '币安提现条件已核对',
      facts:[
        {label:'网络', value:binancePreview.artifact.network},
        {label:'金额', value:`${binancePreview.artifact.amount} USDC`},
        {label:operation.path === 'BINANCE_TO_VAULT' ? '手续费' : '链上确认', value:operation.path === 'BINANCE_TO_VAULT' ? `${binancePreview.artifact.fee} USDC` : '浏览器钱包'},
        {label:'目标地址', value:binancePreview.artifact.destination, mono:true},
      ],
      note:'币安转出直接提交；API Secret 与请求签名不会显示或写入操作记录。',
    }) : '';
    const safeBoundary = safePreview ? renderCapitalHandoffCard({
      eyebrow:'钱包确认',
      title:safeOutbound ? 'Safe Allowance Module 已核对' : 'Safe 入金交易已构建',
      facts:[
        {label:'金额', value:`${operation.amount} USDC`},
        {label:'Safe', value:safePreview.signature_request.safe, mono:true},
        ...(safeOutbound ? [
          {label:'Nonce', value:safePreview.signature_request.nonce},
          {label:'Delegate', value:safePreview.signature_request.delegate, mono:true},
          {label:'收款地址', value:safePreview.signature_request.recipient, mono:true},
        ] : []),
      ],
      note:'钱包只确认冻结后的公开交易；控制台不接收私钥或签名内容。',
    }) : '';
    const notiltBoundary = notiltPreview ? renderCapitalHandoffCard({
      eyebrow:'钱包确认',
      title:safeOutbound ? 'NoTilt 释放请求已构建' : 'NoTilt 入金序列已构建',
      facts:[
        {label:'签名账户', value:notiltPreview.wallet_address || '连接钱包', mono:true},
        {label:'交易数', value:String(Number(notiltPreview.transactions?.length || 0))},
      ],
      note:safeOutbound ? '释放请求确认后校验公开回执；协议解锁后再由钱包执行。' : '固定交易顺序逐笔由钱包核对。',
    }) : '';
    const walletBoundary = hyperliquidPreview ? renderCapitalHandoffCard({
      eyebrow:'钱包确认',
      title:hyperliquidPreview.artifact.kind === 'HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST'
        ? 'Hyperliquid CCTP 提现（0.2 USDC）'
        : hyperliquidPreview.artifact.kind === 'HYPERLIQUID_WITHDRAW3_TYPED_REQUEST'
          ? 'Hyperliquid Bridge 提现（1 USDC）'
          : hyperliquidPreview.artifact.kind === 'HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST'
            ? 'Hyperliquid 主账户资金归集'
            : 'Hyperliquid Arbitrum 入金',
      facts:[
        {label:'金额', value:`${hyperliquidPreview.artifact.amount} USDC`},
        {label:'账户', value:hyperliquidPreview.artifact.account || hyperliquidPreview.artifact.from || '', mono:true},
        ...(hyperliquidPreview.artifact.action?.token ? [{label:'协议资产', value:hyperliquidPreview.artifact.action.token, mono:true}] : []),
      ],
      note:'主钱包或有效多签只需在钱包中核对 EIP-712 或链上交易。',
    }) : '';
    const treasuryWallet = operation.path === 'HYPERLIQUID_TO_VAULT' && providerPreviewReady ? renderCapitalHandoffCard({
      eyebrow:'最终入账',
      title:`存入 ${operation.treasury_provider === 'SAFE_SPENDING_LIMIT' ? 'Safe' : 'NoTilt Vault'}`,
      facts:[{label:'到账状态', value:treasuryReceiptConfirmed ? '链上金库到账已验证' : '等待公开回执'}],
      note:'链上确认后自动更新，无需在回执区再次操作。',
    }) : '';
    const operationAssurance = renderCapitalOperationAssurance({
      operationId:operation.operation_id,
      operationLabel:label,
      amount:operation.amount,
      asset:operation.asset,
      blockers,
      pendingPreflights,
      hardBlockers,
      handoffCards:[safeBoundary, notiltBoundary, walletBoundary, binanceBoundary, treasuryWallet],
    });
    const chainConfirmed = frozenStages.some(stage => stage.status === 'CONFIRMED' && (stage.code.includes('ARBITRUM') || stage.code === 'TREASURY_WITHDRAWAL_RECEIPT_CONFIRMED' || stage.code.startsWith('BINANCE_') && stage.code.endsWith('_RECEIPT_CONFIRMED')));
    const chainSubmitted = frozenStages.some(stage => stage.transaction_hash || stage.submission?.transactionHash
      || String(stage.code || '').endsWith('_SUBMITTED_BY_HUMAN_WALLET'));
    const operationStatus = operation.status === 'SETTLED'
      ? fmtStatus(operation.status)
      : chainConfirmed
        ? '链上已确认'
        : chainSubmitted && operation.status === 'AWAITING_RECEIPT'
          ? '链上确认中'
          : operation.status === 'BLOCKED' && pendingPreflights.length && !hardBlockers.length
            ? '等待安全预检'
            : fmtStatus(operation.status);
    const receiptStatus = operation.status === 'SETTLED' ? fmtStatus(operation.receipt_status) : chainConfirmed ? '链上已确认，后续入账处理中' : chainSubmitted ? '链上确认中' : fmtStatus(operation.receipt_status);
    return `<tr><td data-label="操作">${shortId(operation.operation_id)}<br><span class="subtle">${fmtDate(operation.final_confirmed_at)}</span></td><td data-label="路径 / 金额"><b>${escapeHtml(label)}</b><br><span class="subtle">${operation.treasury_provider === 'SAFE_SPENDING_LIMIT' ? 'Safe Spending Limits' : 'NoTilt Vault'}</span><br><span class="subtle">${fmtNumber(operation.amount)} ${escapeHtml(operation.asset)}</span></td><td data-label="阶段">${escapeHtml(stages || '尚无阶段')}</td><td data-label="状态 / 回执"><b>${escapeHtml(operationStatus)}</b><br><span class="subtle">回执：${escapeHtml(receiptStatus)}</span></td><td data-label="校验 / 签名">${operationAssurance}</td></tr>`;
  }).join('');
  const directOperationPagination = directOperationTotal ? `<nav class="capital-operation-pagination" aria-label="操作与回执分页"><span>第 ${capitalOperationsPage} / ${directOperationTotalPages} 页 · 共 ${directOperationTotal} 条</span><div><label>每页<select data-capital-operation-page-size aria-label="每页记录数"><option value="50" ${capitalOperationsPageSize === 50 ? 'selected' : ''}>50</option><option value="100" ${capitalOperationsPageSize === 100 ? 'selected' : ''}>100</option></select>条</label><button class="secondary" type="button" data-capital-operation-page="${capitalOperationsPage - 1}" ${capitalOperationsPage <= 1 ? 'disabled' : ''}>上一页</button><button class="secondary" type="button" data-capital-operation-page="${capitalOperationsPage + 1}" ${capitalOperationsPage >= directOperationTotalPages ? 'disabled' : ''}>下一页</button></div></nav>` : '';
  const legacyRows = transfers.live.map(transfer => `<tr><td data-label="记录">${shortId(transfer.capital_transfer_id)}</td><td data-label="方向">${escapeHtml(fmtCapitalDirection(transfer.direction))}</td><td data-label="金额">${fmtNumber(transfer.gross_amount)} ${escapeHtml(transfer.asset)}</td><td data-label="状态">${escapeHtml(fmtStatus(transfer.status))}</td><td data-label="外部回执">${escapeHtml(transfer.external_transfer_id || '未提交')}</td></tr>`).join('');
  const selectedProviderReady = selectedTreasuryProvider === 'SAFE_SPENDING_LIMIT'
    ? directConfiguration.safe_spending_scope_configured && item.net_worth?.onchain_probe?.status === 'SUCCESS'
    : directConfiguration.notilt_scope_configured;
  const selectedProviderStatus = selectedProviderReady
    ? '已配置'
    : selectedTreasuryProvider === 'SAFE_SPENDING_LIMIT'
      ? '缺少 Safe、delegate 或可信 RPC'
      : '缺少官方金库或 Agent 范围';
  const vaultReady = Boolean(directConfiguration.notilt_scope_configured);
  const safeProbe = item.net_worth?.onchain_probe?.provider === 'SAFE_SPENDING_LIMIT' ? item.net_worth.onchain_probe : {};
  const safeReady = Boolean(directConfiguration.safe_spending_scope_configured && directConfiguration.safe_gateway_available && safeProbe.status === 'SUCCESS');
  const safeHyperliquidReady = safeReady && Number(safeProbe.available_limit || 0) >= 5 && Number(safeProbe.balance || 0) >= 5;
  const safeReadinessNote = safeReady && !safeHyperliquidReady
    ? `<p class="callout is-warning"><b>Hyperliquid 入金额度不足</b><span>当前 Safe 可用额度 ${escapeHtml(safeProbe.available_limit || '0')} USDC、余额 ${escapeHtml(safeProbe.balance || '0')} USDC；Hyperliquid Arbitrum 入金最低为 5 USDC。请先在 Safe Allowance Module 提升 delegate 可用额度。</span></p>`
    : '';
  const providerCards = `<div class="capital-provider-grid">
    <article class="card capital-provider-card ${selectedTreasuryProvider === 'NOTILT_VAULT' ? 'is-selected' : ''}"><div class="card-heading"><div><p class="eyebrow">生产链上金库</p><h2>Vault</h2></div><span class="status-pill ${vaultReady ? 'status-APPROVED' : 'status-DISABLED'}">${vaultReady ? '已配置' : '待配置'}</span></div><p>NoTilt Vault 用于受控资金保管与固定路径划转。页面只显示配置状态，不回显合约地址、Agent 范围或凭据。</p><dl class="definition-grid">${definition('当前使用', selectedTreasuryProvider === 'NOTILT_VAULT' ? '是' : '否')}${definition('官方 SDK', directConfiguration.notilt_sdk_available ? '可用' : '未就绪')}${definition('Vault 范围', vaultReady ? '已验证' : '不完整')}${definition('签名 / 广播', '始终由人控钱包或有效多签确认')}</dl></article>
    <article class="card capital-provider-card ${selectedTreasuryProvider === 'SAFE_SPENDING_LIMIT' ? 'is-selected' : ''}"><div class="card-heading"><div><p class="eyebrow">生产多签金库</p><h2>Safe</h2></div><span class="status-pill ${safeReady ? 'status-APPROVED' : 'status-DISABLED'}">${safeReady ? '实时可读' : '待配置'}</span></div><p>Safe Spending Limits 使用 Safe、delegate 与可信 RPC 范围。控制台只读取额度与回执，不接收或保存钱包签名。</p><dl class="definition-grid">${definition('当前使用', selectedTreasuryProvider === 'SAFE_SPENDING_LIMIT' ? '是' : '否')}${definition('Safe Gateway', directConfiguration.safe_gateway_available ? '可用' : '未就绪')}${definition('Safe / delegate', directConfiguration.safe_address_configured && directConfiguration.safe_delegate_configured ? '已配置' : '不完整')}${definition('Spending Limit', safeReady ? `${safeProbe.available_limit} USDC 可用` : safeProbe.error_code ? formatDirectCapitalBlocker(safeProbe.error_code) : '尚未就绪')}${definition('Safe 余额', safeReady ? `${safeProbe.balance} USDC` : '尚未读取')}${definition('重置周期 / nonce', safeReady ? `${safeProbe.reset_time_minutes} 分钟 / ${safeProbe.nonce}` : '尚未读取')}</dl>${safeReadinessNote}</article>
  </div>`;
  const capitalNetworkScope = `<dl class="capital-network-scope" aria-label="资金网络与结算资产"><div><dt>资金网络</dt><dd>Arbitrum One（Chain ID 42161）</dd></div><div><dt>结算资产</dt><dd>原生 USDC</dd></div><div class="capital-network-scope-note"><dt>支持范围</dt><dd>当前默认仅支持 Arbitrum One 原生 USDC，不支持 USDT、其他网络资产或跨链版本 USDC。</dd></div></dl>`;
  const liveContent = `<section class="capital-provider-section"><div class="section-heading"><div><p class="eyebrow">Vault / Safe</p><h2>生产资金保管</h2><p>资金中心只保留 Vault、Safe 和相关受控资金路径；账户汇总与净值曲线已移至绩效报表。</p></div><a class="secondary" href="/results" data-link>查看绩效曲线</a></div>${capitalNetworkScope}${providerCards}${directConfigurationEditor}</section>
    <section class="capital-routes-section"><div class="card-heading"><div><p class="eyebrow">受控资金路径</p><h2>Vault / Safe 与交易所</h2><p class="subtle">先选路径，再填写金额；每次都会重新校验地址、网络、资产、额度、实时状态和安全开关。</p></div><span class="status-pill ${item.real_transfer_gate === 'ENABLED' ? 'status-APPROVED' : 'status-DISABLED'}">${escapeHtml(fmtStatus(item.real_transfer_gate || 'DISABLED'))}</span></div>${renderDirectCapitalLiveProgress(item.direct_operations)}<div class="callout direct-capital-boundary" data-provider-boundary><b>当前提供方：${escapeHtml(selectedTreasuryProviderLabel)}。</b> 链上私钥不进入控制台；真实币安提现继续受专用开关和 CAPITAL_TRANSFER 双重门禁。</div><div class="capital-route-grid">${directPathCards}</div></section>${directCapitalDialog}${capitalAssuranceDialog}
    <details class="capital-activity-disclosure" open><summary><span>操作与回执</span><small>${directOperationTotal} 条直达操作</small></summary><div><section><div class="capital-operation-heading"><div><h2>操作日志、阶段与回执</h2><p>提交、确认与入账状态会自动核对；回执区仅用于查看，不再承载二次操作。</p></div></div>${directRows ? `<div class="table-wrap is-scrollable capital-operation-table"><table><thead><tr><th>操作</th><th>路径 / 金额</th><th>阶段</th><th>状态 / 回执</th><th>校验 / 签名</th></tr></thead><tbody>${directRows}</tbody></table></div>${directOperationPagination}` : '<div class="callout">尚无直达资金操作。</div>'}</section>${legacyRows ? `<section><h2>历史资金划转</h2><div class="table-wrap is-scrollable capital-history-table"><table><thead><tr><th>记录</th><th>方向</th><th>金额</th><th>状态</th><th>外部回执</th></tr></thead><tbody>${legacyRows}</tbody></table></div></section>` : ''}</div></details>`;
  const testnetContent = `<section class="empty-state compact-empty capital-testnet-empty"><div><p class="eyebrow">生产专属</p><h2>Vault 与 Safe 不适用于测试模式</h2><p>测试资产、交易所账户汇总与净值曲线请在绩效报表查看；测试模式不显示生产资金路径。</p><div class="toolbar empty-actions"><a class="primary" href="/results" data-link>前往绩效报表</a>${hasCapability('venue.view') ? '<button class="secondary" type="button" data-open-mode-switch>查看当前模式</button>' : ''}</div></div></section>`;
  main.innerHTML = `<section class="page capital-page"><header class="page-head capital-page-head"><div><p class="eyebrow">${fmtExecutionMode(displayEnvironment)} · 资金保管</p><h1>资金中心</h1><p class="lede">集中查看 Vault、Safe 与生产资金路径；资金账户汇总和曲线统一在绩效报表展示。</p></div>${displayEnvironment === 'LIVE' ? `<div class="capital-gate-summary"><small>生产资金操作</small><b>${escapeHtml(fmtStatus(item.real_transfer_gate || 'DISABLED'))}</b><span>在途 / 占用 ${fmtNumber(liveInTransit)} USDC</span></div>` : ''}</header>${displayEnvironment === 'LIVE' ? liveContent : testnetContent}</section>`;
  bindCapitalActions();
  if (displayEnvironment === 'LIVE') {
    reconcilePendingBinanceReceipts(item.direct_operations);
    reconcilePendingHyperliquidWithdrawals(item.direct_operations);
    reconcilePendingSafeHyperliquidDeposits(item.direct_operations);
    reconcilePendingHyperliquidDeposits(item.direct_operations);
  }
}

function drawCapitalChart(series) {
  const canvas = document.querySelector('#capital-chart');
  if (!canvas) return;
  const allPoints = series.flatMap(item => item.points);
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = Math.max(1, canvas.clientHeight || (width < 520 ? 300 : 420));
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
  const colors = ['--chart-source-1','--chart-source-2','--chart-source-3','--chart-source-4','--chart-source-5','--chart-source-6','--chart-total'].map(name => styles.getPropertyValue(name).trim());
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
    const colorIndex = capitalSeriesColorIndex[item.source];
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

function bindCapitalPerformanceChart(historySeries = [], rangeContext = null) {
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
  capitalChartOverlayAbortController?.abort();
  capitalChartOverlayAbortController = new AbortController();
  const chartPanel = document.querySelector('.capital-chart-panel');
  const expandButton = document.querySelector('[data-capital-chart-expand]');
  const setChartExpanded = expanded => {
    if (!chartPanel || !expandButton) return;
    chartPanel.classList.toggle('is-expanded', expanded);
    document.body.classList.toggle('capital-chart-expanded', expanded);
    expandButton.setAttribute('aria-pressed', String(expanded));
    expandButton.setAttribute('aria-label', localizedText(expanded ? '退出资金曲线全屏' : '放大资金曲线'));
    const label = expandButton.querySelector('[data-expand-label]');
    if (label) label.textContent = localizedText(expanded ? '退出全屏' : '全屏查看');
    requestAnimationFrame(redrawCapitalHistory);
  };
  expandButton?.addEventListener('click', () => setChartExpanded(!chartPanel.classList.contains('is-expanded')));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && chartPanel?.classList.contains('is-expanded')) setChartExpanded(false);
  }, {signal:capitalChartOverlayAbortController.signal});
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
}

function bindCapitalActions() {
  const assuranceDialog = document.querySelector('#capital-assurance-dialog');
  const closeAssuranceDialog = () => assuranceDialog?.close();
  document.querySelectorAll('[data-capital-assurance-record]').forEach(button => button.addEventListener('click', event => {
    const record = directCapitalAssuranceRecords.get(event.currentTarget.dataset.capitalAssuranceRecord);
    if (!record || !assuranceDialog) return;
    assuranceDialog.querySelector('[data-capital-assurance-title]').textContent = record.title;
    assuranceDialog.querySelector('[data-capital-assurance-meta]').textContent = record.meta;
    const status = assuranceDialog.querySelector('[data-capital-assurance-status]');
    status.className = `capital-assurance-dialog-status ${record.healthClass}`;
    status.innerHTML = `<span class="capital-assurance-state">${escapeHtml(record.healthLabel)}</span><p>${escapeHtml(record.summaryCopy)}</p>`;
    assuranceDialog.querySelector('[data-capital-assurance-content]').innerHTML = record.body;
    assuranceDialog.showModal();
  }));
  document.querySelectorAll('[data-close-capital-assurance]').forEach(button => button.addEventListener('click', closeAssuranceDialog));
  assuranceDialog?.addEventListener('click', event => {
    if (event.target === assuranceDialog) closeAssuranceDialog();
  });
  document.querySelectorAll('[data-capital-operation-page]').forEach(button => button.addEventListener('click', async event => {
    const page = Number(event.currentTarget.dataset.capitalOperationPage);
    if (!Number.isInteger(page) || page < 1 || page === capitalOperationsPage) return;
    capitalOperationsPage = page;
    await route();
    document.querySelector('.capital-activity-disclosure')?.scrollIntoView({block:'start'});
  }));
  document.querySelector('[data-capital-operation-page-size]')?.addEventListener('change', async event => {
    const requested = Number(event.currentTarget.value);
    capitalOperationsPageSize = requested === 100 ? 100 : 50;
    capitalOperationsPage = 1;
    await route();
    document.querySelector('.capital-activity-disclosure')?.scrollIntoView({block:'start'});
  });
  document.querySelectorAll('[data-direct-capital-start]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    await withPending(target, target.dataset.operationPath === 'BINANCE_TO_VAULT' ? '正在提交币安提现…' : '正在唤起钱包…', async () => {
      try {
        const result = await startDirectCapitalExecution({
          operationId:target.dataset.directCapitalStart,
          version:Number(target.dataset.operationVersion),
          path:target.dataset.operationPath,
          treasuryProvider:target.dataset.treasuryProvider,
        });
        showToast(directCapitalExecutionSuccessMessage(result));
        await refreshCapitalPage();
      } catch (error) { showApiError(error); await refreshCapitalPage(); }
    });
  }));
  document.querySelectorAll('[data-direct-wallet-action]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    const action = directCapitalWalletActions.get(target.dataset.directWalletAction);
    if (!action) return;
    await withPending(target, '正在唤起钱包…', async () => {
      try {
        const refreshedAction = action.stage === 'NOTILT_RELEASE_EXECUTION'
          ? await prepareNoTiltReleaseExecution({operationId:action.operationId, version:action.version})
          : action.stage === 'NOTILT_DESTINATION_TRANSFER'
            ? await prepareNoTiltDestinationTransfer({operationId:action.operationId, version:action.version})
            : action.kind === 'HYPERLIQUID'
              ? await prepareHyperliquidWalletAction({operationId:action.operationId, version:action.version})
              : await prepareTreasuryWalletAction({
              operationId:action.operationId,
              version:action.version,
              path:action.path,
              treasuryProvider:action.treasuryProvider,
            });
        await executeDirectWalletAction(refreshedAction);
        showToast('钱包已提交，公开交易信息已自动记录');
        await refreshCapitalPage();
      } catch (error) { showApiError(error); await refreshCapitalPage(); }
    });
  }));
  document.querySelectorAll('[data-direct-capital-next]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    await withPending(target, '正在唤起钱包…', async () => {
      try {
        const action = target.dataset.nextAction === 'HYPERLIQUID'
          ? await prepareHyperliquidWalletAction({operationId:target.dataset.directCapitalNext, version:Number(target.dataset.operationVersion)})
          : target.dataset.nextAction === 'NOTILT_EXECUTION'
            ? await prepareNoTiltReleaseExecution({operationId:target.dataset.directCapitalNext, version:Number(target.dataset.operationVersion)})
            : target.dataset.nextAction === 'NOTILT_DESTINATION'
              ? await prepareNoTiltDestinationTransfer({operationId:target.dataset.directCapitalNext, version:Number(target.dataset.operationVersion)})
              : await prepareTreasuryWalletAction({
              operationId:target.dataset.directCapitalNext,
              version:Number(target.dataset.operationVersion),
              path:target.dataset.operationPath,
              treasuryProvider:target.dataset.treasuryProvider,
            });
        await executeDirectWalletAction(action);
        showToast('钱包已提交，公开交易信息已自动记录');
        await refreshCapitalPage();
      } catch (error) { showApiError(error); await refreshCapitalPage(); }
    });
  }));
  document.querySelectorAll('[data-binance-submit-direct]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    await withPending(target, '正在提交币安提现…', async () => {
      try {
        await api(`/api/capital/direct-operations/${target.dataset.binanceSubmitDirect}/binance-submit`, {
          method:'POST',
          body:JSON.stringify({expected_version:Number(target.dataset.operationVersion), final_confirmed:true, confirmation_phrase:'CONFIRM_BINANCE_WITHDRAWAL', idempotency_key:crypto.randomUUID()}),
        });
        showToast('币安提现已直接提交，正在等待币安与 Arbitrum 回执');
        await refreshCapitalPage();
      } catch (error) { showApiError(error); }
    });
  }));
  document.querySelectorAll('[data-notilt-receipt-direct]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    await withPending(target, '验证 NoTilt 公开回执…', async () => {
      try {
        await api(`/api/capital/direct-operations/${target.dataset.notiltReceiptDirect}/notilt-release-receipt`, {
          method:'POST',
          body:JSON.stringify({expected_version:Number(target.dataset.operationVersion), transaction_hash:target.dataset.transactionHash, idempotency_key:crypto.randomUUID()}),
        });
        showToast('NoTilt 公开回执已验证，下一步会在协议允许时直接打开钱包');
        await refreshCapitalPage();
      } catch (error) { showApiError(error); }
    });
  }));
  document.querySelectorAll('[data-treasury-withdrawal-receipt-direct]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    await withPending(target, '验证 Safe 转出到账…', async () => {
      try {
        await api(`/api/capital/direct-operations/${target.dataset.treasuryWithdrawalReceiptDirect}/treasury-withdrawal-receipt`, {
          method:'POST',
          body:JSON.stringify({expected_version:Number(target.dataset.operationVersion), transaction_hash:target.dataset.transactionHash, idempotency_key:crypto.randomUUID()}),
        });
        showToast('Safe 转出公开回执已验证，可以继续目标入金');
        await refreshCapitalPage();
      } catch (error) { showApiError(error); }
    });
  }));
  document.querySelectorAll('[data-binance-receipt-direct]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    await withPending(target, '自动核对到账…', async () => {
      try {
        const receipt = await verifyBinanceReceipt({
          operationId:target.dataset.binanceReceiptDirect,
          version:Number(target.dataset.operationVersion),
          stage:target.dataset.binanceStage,
          transactionHash:target.dataset.transactionHash,
        });
        showToast(receipt.pending ? '币安回执仍在确认，刷新页面后会继续核对' : '币安与链上公开回执已核对');
        await refreshCapitalPage();
      } catch (error) { showApiError(error); }
    });
  }));
  document.querySelectorAll('[data-hl-receipt-direct]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    await withPending(target, '自动读取公开回执…', async () => {
      try {
        const operationId = target.dataset.hlReceiptDirect;
        const walletStage = target.dataset.walletStage;
        let version = Number(target.dataset.operationVersion);
        if (walletStage === 'HYPERLIQUID_DEPOSIT') {
          const transactionHash = target.dataset.transactionHash;
          const chain = await api(`/api/capital/direct-operations/${operationId}/hyperliquid-receipt`, {method:'POST', body:JSON.stringify({expected_version:version, stage:'HYPERLIQUID_DEPOSIT_ARBITRUM', transaction_hash:transactionHash, idempotency_key:crypto.randomUUID()})});
          version = Number(chain.version);
          await api(`/api/capital/direct-operations/${operationId}/hyperliquid-receipt`, {method:'POST', body:JSON.stringify({expected_version:version, stage:'HYPERLIQUID_DEPOSIT_LEDGER', action_hash:transactionHash, idempotency_key:crypto.randomUUID()})});
        } else {
          const ledgerStage = walletStage === 'HYPERLIQUID_CLASS_TRANSFER' ? 'HYPERLIQUID_CLASS_TRANSFER_LEDGER' : 'HYPERLIQUID_WITHDRAWAL_LEDGER';
          const payload = {expected_version:version, stage:ledgerStage, nonce:Number(target.dataset.nonce), idempotency_key:crypto.randomUUID()};
          if (target.dataset.actionHash) payload.action_hash = target.dataset.actionHash;
          const ledger = await api(`/api/capital/direct-operations/${operationId}/hyperliquid-receipt`, {method:'POST', body:JSON.stringify(payload)});
          version = Number(ledger.version);
          if (walletStage === 'HYPERLIQUID_WITHDRAWAL' && /^0x[0-9a-fA-F]{64}$/.test(String(ledger.receipt?.hash || ''))) {
            await api(`/api/capital/direct-operations/${operationId}/hyperliquid-receipt`, {method:'POST', body:JSON.stringify({expected_version:version, stage:'HYPERLIQUID_WITHDRAWAL_ARBITRUM', transaction_hash:ledger.receipt.hash, idempotency_key:crypto.randomUUID()})});
          }
        }
        showToast('Hyperliquid 与链上公开回执已自动读取');
        await refreshCapitalPage();
      } catch (error) { showApiError(error); }
    });
  }));
  document.querySelectorAll('[data-treasury-receipt-direct]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    await withPending(target, '自动验证金库到账…', async () => {
      try {
        await api(`/api/capital/direct-operations/${target.dataset.treasuryReceiptDirect}/treasury-receipt`, {method:'POST', body:JSON.stringify({expected_version:Number(target.dataset.operationVersion), transaction_hash:target.dataset.transactionHash, idempotency_key:crypto.randomUUID()})});
        showToast('链上金库到账已自动验证');
        await refreshCapitalPage();
      } catch (error) { showApiError(error); }
    });
  }));
  const directCapitalConfigForm = document.querySelector('#direct-capital-config-form');
  const syncTreasuryProviderFields = () => {
    if (!directCapitalConfigForm) return;
    const provider = directCapitalConfigForm.elements.treasury_provider.value;
    directCapitalConfigForm.querySelectorAll('[data-provider-fields]').forEach(section => {
      const active = section.dataset.providerFields === provider;
      section.classList.toggle('is-selected-provider', active);
      const role = section.querySelector('[data-provider-role]');
      if (role) role.textContent = active ? '当前使用' : '已接入备用';
    });
    const guidance = directCapitalConfigForm.querySelector('[data-provider-guidance]');
    if (guidance) guidance.textContent = `当前使用 ${provider === 'SAFE_SPENDING_LIMIT' ? 'Safe Spending Limits' : 'NoTilt Vault'}；NoTilt 与 Safe 的配置都会独立保存，未选中的金库不会参与新建资金操作。`;
    const withdrawalScopeHelp = directCapitalConfigForm.querySelector('[data-withdrawal-scope-help]');
    if (withdrawalScopeHelp) withdrawalScopeHelp.textContent = localizedText(provider === 'SAFE_SPENDING_LIMIT'
      ? '必须与当前 Safe Smart Account 完全一致'
      : '必须与当前 NoTilt 金库地址完全一致');
    const boundary = document.querySelector('[data-provider-boundary]');
    if (boundary) boundary.innerHTML = `<b>当前使用：${provider === 'SAFE_SPENDING_LIMIT' ? 'Safe Spending Limits' : 'NoTilt Vault'}。</b> 两种金库可同时保持接入；每条新路径只冻结当前金库，并重新校验地址、网络、资产、额度、实时状态与安全开关，且不在服务内签名或广播。`;
  };
  directCapitalConfigForm?.querySelectorAll('input[name="treasury_provider"]').forEach(input => input.addEventListener('change', syncTreasuryProviderFields));
  syncTreasuryProviderFields();
  directCapitalConfigForm?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries([...new FormData(form).entries()].filter(([, value]) => String(value).trim()));
    if (form.elements.clear_notilt_configuration) values.clear_notilt_configuration = form.elements.clear_notilt_configuration.checked;
    if (form.elements.clear_safe_configuration) values.clear_safe_configuration = form.elements.clear_safe_configuration.checked;
    const errorBox = form.querySelector('.form-error');
    errorBox.textContent = '';
    const maxAmount = values.max_amount === undefined ? null : Number(values.max_amount);
    const maxFee = values.max_fee === undefined ? null : Number(values.max_fee);
    if (maxAmount !== null && maxFee !== null && maxFee >= maxAmount) {
      errorBox.textContent = localizedText('最大费用上限必须低于单次金额上限，请调整后重新保存。');
      errorBox.focus();
      return;
    }
    const selectedTreasuryAddress = values.treasury_provider === 'SAFE_SPENDING_LIMIT'
      ? values.safe_address
      : values.vault_address;
    if (selectedTreasuryAddress && values.binance_withdrawal_address
      && String(selectedTreasuryAddress).toLowerCase() !== String(values.binance_withdrawal_address).toLowerCase()) {
      errorBox.textContent = localizedText(values.treasury_provider === 'SAFE_SPENDING_LIMIT'
        ? '币安受限提现地址必须与当前 Safe Smart Account 完全一致，请核对后重新保存。'
        : '币安受限提现地址必须与当前 NoTilt 金库地址完全一致，请核对后重新保存。');
      errorBox.focus();
      return;
    }
    values.network = 'ARBITRUM'; values.asset = 'USDC'; values.idempotency_key = crypto.randomUUID();
    const confirmed = await confirmAction({title:'保存新的资金路径配置版本？', message:'系统只保存公开账户范围、地址和额度，不接收任何密钥或签名材料。新版本只影响之后创建的资金操作；现有操作继续使用冻结引用。', confirmLabel:'确认保存配置'});
    if (!confirmed) return;
    await withPending(event.submitter, '保存中…', async () => {
      try {
        await api('/api/capital/direct-configuration', {method:'PUT', body:JSON.stringify(values)});
        showToast('资金路径配置新版本已保存并写入审计');
        await route();
      } catch (error) { showApiError(error, errorBox); errorBox.focus(); }
    });
  });
  const directCapitalDialog = document.querySelector('#direct-capital-dialog');
  const directCapitalForm = document.querySelector('#direct-capital-form');
  document.querySelectorAll('[data-open-capital-path]').forEach(button => button.addEventListener('click', async event => {
    const target = event.currentTarget;
    if (target.dataset.resumeCapitalOperation) {
      await withPending(target, '正在核对链上回执…', async () => {
        try {
          const result = await continueSafeOutboundExecution({
            operationId:target.dataset.resumeCapitalOperation,
            version:Number(target.dataset.operationVersion),
            path:target.dataset.openCapitalPath,
            transactionHash:target.dataset.transactionHash,
          });
          showToast(result.receiptPending ? '交易已提交，链上或 Hyperliquid 公开回执仍在确认' : '链上回执已确认，资金路径已完成');
          await refreshCapitalPage();
        } catch (error) { showApiError(error); await refreshCapitalPage(); }
      });
      return;
    }
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
    directCapitalDialog?.close();
    const button = currentForm.querySelector('button[type="submit"], button:not([type])');
    let operationCreated = false;
    await withPending(button, '正在安全校验…', async () => {
      try {
        const result = await api('/api/capital/direct-operations', {
          method:'POST',
          body:JSON.stringify({path, amount, final_confirmed:true, idempotency_key:crypto.randomUUID()}),
        });
        operationCreated = true;
        const operation = (result.data?.direct_operations || []).find(item => item.operation_id === result.operation_id);
        if (!operation) throw new Error('新建资金操作未返回可继续执行的版本。');
        const execution = await startDirectCapitalExecution({
          operationId:result.operation_id,
          version:operation.version,
          path,
          treasuryProvider,
        });
        showToast(directCapitalExecutionSuccessMessage(execution));
        await refreshCapitalPage();
      } catch (error) {
        if (operationCreated) {
          showApiError(error);
          await refreshCapitalPage();
        } else {
          showApiError(error, currentForm.querySelector('.form-error'));
          directCapitalDialog?.showModal();
        }
      }
    });
  }));
}
