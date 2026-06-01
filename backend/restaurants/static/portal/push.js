// Concierge AI — Push Notification Client
// Handles browser subscription with Safari/iOS special cases.
// Requires: window.__VAPID_PUBLIC, window.__CSRF, window.__RESTAURANT_SLUG set in template.

(function () {
  "use strict";

  // ─── Platform detection ───────────────────────────────────────────────────

  const ua = navigator.userAgent || "";
  const isIOS              = /iP(hone|ad|od)/.test(ua);
  const isIosStandalone    = isIOS && window.navigator.standalone === true;
  const isIosTab           = isIOS && !window.navigator.standalone;
  const isSafari           = /^((?!chrome|android).)*safari/i.test(ua);
  const isMacSafari        = !isIOS && isSafari;
  const supportsPush       = "Notification" in window && "PushManager" in window && "serviceWorker" in navigator;

  // iOS 16.4+ check: PWA standalone is required AND iOS version must support web push
  // Older iOS Safaris will throw silently when calling requestPermission from PWA
  function iosVersionSupportsPush() {
    if (!isIOS) return true;
    const m = ua.match(/OS (\d+)_(\d+)/);
    if (!m) return false;
    const major = parseInt(m[1], 10);
    const minor = parseInt(m[2], 10);
    return major > 16 || (major === 16 && minor >= 4);
  }

  // ─── VAPID public key → Uint8Array ────────────────────────────────────────

  function urlBase64ToUint8Array(b64) {
    const padding = "=".repeat((4 - (b64.length % 4)) % 4);
    const base64  = (b64 + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw     = atob(base64);
    const arr     = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
    return arr;
  }

  // ─── Tiny dialog helper (uses <dialog> with fallback) ─────────────────────

  function showDialog(id) {
    return new Promise((resolve) => {
      const dlg = document.getElementById(id);
      if (!dlg) return resolve(null);
      const onChoice = (e) => {
        const choice = e.target.dataset.choice;
        if (choice === undefined) return;
        cleanup();
        dlg.close();
        resolve(choice);
      };
      const onClose = () => { cleanup(); resolve(null); };
      function cleanup() {
        dlg.removeEventListener("click", onChoice);
        dlg.removeEventListener("close", onClose);
      }
      dlg.addEventListener("click", onChoice);
      dlg.addEventListener("close", onClose);
      if (typeof dlg.showModal === "function") dlg.showModal();
      else dlg.setAttribute("open", "");
    });
  }

  // ─── Public API exposed on window.Push ───────────────────────────────────

  async function subscribe() {
    if (!supportsPush) {
      alert("This browser does not support push notifications.");
      return null;
    }

    if (isIOS && !iosVersionSupportsPush()) {
      alert("iOS 16.4 or later is required for push notifications. Please update your device.");
      return null;
    }

    // iOS Safari tab → cannot subscribe; show install explainer
    if (isIosTab) {
      await showDialog("pushIosInstallDlg");
      return null;
    }

    // Safari (mac or iOS PWA) requestPermission shows the system prompt only ONCE.
    // If user picks "Don't allow" — only Settings can revert. So warn first.
    if (isSafari && Notification.permission === "default") {
      const choice = await showDialog("pushPrePermissionDlg");
      if (choice !== "yes") return null;
    }

    let perm;
    try {
      perm = await Notification.requestPermission();
    } catch (e) {
      console.warn("requestPermission failed:", e);
      perm = "denied";
    }

    if (perm === "denied") {
      await showDialog("pushDeniedDlg");
      return null;
    }
    if (perm !== "granted") return null;

    const reg = await navigator.serviceWorker.ready;
    let sub;
    try {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(window.__VAPID_PUBLIC),
      });
    } catch (e) {
      console.error("pushManager.subscribe failed:", e);
      alert("Could not subscribe — " + (e.message || e));
      return null;
    }

    const res = await fetch(`/portal/${window.__RESTAURANT_SLUG}/push/subscribe/`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": window.__CSRF },
      body: JSON.stringify({ subscription: sub.toJSON(), userAgent: navigator.userAgent }),
    });
    if (!res.ok) {
      console.error("Backend rejected subscription:", res.status);
      return null;
    }
    const json = await res.json();
    return { sub, server: json };
  }

  async function unsubscribe(endpoint) {
    // Unsubscribe in browser too (so future subscribe creates a fresh endpoint)
    try {
      const reg = await navigator.serviceWorker.ready;
      const current = await reg.pushManager.getSubscription();
      if (current && (!endpoint || current.endpoint === endpoint)) {
        await current.unsubscribe();
      }
    } catch (_) {}

    return fetch(`/portal/${window.__RESTAURANT_SLUG}/push/unsubscribe/`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": window.__CSRF },
      body: JSON.stringify(endpoint ? { endpoint } : {}),
    }).then((r) => r.ok);
  }

  async function sendTest() {
    const res = await fetch(`/portal/${window.__RESTAURANT_SLUG}/push/test/`, {
      method: "POST",
      headers: { "X-CSRFToken": window.__CSRF },
    });
    return res.ok;
  }

  function permission() {
    if (!supportsPush) return "unsupported";
    if (isIosTab) return "needs_install";
    return Notification.permission;   // "default" | "granted" | "denied"
  }

  // ─── Reactive permission state (Permissions API, 2026 standard) ─────────
  // Notification.permission is a snapshot — doesn't update if user changes
  // notification permission in the browser's 🔒 menu. Permissions API has an
  // onchange event so the UI can react live.

  let _permissionListeners = [];
  function onPermissionChange(cb) { _permissionListeners.push(cb); }

  async function _watchPermission() {
    if (!("permissions" in navigator)) return;
    try {
      const status = await navigator.permissions.query({ name: "notifications" });
      status.addEventListener("change", () => {
        _permissionListeners.forEach((cb) => {
          try { cb(status.state); } catch (e) { console.warn(e); }
        });
      });
    } catch (_) { /* unsupported in some browsers — ignore */ }
  }

  // ─── Register service worker on every portal page load ───────────────────

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "all" })
        .catch((e) => console.warn("SW registration failed:", e));
      _watchPermission();
    });
  }

  window.Push = {
    subscribe, unsubscribe, sendTest, permission, onPermissionChange,
    isIOS, isIosStandalone, isIosTab, isSafari, isMacSafari,
    iosVersionSupportsPush, supportsPush,
  };
})();
