const CACHE = 'trading-shell-v211';
const SHELL = [
  '/assets/styles-base.css',
  '/assets/styles-components.css',
  '/assets/styles-api-access.css',
  '/assets/styles-shell.css',
  '/assets/app-state.js',
  '/assets/i18n.js',
  '/assets/shared.js',
  '/assets/session.js',
  '/assets/api-client.js',
  '/assets/app-shell.js',
  '/assets/workspace.js',
  '/assets/signals.js',
  '/assets/proposals.js',
  '/assets/risk.js',
  '/assets/execution.js',
  '/assets/capital.js',
  '/assets/reporting.js',
  '/assets/accounts.js',
  '/assets/router.js',
  '/assets/app.js',
  '/assets/fonts/IBMPlexSansSC-Regular.woff2',
  '/assets/fonts/IBMPlexSansSC-SemiBold.woff2',
  '/assets/fonts/IBMPlexMono-Regular.woff2',
  '/assets/fonts/IBMPlexMono-SemiBold.woff2',
  '/assets/tradingops-logo.png',
  '/assets/tradingops-icon.svg',
  '/manifest.webmanifest',
];
self.addEventListener('install', (event) => event.waitUntil(
  caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()),
));
self.addEventListener('activate', (event) => event.waitUntil(
  caches.keys()
    .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
    .then(() => self.clients.claim()),
));
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  if (!SHELL.includes(url.pathname)) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const response = await fetch(event.request);
      if (response.ok) await cache.put(url.pathname, response.clone());
      return response;
    } catch (error) {
      const cached = await cache.match(url.pathname);
      if (cached) return cached;
      throw error;
    }
  })());
});
