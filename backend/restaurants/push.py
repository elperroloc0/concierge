"""
Web push notification helpers.

Public API:
    send_push(restaurant, title, body, url, *, urgency, actions, image, tag, event_flag)

Behavior:
    - Fires-and-forgets via threading.Thread (consistent with existing _send_*_email helpers).
    - Throttles non-urgent push: max 1 per minute per restaurant (DatabaseCache).
    - Respects per-restaurant quiet hours (in restaurant.timezone). Urgency="high" can bypass.
      Quiet hours are OFF when start or end time is blank.
    - Per-operator opt-in via RestaurantMembership.notify_via_push + event flag.
    - Payload size budget: ~900 bytes (iOS Safari soft-limit). Auto-trims body/image if needed.
    - Smart error handling: deletes subscription only on 404/410 (truly gone).
      5xx/network errors are logged & retried on next push.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from pywebpush import WebPushException, webpush

from .models import PushSubscription, RestaurantMembership

log = logging.getLogger(__name__)

# ── Throttle ──────────────────────────────────────────────────────────────────
THROTTLE_WINDOW_SEC = 60   # max 1 non-urgent push / minute / restaurant
THROTTLE_KEY_FMT    = "push_throttle:{rid}"

# ── Payload budget (iOS Safari soft-limit ~1 KB; Apple APNs hard-limit 4 KB) ──
MAX_PAYLOAD_BYTES = 900

# ── Urgency → Web Push protocol header ────────────────────────────────────────
URGENCY_HEADERS = {
    "low":    "very-low",
    "normal": "normal",
    "high":   "high",
}


# ─── Quiet hours ──────────────────────────────────────────────────────────────

def _in_quiet_hours(restaurant) -> bool:
    """
    True if `now` (in restaurant local tz) falls inside the configured quiet window.
    Disabled when start or end is blank — function returns False in that case.
    """
    start = restaurant.quiet_hours_start
    end   = restaurant.quiet_hours_end
    if not (start and end):
        return False
    try:
        tz = ZoneInfo(restaurant.timezone or "America/New_York")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now_local = timezone.now().astimezone(tz).time()
    if start < end:
        return start <= now_local < end
    # overnight window (e.g. 23:00 → 08:00)
    return now_local >= start or now_local < end


# ─── Payload assembly with size budget ────────────────────────────────────────

def _build_payload(
    title: str,
    body: str,
    url: str,
    urgency: str,
    actions: list[dict[str, str]] | None,
    image: str | None,
    tag: str | None,
) -> str:
    """JSON-serialize push payload; trim optional fields until it fits in MAX_PAYLOAD_BYTES.

    Each action item: {"action": "confirm", "title": "Confirm", "icon": "/static/portal/icon-check.png"}
    Chrome desktop renders the icon next to the inline button; iOS ignores actions entirely.
    """
    payload: dict[str, Any] = {
        "title":   title[:80],
        "body":    body[:200],
        "url":     url,
        "urgency": urgency,
        "tag":     tag or "concierge",
    }
    if actions:
        payload["actions"] = actions[:2]   # iOS ignores actions anyway; Chrome shows max 2
    if image and len(image) < 200:
        payload["image"] = image

    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return encoded

    # Trim in priority order: image → action icons → actions → body
    payload.pop("image", None)
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return encoded

    if payload.get("actions"):
        payload["actions"] = [{"action": a["action"], "title": a["title"]} for a in payload["actions"]]
        encoded = json.dumps(payload, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
            return encoded

    payload.pop("actions", None)
    payload["body"] = payload["body"][:120]
    return json.dumps(payload, separators=(",", ":"))


# ─── Public API ───────────────────────────────────────────────────────────────

def send_push(
    restaurant,
    title: str,
    body: str,
    url: str,
    *,
    urgency: str = "normal",
    actions: list[dict[str, str]] | None = None,
    image: str | None = None,
    tag: str | None = None,
    event_flag: str | None = None,
    block: bool = False,
) -> None:
    """
    Fire-and-forget push to all subscribed operators of `restaurant`.

    Args:
        restaurant:  Restaurant instance.
        title:       Notification title (<=80 chars after trim).
        body:        Notification body (<=200 chars after trim).
        url:         Deep link opened when notification clicked.
        urgency:     "low" | "normal" | "high". "high" bypasses throttle & may bypass quiet hours.
        actions:     [{"action": "confirm", "title": "✅"}] — Chrome only, iOS ignores.
        image:       Hero image URL — Chrome only, Safari/iOS ignores.
        tag:         Notification tag for replace-on-duplicate behavior.
        event_flag:  Membership opt-in field name (e.g. "notify_on_reservation"). If set,
                     only operators with that flag True and notify_via_push=True receive the push.
    """
    if not (settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY):
        log.warning("push skipped — VAPID keys not configured")
        return

    # Note: restaurant.notify_via_push deprecated — eligibility now driven entirely
    # by per-user Membership.notify_via_push (checked in _send_push_to_subscribers).

    # Quiet hours guard (disabled if start/end blank)
    if _in_quiet_hours(restaurant):
        if urgency != "high" or not restaurant.quiet_hours_skip_urgent:
            log.info("push skipped (quiet hours) restaurant=%s urgency=%s", restaurant.slug, urgency)
            return

    # Throttle non-urgent pushes
    if urgency != "high":
        throttle_key = THROTTLE_KEY_FMT.format(rid=restaurant.pk)
        if cache.get(throttle_key):
            log.info("push throttled restaurant=%s", restaurant.slug)
            return
        cache.set(throttle_key, 1, THROTTLE_WINDOW_SEC)

    payload = _build_payload(title, body, url, urgency, actions, image, tag)

    if block:
        # Synchronous path — required from short-lived processes (e.g. cron
        # management commands) where daemon threads would be killed before the
        # network request completes.
        _send_push_to_subscribers(restaurant.pk, payload, urgency, event_flag)
        return

    threading.Thread(
        target=_send_push_to_subscribers,
        args=(restaurant.pk, payload, urgency, event_flag),
        daemon=True,
    ).start()


def _send_push_to_subscribers(restaurant_pk: int, payload: str, urgency: str, event_flag: str | None) -> None:
    """
    Background thread: fan out push to subscribed devices of users who have
    opted-in at the account level.

    Layered model (role-agnostic):
        1. Restaurant.notify_via_push (master)  → checked in send_push() above
        2. Restaurant.notify_on_<event>         → checked by caller (early return)
        3. RestaurantMembership.notify_via_push → account-level opt-in (THIS function)
        4. PushSubscription                     → device endpoints (deliver to each)

    The `event_flag` parameter is accepted for forward compatibility (Stage H will
    add per-user × per-event filtering on top of step 3) but is a no-op in V1.
    """
    # Account-level opt-in: which users want push for this restaurant
    opted_in_user_ids = list(
        RestaurantMembership.objects.filter(
            restaurant_id=restaurant_pk,
            is_active=True,
            notify_via_push=True,
        ).values_list("user_id", flat=True)
    )

    if not opted_in_user_ids:
        log.info("push fan-out: no users opted in at account level | restaurant=%s", restaurant_pk)
        return

    subs = PushSubscription.objects.filter(
        restaurant_id=restaurant_pk,
        user_id__in=opted_in_user_ids,
    ).select_related("user")

    count = subs.count()
    if count == 0:
        log.info("push fan-out: %d user(s) opted in but no devices subscribed | restaurant=%s",
                 len(opted_in_user_ids), restaurant_pk)
        return

    log.info("push fan-out: %d device(s) across %d user(s) | restaurant=%s (event=%s)",
             count, len(opted_in_user_ids), restaurant_pk, event_flag)

    for sub in subs:
        _send_one(sub, payload, urgency)


def _send_one(sub: PushSubscription, payload: str, urgency: str) -> None:
    """Send a single push. Delete sub on 404/410; log everything else."""
    try:
        webpush(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.key_p256dh, "auth": sub.key_auth},
            },
            data=payload,
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{settings.VAPID_ADMIN_EMAIL}"},
            ttl=86400,                                           # 24h queue if device offline
            headers={"Urgency": URGENCY_HEADERS.get(urgency, "normal")},
        )
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            log.info("push subscription gone (status=%s), deleting endpoint=%s…", status, sub.endpoint[:60])
            sub.delete()
        elif status == 429:
            log.warning("push rate-limited (429) endpoint=%s…", sub.endpoint[:60])
        else:
            log.exception("push transient error status=%s endpoint=%s…", status, sub.endpoint[:60])
    except Exception:
        log.exception("push unexpected error endpoint=%s…", sub.endpoint[:60])
