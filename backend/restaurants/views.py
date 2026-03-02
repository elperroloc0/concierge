import json
import os
from collections import Counter, defaultdict
from datetime import timedelta

from backend import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from retell import Retell

from .forms import KnowledgeBaseForm, RestaurantBasicForm
from .models import CallEvent, Restaurant, RestaurantKnowledgeBase


# ─── Retell Webhook Helpers ───────────────────────────────────────────────────

def _build_dynamic_variables(restaurant):
    """Build the full dynamic_variables dict from Restaurant + KnowledgeBase."""
    kb = getattr(restaurant, "knowledge_base", None)
    dyn = {
        "restaurant_name":   restaurant.name,
        "address_full":      restaurant.address_full,
        "location_reference": restaurant.location_reference,
        "website":           restaurant.website,
        "welcome_phrase":    restaurant.welcome_phrase,
        "primary_lang":      restaurant.primary_lang,
        "conversation_tone": restaurant.conversation_tone,
        "timezone":          restaurant.timezone,
    }
    if kb:
        dyn.update({
            "hours_of_operation":     kb.hours_of_operation,
            "kitchen_closing_time":   kb.kitchen_closing_time,
            "holiday_closure_notes":  kb.holiday_closure_notes or ("Closed on major holidays" if kb.closes_on_holidays else "Open on holidays"),
            "food_menu_url":          kb.food_menu_url,
            "food_menu_summary":      kb.food_menu_summary,
            "bar_menu_url":           kb.bar_menu_url,
            "bar_menu_summary":       kb.bar_menu_summary,
            "happy_hour_details":     kb.happy_hour_details,
            "dietary_options":        kb.dietary_options,
            "auto_gratuity":          "Yes" if kb.auto_gratuity else "No",
            "service_charge_pct":     kb.service_charge_pct or "N/A",
            "service_charge_scope":   kb.get_service_charge_scope_display(),
            "max_cards_to_split":     str(kb.max_cards_to_split) if kb.max_cards_to_split else "N/A",
            "reservation_grace_min":  str(kb.reservation_grace_min) if kb.reservation_grace_min else "N/A",
            "no_show_fee":            kb.no_show_fee or "None",
            "large_party_min_guests": str(kb.large_party_min_guests) if kb.large_party_min_guests else "N/A",
            "has_private_dining":     "Yes" if kb.has_private_dining else "No",
            "private_dining_min_spend": kb.private_dining_min_spend,
            "allows_decorations":     "Yes" if kb.allows_decorations else "No",
            "decoration_cleaning_fee": kb.decoration_cleaning_fee or "None",
            "press_contact":          kb.press_contact,
            "live_music_details":     kb.live_music_details,
            "party_vibe_start_time":  kb.party_vibe_start_time,
            "noise_level":            kb.get_noise_level_display() if kb.noise_level else "N/A",
            "dress_code":             kb.dress_code or "Casual",
            "cover_charge":           kb.cover_charge or "None",
            "has_terrace":            "Yes" if kb.has_terrace else "No",
            "ac_intensity":           kb.get_ac_intensity_display() if kb.ac_intensity else "N/A",
            "stroller_friendly":      "Yes" if kb.stroller_friendly else "No",
            "has_valet":              "Yes" if kb.has_valet else "No",
            "valet_cost":             kb.valet_cost or "N/A",
            "free_parking_info":      kb.free_parking_info,
            "guest_info_to_collect":  kb.guest_info_to_collect,
        })
    return dyn


# ─── Retell Webhooks ──────────────────────────────────────────────────────────

def index(request):
    return HttpResponse("Hello1")


def account(request):
    return HttpResponse("account page")


