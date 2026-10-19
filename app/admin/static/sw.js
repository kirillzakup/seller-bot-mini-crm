// Seller-bot service worker: enables system notifications (showNotification) and
// installability (PWA). Served from "/" so its scope covers /admin.
// Web Push (notifications when the browser is fully closed) can be added here later.

self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

// Web Push: fires even when the browser/PWA is closed (Android; iOS installed PWA).
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) {}
  e.waitUntil(
    self.registration.showNotification(d.title || 'Новое сообщение', {
      body: d.body || '',
      tag: 'sellerbot-push',
      renotify: true,
      icon: '/static/icon.svg',
      badge: '/static/icon.svg',
      data: { url: d.url || '/admin/dialogs' },
    })
  );
});

// Focus the admin tab (or open Диалоги) when a notification is tapped.
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/admin/dialogs';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((cs) => {
      for (const c of cs) {
        if (c.url.includes('/admin') && 'focus' in c) return c.focus();
      }
      return self.clients.openWindow ? self.clients.openWindow(url) : undefined;
    })
  );
});
