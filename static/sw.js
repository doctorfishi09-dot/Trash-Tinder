// Trash Tinder service worker — minimal app-shell cache for offline installability.
// The real data is always fetched live; we just cache the static shell so the app
// still opens when reception is flaky.

const CACHE = 'trash-tinder-v1';
const SHELL = [
  '/',
  '/app.js',
  '/style.css',
  '/manifest.webmanifest',
  '/icon.svg',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  // Never cache API, SSE stream, or uploaded photos.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/photos/')) {
    return; // fall through to network
  }
  if (event.request.method !== 'GET') return;
  event.respondWith(
    caches.match(event.request).then(hit => {
      if (hit) return hit;
      return fetch(event.request).then(res => {
        if (res && res.status === 200 && SHELL.includes(url.pathname)) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(event.request, copy));
        }
        return res;
      }).catch(() => hit);
    })
  );
});