@csrf_exempt
def retell_inbound_webhook(request, rest_id):
    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)

    raw_bytes = request.body
    raw_str = raw_bytes.decode("utf-8")

    try:
        payload = json.loads(raw_str)
    except json.JSONDecodeError:
        return JsonResponse({"detail": "invalid json"}, status=400)

    restaurant = get_object_or_404(Restaurant, id=rest_id, is_active=True)

    to_number = (payload.get("to_number") or "").strip()

    # Dev bypass (no signature check needed)
    if settings.DEBUG and request.headers.get("X-DEV-BYPASS") == os.environ.get("RETELL_DEV_BYPASS_SECRET", ""):
        return JsonResponse({"dynamic_variables": _build_dynamic_variables(restaurant)}, status=200)

    if not to_number or not restaurant.retell_phone_number:
        return JsonResponse({"detail": "missing to_number or restaurant phone not configured"}, status=400)

    if to_number != restaurant.retell_phone_number:
        return JsonResponse({"detail": "phone number mismatch"}, status=400)

    signature = request.headers.get("x-retell-signature", "")
    if not signature:
        return JsonResponse({"detail": "missing signature"}, status=401)

    if not restaurant.retell_api_key:
        return JsonResponse({"detail": "retell api key not configured"}, status=500)

    retell_client = Retell(api_key=restaurant.retell_api_key)
    if not retell_client.verify(raw_str, restaurant.retell_api_key, signature):
        return JsonResponse({"detail": "invalid signature"}, status=401)

    return JsonResponse({"dynamic_variables": _build_dynamic_variables(restaurant)}, status=200)


