// Trash Tinder service worker — minimal app-shell cache for offline installability,
// plus Web Push handlers for new-item and deadline-warning notifications.
// The real data is always fetched live; we just cache the static shell so the app
// still opens when reception is flaky.

const CACHE = 'trash-tinder-v2';
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

// ---------- Web Push ----------

self.addEventListener('push', event => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'Trash Tinder', body: event.data ? event.data.text() : '' };
  }
  const title = data.title || 'Trash Tinder';
  const opts = {
    body: data.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    data: {
      url: data.url || '/',
      item_id: data.item_id || null,
      household_id: data.household_id || null,
      kind: data.kind || null,
    },
    tag: data.kind === 'deadline_warning' && data.item_id ? ('deadline-' + data.item_id) : undefined,
  };
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const data = event.notification.data || {};
  const householdId = data.household_id || null;
  const fallbackUrl = householdId ? ('/?household=' + encodeURIComponent(householdId)) : (data.url || '/');
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async list => {
      for (const client of list) {
        try {
          const u = new URL(client.url);
          if (u.origin === self.location.origin) {
            if (householdId) {
              client.postMessage({ type: 'switch_household', household_id: householdId });
            }
            return client.focus();
          }
        } catch (e) { /* ignore */ }
      }
      return self.clients.openWindow(fallbackUrl);
    })
  );
});
