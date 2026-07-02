
// Service worker - network first for HTML, cache for static assets only
const CACHE_NAME = 'pos-cache-v4';
const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

self.addEventListener('install', (evt) => {
  evt.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (evt) => {
  evt.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (evt) => {
  const url = new URL(evt.request.url);

  // Always fetch HTML and JS fresh from network - never cache them
  if (url.pathname === '/' || url.pathname.endsWith('.js') || url.pathname.endsWith('.html')) {
    evt.respondWith(fetch(evt.request));
    return;
  }

  // For other static assets: cache first
  evt.respondWith(
    caches.match(evt.request).then(resp => resp || fetch(evt.request))
  );
});
