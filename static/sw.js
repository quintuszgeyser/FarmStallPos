// Simple service worker with versioned cache
const CACHE_NAME = 'pos-cache-v2';
const ASSETS = [
  '/',
  '/templates/index.html',
  '/static/main.js',
  '/static/manifest.json'
];
self.addEventListener('install', (evt) => {
  evt.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS)));
});
self.addEventListener('activate', (evt) => {
  evt.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))));
});
self.addEventListener('fetch', (evt) => {
  evt.respondWith(caches.match(evt.request).then(resp => resp || fetch(evt.request)));
});