@csrf_exempt
def retell_events_webhook(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    raw = request.body.decode("utf-8")
    sig = request.headers.get("x-retell-signature", "")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return JsonResponse({"detail": "invalid json"}, status=400)

    to_number = (data.get("to_number") or data.get("call", {}).get("to_number") or "").strip()
    restaurant = Restaurant.objects.filter(retell_phone_number=to_number, is_active=True).first()
    if not restaurant:
        return JsonResponse({"detail": "unknown number"}, status=404)

    if not sig or not restaurant.retell_api_key:
        return JsonResponse({"detail": "unauthorized"}, status=401)

    retell_client = Retell(api_key=restaurant.retell_api_key)
    if not retell_client.verify(raw, restaurant.retell_api_key, sig):
        return JsonResponse({"detail": "invalid signature"}, status=401)

    event_type = data.get("event_type", "")
    CallEvent.objects.create(restaurant=restaurant, event_type=event_type, payload=data)
    return JsonResponse({"status": "ok"}, status=200)


# ─── Portal Analytics Helpers ─────────────────────────────────────────────────

TOPIC_KEYWORDS = {
    "Reservations":   ["reserva", "reservation", "book", "table", "mesa"],
    "Hours":          ["hora", "hours", "schedule", "horario", "open", "close", "abierto", "cerrado"],
    "Menu":           ["menu", "carta", "food", "comida", "dish", "plato", "drink", "bebida", "cocktail"],
    "Billing":        ["precio", "price", "cost", "charge", "tip", "propina", "gratuity", "pay", "pago"],
    "Happy Hour":     ["happy hour", "especial", "special", "discount", "descuento"],
    "Parking":        ["parking", "park", "valet", "estacionamiento"],
    "Private Events": ["evento", "event", "private", "privado", "buyout", "party", "fiesta"],
}

ESCALATE_KEYWORDS = ["transfer", "connect you", "representative", "speak to", "call back", "manager"]


def _classify_call(payload):
    """Return (topics: list[str], outcome: str) from a call_ended payload."""
    call = payload.get("call", {})
    transcript = (call.get("transcript") or "").lower()
    duration = 0
    try:
        start = call.get("start_timestamp", 0)
        end = call.get("end_timestamp", 0)
        if start and end:
            duration = int((end - start) / 1000)
    except Exception:
        pass

    topics = [
        topic for topic, keywords in TOPIC_KEYWORDS.items()
        if any(kw in transcript for kw in keywords)
    ]

    if duration < 20:
        outcome = "Incomplete"
    elif any(kw in transcript for kw in ESCALATE_KEYWORDS):
        outcome = "Escalated"
    else:
        outcome = "Resolved"

    return topics or ["General"], outcome, duration


# ─── Portal Views ─────────────────────────────────────────────────────────────

def portal_login(request):
    if request.user.is_authenticated:
        return redirect("portal_dashboard")

    error = None
    if request.method == "POST":
        user = authenticate(
            request,
            username=request.POST.get("username", "").strip(),
            password=request.POST.get("password", "").strip(),
        )
        if user is not None:
            login(request, user)
            return redirect("portal_dashboard")
        error = "Invalid username or password."

    return render(request, "portal/login.html", {"error": error})


def portal_logout(request):
    logout(request)
    return redirect("portal_login")


@login_required
def portal_dashboard(request):
    restaurant = get_object_or_404(Restaurant, user=request.user, is_active=True)

    thirty_days_ago = timezone.now() - timedelta(days=30)
    ended_events = CallEvent.objects.filter(
        restaurant=restaurant, event_type="call_ended", created_at__gte=thirty_days_ago
    ).order_by("created_at")

    total_calls = ended_events.count()
    caller_numbers = []
    topic_counter = Counter()
    outcome_counter = Counter()
    calls_by_day = defaultdict(int)

    for event in ended_events:
        call_data = event.payload.get("call", {})
        from_num = call_data.get("from_number", "Unknown")
        caller_numbers.append(from_num)

        topics, outcome, _ = _classify_call(event.payload)
        topic_counter.update(topics)
        outcome_counter[outcome] += 1

        day_key = event.created_at.strftime("%b %d")
        calls_by_day[day_key] += 1

    unique_callers = len(set(caller_numbers))
    repeat_callers = sum(1 for _, c in Counter(caller_numbers).items() if c > 1)

    context = {
        "restaurant": restaurant,
        "total_calls": total_calls,
        "unique_callers": unique_callers,
        "repeat_callers": repeat_callers,
        "calls_by_day_labels": json.dumps(list(calls_by_day.keys())),
        "calls_by_day_data": json.dumps(list(calls_by_day.values())),
        "topic_labels": json.dumps(list(topic_counter.keys())),
        "topic_data": json.dumps(list(topic_counter.values())),
        "outcome_labels": json.dumps(list(outcome_counter.keys())),
        "outcome_data": json.dumps(list(outcome_counter.values())),
    }
    return render(request, "portal/dashboard.html", context)


@login_required
def portal_knowledge_base(request):
    restaurant = get_object_or_404(Restaurant, user=request.user, is_active=True)
    kb, _ = RestaurantKnowledgeBase.objects.get_or_create(restaurant=restaurant)

    if request.method == "POST":
        basic_form = RestaurantBasicForm(request.POST, instance=restaurant)
        kb_form = KnowledgeBaseForm(request.POST, instance=kb)
        if basic_form.is_valid() and kb_form.is_valid():
            basic_form.save()
            kb_form.save()
            messages.success(request, "Knowledge base updated successfully.")
            return redirect("portal_kb")
    else:
        basic_form = RestaurantBasicForm(instance=restaurant)
        kb_form = KnowledgeBaseForm(instance=kb)

    return render(request, "portal/knowledge_base.html", {
        "restaurant": restaurant,
        "basic_form": basic_form,
        "kb_form": kb_form,
    })


@login_required
def portal_calls(request):
    restaurant = get_object_or_404(Restaurant, user=request.user, is_active=True)

    ended_events = CallEvent.objects.filter(
        restaurant=restaurant, event_type="call_ended"
    ).order_by("-created_at")

    enriched = []
    for event in ended_events:
        call_data = event.payload.get("call", {})
        topics, outcome, duration = _classify_call(event.payload)
        enriched.append({
            "date": event.created_at,
            "from_number": call_data.get("from_number", "Unknown"),
            "duration_sec": duration,
            "topics": ", ".join(topics),
            "outcome": outcome,
            "transcript": call_data.get("transcript", ""),
            "pk": event.pk,
        })

    paginator = Paginator(enriched, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "portal/calls.html", {
        "restaurant": restaurant,
        "page_obj": page_obj,
    })
