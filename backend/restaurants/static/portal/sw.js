// Concierge AI — Service Worker
// Served from / via Django view (push_service_worker) with Service-Worker-Allowed: /
// Scope: "/" — controls every page on the origin.

const VERSION = "v1";

// === Lifecycle ===

self.addEventListener("install", (event) => {
  // Take over immediately on first install — no reload needed
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // Claim all open tabs so they're controlled without reload
  event.waitUntil(self.clients.claim());
});

// === Push received ===

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: "Concierge AI", body: (event.data && event.data.text()) || "" };
  }

  const {
    title   = "Concierge AI",
    body    = "",
    url     = "/",
    tag     = "concierge",
    urgency = "normal",          // "low" | "normal" | "high"
    actions = [],                // Chrome inline buttons; iOS ignores
    image,                       // Chrome hero image; Safari ignores
  } = data;

  // Vibration patterns per urgency (Android only — Mac/Desktop ignore)
  const vibratePatterns = {
    low:    [],                          // silent
    normal: [200, 100, 200],
    high:   [300, 200, 300, 200, 300],   // distinct, urgent
  };

  const options = {
    body,
    icon:    "/static/portal/icon-192.png",   // falls back to default if missing
    badge:   "/static/portal/badge-72.png",
    tag,                                       // duplicates with same tag REPLACE, don't stack
    renotify: urgency === "high",              // re-alert even if a notification with same tag exists
    requireInteraction: urgency === "high",    // complaints stay on screen until tapped
    silent: urgency === "low",                 // low-priority doesn't make sound
    vibrate: vibratePatterns[urgency] || vibratePatterns.normal,
    actions: actions.slice(0, 2),              // browser caps actions at 2 anyway
    image,
    timestamp: Date.now(),
    data: { url, urgency },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

// === Click on notification (or inline action) ===

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const baseUrl = (event.notification.data && event.notification.data.url) || "/";
  const action  = event.action;   // "" | "confirm" | "decline" | "callback"

  // Inline action → route to /respond/ endpoint of the same one-tap page
  // Stage E will hook server-side handlers; for now we just open the page.
  // Note: we DON'T fetch from inside the SW — iOS 3-second limit kills it.
  const target = action ? `${baseUrl}?action=${encodeURIComponent(action)}` : baseUrl;

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      // If a window matching the URL is already open — focus it
      for (const w of wins) {
        try {
          const u = new URL(w.url);
          const t = new URL(target, self.location.origin);
          if (u.pathname === t.pathname && "focus" in w) {
            return w.focus().then(() => w.navigate ? w.navigate(target) : w.focus());
          }
        } catch (_) {}
      }
      return self.clients.openWindow(target);
    })
  );
});
