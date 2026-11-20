// Push arrives without a payload; fetch the text of the latest event (decrypted here with the family key).
importScripts('/crypto.js');
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('push', (e) => {
  e.waitUntil((async () => {
    let n = { title: 'Gọi điện - Nhắn tin con', body: 'Có tin mới từ máy của con' };
    try {
      const r = await fetch('/api/notify-preview', { credentials: 'include', cache: 'no-store' });
      if (r.ok) n = await r.json();
      await KT.load();
      n.title = (await KT.decText(n.title)) ?? 'Gọi điện - Nhắn tin con';
      n.body = (await KT.decText(n.body)) ?? 'Có tin mới (đã mã hoá), mở app để xem';
    } catch {}
    const call = /^📞/.test(n.title || '');
    await self.registration.showNotification(n.title, { body: n.body, icon: '/icon-192.png', badge: '/icon-192.png', tag: call ? 'call' : 'bo', renotify: true, requireInteraction: call, vibrate: call ? [400, 200, 400, 200, 400] : undefined });
  })());
});
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    if (all.length) return all[0].focus();
    return self.clients.openWindow('/');
  })());
});
