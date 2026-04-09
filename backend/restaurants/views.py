import json
import logging
import os
import re
import threading


def csrf_failure(request, reason=""):
    from django.shortcuts import redirect
    return redirect(f"/portal/login/?next={request.path}")


def bad_request(request, exception=None):
    from django.shortcuts import render
    return render(request, "400.html", status=400)


def permission_denied(request, exception=None):
    from django.shortcuts import render
    return render(request, "403.html", status=403)


def page_not_found(request, exception=None):
    from django.shortcuts import render
    return render(request, "404.html", status=404)


def server_error(request):
    from django.shortcuts import render
    return render(request, "500.html", status=500)
import urllib.parse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Q
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from retell import Retell

from .decorators import portal_view
from .forms import KnowledgeBaseForm, RestaurantBasicForm, AccountEmailForm, PasswordUpdateForm
from .models import CallDetail, CallEvent, CallerMemory, Restaurant, RestaurantKnowledgeBase, RestaurantMembership, SmsLog, Subscription, PendingEmailChange, WeeklyReport
from .services.retell_client import RetellClient
from .services.retell_tools import build_tool_list


# ─── Global Redirects ─────────────────────────────────────────────────────────

def portal_demo_request(request):
    """Handle demo request form submission from landing page."""
    if request.method == "POST":
        # Log the request — full lead handling to be implemented
        logger.info(
            "Demo request: name=%s %s email=%s business=%s industry=%s",
            request.POST.get("first_name", ""),
            request.POST.get("last_name", ""),
            request.POST.get("email", ""),
            request.POST.get("business_name", ""),
            request.POST.get("industry", ""),
        )
    return render(request, "landing_thanks.html")


def root_redirect(request):
    """Show landing page for visitors; redirect authenticated users to their portal."""
    if request.user.is_authenticated:
        redir = _get_login_redirect(request.user)
        if redir:
            return redir
        return redirect("portal_login")
    return render(request, "landing.html")


# ─── Retell Webhook Helpers ───────────────────────────────────────────────────

def _friendly_url(url: str) -> str:
    """Return just the domain for spoken use: 'https://foo.com/menu' → 'foo.com'"""
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.removeprefix("www.")


def _camel_split(s: str) -> str:
    """'CalleDragonesMia' → 'Calle Dragones Mia'. No-op on all-lowercase strings."""
    return re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', s)


def _spoken_email(email: str, lang: str = "en") -> str:
    """Convert an email address to a naturally spoken form.
    Spanish: 'info@calledragones.com' → 'info arroba calledragones punto com'
    English: 'info@calledragones.com' → 'info at calledragones dot com'
    CamelCase local parts are split: 'AdmonCalle@...' → 'Admon Calle ...'
    """
    if not email:
        return ""
    at_word  = "arroba" if lang == "es" else "at"
    dot_word = "punto"  if lang == "es" else "dot"
    local, _, rest = email.partition("@")
    local = _camel_split(local)
    rest  = rest.replace(".", f" {dot_word} ")
    return f"{local} {at_word} {rest}"


def _spoken_domain(domain: str, lang: str = "en") -> str:
    """Convert a domain to spoken form.
    'CalleDragonesMia.com' → 'Calle Dragones Mia punto com'
    """
    if not domain:
        return ""
    dot_word = "punto" if lang == "es" else "dot"
    # Split CamelCase in each segment so TTS pronounces them as separate words
    parts = domain.split(".")
    parts = [_camel_split(p) for p in parts]
    return f" {dot_word} ".join(parts)


def _spoken_address(address: str, lang: str = "en") -> str:
    """
    Expand common address abbreviations so Text-To-Speech (TTS) reads them naturally.
    e.g. '1036 SW 8th St' -> '1036 South West 8th Street'
    """
    if not address:
        return ""

    # Simple dictionary replacement for common English/Spanish street abbreviations
    replacements = {
        r'\bSt\.?\b': 'Street' if lang == 'en' else 'Calle',
        r'\bAve\.?\b': 'Avenue' if lang == 'en' else 'Avenida',
        r'\bBlvd\.?\b': 'Boulevard' if lang == 'en' else 'Bulevar',
        r'\bRd\.?\b': 'Road' if lang == 'en' else 'Ruta',
        r'\bDr\.?\b': 'Drive',
        r'\bLn\.?\b': 'Lane',
        r'\bCt\.?\b': 'Court',
        r'\bPl\.?\b': 'Place',
        r'\bSq\.?\b': 'Square',
        r'\bN\.?\b': 'North' if lang == 'en' else 'Norte',
        r'\bS\.?\b': 'South' if lang == 'en' else 'Sur',
        r'\bE\.?\b': 'East' if lang == 'en' else 'Este',
        r'\bW\.?\b': 'West' if lang == 'en' else 'Oeste',
        r'\bNW\b': 'North West' if lang == 'en' else 'Noroeste',
        r'\bNE\b': 'North East' if lang == 'en' else 'Noreste',
        r'\bSW\b': 'South West' if lang == 'en' else 'Suroeste',
        r'\bSE\b': 'South East' if lang == 'en' else 'Sureste',
        r'\bSte\.?\b': 'Suite',
        r'\bApt\.?\b': 'Apartment' if lang == 'en' else 'Apartamento',
    }

    spoken = address
    for pattern, replacement in replacements.items():
        spoken = re.sub(pattern, replacement, spoken, flags=re.IGNORECASE)

    return spoken


def _build_non_customer_rules(kb) -> str:
    """
    Assemble the NON-CUSTOMER CALL HANDLING block injected into the agent prompt.
    Returns an empty string if all categories are set to 'ignore' / defaults.
    """
    rules = []

    # ── Determine which action labels are used ──
    # Maps each handling value to a compact label + definition
    ACTION_LABEL = {
        "message":      "MSG",
        "transfer":     "TRANSFER",
        "decline":      "DECLINE",
        "give_contact": "CONTACT",
        "end_call":     "END",
    }

    ACTION_DEF = {
        "MSG":      "name + company + reason → save_caller_info(follow_up_needed=true)",
        "TRANSFER": "transfer call",
        "DECLINE":  "decline, suggest email → end_call",
        "CONTACT":  "press contact via get_info(\"private_events\")",
        "END":      "end_call",
    }

    used_actions = set()

    def add_rule(category: str, handling: str, ask_urgency: bool = False, extra: str = ""):
        label = ACTION_LABEL.get(handling, "MSG")
        used_actions.add(label)
        urgency = ", check urgency" if ask_urgency and handling != "transfer" else ""
        suffix = f" ({extra})" if extra else ""
        rules.append(f"- {category}: {label}{urgency}{suffix}")

    # Partner companies
    if kb.partner_call_handling != "ignore":
        partners = ", ".join(
            p.strip() for p in kb.partner_companies.splitlines() if p.strip()
        ) if kb.partner_companies.strip() else "known partners"
        add_rule(f"Partners ({partners})", kb.partner_call_handling, kb.partner_call_ask_urgency)

    # Vendors / Suppliers
    if kb.vendor_call_handling != "ignore":
        add_rule("Vendors/suppliers", kb.vendor_call_handling, kb.vendor_call_ask_urgency)

    # Press / Media / Influencers
    if kb.press_call_handling != "ignore":
        add_rule("Press/media/influencers", kb.press_call_handling, kb.press_call_ask_urgency)

    # External services
    if kb.service_call_handling != "ignore":
        add_rule("Service providers (plumbers, cleaners, maintenance)", kb.service_call_handling, kb.service_call_ask_urgency)

    # Sales / Marketing
    if kb.sales_call_handling != "ignore":
        add_rule("Sales/marketing", kb.sales_call_handling)

    # Financial / Legal / Collections
    if kb.financial_call_handling != "ignore":
        add_rule("Financial/legal/collections", kb.financial_call_handling)

    # Spam / Robocalls
    spam_handling = kb.spam_call_handling if kb.spam_call_handling in ("decline", "end_call") else "end_call"
    if spam_handling == "decline":
        used_actions.add("DECLINE")
        rules.append("- Spam/robocalls: DECLINE")
    else:
        used_actions.add("END")
        rules.append("- Spam/robocalls: END")

    if not rules:
        return ""

    # ── Build urgency block (only if any category uses it) ──
    any_urgency = any([
        kb.partner_call_handling  != "ignore" and kb.partner_call_ask_urgency,
        kb.vendor_call_handling   != "ignore" and kb.vendor_call_ask_urgency,
        kb.press_call_handling    != "ignore" and kb.press_call_ask_urgency,
        kb.service_call_handling  != "ignore" and kb.service_call_ask_urgency,
    ])

    urgent_outcome = (
        "TRANSFER" if kb.urgent_call_action == "transfer"
        else "take urgent message → save_caller_info(follow_up_needed=true)"
    )

    urgency_block = (
        f"Urgency: if caller signals time-sensitivity ('urgent', 'right now', 'waiting outside'), "
        f"offer: connect now or take a message. Urgent → {urgent_outcome}.\n"
    ) if any_urgency else ""

    # ── Assemble ──
    # Only include definitions for actions actually used
    action_defs = " | ".join(
        f"{k} = {ACTION_DEF[k]}" for k in ("MSG", "TRANSFER", "DECLINE", "CONTACT", "END") if k in used_actions
    )

    header = (
        "NON-CUSTOMER CALL HANDLING\n"
        "Detect by context: company intro, deliveries, orders, services, automated voice.\n"
        f"Actions — {action_defs}\n"
        f"{urgency_block}"
    )
    return header + "\n".join(rules)


def _get_caller_summary(from_number: str, restaurant) -> str:
    """
    Return a lightweight RETURNING CALLER block to inject at call start,
    or empty string if this is a first-time caller.
    Reads from CallerMemory — populated after each call_ended event.
    """
    if not from_number:
        return ""
    try:
        mem = CallerMemory.objects.get(phone=from_number, restaurant=restaurant)
    except CallerMemory.DoesNotExist:
        return ""

    name_label = mem.name or "known caller"
    lines = [f"### RETURNING CALLER — {name_label}"]
    lines.append(
        f"Called {mem.call_count} time(s)."
        + (f" Last call: {mem.last_call_at.strftime('%b %d, %Y')}." if mem.last_call_at else "")
    )
    if mem.last_call_summary:
        lines.append(f"Last call summary: {mem.last_call_summary}")
    if mem.preferences:
        lines.append(f"Known preferences: {mem.preferences}")
    if mem.staff_notes:
        lines.append(f"Staff notes: {mem.staff_notes}")
    lines.append(
        "Use this context naturally — acknowledge prior interactions if the caller "
        "references them. Call get_caller_profile() if you need the full history."
    )
    return "\n".join(lines) + "\n"


def _build_dynamic_variables(restaurant):
    """Build the full dynamic_variables dict from Restaurant + KnowledgeBase."""
    kb = getattr(restaurant, "knowledge_base", None)

    # Current date/time in the restaurant's configured timezone
    tz_name = restaurant.timezone or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz=tz)

    lang   = restaurant.primary_lang  # "es" | "en" | "other"
    domain = _friendly_url(restaurant.website)

    dyn = {
        "restaurant_name":       restaurant.name,
        "address_full":          _spoken_address(restaurant.address_full, lang),
        "website":               restaurant.website,
        "website_domain":        domain,
        "website_domain_spoken_es": _spoken_domain(domain, "es"),
        "website_domain_spoken_en": _spoken_domain(domain, "en"),
        "contact_email":         restaurant.contact_email or "",
        "contact_email_spoken_es": (
            kb.contact_email_spoken
            if kb and kb.contact_email_spoken and lang == "es"
            else _spoken_email(restaurant.contact_email or "", "es")
        ),
        "contact_email_spoken_en": (
            kb.contact_email_spoken
            if kb and kb.contact_email_spoken and lang == "en"
            else _spoken_email(restaurant.contact_email or "", "en")
        ),
        "agent_name":            restaurant.agent_name or restaurant.name,
        "welcome_phrase":        restaurant.welcome_phrase,
        "primary_lang":          restaurant.primary_lang,
        "conversation_tone":     restaurant.conversation_tone,
        "timezone":              restaurant.timezone,
        # Live date/time injected on every call
        "current_date":          now.strftime("%A, %B %d, %Y"),   # Monday, March 02, 2026
        "current_time":          now.strftime("%I:%M %p"),         # 02:30 PM
        "current_day":           now.strftime("%A"),               # Monday
    }
    # KB fields that the agent needs at call-start (reservation routing + escalation).
    # All other KB data is fetched on demand via the get_info tool.
    if kb:
        dyn.update({
            "large_party_min_guests":   str(kb.large_party_min_guests) if kb.large_party_min_guests else "N/A",
            "escalation_enabled":       "yes" if kb.escalation_enabled else "no",
            "escalation_conditions":    kb.escalation_conditions or "",
            "brand_voice_notes":        kb.brand_voice_notes or "",
            "team_members":             kb.team_members or "",
            "non_customer_call_rules":  _build_non_customer_rules(kb),
            "caller_summary":            "",
        })
    else:
        dyn.update({
            "large_party_min_guests":   "N/A",
            "escalation_enabled":       "no",
            "escalation_conditions":    "",
            "team_members":             "",
            "brand_voice_notes":        "",
            "non_customer_call_rules":  "",
            "caller_summary":            "",
        })
    return dyn


# ─── Guest Info Extraction ────────────────────────────────────────────────────

_WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
}

_REASON_KEYWORDS = [
    ("reservation",   ["reserva", "reservation", "book", "table", "mesa", "booking", "reservar"]),
    ("hours",         ["hora", "hours", "horario", "open", "close", "abierto", "cerrado", "schedule", "when do you"]),
    ("menu",          ["menu", "carta", "food", "comida", "dish", "plato", "drink", "bebida", "cocktail", "eat"]),
    ("billing",       ["precio", "price", "cost", "charge", "tip", "propina", "gratuity", "pago", "card", "split"]),
    ("parking",       ["parking", "park", "valet", "estacionamiento", "carro", "car"]),
    ("private_event", ["evento", "event", "private", "privado", "buyout", "party", "fiesta", "celebration",
                       "video", "videoclip", "video clip", "music video", "filming", "film shoot",
                       "grabacion", "grabación", "filmacion", "filmación", "produccion", "producción",
                       "production", "shoot", "rodaje"]),
    ("complaint",     ["complaint", "queja", "upset", "frustrated", "bad experience", "problema", "wrong"]),
]


# ─── Date Resolution Helpers ──────────────────────────────────────────────────

# Spoken day-of-month words → integer (covers 1–31 in English and Spanish)
_DAY_WORDS = {
    # English
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20, "twenty first": 21, "twenty second": 22,
    "twenty third": 23, "twenty fourth": 24, "twenty fifth": 25,
    "twenty sixth": 26, "twenty seventh": 27, "twenty eighth": 28,
    "twenty ninth": 29, "thirtieth": 30, "thirty first": 31,
    # Spanish
    "uno": 1, "primero": 1, "dos": 2, "segundo": 2, "tres": 3, "tercero": 3,
    "cuatro": 4, "cuarto": 4, "cinco": 5, "quinto": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
    "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19,
    "veinte": 20, "veintiuno": 21, "veintidos": 22, "veintitres": 23,
    "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26,
    "veintisiete": 27, "veintiocho": 28, "veintinueve": 29,
    "treinta": 30, "treinta y uno": 31,
}

_DAYS_EN = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_DAYS_ES = {
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
    "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
}
_MONTHS_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo",
    6: "junio", 7: "julio", 8: "agosto", 9: "septiembre",
    10: "octubre", 11: "noviembre", 12: "diciembre",
}
_MONTHS_EN = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May",
    6: "June", 7: "July", 8: "August", 9: "September",
    10: "October", 11: "November", 12: "December",
}
_DAY_NAMES_ES = {0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves", 4: "viernes", 5: "sábado", 6: "domingo"}
_DAY_NAMES_EN = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}


def _ordinal_en(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _spoken_date_es(d: date) -> str:
    """'el viernes 6 de marzo'"""
    return f"el {_DAY_NAMES_ES[d.weekday()]} {d.day} de {_MONTHS_ES[d.month]}"


def _spoken_date_en(d: date) -> str:
    """'Friday, March 6th'"""
    return f"{_DAY_NAMES_EN[d.weekday()]}, {_MONTHS_EN[d.month]} {_ordinal_en(d.day)}"


def _resolve_relative_date(text: str, today: date):
    """
    Resolve a spoken relative date to a datetime.date.
    Returns (date | None, ambiguity_message | None).
    """
    # Normalize: strip accents for matching robustness
    import unicodedata
    tl = unicodedata.normalize("NFKD", text.lower().strip())
    tl = "".join(c for c in tl if not unicodedata.combining(c))
    # Strip ordinal suffixes: "5th" → "5", "3rd" → "3", "1st" → "1"
    tl = re.sub(r'\b(\d+)(st|nd|rd|th)\b', r'\1', tl)

    if tl in ("today", "hoy", "hoy mismo"):
        return today, None

    if tl in ("tomorrow", "manana", "el dia de manana"):
        return today + timedelta(days=1), None

    if any(p in tl for p in ("next week", "proxima semana", "semana que viene", "semana proxima")):
        return None, "Which day next week? Ask: '¿Qué día de la próxima semana?'"

    is_next = any(p in tl for p in (
        "next ", "proximo", "que viene", "siguiente", "de la proxima",
    ))

    all_days = {**_DAYS_EN, **_DAYS_ES}
    day_num = None
    for name, num in all_days.items():
        if name in tl:
            day_num = num
            break

    # If caller gave a specific month ("sábado 11 de abril"), skip day-name resolution
    # and fall through to the month+day parser, which is more precise.
    all_month_names = [v.lower() for v in _MONTHS_ES.values()] + [v.lower() for v in _MONTHS_EN.values()]
    has_explicit_month = any(m in tl for m in all_month_names)

    if day_num is not None and not has_explicit_month:
        days_until = (day_num - today.weekday()) % 7
        if days_until == 0:
            days_until = 7  # never today — push to next occurrence
        if is_next and days_until < 7:
            days_until += 7  # "next X" = always next week
        return today + timedelta(days=days_until), None

    # Extract explicit 4-digit year if the caller mentioned one
    year_m = re.search(r'\b(20\d{2})\b', tl)
    explicit_year = int(year_m.group(1)) if year_m else None

    # Reject years more than 1 year ahead (e.g. 2028 when today is 2026)
    if explicit_year is not None and explicit_year > today.year + 1:
        return None, (
            f"We only accept reservations up to a year ahead — "
            f"did you mean {today.year} or {today.year + 1}?"
        )

    # Month-name + day number (English and Spanish)
    # name→number: {"january": 1, "enero": 1, "marzo": 3, ...}
    # Keys normalized (no accents) to match the already-normalized `tl`
    import unicodedata as _ud
    def _norm(s):
        return "".join(c for c in _ud.normalize("NFKD", s.lower()) if not _ud.combining(c))
    _month_lookup = {}
    for _k, _v in {**_MONTHS_EN, **{k: v for k, v in _MONTHS_ES.items()}}.items():
        _month_lookup[_norm(_v)] = _k
    # Ensure English names aren't lost to the Spanish merge (same int keys)
    for _k, _v in _MONTHS_EN.items():
        _month_lookup[_norm(_v)] = _k
    for month_name, month_num in _month_lookup.items():
        if month_name in tl:
            # Try numeric digit first
            day_n = None
            m = re.search(r"\b(\d{1,2})\b", tl)
            if m:
                day_n = int(m.group(1))
            else:
                # Fall back to spoken day-of-month words (e.g. "quince", "fifteenth")
                # Check multi-word keys first (longest match wins)
                for word, num in sorted(_DAY_WORDS.items(), key=lambda x: -len(x[0])):
                    if word in tl:
                        day_n = num
                        break
            if day_n is not None:
                try:
                    if explicit_year is not None:
                        # Caller specified a year — use it as-is (past years flagged via is_past)
                        candidate = date(explicit_year, month_num, day_n)
                    else:
                        # No year given — assume current year, bump to next if already past
                        candidate = date(today.year, month_num, day_n)
                        if candidate < today:
                            candidate = date(today.year + 1, month_num, day_n)
                    return candidate, None
                except ValueError:
                    pass

    # MM/DD or DD/MM numeric pattern
    slash_m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})\b", tl)
    if slash_m:
        a, b = int(slash_m.group(1)), int(slash_m.group(2))
        for month_n, day_n in [(a, b), (b, a)]:
            if 1 <= month_n <= 12 and 1 <= day_n <= 31:
                try:
                    if explicit_year is not None:
                        candidate = date(explicit_year, month_n, day_n)
                    else:
                        candidate = date(today.year, month_n, day_n)
                        if candidate < today:
                            candidate = date(today.year + 1, month_n, day_n)
                    return candidate, None
                except ValueError:
                    pass

    return None, "Could not understand the date — ask the caller for day and month."


def _parse_transcript_for_guest_info(transcript: str) -> dict:
    """
    Regex fallback extractor for when call_analysis is absent or incomplete.
    Handles Spanish and English restaurant call patterns.
    Returns a dict with only the fields that were confidently extracted.
    """
    result: dict = {}
    tl = transcript.lower()

    # --- Caller name ---
    name_pats = [
        r"(?:my name is|i'?m|this is)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        r"(?:me llamo|mi nombre es|soy)\s+([A-Z][a-záéíóúüñ]+(?:\s+[A-Z][a-záéíóúüñ]+)?)",
    ]
    for pat in name_pats:
        m = re.search(pat, transcript)
        if m:
            result["caller_name"] = m.group(1).strip()
            break

    # --- Party size ---
    size_pats = [
        r"(?:table|party|group)\s+(?:of|for)\s+(\d+|" + "|".join(_WORD_TO_NUM) + r")",
        r"for\s+(\d+)\s*(?:people|persons|guests|of us)?",
        r"(\d+)\s+(?:people|persons|guests)\b",
        r"(?:mesa|reserva)\s+para\s+(\d+|" + "|".join(_WORD_TO_NUM) + r")",
        r"(?:somos|seríamos|éramos)\s+(\d+|" + "|".join(_WORD_TO_NUM) + r")",
    ]
    for pat in size_pats:
        m = re.search(pat, tl)
        if m:
            raw = m.group(1)
            try:
                result["party_size"] = int(raw)
            except ValueError:
                result["party_size"] = _WORD_TO_NUM.get(raw)
            break

    # --- Reservation date ---
    _SPANISH_MONTHS_RE = r"(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)"
    _DAYS_ES_RE = r"(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)"
    date_pats = [
        # Most specific first: "el jueves 5 de marzo" / "viernes 13 de marzo"
        rf"(?:el\s+)?{_DAYS_ES_RE}\s+\d{{1,2}}\s+de\s+{_SPANISH_MONTHS_RE}",
        # "el 5 de marzo"
        rf"el\s+\d{{1,2}}\s+de\s+{_SPANISH_MONTHS_RE}",
        # "5 de marzo" (no article)
        rf"\d{{1,2}}\s+de\s+{_SPANISH_MONTHS_RE}",
        # English: "March 5th", "March 5"
        r"(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?",
        # English relative: "this Friday", "next Saturday"
        r"(?:this|next)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
        # English ordinal: "on the 5th"
        r"on\s+the\s+\d{1,2}(?:st|nd|rd|th)?",
        # Day name only (least specific — fallback)
        rf"(?:este|el\s+pr[oó]ximo|el)\s+{_DAYS_ES_RE}",
    ]
    for pat in date_pats:
        m = re.search(pat, tl)
        if m:
            result["reservation_date"] = m.group(0).strip()
            break

    # --- Reservation time ---
    _SPANISH_NUM_WORDS = {
        "una": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
        "seis": "6", "siete": "7", "ocho": "8", "nueve": "9", "diez": "10",
        "once": "11", "doce": "12", "medianoche": "00", "mediodia": "12",
    }
    time_pats = [
        # Digits + AM/PM: "6:00 PM", "8 PM"
        r"(?:at\s+)?(\d{1,2}(?::\d{2})?)\s*(pm|am|p\.m\.|a\.m\.)",
        # "around 7 PM"
        r"around\s+(\d{1,2}(?::\d{2})?)\s*(pm|am)?",
        # "a las 6:00" / "a las 8"
        r"(?:a\s+las?|alrededor\s+de\s+las?)\s+(\d{1,2}(?::\d{2})?)",
    ]
    for pat in time_pats:
        m = re.search(pat, tl, re.IGNORECASE)
        if m:
            result["reservation_time"] = m.group(0).strip()
            break
    # Fallback: Spanish word form — "a las ocho PM", "las ocho de la noche"
    if "reservation_time" not in result:
        m = re.search(
            r"(?:a\s+las?\s+)?(" + "|".join(_SPANISH_NUM_WORDS) + r")\s*(pm|am|de\s+la\s+noche|de\s+la\s+ma[ñn]ana)?",
            tl, re.IGNORECASE,
        )
        if m:
            word = m.group(1).lower()
            num  = _SPANISH_NUM_WORDS.get(word, "")
            period_raw = (m.group(2) or "").lower()
            if "noche" in period_raw or "pm" in period_raw:
                period = "PM"
            elif "mañana" in period_raw or "manana" in period_raw or "am" in period_raw:
                period = "AM"
            else:
                period = ""
            if num:
                result["reservation_time"] = f"{num}:00 {period}".strip()

    # --- Call reason ---
    for reason, keywords in _REASON_KEYWORDS:
        if any(kw in tl for kw in keywords):
            result["call_reason"] = reason
            break
    if "call_reason" not in result:
        result["call_reason"] = "other"

    # --- wants_reservation ---
    result["wants_reservation"] = any(
        kw in tl for kw in ["reserva", "reservation", "book a table", "hacer una reserva", "quiero reservar"]
    )

    # --- Special requests (keyword hints — primary source is call_analysis) ---
    special_hits = []
    special_pats = [
        (r"cumplea[ñn]os?|birthday",                  "birthday"),
        (r"aniversario|anniversary",                   "anniversary"),
        (r"sorpresa|surprise",                         "surprise"),
        (r"vegano?|vegan\b",                           "vegan"),
        (r"vegetariano?|vegetarian",                   "vegetarian"),
        (r"sin gluten|gluten.free|cel[ií]aco",         "gluten-free"),
        (r"alergi\w+|allerg\w+",                       "allergy"),
        (r"terraza|terrace|exterior|outdoor",          "terrace/outdoor"),
        (r"silla\s+de\s+ruedas|wheelchair|accesib\w+", "accessibility"),
        (r"high\s*chair|silla\s+de\s+beb[eé]|ni[ñn]o", "high chair"),
        (r"privado?|private\s+room",                   "private area"),
        (r"oc[ae]si[oó]n especial|special occasion",   "special occasion"),
    ]
    for pat, label in special_pats:
        if re.search(pat, tl, re.IGNORECASE):
            special_hits.append(label)
    if special_hits:
        result["special_requests"] = ", ".join(special_hits)

    # follow_up_needed is now set explicitly by the AI via save_caller_info tool.
    # It will be merged in _build_call_detail_from_payload via 'mid'.

    return result


_SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


def _parse_reservation_date(s):
    """Parse date string (ISO or Spanish natural language) to a date object."""
    if not s:
        return None
    raw = str(s).strip()
    # ISO
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        pass
    # Spanish with number: "el jueves 5 de marzo", "el 5 de marzo", "5 de marzo"
    m = re.search(r'(\d{1,2})\s+de\s+(\w+)', raw.lower())
    if m:
        month = _SPANISH_MONTHS.get(m.group(2))
        if month:
            day  = int(m.group(1))
            year = date.today().year
            try:
                d = date(year, month, day)
                if d < date.today() - timedelta(days=7):
                    d = date(year + 1, month, day)
                return d
            except ValueError:
                pass
    return None


def _parse_reservation_time(s):
    """Parse time string (ISO 24h, 12h AM/PM, or Spanish word-form) to a time object."""
    if not s:
        return None
    raw = str(s).strip()
    # Strict 24h
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time()
        except (ValueError, TypeError):
            pass
    # Extract digit + AM/PM from any string (e.g. "a las 6:00 PM", "6 PM")
    m = re.search(r'(\d{1,2}(?::\d{2})?)\s*(am|pm|a\.m\.|p\.m\.)', raw, re.IGNORECASE)
    if m:
        t_str  = m.group(1)
        period = m.group(2).replace(".", "").upper()
        if ":" not in t_str:
            t_str += ":00"
        try:
            return datetime.strptime(f"{t_str} {period}", "%I:%M %p").time()
        except (ValueError, TypeError):
            pass
    # Plain digit without AM/PM: "18:00" already handled; "6:00" ambiguous — treat as-is
    m = re.search(r'(\d{1,2}):(\d{2})', raw)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)}:{m.group(2)}", "%H:%M").time()
        except (ValueError, TypeError):
            pass
    # Spanish word number: "8:00 PM" already caught above; "ocho PM" → "8:00 PM"
    _WORD_TO_HOUR = {
        "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
        "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    }
    for word, hour in _WORD_TO_HOUR.items():
        if re.search(rf'\b{word}\b', raw, re.IGNORECASE):
            if re.search(r'pm|noche', raw, re.IGNORECASE) and hour < 12:
                hour += 12
            elif re.search(r'am|mañana|manana', raw, re.IGNORECASE) and hour == 12:
                hour = 0
            try:
                from datetime import time as time_cls
                return time_cls(hour, 0)
            except ValueError:
                pass
            break
    return None


def _build_call_detail_from_payload(call_event: CallEvent) -> None:
    """
    Create or update a CallDetail from a call_ended CallEvent payload.
    Primary source: call.call_analysis (Retell post-call extraction).
    Fallback: transcript regex for any missing/empty fields.
    """
    payload    = call_event.payload
    call       = payload.get("call", {})

    # Retell API change: custom fields are now nested under `custom_analysis_data`
    full_analysis = call.get("call_analysis") or {}
    analysis      = full_analysis.get("custom_analysis_data") or full_analysis

    transcript = (call.get("transcript") or "").strip()
    fallback   = _parse_transcript_for_guest_info(transcript) if transcript else {}

    # Third fallback: name saved real-time by save_caller_info tool during the call
    call_id = call.get("call_id", "")
    if call_id:
        mid = (CallDetail.objects
               .filter(call_event__payload__call__call_id=call_id)
               .exclude(call_event=call_event)
               .order_by("created_at").first())
        if mid:
            if mid.caller_name and not analysis.get("caller_name") and not fallback.get("caller_name"):
                fallback["caller_name"] = mid.caller_name
            if mid.follow_up_needed:
                fallback["follow_up_needed"] = True

    def _get(key, default=""):
        val = analysis.get(key)
        if val is None or val == "" or val == 0:
            return fallback.get(key, default)
        return val

    def _get_bool(key, default=None):
        val = analysis.get(key)
        if val is None:
            return fallback.get(key, default)
        return bool(val)

    raw_size  = _get("party_size", None)
    party_size = None
    if raw_size is not None:
        try:
            v = int(raw_size)
            party_size = v if v > 0 else None
        except (ValueError, TypeError):
            party_size = None

    # Defective reservation detection — Level 1
    # A reservation call is defective if wants_reservation=True but any required field is missing.
    _wants_res = bool(analysis.get("wants_reservation") or fallback.get("wants_reservation"))
    _def_flags = []
    if _wants_res:
        if not str(analysis.get("caller_name") or fallback.get("caller_name", "")).strip():
            _def_flags.append("missing caller name")
        if not _parse_reservation_date(_get("reservation_date", "")):
            _def_flags.append("missing reservation date")
        if not _parse_reservation_time(_get("reservation_time", "")):
            _def_flags.append("missing reservation time")
        _raw_ps = _get("party_size", None)
        _parsed_ps = None
        if _raw_ps is not None:
            try:
                _v = int(_raw_ps)
                _parsed_ps = _v if _v > 0 else None
            except (ValueError, TypeError):
                pass
        if not _parsed_ps:
            _def_flags.append("missing party size")
    needs_review = bool(_def_flags)

    # Quality signals from Retell post-call analysis
    _SIGNAL_KEYS = {
        "agent_failed_to_answer", "unanswered_question", "agent_response_to_unanswered",
        "agent_confusion_moment", "caller_frustration", "transfer_was_necessary",
        "language_consistency", "is_spam_or_robocall", "call_quality",
    }
    call_signals = {k: v for k, v in analysis.items() if k in _SIGNAL_KEYS}
    is_spam = bool(call_signals.get("is_spam_or_robocall", False))

    # Level 2: quality-based failure (caller unsatisfied or agent error)
    _sentiment = _get("caller_sentiment", "neutral")
    _sentiment_fail = _sentiment in ("frustrated", "upset")
    _quality_fail = call_signals.get("call_quality") == "poor"
    if not needs_review and (_quality_fail or _sentiment_fail):
        needs_review = True

    duration_ms = call.get("duration_ms")
    duration_seconds = int(duration_ms / 1000) if duration_ms is not None else None

    CallDetail.objects.update_or_create(
        call_event=call_event,
        defaults={
            "caller_name":       str(_get("caller_name", ""))[:255],
            "caller_phone":      (call.get("from_number") or "").strip(),
            "caller_email":      str(_get("caller_email", ""))[:255],
            "call_reason":       _get("call_reason", "other"),
            "wants_reservation": _get_bool("wants_reservation", None),
            "party_size":        party_size,
            "reservation_date":  _parse_reservation_date(_get("reservation_date", "")),
            "reservation_time":  _parse_reservation_time(_get("reservation_time", "")),
            "special_requests":  str(_get("special_requests", "")),
            "caller_sentiment":  _get("caller_sentiment", "neutral"),
            "follow_up_needed":  _get_bool("follow_up_needed", False),
            "recording_url":     call.get("recording_url", ""),
            "call_summary":      (full_analysis.get("call_summary") or "").strip(),
            "call_signals":      call_signals,
            "duration_seconds":  duration_seconds,
            "is_spam":           is_spam,
            "needs_review":      needs_review,
            "notes":             "",
        },
    )

    # Delete any mid-call placeholder CallEvents for the same call_id now that
    # we have the complete call_ended record. Keeps the DB clean.
    if call_id:
        CallEvent.objects.filter(
            event_type="call_in_progress",
            payload__call__call_id=call_id,
        ).exclude(pk=call_event.pk).delete()


def _upsert_caller_memory(call_event: CallEvent, restaurant) -> None:
    """
    Create or update CallerMemory after a call ends.
    Merges new name/email if provided; always updates call_count,
    last_call_at, and last_call_summary from Retell.
    """
    from django.utils import timezone as dj_tz

    call         = call_event.payload.get("call", {})
    from_number  = (call.get("from_number") or "").strip()
    if not from_number:
        return

    full_analysis = call.get("call_analysis") or {}
    analysis      = full_analysis.get("custom_analysis_data") or full_analysis
    new_name      = str(analysis.get("caller_name") or "").strip()[:255]
    new_email     = str(analysis.get("caller_email") or "").strip()[:255]
    new_summary   = (full_analysis.get("call_summary") or "").strip()
    call_reason   = str(analysis.get("call_reason") or "").strip()
    # Guests always take precedence — a non_customer call never downgrades a guest
    new_type = CallerMemory.CALLER_TYPE_BUSINESS if call_reason == "non_customer" else CallerMemory.CALLER_TYPE_GUEST

    mem, created = CallerMemory.objects.get_or_create(
        phone=from_number,
        restaurant=restaurant,
        defaults={
            "name":              new_name,
            "email":             new_email,
            "caller_type":       new_type,
            "call_count":        1,
            "last_call_at":      dj_tz.now(),
            "last_call_summary": new_summary,
        },
    )

    if not created:
        changed = ["call_count", "last_call_at"]
        # Name: auto-fill if empty; queue for verification if different from existing
        if new_name:
            if not mem.name:
                mem.name = new_name
                changed.append("name")
            elif new_name != mem.name and new_name != mem.pending_name:
                mem.pending_name = new_name
                changed.append("pending_name")
        if new_email and not mem.email:
            mem.email = new_email
            changed.append("email")
        # Guests always take precedence over business — never downgrade
        if new_type == CallerMemory.CALLER_TYPE_GUEST and mem.caller_type != CallerMemory.CALLER_TYPE_GUEST:
            mem.caller_type = CallerMemory.CALLER_TYPE_GUEST
            changed.append("caller_type")
        mem.call_count  += 1
        mem.last_call_at = dj_tz.now()
        if new_summary:
            mem.last_call_summary = new_summary
            changed.append("last_call_summary")
        mem.save(update_fields=changed)

    logger.info(
        "_upsert_caller_memory: restaurant=%s phone=%s count=%d created=%s",
        restaurant.slug, from_number[-4:], mem.call_count, created,
    )


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
        logger.error("Retell inbound webhook | Invalid JSON payload")
        return JsonResponse({"detail": "invalid json"}, status=400)

    _from = payload.get("from_number") or payload.get("call_inbound", {}).get("from_number", "?")
    _to = payload.get("to_number") or payload.get("call_inbound", {}).get("to_number", "?")
    _cid = payload.get("call_id") or payload.get("call_inbound", {}).get("call_id", "?")
    logger.info("Retell inbound | call_id=%s | from=%s → to=%s", _cid, _from, _to)

    restaurant = Restaurant.objects.filter(id=rest_id).first()

    # ── Check Account & Subscription Status ──
    is_valid_account = True
    if not restaurant or not restaurant.is_active:
        logger.warning("Retell inbound webhook | Restaurant inactive or missing | id=%s", rest_id)
        is_valid_account = False
    else:
        sub = getattr(restaurant, "subscription", None)
        if not sub or not sub.is_active:
            logger.warning("Retell inbound webhook | Subscription inactive | restaurant=%s", restaurant.slug)
            is_valid_account = False
        elif sub.communication_balance <= 0:
            logger.warning("Retell inbound webhook | Insufficient balance (%.2f) | restaurant=%s",
                           sub.communication_balance, restaurant.slug)
            is_valid_account = False

    # If the account is invalid, we MUST return 200 OK so Retell doesn't fall back to defaults.
    # Inject account_status_directive so the LLM knows to hang up immediately.
    if not is_valid_account:
        # Self-heal: if the phone is still connected to Retell despite being inactive,
        # disconnect it now so future calls never reach here.
        if restaurant and restaurant.retell_phone_number:
            _disconnect_retell_phone(restaurant)
            logger.warning(
                "Retell inbound webhook | Auto-disconnected phone for inactive account | restaurant=%s",
                restaurant.slug,
            )
        _inactive_directive = (
            "⚠ SYSTEM ALERT — This account is currently inactive. "
            "Your ONLY task: politely tell the caller this phone line is temporarily unavailable, "
            "then end the call immediately using `end_call`. "
            "Do not take reservations or answer any other questions."
        )
        return JsonResponse({"call_inbound": {"dynamic_variables": {"account_status_directive": _inactive_directive}}}, status=200)

    # From here on, restaurant is guaranteed to be valid and active
    to_number = (
        payload.get("to_number")
        or payload.get("call_inbound", {}).get("to_number")
        or ""
    ).strip()
    if not to_number:
        return JsonResponse({"detail": "missing to_number"}, status=400)

    if not restaurant.retell_phone_number:
        return JsonResponse({"detail": "missing retell_phone_number"}, status=400)

    # The webhook validates the to_number against the DB a second time
    if to_number != restaurant.retell_phone_number:
        return JsonResponse({"detail": "unknown number"}, status=404)

    logger.info("Retell inbound | restaurant=%s | active=True → building dynamic vars", restaurant.slug)

    dyn_vars = _build_dynamic_variables(restaurant)
    dyn_vars["account_status_directive"] = ""   # active — no override needed
    dyn_vars["caller_from_number"] = _from if _from != "?" else ""
    dyn_vars["caller_summary"] = _get_caller_summary(_from if _from != "?" else "", restaurant)
    dyn_response = {"call_inbound": {"dynamic_variables": dyn_vars}}

    # Verify Retell signature
    signature = request.headers.get("x-retell-signature", "")
    if not signature:
        return JsonResponse({"detail": "missing signature"}, status=401)

    if not restaurant.retell_api_key:
        return JsonResponse({"detail": "missing api key"}, status=500)

    retell_client = Retell(api_key=restaurant.retell_api_key)
    if not retell_client.verify(raw_str, restaurant.retell_api_key, signature):
        logger.warning(
            "Retell inbound webhook | Invalid signature | restaurant=%s | Check if Retell Webhook Secret differs from API Key.",
            restaurant.slug
        )
        return JsonResponse({"detail": "invalid signature"}, status=401)

    return JsonResponse(dyn_response, status=200)


def _get_operator_email(restaurant: Restaurant) -> str | None:
    """Return the active operator's email if they exist, else None."""
    m = (
        RestaurantMembership.objects
        .filter(restaurant=restaurant, role="operator", is_active=True)
        .select_related("user")
        .first()
    )
    return m.user.email if m else None


def _send_call_alert_email(
    call_event: CallEvent,
    restaurant: Restaurant,
    *,
    subject_prefix: str,
    reason_display: str,
    reason_bg: str,
    reason_color: str,
    reason_border: str,
    text_body_extra: str = "",
    extra_recipients: list[str] | None = None,
) -> None:
    """Shared helper: render and send an event-driven alert email to the restaurant owner (and optionally the operator)."""
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.utils.timezone import localtime

    notify_email = restaurant.notify_email or (restaurant.user.email if restaurant.user else "")
    if not restaurant.notify_via_email or not notify_email:
        return

    recipients = [notify_email]
    if extra_recipients:
        for addr in extra_recipients:
            if addr and addr != notify_email:
                recipients.append(addr)

    try:
        detail = call_event.detail
    except CallDetail.DoesNotExist:
        detail = None

    call_payload       = call_event.payload.get("call", {})
    caller_phone       = (call_payload.get("from_number") or "").strip()
    caller_name        = detail.caller_name  if detail else ""
    caller_email_val   = detail.caller_email if detail else ""
    full_analysis      = call_payload.get("call_analysis") or {}
    call_summary       = (full_analysis.get("call_summary") or "").strip()
    transcript_raw     = call_payload.get("transcript", "") or ""
    transcript_snippet = call_summary or (transcript_raw[-400:].strip() if transcript_raw else "")
    call_dt = localtime(call_event.created_at).strftime("%b %-d, %Y at %-I:%M %p")

    ctx = {
        "restaurant_name":    restaurant.name,
        "call_dt":            call_dt,
        "caller_name":        caller_name,
        "caller_phone":       caller_phone,
        "caller_email":       caller_email_val,
        "transcript_snippet": transcript_snippet,
        "follow_up_needed":   detail.follow_up_needed if detail else False,
        "wants_reservation":  detail.wants_reservation if detail else False,
        "party_size":         detail.party_size if detail else None,
        "reservation_date":   detail.reservation_date if detail else None,
        "reservation_time":   detail.reservation_time if detail else None,
        "special_requests":   detail.special_requests if detail else "",
        "caller_notes":       detail.notes if detail else "",
        "duration":           "",
        "reason_display":     reason_display,
        "reason_bg":          reason_bg,
        "reason_color":       reason_color,
        "reason_border":      reason_border,
        "portal_url":         f"{settings.RETELL_WEBHOOK_BASE_URL}/portal/{restaurant.slug}/calls/",
    }

    html_body = render_to_string("emails/post_call_summary.html", ctx)
    subject   = f"{subject_prefix} — {restaurant.name} [{caller_phone or 'Unknown #'}]"
    text_body = (
        f"{subject_prefix}\n\n"
        f"Restaurant: {restaurant.name}\n"
        f"Time: {call_dt}\n"
        f"Caller: {caller_name or 'Unknown'}\n"
        f"Phone: {caller_phone}\n"
        f"{text_body_extra}\n"
    )

    try:
        msg = EmailMultiAlternatives(subject, text_body, settings.DEFAULT_FROM_EMAIL, recipients)
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        logger.info("call_alert: [%s] sent to %s | restaurant=%s", reason_display, recipients, restaurant.slug)
    except Exception:
        logger.exception("call_alert: [%s] failed | restaurant=%s", reason_display, restaurant.slug)


def _send_followup_alert_email(call_event: CallEvent, restaurant: Restaurant) -> None:
    """Send follow-up alert if the preference flag is on."""
    if not restaurant.notify_on_followup:
        return
    note = ""
    try:
        note = call_event.detail.notes or ""
    except CallDetail.DoesNotExist:
        pass
    extra = "The caller asked to be called back or requested a human agent.\n"
    if note:
        extra += f"\nCaller message:\n{note}\n"
    op = RestaurantMembership.objects.filter(
        restaurant=restaurant, role="operator", is_active=True, notify_on_followup=True
    ).select_related("user").first()
    _send_call_alert_email(
        call_event, restaurant,
        subject_prefix="⚠️ Follow-up Needed",
        reason_display="Follow-up Required",
        reason_bg="#fee2e2", reason_color="#b91c1c", reason_border="#fca5a5",
        text_body_extra=extra,
        extra_recipients=[op.notify_email or op.user.email] if op else None,
    )


def _send_reservation_alert_email(call_event: CallEvent, restaurant: Restaurant) -> None:
    """Send reservation-intent alert if the preference flag is on."""
    if not restaurant.notify_on_reservation:
        return
    op = RestaurantMembership.objects.filter(
        restaurant=restaurant, role="operator", is_active=True, notify_on_reservation=True
    ).select_related("user").first()
    _send_call_alert_email(
        call_event, restaurant,
        subject_prefix="📅 Reservation Request",
        reason_display="Reservation Intent",
        reason_bg="#dbeafe", reason_color="#1e40af", reason_border="#93c5fd",
        text_body_extra="A caller expressed interest in making a reservation.\n",
        extra_recipients=[op.notify_email or op.user.email] if op else None,
    )


def _send_complaint_alert_email(call_event: CallEvent, restaurant: Restaurant) -> None:
    """Send complaint alert if the preference flag is on."""
    if not restaurant.notify_on_complaint:
        return
    op = RestaurantMembership.objects.filter(
        restaurant=restaurant, role="operator", is_active=True, notify_on_complaint=True
    ).select_related("user").first()
    _send_call_alert_email(
        call_event, restaurant,
        subject_prefix="🚨 Complaint Received",
        reason_display="Complaint",
        reason_bg="#fee2e2", reason_color="#991b1b", reason_border="#fca5a5",
        text_body_extra="A caller raised a complaint. Immediate attention may be required.\n",
        extra_recipients=[op.notify_email or op.user.email] if op else None,
    )


def _send_defective_call_alert_email(call_event: CallEvent, restaurant: Restaurant) -> None:
    """Send defective-call alert: incomplete reservation (Level 1) or quality failure (Level 2)."""
    if not restaurant.notify_via_email or not restaurant.notify_on_defective_call:
        return
    try:
        detail = call_event.detail
    except CallDetail.DoesNotExist:
        return

    # Skip spam and abandoned calls (under 20 seconds)
    if detail.is_spam:
        return
    call_data = call_event.payload.get("call", {})
    duration = (call_data.get("end_timestamp", 0) - call_data.get("start_timestamp", 0)) / 1000
    if duration < 20:
        return

    # ── Level 1: incomplete reservation ───────────────────────────────────────
    if detail.wants_reservation:
        flags = []
        if not detail.caller_name:
            flags.append("nombre del caller")
        if not detail.reservation_date:
            flags.append("fecha de reservación")
        if not detail.reservation_time:
            flags.append("hora de reservación")
        if not detail.party_size:
            flags.append("número de personas")
        missing_str = ", ".join(flags) if flags else "información incompleta"
        extra = f"El agente no completó la reservación.\nFalta: {missing_str}.\n"
        _send_call_alert_email(
            call_event, restaurant,
            subject_prefix="⚠️ Reservación incompleta",
            reason_display="Reservación incompleta",
            reason_bg="#fff7ed", reason_color="#c2410c", reason_border="#fed7aa",
            text_body_extra=extra,
            extra_recipients=None,
        )
        return

    # ── Level 2: quality failure (no reservation involved) ────────────────────
    signals = detail.call_signals
    reasons = []
    sentiment = detail.caller_sentiment or ""
    if sentiment in ("frustrated", "upset"):
        reasons.append(f"Caller {sentiment}")
    if signals.get("agent_confusion_moment"):
        reasons.append(f"Confusión del agente: {signals['agent_confusion_moment']}")
    if signals.get("agent_failed_to_answer") and signals.get("unanswered_question"):
        reasons.append(f"Pregunta sin respuesta: \"{signals['unanswered_question']}\"")
    if signals.get("language_consistency") is False:
        reasons.append("Inconsistencia de idioma")
    if signals.get("call_quality") == "poor":
        reasons.append("Calidad de llamada: poor")

    if not reasons:
        return

    reasons_str = "\n".join(f"  • {r}" for r in reasons)
    extra = f"El agente tuvo problemas en esta llamada:\n{reasons_str}\n"
    _send_call_alert_email(
        call_event, restaurant,
        subject_prefix="⚠️ Llamada con problemas",
        reason_display="Problema de calidad",
        reason_bg="#fff7ed", reason_color="#c2410c", reason_border="#fed7aa",
        text_body_extra=extra,
        extra_recipients=None,
    )


def _send_non_customer_alert_email(call_event: CallEvent, restaurant: Restaurant) -> None:
    """Send a business-call alert when a non-customer call is identified."""
    if not restaurant.notify_on_non_customer:
        return
    op = RestaurantMembership.objects.filter(
        restaurant=restaurant, role="operator", is_active=True, notify_on_non_customer=True
    ).select_related("user").first()
    _send_call_alert_email(
        call_event, restaurant,
        subject_prefix="📞 Business Call",
        reason_display="Non-Customer / Business Call",
        reason_bg="#f3f4f6", reason_color="#374151", reason_border="#d1d5db",
        text_body_extra="The AI identified this call as a non-customer business call (vendor, press, sales, service, etc.).\n",
        extra_recipients=[op.notify_email or op.user.email] if op else None,
    )


def _send_low_balance_email(restaurant: Restaurant, balance, level: str) -> None:
    """Send a low-balance alert email to the restaurant owner."""
    from django.core.mail import EmailMultiAlternatives

    notify_email = restaurant.notify_email or (restaurant.user.email if restaurant.user else "")
    if not notify_email:
        return

    base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"
    billing_url = f"{base_url}/portal/{restaurant.slug}/billing/"

    if level == "critical":
        subject = f"🔴 Créditos casi agotados — {restaurant.name}"
        text_body = (
            f"Tu saldo de comunicaciones es ${balance:.2f}.\n"
            "El agente dejará de contestar llamadas si el saldo llega a cero.\n\n"
            f"Recarga ahora: {billing_url}\n"
        )
    else:
        subject = f"🟡 Saldo bajo — {restaurant.name}"
        text_body = (
            f"Tu saldo de comunicaciones bajó a ${balance:.2f}.\n"
            "Recarga pronto para evitar interrupciones en el servicio.\n\n"
            f"Recargar: {billing_url}\n"
        )

    html_body = (
        f"<p>{text_body.replace(chr(10), '<br>')}</p>"
        f"<p><a href='{billing_url}' style='background:#2563eb;color:#fff;padding:10px 20px;"
        f"border-radius:6px;text-decoration:none;font-weight:600;'>Recargar créditos</a></p>"
    )

    try:
        msg = EmailMultiAlternatives(subject, text_body, settings.DEFAULT_FROM_EMAIL, [notify_email])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        logger.info("low_balance_email: [%s] $%.2f sent to %s | restaurant=%s",
                    level, balance, notify_email, restaurant.slug)
    except Exception:
        logger.exception("low_balance_email: failed | restaurant=%s", restaurant.slug)


def _send_payment_failed_email(restaurant: Restaurant) -> None:
    """Send an email to the restaurant owner when their payment fails."""
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string

    notify_email = restaurant.notify_email or (restaurant.user.email if restaurant.user else "")
    if not notify_email:
        return

    base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"
    ctx = {
        "restaurant_name": restaurant.name,
        "billing_url":     f"{base_url}/portal/{restaurant.slug}/billing/",
    }

    html_body = render_to_string("emails/payment_failed.html", ctx)
    text_body = (
        f"⚠️ Payment Failed — {restaurant.name}\n\n"
        "Your subscription payment could not be processed.\n"
        "Please update your payment method to keep your AI agent active.\n\n"
        f"Update now: {ctx['billing_url']}\n"
    )
    subject = f"⚠️ Payment Failed — {restaurant.name}"

    try:
        msg = EmailMultiAlternatives(subject, text_body, settings.DEFAULT_FROM_EMAIL, [notify_email])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        logger.info("payment_failed_email: sent to %s | restaurant=%s", notify_email, restaurant.slug)
    except Exception:
        logger.exception("payment_failed_email: failed | restaurant=%s", restaurant.slug)


def _send_subscription_welcome_email(restaurant: Restaurant) -> None:
    """Send a welcome email when a subscription is activated."""
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string

    notify_email = restaurant.notify_email or (restaurant.user.email if restaurant.user else "")
    if not notify_email:
        return

    base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"
    sub = getattr(restaurant, "subscription", None)
    ctx = {
        "restaurant_name": restaurant.name,
        "portal_url": f"{base_url}/portal/{restaurant.slug}/",
        "period_end": sub.current_period_end.strftime("%B %-d, %Y") if sub and sub.current_period_end else "",
    }

    html_body = render_to_string("emails/subscription_welcome.html", ctx)
    text_body = (
        f"Your Concierge AI subscription for {restaurant.name} is now active.\n"
        f"Your AI phone agent is ready to answer calls 24/7.\n\n"
        f"Go to your portal: {ctx['portal_url']}\n"
    )
    subject = f"Your AI agent is live — {restaurant.name}"

    try:
        msg = EmailMultiAlternatives(subject, text_body, settings.DEFAULT_FROM_EMAIL, [notify_email])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        logger.info("subscription_welcome_email: sent to %s | restaurant=%s", notify_email, restaurant.slug)
    except Exception:
        logger.exception("subscription_welcome_email: failed | restaurant=%s", restaurant.slug)


def _send_subscription_cancelled_email(restaurant: Restaurant) -> None:
    """Send a confirmation email when a subscription is cancelled."""
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string

    notify_email = restaurant.notify_email or (restaurant.user.email if restaurant.user else "")
    if not notify_email:
        return

    base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"
    sub = getattr(restaurant, "subscription", None)
    ctx = {
        "restaurant_name": restaurant.name,
        "billing_url": f"{base_url}/portal/{restaurant.slug}/billing/",
        "period_end": sub.current_period_end.strftime("%B %-d, %Y") if sub and sub.current_period_end else "",
    }

    html_body = render_to_string("emails/subscription_cancelled.html", ctx)
    text_body = (
        f"Your Concierge AI subscription for {restaurant.name} has been cancelled.\n"
    )
    if ctx["period_end"]:
        text_body += f"Your agent will remain active until {ctx['period_end']}.\n"
    text_body += f"\nResubscribe: {ctx['billing_url']}\n"
    subject = f"Subscription cancelled — {restaurant.name}"

    try:
        msg = EmailMultiAlternatives(subject, text_body, settings.DEFAULT_FROM_EMAIL, [notify_email])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        logger.info("subscription_cancelled_email: sent to %s | restaurant=%s", notify_email, restaurant.slug)
    except Exception:
        logger.exception("subscription_cancelled_email: failed | restaurant=%s", restaurant.slug)


def _send_knowledge_gap_alert(restaurant: Restaurant, detail) -> None:
    """
    Alert the owner immediately when Retell detected the agent couldn't answer a question.
    Only fires when unanswered_question is non-empty (avoids false positives).
    """
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string

    unanswered = (detail.call_signals.get("unanswered_question") or "").strip()
    if not unanswered:
        return

    notify_email = restaurant.notify_email or (restaurant.user.email if restaurant.user else "")
    if not notify_email:
        return

    base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"
    kb_url = f"{base_url}/portal/{restaurant.slug}/knowledge-base/"

    agent_response = (detail.call_signals.get("agent_response_to_unanswered") or "").strip()
    subject = f"El agente no pudo responder una pregunta — {restaurant.name}"
    text_body = (
        f'Un cliente preguntó:\n  "{unanswered}"\n\n'
        f'El agente respondió:\n  "{agent_response}"\n\n'
        f"Actualiza el Knowledge Base para que el agente pueda responder esto en futuras llamadas:\n"
        f"{kb_url}\n"
    )

    html_body = render_to_string("emails/knowledge_gap_alert.html", {
        "restaurant_name": restaurant.name,
        "unanswered":      unanswered,
        "agent_response":  agent_response,
        "kb_url":          kb_url,
    })

    try:
        msg = EmailMultiAlternatives(subject, text_body, settings.DEFAULT_FROM_EMAIL, [notify_email])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        logger.info("knowledge_gap_alert: sent to %s | question=%r | restaurant=%s",
                    notify_email, unanswered[:80], restaurant.slug)
    except Exception:
        logger.exception("knowledge_gap_alert: failed | restaurant=%s", restaurant.slug)


def _send_post_call_sms(call_event: CallEvent, restaurant: Restaurant) -> None:
    """Send a contextual follow-up SMS after call_ended, but only if no SMS was already sent."""
    if not restaurant.enable_sms:
        return

    from twilio.rest import Client as TwilioClient

    call_payload = call_event.payload.get("call", {})
    caller_phone = (call_payload.get("from_number") or "").strip()
    call_id      = (call_payload.get("call_id") or "").strip()

    if not caller_phone:
        return

    already_sent = (
        SmsLog.objects.filter(call_event__payload__call__call_id=call_id).exists()
        if call_id else
        SmsLog.objects.filter(call_event=call_event).exists()
    )
    if already_sent:
        return

    # Only use restaurant credentials if ALL three are present — mixing accounts
    # causes "Mismatch between From number and account" errors.
    if restaurant.twilio_account_sid and restaurant.twilio_auth_token and restaurant.twilio_from_number:
        account_sid = restaurant.twilio_account_sid
        auth_token  = restaurant.twilio_auth_token
        from_number = restaurant.twilio_from_number
    else:
        account_sid = settings.TWILIO_ACCOUNT_SID
        auth_token  = settings.TWILIO_AUTH_TOKEN
        from_number = settings.TWILIO_FROM_NUMBER

    if not (account_sid and auth_token and from_number):
        return

    try:
        detail = call_event.detail
    except CallDetail.DoesNotExist:
        detail = None

    kb       = getattr(restaurant, "knowledge_base", None)
    greeting = "Hi!"

    # Only send post-call SMS for calls with an actionable reason.
    # A generic "thanks for calling" blast to every caller is spam.
    if detail and detail.wants_reservation:
        website = restaurant.website or ""
        parts = []
        if detail.party_size:        parts.append(f"{detail.party_size} guests")
        if detail.reservation_date:  parts.append(detail.reservation_date.strftime("%a %b %-d"))
        if detail.reservation_time and detail.reservation_date:  parts.append(f"at {detail.reservation_time.strftime('%-I:%M %p')}")
        if not parts:
            return  # не отправлять SMS если нет конкретных деталей бронирования
        summary = ", ".join(parts)
        message = f"{greeting} Your request at {restaurant.name} ({summary}) is noted. Book instantly: {website}"
    elif detail and detail.call_reason == "menu":
        menu_url = (kb.food_menu_url if kb else "") or restaurant.website or ""
        message  = f"{greeting} Here's the {restaurant.name} menu: {menu_url}"
    elif detail and detail.call_reason == "parking":
        address = restaurant.address_full or ""
        valet_cost = (kb.valet_cost if kb else "") or ""
        valet_part = f" Valet parking available{f' — {valet_cost}' if valet_cost else ''}. Street parking also nearby."
        message = f"{greeting} {restaurant.name} is located at {address}.{valet_part}"
    elif detail and detail.call_reason == "private_event":
        contact = restaurant.contact_email or (kb.press_contact if kb else "") or ""
        message = (
            f"{greeting} Thank you for your interest in hosting a private event at {restaurant.name}."
            + (f" For availability and packages, please contact us at: {contact}" if contact else "")
        )
    elif detail and detail.call_reason == "bar_menu":
        bar_url = (kb.bar_menu_url if kb else "") or restaurant.website or ""
        message = f"{greeting} Here's the {restaurant.name} drinks menu: {bar_url}"
    elif detail and detail.call_reason == "happy_hour":
        hh = (kb.happy_hour_details if kb else "") or ""
        website = restaurant.website or ""
        message = (
            f"{greeting} {restaurant.name} happy hour: {hh}"
            if hh else
            f"{greeting} Check out {restaurant.name} happy hour details at: {website}"
        )
    else:
        # No actionable reason — skip the post-call SMS entirely.
        return

    message = message[:320]

    callback_url = (
        f"{settings.RETELL_WEBHOOK_BASE_URL}/api/retell/twilio/sms-status/"
        if settings.RETELL_WEBHOOK_BASE_URL else None
    )

    log = SmsLog(restaurant=restaurant, call_event=call_event, to_number=caller_phone, message=message)
    try:
        twilio_client = TwilioClient(account_sid, auth_token)
        create_kwargs = {"body": message, "from_": from_number, "to": caller_phone}
        if callback_url:
            create_kwargs["status_callback"] = callback_url
        msg = twilio_client.messages.create(**create_kwargs)
        log.status     = SmsLog.STATUS_SENT
        log.twilio_sid = msg.sid
        log.save()
        logger.info("post_call_sms: sent to %s (sid=%s)", caller_phone, msg.sid)
    except Exception as exc:
        log.status        = SmsLog.STATUS_FAILED
        log.error_message = str(exc)
        log.save()
        logger.exception("post_call_sms: failed to send to %s", caller_phone)


@csrf_exempt
def retell_events_webhook(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    raw = request.body.decode("utf-8")
    sig = request.headers.get("x-retell-signature", "")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Retell events webhook | Invalid JSON payload")
        return JsonResponse({"detail": "invalid json"}, status=400)

    _call = data.get("call", {})
    _evt = data.get("event", "?")
    _cid = _call.get("call_id", "?")
    _from = _call.get("from_number", "?")
    _to = _call.get("to_number", "?")
    _dur = f" | duration={round(_call['duration_ms']/1000)}s" if "duration_ms" in _call else ""
    logger.info("Retell event=%s | call_id=%s | from=%s → to=%s%s", _evt, _cid, _from, _to, _dur)

    to_number = (data.get("to_number") or data.get("call", {}).get("to_number") or "").strip()
    restaurant = Restaurant.objects.filter(retell_phone_number=to_number).first()
    if not restaurant:
        logger.warning("Retell events webhook | Unknown number: %r", to_number)
        return JsonResponse({"detail": "unknown number"}, status=404)

    if not sig or not restaurant.retell_api_key:
        return JsonResponse({"detail": "unauthorized"}, status=401)
    retell_client = Retell(api_key=restaurant.retell_api_key)
    if not retell_client.verify(raw, restaurant.retell_api_key, sig):
        logger.warning(
            "Retell events webhook | Invalid signature | restaurant=%s | Check if Retell Webhook Secret differs from API Key.",
            restaurant.slug
        )
        return JsonResponse({"detail": "invalid signature"}, status=401)

    # Retell sends "event" (not "event_type") — fall back for safety
    event_type = data.get("event") or data.get("event_type", "")
    call_event = CallEvent.objects.create(restaurant=restaurant, event_type=event_type, payload=data)

    if event_type == "call_ended":
        # ── Subtract Call Cost from Communication Balance ─────────────────────
        call_payload = data.get("call", {})

        # Retell API structure changed: combined_cost is now inside call_cost
        cost_data = call_payload.get("call_cost", {})
        if isinstance(cost_data, dict) and "combined_cost" in cost_data:
            combined_cost_cents = cost_data.get("combined_cost")
        else:
            combined_cost_cents = call_payload.get("combined_cost") # Legacy fallback

        marked_up_cost = None
        if combined_cost_cents is not None:
            try:
                from decimal import Decimal
                sub = getattr(restaurant, "subscription", None)
                if sub:
                    # Retell returns cost in cents. Convert to USD.
                    raw_cost_usd = Decimal(str(combined_cost_cents)) / Decimal("100")
                    markup = sub.communication_markup or Decimal("1.30")
                    marked_up_cost = (raw_cost_usd * markup).quantize(Decimal("0.0001"))

                    balance_before = sub.communication_balance
                    sub.communication_balance -= marked_up_cost
                    sub.save(update_fields=["communication_balance"])
                    balance_after = sub.communication_balance

                    logger.info("Retell call_ended | base_cost_usd=%.4f × markup=%.2f = final_deduction=%.4f | balance=%.2f | restaurant=%s",
                                raw_cost_usd, markup, marked_up_cost, balance_after, restaurant.slug)

                    # Disconnect Retell if balance depleted
                    if balance_after <= 0:
                        _disconnect_retell_phone(restaurant)
                        _notify_service_disconnected(restaurant, "Tu saldo de comunicación se ha agotado. Tu agente de IA ya no está contestando llamadas. Recarga tu saldo para reactivar el servicio.")

                    # Send low-balance alert on first threshold crossing
                    from decimal import Decimal as _D
                    if balance_before > _D("3") and balance_after <= _D("3"):
                        _send_low_balance_email(restaurant, balance_after, "critical")
                    elif balance_before > _D("8") and balance_after <= _D("8"):
                        _send_low_balance_email(restaurant, balance_after, "warning")
            except Exception:
                logger.exception("Failed to update communication balance for restaurant=%s", restaurant.slug)

    elif event_type == "call_analyzed":
        # ── Extract Call Details and Send Notifications ───────────────────────
        try:
            _build_call_detail_from_payload(call_event)
        except Exception:
            logger.exception("Failed to build CallDetail for CallEvent pk=%s", call_event.pk)

        try:
            _upsert_caller_memory(call_event, restaurant)
        except Exception:
            logger.exception("Failed to upsert CallerMemory for CallEvent pk=%s", call_event.pk)

        # Always clean up call_in_progress placeholders for this call_id,
        # even if _build_call_detail_from_payload raised an exception.
        _analyzed_call_id = data.get("call", {}).get("call_id", "")
        if _analyzed_call_id:
            CallEvent.objects.filter(
                event_type="call_in_progress",
                payload__call__call_id=_analyzed_call_id,
            ).exclude(pk=call_event.pk).delete()

        # Calculate marked-up cost again just to store it on the detail record
        call_payload = data.get("call", {})
        cost_data = call_payload.get("call_cost", {})
        if isinstance(cost_data, dict) and "combined_cost" in cost_data:
            combined_cost_cents = cost_data.get("combined_cost")
        else:
            combined_cost_cents = call_payload.get("combined_cost")

        marked_up_cost = None
        if combined_cost_cents is not None:
            try:
                from decimal import Decimal
                sub = getattr(restaurant, "subscription", None)
                if sub:
                    raw_cost_usd = Decimal(str(combined_cost_cents)) / Decimal("100")
                    markup = sub.communication_markup or Decimal("1.30")
                    marked_up_cost = (raw_cost_usd * markup).quantize(Decimal("0.0001"))
            except Exception:
                logger.exception(
                    "call_analyzed: failed to calculate marked_up_cost | call_event=%s | raw_cents=%s",
                    call_event.pk, combined_cost_cents,
                )

        if marked_up_cost is not None:
            try:
                CallDetail.objects.filter(call_event=call_event).update(call_cost=marked_up_cost)
            except Exception:
                logger.exception("Failed to store call_cost for CallEvent pk=%s", call_event.pk)

        # ── Send event-driven email alerts ───────────────────────────────────
        try:
            detail = getattr(call_event, "detail", None)
            if detail:
                if detail.call_reason == "non_customer":
                    _send_non_customer_alert_email(call_event, restaurant)
                else:
                    # Each alert type is independent — a call can trigger multiple
                    if detail.needs_review:
                        _send_defective_call_alert_email(call_event, restaurant)
                    if detail.wants_reservation:
                        _send_reservation_alert_email(call_event, restaurant)
                    if detail.follow_up_needed:
                        _send_followup_alert_email(call_event, restaurant)
                    if detail.call_reason == "complaint":
                        _send_complaint_alert_email(call_event, restaurant)
                if (not detail.is_spam
                        and detail.call_signals.get("agent_failed_to_answer")
                        and (detail.call_signals.get("unanswered_question") or "").strip()
                        and restaurant.notify_via_email):
                    _send_knowledge_gap_alert(restaurant, detail)
        except Exception:
            logger.exception("Failed to send alert email(s) for CallEvent pk=%s", call_event.pk)

        # Automatic post-call SMS disabled — only consent-based SMS via retell_tool_send_sms
        # try:
        #     _send_post_call_sms(call_event, restaurant)
        # except Exception:
        #     logger.exception("Failed to send post-call SMS for CallEvent pk=%s", call_event.pk)

    return JsonResponse({"status": "ok"}, status=200)


# ─── get_info Tool ────────────────────────────────────────────────────────────

def _format_kb_topic(kb, topic: str, restaurant=None, lang: str = "en") -> str:
    """Return a clean text block for a given KB topic. Empty fields are omitted."""
    lines = []

    def add(label, value):
        v = str(value).strip() if value is not None else ""
        if v and v not in ("N/A", "None", "0", "False", ""):
            lines.append(f"{label}: {v}")

    if topic == "hours":
        add("Hours of operation", kb.hours_of_operation)
        add("Kitchen closes", kb.kitchen_closing_time)
        add("Holiday closures", kb.holiday_closure_notes or ("Closed on major holidays" if kb.closes_on_holidays else ""))
        add("Operational closures & date-specific changes", kb.private_event_closures)
        # special_events_info removed — entertainment events do NOT block reservations

    elif topic == "menu":
        if any([kb.menu_cuisine_type, kb.menu_best_sellers, kb.menu_price_range]):
            add("Cuisine & concept", kb.menu_cuisine_type)
            add("Signature dishes", kb.menu_best_sellers)
            add("Price range", kb.menu_price_range)
            add("Menu sections", kb.menu_categories)
        else:
            add("Food menu", kb.food_menu_summary)  # legacy fallback
        if kb.food_menu_url:
            lines.append(f"Menu link (SMS only — never read aloud): {kb.food_menu_url}")

    elif topic == "bar_menu":
        if any([kb.bar_concept, kb.bar_signature_drinks]):
            add("Bar concept", kb.bar_concept)
            add("Signature cocktails", kb.bar_signature_drinks)
            add("Wine & beer", kb.bar_wine_beer)
        else:
            add("Bar & cocktails", kb.bar_menu_summary)  # legacy fallback
        add("Bottle service", kb.bottle_service)
        if kb.bar_menu_url:
            lines.append(f"Bar menu link (SMS only — never read aloud): {kb.bar_menu_url}")

    elif topic == "happy_hour":
        add("Happy hour", kb.happy_hour_details)

    elif topic == "dietary":
        add("Dietary options", kb.dietary_options)

    elif topic == "parking":
        add("Free parking", kb.free_parking_info)
        if kb.has_valet:
            add("Valet", kb.valet_cost or "Available")
        else:
            lines.append("Valet: Not available")

    elif topic == "billing":
        if kb.auto_gratuity:
            lines.append("Auto-gratuity: Yes")
        add("Service charge", f"{kb.service_charge_pct} ({kb.get_service_charge_scope_display()})" if kb.service_charge_pct else None)
        add("Max cards to split", str(kb.max_cards_to_split) if kb.max_cards_to_split else None)
        add("Corkage policy", kb.corkage_policy)
        add("Cover / show charge", kb.cover_charge)
        add("No-show fee", kb.no_show_fee)

    elif topic == "reservations":
        add("Grace period", f"{kb.reservation_grace_min} minutes" if kb.reservation_grace_min else None)
        add("Large party threshold", f"{kb.large_party_min_guests}+ guests" if kb.large_party_min_guests else None)

    elif topic == "private_events":
        lines.append(f"Private dining: {'Available' if kb.has_private_dining else 'Not available'}")
        add("Minimum spend", kb.private_dining_min_spend)
        lines.append(f"Decorations: {'Allowed' if kb.allows_decorations else 'Not allowed'}")
        add("Cleaning fee", kb.decoration_cleaning_fee)
        if kb.press_contact:
            import re as _re
            spoken = _re.sub(
                r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
                lambda m: _spoken_email(m.group(), lang),
                kb.press_contact,
            )
            lines.append(f"Press / partnerships: {spoken}")
        add("Additional info", kb.additional_info)

    elif topic == "ambience":
        if kb.has_live_music:
            add("Live music", kb.live_music_details)
            add("Party vibe starts", kb.party_vibe_start_time)
        add("Noise level", kb.get_noise_level_display() if kb.noise_level else None)
        add("Dress code", kb.dress_code)
        add("Special events & entertainment", kb.special_events_info)

    elif topic == "facilities":
        if restaurant and restaurant.location_reference:
            add("Location & how to find us", restaurant.location_reference)
        lines.append(f"Terrace: {'Yes' if kb.has_terrace else 'No'}")
        add("Air conditioning", kb.get_ac_intensity_display() if kb.ac_intensity else None)
        lines.append(f"Stroller-friendly: {'Yes' if kb.stroller_friendly else 'No'}")

    elif topic == "special_events":
        add("Special events & entertainment", kb.special_events_info)
        if not lines:
            lines.append("No specific event details are available right now.")

    elif topic == "additional":
        add("Affiliated restaurants", kb.affiliated_restaurants)
        add("Additional info", kb.additional_info)
        if kb.owner_notes.strip():
            lines.append(kb.owner_notes.strip())

    else:
        return "Unknown topic. Use one of: hours, menu, bar_menu, happy_hour, dietary, parking, billing, reservations, private_events, ambience, facilities, special_events, additional."

    return "\n".join(lines) if lines else "No information available for this topic."


@csrf_exempt
def retell_tool_get_info(request):
    """Retell custom tool — fetches a specific KB section on demand."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"result": "error: invalid json"}, status=400)

    call      = data.get("call", {})
    to_number = call.get("to_number", "").strip()
    topic     = data.get("args", {}).get("topic", "").strip().lower()

    restaurant = Restaurant.objects.filter(retell_phone_number=to_number, is_active=True).first()
    if not restaurant:
        logger.warning("retell_tool_get_info | Unknown number: %r", to_number)
        return JsonResponse({"result": "Restaurant info not available."})

    kb = getattr(restaurant, "knowledge_base", None)
    if not kb:
        return JsonResponse({"result": "No information configured for this topic yet."})

    result = _format_kb_topic(kb, topic, restaurant=restaurant, lang=restaurant.primary_lang)
    logger.info("get_info: restaurant=%s topic=%r → %d chars", restaurant.slug, topic, len(result))
    return JsonResponse({"result": result})


@csrf_exempt
def retell_tool_get_caller_profile(request):
    """
    Retell custom tool — returns the full CallerMemory profile for the current caller.

    Security: caller is identified exclusively from call.from_number (Retell call context),
    never from agent-supplied parameters. All DB access is via Django ORM (parameterized).
    This endpoint is read-only — no writes occur during the call.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"result": "error: invalid json"}, status=400)

    call        = data.get("call", {})
    to_number   = call.get("to_number", "").strip()   # identifies the restaurant
    from_number = call.get("from_number", "").strip()  # identifies the caller

    if not from_number:
        return JsonResponse({"result": "No caller number available."})

    restaurant = Restaurant.objects.filter(retell_phone_number=to_number, is_active=True).first()
    if not restaurant:
        return JsonResponse({"result": "Restaurant not found."})

    try:
        mem = CallerMemory.objects.get(phone=from_number, restaurant=restaurant)
    except CallerMemory.DoesNotExist:
        return JsonResponse({"result": "No profile found for this caller."})

    parts = []
    if mem.name:
        parts.append(f"Name: {mem.name}")
    if mem.email:
        parts.append(f"Email: {mem.email}")
    parts.append(f"Total calls: {mem.call_count}")
    if mem.last_call_at:
        parts.append(f"Last call: {mem.last_call_at.strftime('%b %d, %Y')}")
    if mem.last_call_summary:
        parts.append(f"Last call summary: {mem.last_call_summary}")
    if mem.preferences:
        parts.append(f"Preferences: {mem.preferences}")
    if mem.staff_notes:
        parts.append(f"Staff notes: {mem.staff_notes}")

    logger.info(
        "get_caller_profile: restaurant=%s caller=%s → %d fields returned",
        restaurant.slug, from_number[-4:], len(parts),
    )
    return JsonResponse({"result": "\n".join(parts)})


def _send_sms_via_twilio(restaurant, to_number: str, message: str) -> str:
    """Send an SMS using restaurant or platform Twilio credentials. Returns Twilio SID.

    Deducts sms_unit_cost from the restaurant's communication_balance only when
    using platform credentials (not when the restaurant has its own Twilio account).
    """
    from twilio.rest import Client as TwilioClient
    from decimal import Decimal

    using_own_credentials = bool(
        restaurant and
        restaurant.twilio_account_sid and
        restaurant.twilio_auth_token and
        restaurant.twilio_from_number
    )

    if using_own_credentials:
        account_sid = restaurant.twilio_account_sid
        auth_token  = restaurant.twilio_auth_token
        from_number = restaurant.twilio_from_number
    else:
        account_sid = settings.TWILIO_ACCOUNT_SID
        auth_token  = settings.TWILIO_AUTH_TOKEN
        from_number = settings.TWILIO_FROM_NUMBER

    client = TwilioClient(account_sid, auth_token)
    msg = client.messages.create(body=message, from_=from_number, to=to_number)

    # Deduct from balance only when using platform Twilio
    if not using_own_credentials and restaurant:
        sub = getattr(restaurant, "subscription", None)
        if sub:
            unit_cost = sub.sms_unit_cost or Decimal("0")
            if unit_cost > 0:
                sub.communication_balance -= unit_cost
                sub.save(update_fields=["communication_balance"])
                logger.info(
                    "sms_billing: deducted %.4f for SMS to %s | balance=%.2f | restaurant=%s",
                    unit_cost, to_number, sub.communication_balance, restaurant.slug,
                )

    return msg.sid


def _build_sms_message(sms_type: str, restaurant, kb, custom_message: str = "") -> str | None:
    """Build an SMS message from DB data based on the requested type."""
    name = restaurant.name if restaurant else ""
    lang = restaurant.primary_lang if restaurant else "en"

    if sms_type == "menu_link":
        url = (kb.food_menu_url if kb else "") or (restaurant.website if restaurant else "")
        if not url:
            return None
        return (f"Menú de {name}: {url}" if lang == "es" else f"{name} menu: {url}")[:160]

    if sms_type == "bar_menu_link":
        url = (kb.bar_menu_url if kb else "") or (restaurant.website if restaurant else "")
        if not url:
            return None
        return (f"Carta de bar de {name}: {url}" if lang == "es" else f"{name} bar menu: {url}")[:160]

    if sms_type == "hours":
        hours = (kb.hours_of_operation if kb else "") or ""
        if not hours:
            return None
        return (f"{name} — horario: {hours}" if lang == "es" else f"{name} — hours: {hours}")[:160]

    if sms_type == "music":
        details = (kb.live_music_details if kb else "") or ""
        url     = restaurant.social_media_url if restaurant else ""
        if details:
            base = f"{name} - música en vivo: {details}" if lang == "es" else f"{name} - live music: {details}"
            if url and len(base) + len(url) + 2 <= 160:
                base += f" {url}"
            return base[:160]
        if url:
            return (f"{name} - música en vivo: {url}" if lang == "es" else f"{name} - live music: {url}")[:160]
        return None

    if sms_type == "valet":
        if not kb:
            return None
        parts = []
        if kb.has_valet:
            valet = f"Valet disponible" if lang == "es" else "Valet available"
            if kb.valet_cost:
                valet += f" — {kb.valet_cost}"
            parts.append(valet)
        if kb.free_parking_info:
            parts.append(kb.free_parking_info)
        if not parts:
            return None
        body = f"{name}: " + " | ".join(parts)
        return body[:160]

    if sms_type == "social_media":
        url = (restaurant.social_media_url if restaurant else "") or (restaurant.website if restaurant else "")
        if not url:
            return None
        return (f"Síguenos en {name}: {url}" if lang == "es" else f"Follow {name}: {url}")[:160]

    if sms_type == "address":
        address = restaurant.address_full if restaurant else ""
        if not address:
            return None
        return (f"{name} está en: {address}" if lang == "es" else f"{name} is at: {address}")[:160]

    if sms_type == "event_inquiry":
        email = restaurant.contact_email if restaurant else ""
        url   = restaurant.website if restaurant else ""
        if email:
            return (f"Eventos en {name}: {email}" if lang == "es" else f"{name} events: {email}")[:160]
        if url:
            return (f"Eventos en {name}: {url}" if lang == "es" else f"{name} events: {url}")[:160]
        return None

    if sms_type == "website":
        url = restaurant.website if restaurant else ""
        if not url:
            return None
        return f"{name}: {url}"[:160]

    if sms_type == "custom":
        return custom_message[:160] if custom_message else None

    return None


@csrf_exempt
def retell_tool_send_sms(request):
    """Retell custom tool webhook — sends an SMS to the caller via Twilio."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"result": "error: invalid json"}, status=400)

    call      = data.get("call", {})
    to_number = call.get("from_number", "").strip()   # caller's number → SMS destination
    to_retell = call.get("to_number", "").strip()     # our Retell number → identify restaurant
    sms_type       = data.get("args", {}).get("sms_type", "").strip()
    custom_message = data.get("args", {}).get("message", "").strip()

    if not to_number or not sms_type:
        return JsonResponse({"result": "error: missing to_number or sms_type"})

    restaurant = Restaurant.objects.select_related("knowledge_base").filter(
        retell_phone_number=to_retell, is_active=True
    ).first()
    kb         = getattr(restaurant, "knowledge_base", None)
    call_id    = call.get("call_id")
    call_event = (
        CallEvent.objects.filter(payload__call__call_id=call_id).first()
        if call_id else None
    )

    message = _build_sms_message(sms_type, restaurant, kb, custom_message)
    if not message:
        return JsonResponse({"result": f"error: no content available for sms_type '{sms_type}'"})

    log = SmsLog(
        restaurant=restaurant,
        call_event=call_event,
        to_number=to_number,
        message=message,
    )

    try:
        log.twilio_sid = _send_sms_via_twilio(restaurant, to_number, message)
        log.status     = SmsLog.STATUS_SENT
        log.save()
        return JsonResponse({"result": f"SMS sent successfully to {to_number}"})
    except Exception as e:
        log.status        = SmsLog.STATUS_FAILED
        log.error_message = str(e)
        log.save()
        logger.exception("SMS send failed to %s", to_number)
        return JsonResponse({"result": f"SMS could not be sent: {e}"})


@csrf_exempt
def twilio_sms_status_webhook(request):
    """Twilio status callback — updates SmsLog.status when a message is delivered or fails."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    message_sid    = request.POST.get("MessageSid", "").strip()
    message_status = request.POST.get("MessageStatus", "").strip()

    if not message_sid:
        return HttpResponse(status=400)

    log = SmsLog.objects.select_related("restaurant").filter(twilio_sid=message_sid).first()
    if not log:
        # Unknown SID — not from this system, acknowledge silently
        return HttpResponse(status=200)

    # ── Validate Twilio signature ──────────────────────────────────────────────
    from twilio.request_validator import RequestValidator
    r = log.restaurant
    auth_token = (
        r.twilio_auth_token
        if r and r.twilio_account_sid and r.twilio_auth_token and r.twilio_from_number
        else settings.TWILIO_AUTH_TOKEN
    )
    validator = RequestValidator(auth_token)
    url       = request.build_absolute_uri()
    signature = request.META.get("HTTP_X_TWILIO_SIGNATURE", "")
    if not validator.validate(url, request.POST, signature):
        logger.warning("twilio_sms_status: invalid signature for sid=%s", message_sid)
        return HttpResponse(status=403)

    # ── Update status ──────────────────────────────────────────────────────────
    if message_status == "delivered":
        log.status       = SmsLog.STATUS_DELIVERED
        log.delivered_at = timezone.now()
        log.save(update_fields=["status", "delivered_at"])
        logger.info("twilio_sms_status: delivered sid=%s", message_sid)
    elif message_status in ("undelivered", "failed"):
        error_code = request.POST.get("ErrorCode", "")
        log.status        = SmsLog.STATUS_FAILED
        log.error_message = f"Twilio: {message_status}" + (f" (code {error_code})" if error_code else "")
        log.save(update_fields=["status", "error_message"])
        logger.warning("twilio_sms_status: %s sid=%s code=%s", message_status, message_sid, error_code)
    # "queued", "sending", "sent" are intermediate — no action needed

    return HttpResponse(status=204)



@csrf_exempt
def retell_tool_save_caller_info(request):
    """Retell custom tool — saves caller name to CallDetail silently during the call."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"result": "error: invalid json"}, status=400)

    call          = data.get("call", {})
    args          = data.get("args", {})
    call_id       = call.get("call_id", "").strip()
    from_number   = call.get("from_number", "").strip()
    to_number     = call.get("to_number", "").strip()
    caller_name   = args.get("caller_name", "").strip()[:255]
    caller_email  = args.get("caller_email", "").strip()[:255]
    note          = args.get("note", "").strip()[:1000]
    # AI-driven follow-up flag — explicit signal from the agent
    follow_up_raw = args.get("follow_up_needed")
    follow_up     = bool(follow_up_raw) if follow_up_raw is not None else None

    if not caller_name:
        return JsonResponse({"result": "error: caller_name is required"}, status=400)

    restaurant = Restaurant.objects.filter(retell_phone_number=to_number, is_active=True).first()

    call_event = None
    if call_id:
        call_event = CallEvent.objects.filter(
            payload__call__call_id=call_id
        ).order_by("created_at").first()

    if call_event is None and restaurant:
        call_event = CallEvent.objects.create(
            restaurant=restaurant,
            event_type="call_in_progress",
            payload={"call": {"call_id": call_id, "from_number": from_number, "to_number": to_number}},
        )

    if call_event is None:
        logger.warning("save_caller_info: no call_event for call_id=%r", call_id)
        return JsonResponse({"result": "Info saved"})

    detail, created = CallDetail.objects.get_or_create(
        call_event=call_event,
        defaults={
            "caller_name": caller_name,
            "caller_phone": from_number,
            "caller_email": caller_email,
            "notes": note,
            "follow_up_needed": follow_up or False,
        },
    )
    if not created:
        update_fields = []
        if detail.caller_name != caller_name:
            detail.caller_name = caller_name
            update_fields.append("caller_name")
        if from_number and detail.caller_phone != from_number:
            detail.caller_phone = from_number
            update_fields.append("caller_phone")
        if caller_email and detail.caller_email != caller_email:
            detail.caller_email = caller_email
            update_fields.append("caller_email")
        if note and note not in detail.notes:
            detail.notes = f"{detail.notes}\n{note}".strip() if detail.notes else note
            update_fields.append("notes")
        # Only set follow_up_needed to True — never clear it mid-call
        if follow_up and not detail.follow_up_needed:
            detail.follow_up_needed = True
            update_fields.append("follow_up_needed")
        if update_fields:
            detail.save(update_fields=update_fields + ["updated_at"])

    if follow_up:
        logger.info("save_caller_info: follow_up_needed flagged for call_id=%r restaurant=%s", call_id, to_number)

    return JsonResponse({"result": "Info saved"})


@csrf_exempt
def retell_tool_resolve_date(request):
    """Retell custom tool — resolves a relative date phrase to an actual calendar date."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        logger.error("Retell tool resolve_date | Invalid JSON payload")
        return JsonResponse({"error": "invalid json"}, status=400)

    args      = data.get("args", {})
    call      = data.get("call", {})
    to_number = call.get("to_number", "").strip()
    text      = args.get("text", "").strip()

    if not text:
        return JsonResponse({"date_iso": "", "spoken_es": "", "spoken_en": "",
                             "is_past": False, "ambiguity": "No date text provided."})

    restaurant = Restaurant.objects.filter(retell_phone_number=to_number, is_active=True).first()
    tz_name = restaurant.timezone if restaurant else "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    today = datetime.now(tz=tz).date()

    resolved, ambiguity = _resolve_relative_date(text, today)

    if resolved is None:
        return JsonResponse({
            "date_iso": "", "spoken_es": "", "spoken_en": "",
            "is_past": False, "ambiguity": ambiguity or "Could not parse date.",
        })

    is_past = resolved < today
    return JsonResponse({
        "date_iso":  resolved.isoformat(),
        "spoken_es": _spoken_date_es(resolved),
        "spoken_en": _spoken_date_en(resolved),
        "is_past":   is_past,
        "ambiguity": "",
    })


# ─── KB Quality Helpers ───────────────────────────────────────────────────────

def _kb_lint(restaurant, kb):
    """
    Returns {"errors": [...], "warnings": [...], "error_tabs": [...], "warning_tabs": [...]} for the KB page.
    Errors = critical missing fields (red). Warnings = long/suboptimal fields (yellow).
    Tab IDs match the data-bs-target values in the template (without #tab- prefix).
    """
    errors = []
    warnings = []
    error_tabs = []
    warning_tabs = []

    if not restaurant.website:
        errors.append("Website URL is missing. Many agent responses route callers to your website.")
        if "basic" not in error_tabs:
            error_tabs.append("basic")
    if not restaurant.welcome_phrase:
        errors.append("Opening greeting is missing. The agent won't know how to answer the phone.")
        if "basic" not in error_tabs:
            error_tabs.append("basic")
    if kb and not kb.hours_of_operation:
        errors.append("Hours of operation are missing. Callers frequently ask when you're open.")
        if "hours" not in error_tabs:
            error_tabs.append("hours")

    if kb:
        if len(kb.food_menu_summary) > 1500:
            warnings.append(
                f"Food menu summary is long ({len(kb.food_menu_summary)} chars). "
                "Consider trimming to under 1500 characters — long descriptions reduce agent accuracy."
            )
            if "menu" not in warning_tabs:
                warning_tabs.append("menu")
        if len(kb.bar_menu_summary) > 1500:
            warnings.append(
                f"Bar menu summary is long ({len(kb.bar_menu_summary)} chars). "
                "Consider trimming to under 1500 characters — long descriptions reduce agent accuracy."
            )
            if "menu" not in warning_tabs:
                warning_tabs.append("menu")
        if len(kb.happy_hour_details) > 400:
            warnings.append(
                f"Happy hour details are long ({len(kb.happy_hour_details)} chars). "
                "Use 1–2 sentences: days, times, and the main deal."
            )
            if "menu" not in warning_tabs:
                warning_tabs.append("menu")
        if len(kb.additional_info) > 1500:
            warnings.append(
                f"Additional info is very long ({len(kb.additional_info)} chars). "
                "Consider moving content to specific fields — long dumps reduce agent accuracy."
            )
            if "other" not in warning_tabs:
                warning_tabs.append("other")
        if len(kb.owner_notes) > 1500:
            warnings.append(
                f"Custom info is very long ({len(kb.owner_notes)} chars). "
                "Consider trimming — long dumps reduce agent accuracy."
            )
            if "other" not in warning_tabs:
                warning_tabs.append("other")

    return {
        "errors": errors,
        "warnings": warnings,
        "error_tabs": error_tabs,
        "warning_tabs": warning_tabs,
    }


def _kb_health_score(restaurant):
    """
    Returns (score_pct: int, critical_missing: list[str]).
    score_pct is 0–100 based on how many key fields are filled.
    """
    kb = getattr(restaurant, "knowledge_base", None)

    critical_missing = []
    if not restaurant.website:
        critical_missing.append("Website URL")
    if not restaurant.welcome_phrase:
        critical_missing.append("Opening greeting")
    if not restaurant.address_full:
        critical_missing.append("Address")
    if kb and not kb.hours_of_operation:
        critical_missing.append("Hours of operation")
    _menu_filled = kb and (kb.food_menu_url or kb.menu_cuisine_type or kb.menu_best_sellers or kb.food_menu_summary)
    if not _menu_filled:
        critical_missing.append("Menu info")

    scored = [
        restaurant.website, restaurant.address_full, restaurant.welcome_phrase,
    ]
    if kb:
        scored += [
            kb.hours_of_operation,
            kb.food_menu_url,
            kb.menu_cuisine_type, kb.menu_best_sellers, kb.menu_price_range,
            kb.bar_concept, kb.bar_signature_drinks,
            kb.happy_hour_details, kb.dietary_options,
            str(kb.reservation_grace_min) if kb.reservation_grace_min else "",
        ]

    filled = sum(1 for f in scored if f)
    score  = int(filled / len(scored) * 100)

    return score, critical_missing


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

def _get_login_redirect(user):
    """Return redirect to dashboard (single membership) or selector (multiple), or None."""
    memberships = RestaurantMembership.objects.filter(
        user=user, is_active=True,
    ).select_related("restaurant")
    count = memberships.count()
    if count == 0:
        return None
    if count == 1:
        return redirect("portal_dashboard", slug=memberships.first().restaurant.slug)
    return redirect("portal_select_restaurant")


@login_required(login_url="portal_login")
def portal_select_restaurant(request):
    """Show a restaurant picker for users with multiple memberships."""
    memberships = RestaurantMembership.objects.filter(
        user=request.user, is_active=True,
    ).select_related("restaurant").order_by("restaurant__name")

    if memberships.count() <= 1:
        redir = _get_login_redirect(request.user)
        return redir or redirect("portal_login")

    return render(request, "portal/select_restaurant.html", {
        "memberships": memberships,
    })


def portal_login(request):
    if request.user.is_authenticated:
        redir = _get_login_redirect(request.user)
        if redir:
            return redir

    error = None
    if request.method == "POST":
        from django.contrib.auth import get_user_model
        User = get_user_model()
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "").strip()
        user = None
        try:
            user_obj = User.objects.get(email__iexact=email)
            user = authenticate(request, username=user_obj.username, password=password)
        except User.DoesNotExist:
            pass
        if user is not None:
            login(request, user)
            redir = _get_login_redirect(user)
            if redir:
                return redir
            logger.warning("portal_login: user=%s has no membership — redirecting to login", user.username)
            return redirect("portal_login")
        error = "Invalid email or password."

    return render(request, "portal/login.html", {"error": error})


def portal_logout(request):
    logout(request)
    return redirect("portal_login")


def portal_password_reset_request(request):
    """Step 1: user enters their email to receive a reset link."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    if request.user.is_authenticated:
        return redirect("portal_login")

    sent = False
    error = None

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        User = get_user_model()
        try:
            user = User.objects.get(email__iexact=email)
            uid   = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            reset_url = request.build_absolute_uri(
                reverse("portal_password_reset_confirm", kwargs={"uidb64": uid, "token": token})
            )
            from django.core.mail import send_mail
            send_mail(
                subject="Reset your Concierge Portal password",
                message=(
                    f"Hi,\n\n"
                    f"We received a request to reset the password for your Concierge Portal account ({email}).\n\n"
                    f"Click the link below to set a new password (valid for 24 hours):\n{reset_url}\n\n"
                    f"If you did not request this, you can ignore this email.\n\n"
                    f"— Concierge AI"
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
            )
        except User.DoesNotExist:
            pass  # Don't reveal whether email exists
        except Exception:
            logger.exception("portal_password_reset_request: failed to send email")
            error = "Failed to send reset email. Please try again later."

        if not error:
            sent = True

    return render(request, "portal/password_reset_request.html", {"sent": sent, "error": error})


def portal_password_reset_confirm(request, uidb64, token):
    """Step 2: user sets a new password via the link."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.forms import SetPasswordForm
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_str
    from django.utils.http import urlsafe_base64_decode

    User = get_user_model()
    error = None
    form = None

    try:
        uid  = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    valid_link = user is not None and default_token_generator.check_token(user, token)

    if not valid_link:
        return render(request, "portal/password_reset_confirm.html", {"valid_link": False})

    if request.method == "POST":
        form = SetPasswordForm(user, request.POST)
        if form.is_valid():
            form.save()
            return render(request, "portal/password_reset_confirm.html", {
                "valid_link": True,
                "done": True,
            })
    else:
        form = SetPasswordForm(user)

    # Apply portal form-control styling to the form fields
    for field in form.fields.values():
        field.widget.attrs.update({"class": "form-control"})

    return render(request, "portal/password_reset_confirm.html", {
        "valid_link": True,
        "done": False,
        "form": form,
    })


@portal_view()
def portal_account(request, slug):
    restaurant = request.restaurant
    user = request.user

    if request.method == "POST" and "dismiss_welcome" in request.POST:
        request.membership.welcomed = True
        request.membership.save(update_fields=["welcomed"])
        return redirect("portal_account", slug=slug)

    if request.method == "POST":
        if "update_email" in request.POST:
            email_form = AccountEmailForm(request.POST, user=user, restaurant=restaurant)
            password_form = PasswordUpdateForm(user)
            if email_form.is_valid():
                new_email = email_form.cleaned_data["new_email"]

                # Delete any existing pending changes for this user
                PendingEmailChange.objects.filter(user=user).delete()

                # Create new pending change
                pending = PendingEmailChange.objects.create(user=user, new_email=new_email)

                # Send confirmation email to new address
                confirm_url = request.build_absolute_uri(
                    reverse("portal_confirm_email", kwargs={"token": pending.token})
                )
                try:
                    from django.core.mail import send_mail
                    send_mail(
                        subject="Confirm your new email address for Concierge AI",
                        message=f"Please click the following link to confirm this is your new email address for the {restaurant.name} Portal:\n\n{confirm_url}\n\nIf you did not request this change, please ignore this email.",
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[new_email],
                        fail_silently=False,
                    )

                    # Send security alert to old address
                    old_email = user.email or user.username

                    send_mail(
                        subject="Security Alert: Email change requested",
                        message=f"A request was made to change the email address on your {restaurant.name} portal account to {new_email}.\n\nIf you did not make this request, please log in and change your password immediately, as your account may be compromised.",
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[old_email],
                        fail_silently=True,
                    )
                    messages.success(request, f"A confirmation email has been sent to {new_email}. Please check your inbox.")
                except Exception as e:
                    logger.exception("Failed to send email confirmation")
                    messages.error(request, "Failed to send confirmation email. Please ensure the system's email settings are configured correctly.")
                    pending.delete()

                return redirect("portal_account", slug=slug)
            else:
                 # If email form is invalid, we fall through to rendering the template with the invalid form
                 pass

        elif "update_password" in request.POST:
            email_form = AccountEmailForm(user=user, restaurant=restaurant)
            password_form = PasswordUpdateForm(user, request.POST)
            if password_form.is_valid():
                password_form.save()
                from django.contrib.auth import update_session_auth_hash
                update_session_auth_hash(request, password_form.user)
                messages.success(request, "Your password was successfully updated!")
                return redirect("portal_account", slug=slug)
            else:
                 # If password form is invalid, we fall through to rendering the template with the invalid form
                 pass
    else:
        email_form = AccountEmailForm(user=user, restaurant=restaurant)
        password_form = PasswordUpdateForm(user)

    pending_changes = PendingEmailChange.objects.filter(user=user, expires_at__gt=timezone.now())

    # Operator info for Team section (owner only)
    operator_membership = None
    if request.membership.role == "owner":
        operator_membership = RestaurantMembership.objects.filter(
            restaurant=restaurant, role="operator", is_active=True
        ).select_related("user").first()

    return render(request, "portal/account.html", {
        "restaurant": restaurant,
        "email_form": email_form,
        "password_form": password_form,
        "pending_changes": pending_changes,
        "operator": operator_membership,
        "show_welcome": not request.membership.welcomed,
    })


@portal_view(require_owner=True)
def portal_add_operator(request, slug):
    """Owner adds an operator by email + name."""
    if request.method != "POST":
        return redirect("portal_account", slug=slug)

    restaurant = request.restaurant
    email = request.POST.get("operator_email", "").strip().lower()
    name = request.POST.get("operator_name", "").strip()

    if not email:
        messages.error(request, "Email is required.")
        return redirect("portal_account", slug=slug)

    # Check if operator already exists
    existing = RestaurantMembership.objects.filter(
        restaurant=restaurant, role="operator", is_active=True
    ).exists()
    if existing:
        messages.error(request, "An operator is already active. Remove them first.")
        return redirect("portal_account", slug=slug)

    # Find or create user
    from django.contrib.auth import get_user_model
    User = get_user_model()

    user, created = User.objects.get_or_create(
        email__iexact=email,
        defaults={"username": email, "email": email},
    )
    if created:
        import secrets
        temp_password = secrets.token_urlsafe(12)
        user.set_password(temp_password)
        if name:
            parts = name.split(None, 1)
            user.first_name = parts[0]
            user.last_name = parts[1] if len(parts) > 1 else ""
        user.save()
    else:
        temp_password = None
        # Check user isn't already the owner
        if RestaurantMembership.objects.filter(
            user=user, restaurant=restaurant, role="owner"
        ).exists():
            messages.error(request, "This email belongs to the restaurant owner.")
            return redirect("portal_account", slug=slug)

    # Create membership
    membership, mem_created = RestaurantMembership.objects.get_or_create(
        user=user,
        restaurant=restaurant,
        defaults={
            "role": "operator",
            "invited_by": request.user,
            "can_edit_kb": "can_edit_kb" in request.POST,
        },
    )
    if not mem_created:
        membership.is_active = True
        membership.role = "operator"
        membership.can_edit_kb = "can_edit_kb" in request.POST
        membership.save()

    # Send welcome email (both new and existing users)
    login_url = request.build_absolute_uri("/portal/login/")
    if temp_password:
        credentials_line = f"Email: {email}\nTemporary password: {temp_password}\n\nPlease change your password after your first login."
    else:
        credentials_line = f"Log in with your existing account ({email})."

    email_sent = False
    try:
        from django.core.mail import send_mail
        send_mail(
            subject=f"You've been added to {restaurant.name} on Concierge AI",
            message=(
                f"Hi {name or user.get_full_name() or 'there'},\n\n"
                f"You've been added as an operator for {restaurant.name}.\n\n"
                f"Log in at: {login_url}\n"
                f"{credentials_line}\n\n"
                f"— Concierge AI"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
        )
        email_sent = True
    except Exception:
        logger.exception("Failed to send operator welcome email")

    if email_sent:
        messages.success(request, f"Operator {name or email} added. Welcome email sent to {email}.")
    else:
        messages.warning(request, f"Operator {name or email} added, but the welcome email to {email} could not be sent. Check your email settings.")
    return redirect("portal_account", slug=slug)


@portal_view(require_owner=True)
def portal_remove_operator(request, slug):
    """Owner deactivates the operator membership."""
    if request.method != "POST":
        return redirect("portal_account", slug=slug)

    restaurant = request.restaurant
    RestaurantMembership.objects.filter(
        restaurant=restaurant, role="operator", is_active=True
    ).update(is_active=False)

    messages.success(request, "Operator removed.")
    return redirect("portal_account", slug=slug)


@portal_view(require_owner=True)
def portal_update_operator(request, slug):
    """Owner updates operator permissions (can_edit_kb)."""
    if request.method != "POST":
        return redirect("portal_account", slug=slug)

    restaurant = request.restaurant
    membership = RestaurantMembership.objects.filter(
        restaurant=restaurant, role="operator", is_active=True
    ).first()
    if membership:
        membership.can_edit_kb = "can_edit_kb" in request.POST
        membership.save(update_fields=["can_edit_kb"])
        messages.success(request, "Operator permissions updated.")

    return redirect("portal_account", slug=slug)


def portal_confirm_email(request, token):
    pending = get_object_or_404(PendingEmailChange, token=token)

    if not pending.is_valid():
        pending.delete()
        return render(request, "portal/email_confirmed.html", {
            "success": False,
            "error": "This confirmation link has expired."
        })

    user = pending.user
    old_email = user.email
    new_email = pending.new_email

    # Check if someone else took this email while we were waiting
    from django.contrib.auth import get_user_model
    User = get_user_model()
    if User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
        pending.delete()
        return render(request, "portal/email_confirmed.html", {
            "success": False,
            "error": "This email address is already in use by another account."
        })

    user.email = new_email
    user.username = new_email  # We use email as username in this system
    user.save()

    # Also update restaurant's contact email for owner memberships
    for m in RestaurantMembership.objects.filter(user=user, role="owner", is_active=True).select_related("restaurant"):
        m.restaurant.contact_email = new_email
        m.restaurant.save()

    # Clean up pending change
    pending.delete()

    try:
        from django.core.mail import send_mail
        send_mail(
            subject="Your email has been successfully updated",
            message=f"This is a confirmation that your email address has been successfully changed from {old_email} to {new_email}.",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[new_email],
            fail_silently=True,
        )
    except Exception:
        logger.warning("portal_confirm_email: failed to send confirmation mail | new_email=%s", new_email)

    return render(request, "portal/email_confirmed.html", {
        "success": True,
        "new_email": new_email
    })



@portal_view()
def portal_dashboard(request, slug):
    restaurant = request.restaurant

    from django.db.models import Sum

    now = timezone.now()
    thirty_days_ago = now - timedelta(days=30)
    sixty_days_ago = now - timedelta(days=60)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)

    # Current 30d events
    ended_events = list(
        CallEvent.objects.filter(
            restaurant=restaurant, detail__isnull=False, created_at__gte=thirty_days_ago
        ).order_by("created_at")
    )

    # Previous 30d events (for trend comparison)
    prev_events = list(
        CallEvent.objects.filter(
            restaurant=restaurant, detail__isnull=False,
            created_at__gte=sixty_days_ago, created_at__lt=thirty_days_ago,
        )
    )

    caller_numbers = []
    topic_counter = Counter()
    outcome_counter = Counter()
    calls_by_day = defaultdict(int)
    heatmap = [[0] * 24 for _ in range(7)]   # [weekday 0=Mon][hour]
    topic_outcome = defaultdict(Counter)       # topic -> {outcome: count}
    total_duration_sec = 0

    for event in ended_events:
        call_data = event.payload.get("call", {})
        caller_numbers.append(call_data.get("from_number", "Unknown"))

        topics, outcome, duration = _classify_call(event.payload)
        total_duration_sec += duration
        topic_counter.update(topics)
        outcome_counter[outcome] += 1
        calls_by_day[event.created_at.strftime("%b %d")] += 1
        heatmap[event.created_at.weekday()][event.created_at.hour] += 1
        for topic in topics:
            topic_outcome[topic][outcome] += 1

    total_calls = len(ended_events)
    unique_callers = len(set(caller_numbers))
    repeat_callers = sum(1 for _, c in Counter(caller_numbers).items() if c > 1)
    total_minutes = round(total_duration_sec / 60)

    # Previous period baseline for trends
    prev_caller_numbers = [
        e.payload.get("call", {}).get("from_number", "Unknown") for e in prev_events
    ]
    prev_total_calls = len(prev_events)
    prev_unique_callers = len(set(prev_caller_numbers))
    prev_repeat_callers = sum(1 for _, c in Counter(prev_caller_numbers).items() if c > 1)

    # Leads
    leads_today = CallDetail.objects.filter(
        call_event__restaurant=restaurant, wants_reservation=True, created_at__gte=today_start,
    ).count()
    leads_yesterday = CallDetail.objects.filter(
        call_event__restaurant=restaurant, wants_reservation=True,
        created_at__gte=yesterday_start, created_at__lt=today_start,
    ).count()
    leads_30d = CallDetail.objects.filter(
        call_event__restaurant=restaurant, wants_reservation=True, created_at__gte=thirty_days_ago,
    ).count()

    # Cost / ROI
    cost_agg = CallDetail.objects.filter(
        call_event__restaurant=restaurant, call_event__created_at__gte=thirty_days_ago,
    ).aggregate(total=Sum("call_cost"))
    total_cost_30d = float(cost_agg["total"] or 0)
    resolved_count = outcome_counter.get("Resolved", 0)
    cost_per_lead = round(total_cost_30d / leads_30d, 4) if leads_30d > 0 else None
    cost_per_resolved = round(total_cost_30d / resolved_count, 4) if resolved_count > 0 else None

    # ROI estimate — based on staff-confirmed reservations only
    kb = getattr(restaurant, "knowledge_base", None)
    avg_cover = float(kb.avg_revenue_per_cover) if kb and kb.avg_revenue_per_cover else None

    confirmed_qs = CallDetail.objects.filter(
        call_event__restaurant=restaurant,
        reservation_status="confirmed",
        reservation_confirmed_at__gte=thirty_days_ago,
    )
    confirmed_count = confirmed_qs.count()
    pending_leads = CallDetail.objects.filter(
        call_event__restaurant=restaurant,
        wants_reservation=True,
        reservation_status="pending",
        created_at__gte=thirty_days_ago,
    ).count()

    roi_data = None
    if avg_cover and confirmed_count > 0 and total_cost_30d > 0:
        party_sizes = list(confirmed_qs.filter(party_size__isnull=False).values_list("party_size", flat=True))
        avg_party = round(sum(party_sizes) / len(party_sizes), 1) if party_sizes else 2.0
        estimated_revenue = confirmed_count * avg_party * avg_cover
        roi_multiplier = round(estimated_revenue / total_cost_30d, 1)
        roi_data = {
            "estimated_revenue": round(estimated_revenue, 2),
            "avg_party": avg_party,
            "avg_cover": avg_cover,
            "roi_multiplier": roi_multiplier,
            "total_cost": round(total_cost_30d, 2),
            "confirmed_count": confirmed_count,
        }

    # Trend helper: returns {'pct': int, 'up': bool} or None
    def _trend(current, previous):
        if previous == 0:
            return None
        pct = round((current - previous) / previous * 100)
        return {"pct": abs(pct), "up": pct >= 0}

    trends = {
        "calls":  _trend(total_calls, prev_total_calls),
        "unique": _trend(unique_callers, prev_unique_callers),
        "repeat": _trend(repeat_callers, prev_repeat_callers),
        "leads":  _trend(leads_today, leads_yesterday),
    }

    # Heatmap: pre-compute opacity (0.0–1.0) for template
    heatmap_max = max((heatmap[d][h] for d in range(7) for h in range(24)), default=1) or 1
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    heatmap_rows = [
        {
            "day": day_names[d],
            "hours": [
                {"count": heatmap[d][h], "opacity": round(heatmap[d][h] / heatmap_max, 2)}
                for h in range(24)
            ],
        }
        for d in range(7)
    ]

    # Topic × Outcome table (sorted by volume)
    topic_outcome_table = [
        {
            "topic": topic,
            "resolved":   topic_outcome[topic].get("Resolved", 0),
            "escalated":  topic_outcome[topic].get("Escalated", 0),
            "incomplete": topic_outcome[topic].get("Incomplete", 0),
            "total":      sum(topic_outcome[topic].values()),
        }
        for topic in sorted(topic_outcome, key=lambda t: -sum(topic_outcome[t].values()))
    ]

    recent_reservation_inquiries = (
        CallDetail.objects
        .filter(call_event__restaurant=restaurant, wants_reservation=True)
        .order_by("-created_at")[:5]
    )

    kb_score, kb_missing = _kb_health_score(restaurant)

    context = {
        "restaurant": restaurant,
        "total_calls": total_calls,
        "total_minutes": total_minutes,
        "unique_callers": unique_callers,
        "repeat_callers": repeat_callers,
        "leads_today": leads_today,
        "leads_30d": leads_30d,
        "trends": trends,
        "recent_reservation_inquiries": recent_reservation_inquiries,
        "calls_by_day_labels": json.dumps(list(calls_by_day.keys())),
        "calls_by_day_data": json.dumps(list(calls_by_day.values())),
        "topic_labels": json.dumps(list(topic_counter.keys())),
        "topic_data": json.dumps(list(topic_counter.values())),
        "outcome_labels": json.dumps(list(outcome_counter.keys())),
        "outcome_data": json.dumps(list(outcome_counter.values())),
        "kb_score": kb_score,
        "kb_missing": kb_missing,
        "heatmap_rows": heatmap_rows,
        "topic_outcome_table": topic_outcome_table,
        "total_cost_30d": total_cost_30d,
        "cost_per_lead": cost_per_lead,
        "cost_per_resolved": cost_per_resolved,
        "resolved_count": resolved_count,
        "roi_data": roi_data,
        "avg_cover_set": avg_cover is not None,
        "confirmed_count": confirmed_count,
        "pending_leads": pending_leads,
    }
    return render(request, "portal/dashboard.html", context)


@portal_view(require_owner=True)
def portal_update_avg_cover(request, slug):
    restaurant = request.restaurant
    if request.method == "POST":
        value = request.POST.get("avg_revenue_per_cover", "").strip()
        try:
            kb = restaurant.knowledge_base
            kb.avg_revenue_per_cover = value if value else None
            kb.save(update_fields=["avg_revenue_per_cover"])
        except Exception:
            pass
    return redirect("portal_dashboard", slug=slug)


def _do_retell_sync(restaurant_pk: int) -> None:
    """
    Push prompt + tools to Retell. Runs in a background thread so it never
    blocks the HTTP response. Fetches fresh DB objects inside the thread to
    avoid sharing Django ORM state across threads.
    """
    from .models import Restaurant as _Restaurant
    try:
        restaurant = _Restaurant.objects.select_related("knowledge_base").get(pk=restaurant_pk)
        kb = restaurant.knowledge_base
    except Exception:
        logger.exception("_do_retell_sync: restaurant pk=%s not found", restaurant_pk)
        return

    base_url = settings.RETELL_WEBHOOK_BASE_URL
    if not base_url or not restaurant.retell_api_key or not restaurant.retell_llm_id:
        return

    escalation_number = kb.escalation_transfer_number if kb.escalation_enabled else None
    tools = build_tool_list(
        base_url,
        escalation_number=escalation_number,
        enable_sms=restaurant.enable_sms,
        lang=restaurant.primary_lang,
    )

    try:
        from .admin import _build_agent_prompt
        client = RetellClient(api_key=restaurant.retell_api_key)
        prompt  = _build_agent_prompt(restaurant)
        llm_result = client.update_llm(
            restaurant.retell_llm_id,
            general_tools=tools,
            general_prompt=prompt,
            begin_message="{{welcome_phrase}}",
        )
        if restaurant.retell_agent_id:
            client.point_agent_to_llm_version(
                restaurant.retell_agent_id,
                restaurant.retell_llm_id,
                llm_result.version,
            )
            published_version = client.publish_agent(restaurant.retell_agent_id)
            if restaurant.retell_phone_number:
                client.pin_phone_to_agent_version(
                    restaurant.retell_phone_number,
                    restaurant.retell_agent_id,
                    published_version,
                )
        logger.info("_do_retell_sync: completed for restaurant=%s", restaurant.slug)
    except Exception:
        logger.exception("_do_retell_sync: Retell API error for restaurant=%s", restaurant.slug)


def _sync_retell_tools(request, restaurant: Restaurant, kb: RestaurantKnowledgeBase) -> None:
    """Kick off a background Retell sync and return immediately."""
    base_url = settings.RETELL_WEBHOOK_BASE_URL
    if not base_url:
        messages.warning(request, "Call transfer could not be synced: RETELL_WEBHOOK_BASE_URL is not configured.")
        return
    if not restaurant.retell_api_key or not restaurant.retell_llm_id:
        messages.warning(request, "Call transfer settings saved, but Retell is not yet configured for this account. Contact support to activate.")
        return

    threading.Thread(target=_do_retell_sync, args=(restaurant.pk,), daemon=True).start()

    if kb.escalation_enabled and kb.escalation_transfer_number:
        messages.success(request, f"Call transfer activated — calls will be forwarded to {kb.escalation_transfer_number} when conditions are met.")
    else:
        messages.info(request, "Call transfer deactivated.")



@portal_view()
def portal_knowledge_base(request, slug):
    restaurant = request.restaurant
    kb, _ = RestaurantKnowledgeBase.objects.get_or_create(restaurant=restaurant)
    m = request.membership
    can_edit = m.role == "owner" or m.can_edit_kb

    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden("You don't have permission to edit the Knowledge Base.")
        basic_form = RestaurantBasicForm(request.POST, instance=restaurant)
        kb_form = KnowledgeBaseForm(request.POST, instance=kb)
        if basic_form.is_valid() and kb_form.is_valid():
            # Capture old escalation state before saving
            old_enabled = kb.escalation_enabled
            old_number  = kb.escalation_transfer_number
            old_enable_sms = restaurant.enable_sms

            basic_form.save()
            kb_form.save()
            kb.refresh_from_db()

            _sync_retell_tools(request, restaurant, kb)

            messages.success(request, "Knowledge base updated successfully.")
            return redirect("portal_kb", slug=restaurant.slug)
    else:
        basic_form = RestaurantBasicForm(instance=restaurant)
        kb_form = KnowledgeBaseForm(instance=kb)

    lint = _kb_lint(restaurant, kb)
    return render(request, "portal/knowledge_base.html", {
        "restaurant": restaurant,
        "basic_form": basic_form,
        "kb_form": kb_form,
        "lint": lint,
        "can_edit_kb": can_edit,
    })


@portal_view()
def portal_calls(request, slug):
    restaurant = request.restaurant

    # ── Filters from GET params ────────────────────────────────────────────────
    reason_filter      = request.GET.get("reason", "")
    followup_filter    = request.GET.get("follow_up", "")
    reservation_filter = request.GET.get("reservation", "")
    date_from          = request.GET.get("date_from", "")
    date_to            = request.GET.get("date_to", "")
    phone_filter       = request.GET.get("phone", "").strip()

    # Deduplicate by call_id in Python (one row per actual call).
    # Prefer call_analyzed > call_ended > others — handles legacy data where
    # multiple event types each had a CallDetail for the same call_id.
    # Two-query approach avoids O(n²) correlated JSON subqueries on large tables.
    _TYPE_PRIORITY = {"call_analyzed": 0, "call_ended": 1}
    _candidates = (
        CallEvent.objects
        .filter(restaurant=restaurant, detail__isnull=False)
        .values("id", "event_type", "payload__call__call_id")
    )
    _best_pks: dict[str, tuple[int, int]] = {}  # call_id → (pk, priority)
    for _row in _candidates:
        _cid = _row["payload__call__call_id"] or f"__noid_{_row['id']}"
        _p   = _TYPE_PRIORITY.get(_row["event_type"], 2)
        if _cid not in _best_pks or _p < _best_pks[_cid][1]:
            _best_pks[_cid] = (_row["id"], _p)

    base_qs = (
        CallEvent.objects
        .filter(pk__in=[pk for pk, _ in _best_pks.values()])
        .select_related("detail")
        .prefetch_related("sms_logs")
        .order_by("-created_at")
    )

    # ── Unfiltered stats (always reflect totals, not current filter) ───────────
    total_calls          = base_qs.count()
    reservation_intents  = base_qs.filter(detail__wants_reservation=True).count()
    reservations_pending = base_qs.filter(detail__wants_reservation=True, detail__reservation_status="pending").count()
    follow_ups_pending   = base_qs.filter(
        Q(detail__follow_up_needed=True) | Q(detail__needs_review=True)
    ).count()
    sms_sent            = SmsLog.objects.filter(
        call_event__restaurant=restaurant, status="sent"
    ).count()

    # ── Apply filters to paginated queryset ───────────────────────────────────
    qs = base_qs
    if reason_filter:
        qs = qs.filter(detail__call_reason=reason_filter)
    if followup_filter == "1":
        qs = qs.filter(Q(detail__follow_up_needed=True) | Q(detail__needs_review=True))
    if reservation_filter == "1":
        qs = qs.filter(detail__wants_reservation=True)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    if phone_filter:
        qs = qs.filter(detail__caller_phone__icontains=phone_filter)

    # ── Repeat caller detection (phone numbers with >1 call) ──────────────────
    from django.db.models import Count as _Count
    repeat_phones = dict(
        CallDetail.objects
        .filter(call_event__restaurant=restaurant)
        .exclude(caller_phone="")
        .values("caller_phone")
        .annotate(_n=_Count("id"))
        .filter(_n__gt=1)
        .values_list("caller_phone", "_n")
    )

    paginator = Paginator(qs, 20)
    page_qs   = paginator.get_page(request.GET.get("page"))

    # ── Enrich only the current page (avoids loading all events) ──────────────
    enriched = []
    for event in page_qs.object_list:
        call_data = event.payload.get("call", {})
        _, outcome, duration = _classify_call(event.payload)
        detail    = getattr(event, "detail", None)
        phone = (detail.caller_phone if detail else "") or call_data.get("from_number", "")
        enriched.append({
            "event":        event,
            "date":         event.created_at,
            "from_number":  call_data.get("from_number", ""),
            "duration_sec": duration,
            "outcome":      outcome,
            "transcript":   call_data.get("transcript", ""),
            "detail":       detail,
            "sms_logs":     list(
            SmsLog.objects.filter(call_event__payload__call__call_id=call_data["call_id"])
            if call_data.get("call_id") else event.sms_logs.all()
        ),
            "call_count":   repeat_phones.get(phone, 1) if phone else 1,
        })

    return render(request, "portal/calls.html", {
        "restaurant":          restaurant,
        "page_obj":            page_qs,
        "enriched":            enriched,
        "reason_choices":      CallDetail.CALL_REASON_CHOICES,
        # filters (to repopulate form)
        "reason_filter":       reason_filter,
        "followup_filter":     followup_filter,
        "reservation_filter":  reservation_filter,
        "date_from":           date_from,
        "date_to":             date_to,
        "phone_filter":        phone_filter,
        # stats
        "total_calls":         total_calls,
        "reservation_intents":  reservation_intents,
        "reservations_pending": reservations_pending,
        "follow_ups_pending":  follow_ups_pending,
        "sms_sent":            sms_sent,
    })


@portal_view()
def portal_send_sms(request, slug, event_pk):
    """AJAX: send a manual SMS to the caller from the portal call history."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    restaurant = request.restaurant
    event      = get_object_or_404(CallEvent, pk=event_pk, restaurant=restaurant)

    # Resolve caller phone: prefer detail.caller_phone, fall back to call payload
    detail    = getattr(event, "detail", None)
    to_number = (
        (detail.caller_phone if detail and detail.caller_phone else "") or
        event.payload.get("call", {}).get("from_number", "")
    ).strip()

    if not to_number:
        return JsonResponse({"error": "No phone number on record for this call."}, status=400)

    sms_type       = request.POST.get("sms_type", "").strip()
    custom_message = request.POST.get("message", "").strip()

    if not sms_type:
        return JsonResponse({"error": "sms_type is required."}, status=400)

    kb      = getattr(restaurant, "knowledge_base", None)
    message = _build_sms_message(sms_type, restaurant, kb, custom_message)

    if not message:
        return JsonResponse({"error": f"No content available for type '{sms_type}'. Check that the relevant URL or info is filled in."}, status=400)

    log = SmsLog(restaurant=restaurant, call_event=event, to_number=to_number, message=message)
    try:
        log.twilio_sid = _send_sms_via_twilio(restaurant, to_number, message)
        log.status     = SmsLog.STATUS_SENT
        log.save()
        logger.info("portal_send_sms: sent to %s for event %s", to_number, event_pk)
        return JsonResponse({"ok": True, "message": message, "to_number": to_number})
    except Exception as exc:
        log.status        = SmsLog.STATUS_FAILED
        log.error_message = str(exc)
        log.save()
        logger.exception("portal_send_sms: failed for event %s", event_pk)
        return JsonResponse({"error": str(exc)}, status=500)


@portal_view()
def portal_resolve_followup(request, slug, event_pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    restaurant = request.restaurant
    event = get_object_or_404(CallEvent, pk=event_pk, restaurant=restaurant)
    try:
        detail = event.detail
        detail.follow_up_needed = False
        detail.needs_review = False
        detail.reviewed_at = timezone.now()
        detail.save(update_fields=["follow_up_needed", "needs_review", "reviewed_at"])
    except CallDetail.DoesNotExist:
        pass
    return JsonResponse({"ok": True})


@portal_view()
def portal_reservation_status(request, slug, event_pk):
    """AJAX: set reservation_status on a CallDetail (confirmed / lost / pending)."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    restaurant = request.restaurant
    event = get_object_or_404(CallEvent, pk=event_pk, restaurant=restaurant)
    status = request.POST.get("status", "")
    if status not in ("confirmed", "lost", "pending"):
        return JsonResponse({"ok": False, "error": "Invalid status"}, status=400)
    try:
        detail = event.detail
        detail.reservation_status = status
        detail.reservation_confirmed_at = timezone.now() if status == "confirmed" else None
        detail.save(update_fields=["reservation_status", "reservation_confirmed_at"])
        return JsonResponse({"ok": True, "status": status})
    except CallDetail.DoesNotExist:
        return JsonResponse({"ok": False, "error": "No detail"}, status=404)


@portal_view()
def portal_mark_reviewed(request, slug, event_pk):
    """AJAX: mark a defective call as reviewed."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    restaurant = request.restaurant
    event = get_object_or_404(CallEvent, pk=event_pk, restaurant=restaurant)
    try:
        detail = event.detail
        detail.needs_review = False
        detail.reviewed_at = timezone.now()
        detail.save(update_fields=["needs_review", "reviewed_at"])
        return JsonResponse({"ok": True})
    except CallDetail.DoesNotExist:
        return JsonResponse({"ok": False, "error": "No detail"}, status=404)


@portal_view()
def portal_guests(request, slug):
    """CRM list: all CallerMemory records for this restaurant, split by caller_type."""
    restaurant = request.restaurant

    active_tab = request.GET.get("tab", "guest")
    if active_tab not in ("guest", "business"):
        active_tab = "guest"

    q = request.GET.get("q", "").strip()

    qs = (
        restaurant.caller_memories
        .filter(caller_type=active_tab)
        .order_by("-last_call_at")
    )
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    paginator = Paginator(qs, 25)
    page_obj  = paginator.get_page(request.GET.get("page"))

    guest_count    = restaurant.caller_memories.filter(caller_type="guest").count()
    business_count = restaurant.caller_memories.filter(caller_type="business").count()

    return render(request, "portal/guests.html", {
        "restaurant":    restaurant,
        "page_obj":      page_obj,
        "active_tab":    active_tab,
        "q":             q,
        "guest_count":   guest_count,
        "business_count": business_count,
    })


@portal_view()
def portal_guest_detail(request, slug, memory_pk):
    """CRM detail: view/edit a single CallerMemory profile + call history."""
    restaurant = request.restaurant
    memory     = get_object_or_404(CallerMemory, pk=memory_pk, restaurant=restaurant)

    saved = False
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "save":
            memory.preferences = request.POST.get("preferences", "").strip()
            memory.staff_notes = request.POST.get("staff_notes", "").strip()
            new_type = request.POST.get("caller_type", "").strip()
            if new_type in (CallerMemory.CALLER_TYPE_GUEST, CallerMemory.CALLER_TYPE_BUSINESS):
                memory.caller_type = new_type
            memory.save(update_fields=["preferences", "staff_notes", "caller_type"])
            saved = True
        elif action == "save_name":
            memory.name = request.POST.get("name", "").strip()[:255]
            memory.save(update_fields=["name"])
            saved = True
        elif action == "accept_pending_name":
            memory.name = memory.pending_name
            memory.pending_name = ""
            memory.save(update_fields=["name", "pending_name"])
            saved = True
        elif action == "reject_pending_name":
            memory.pending_name = ""
            memory.save(update_fields=["pending_name"])
            saved = True

    # Call history for this phone number at this restaurant
    call_history = (
        CallDetail.objects
        .filter(caller_phone=memory.phone, call_event__restaurant=restaurant)
        .select_related("call_event")
        .order_by("-created_at")[:20]
    )

    return render(request, "portal/guest_detail.html", {
        "restaurant":   restaurant,
        "memory":       memory,
        "call_history": call_history,
        "saved":        saved,
        "caller_type_choices": CallerMemory.CALLER_TYPE_CHOICES,
    })


@portal_view()
def portal_guest_delete(request, slug, memory_pk):
    """Delete a CallerMemory record. POST only."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    restaurant = request.restaurant
    memory     = get_object_or_404(CallerMemory, pk=memory_pk, restaurant=restaurant)
    memory.delete()
    messages.success(request, f"Caller profile for {memory.name or memory.phone} deleted.")
    return redirect("portal_guests", slug=slug)


@portal_view()
def portal_guest_create(request, slug):
    """Manually create a CallerMemory record from the portal."""
    restaurant = request.restaurant

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    import re as _re
    phone = request.POST.get("phone", "").strip()
    if not phone:
        messages.error(request, "Phone number is required.")
        return redirect("portal_guests", slug=slug)
    if not _re.fullmatch(r"\+?[1-9]\d{6,14}", phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")):
        messages.error(request, f"'{phone}' is not a valid phone number. Use E.164 format, e.g. +13055550100.")
        return redirect("portal_guests", slug=slug)

    if CallerMemory.objects.filter(restaurant=restaurant, phone=phone).exists():
        messages.error(request, f"A profile for {phone} already exists.")
        return redirect("portal_guests", slug=slug)

    caller_type = request.POST.get("caller_type", CallerMemory.CALLER_TYPE_GUEST)
    if caller_type not in (CallerMemory.CALLER_TYPE_GUEST, CallerMemory.CALLER_TYPE_BUSINESS):
        caller_type = CallerMemory.CALLER_TYPE_GUEST

    memory = CallerMemory.objects.create(
        restaurant=restaurant,
        phone=phone,
        name=request.POST.get("name", "").strip(),
        email=request.POST.get("email", "").strip(),
        caller_type=caller_type,
        preferences=request.POST.get("preferences", "").strip(),
        staff_notes=request.POST.get("staff_notes", "").strip(),
    )
    return redirect("portal_guest_detail", slug=slug, memory_pk=memory.pk)


# ─── Billing (Stripe) ────────────────────────────────────────────────────────

def _get_or_create_subscription(restaurant):
    sub, _ = Subscription.objects.get_or_create(restaurant=restaurant)
    return sub


@portal_view(require_owner=True)
def portal_billing(request, slug):
    restaurant = request.restaurant
    sub = _get_or_create_subscription(restaurant)

    # ── Expense aggregation ──────────────────────────────────────────────────
    from django.db.models import Sum
    now = timezone.now()
    expenses_7d  = CallDetail.objects.filter(
        call_event__restaurant=restaurant,
        call_cost__isnull=False,
        created_at__gte=now - timezone.timedelta(days=7),
    ).aggregate(total=Sum("call_cost"))["total"] or 0
    expenses_30d = CallDetail.objects.filter(
        call_event__restaurant=restaurant,
        call_cost__isnull=False,
        created_at__gte=now - timezone.timedelta(days=30),
    ).aggregate(total=Sum("call_cost"))["total"] or 0
    recent_calls = CallDetail.objects.filter(
        call_event__restaurant=restaurant,
        call_cost__isnull=False,
    ).select_related("call_event").order_by("-created_at")[:10]

    return render(request, "portal/billing.html", {
        "restaurant": restaurant,
        "sub": sub,
        "stripe_publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
        "expenses_7d":  expenses_7d,
        "expenses_30d": expenses_30d,
        "recent_calls": recent_calls,
        "features": [
            "AI phone agent available 24/7",
            "Multilingual support — answers in the caller's language",
            "Full knowledge base (hours, menu, billing, events)",
            "Call history & transcripts",
            "Business analytics dashboard",
            "Monthly call reports via email",
        ],
    })


@portal_view(require_owner=True)
def portal_cancel_subscription(request, slug):
    if request.method != "POST":
        return redirect("portal_billing", slug=slug)

    restaurant = request.restaurant
    sub = _get_or_create_subscription(restaurant)

    if sub.stripe_subscription_id:
        try:
            stripe.api_key = settings.STRIPE_SECRET_KEY
            stripe.Subscription.modify(
                sub.stripe_subscription_id,
                cancel_at="min_period_end",
            )
            logger.info("portal_cancel: set cancel_at_period_end | sub=%s | restaurant=%s",
                        sub.stripe_subscription_id, restaurant.slug)
        except Exception:
            logger.exception("portal_cancel: Stripe API error | restaurant=%s", restaurant.slug)
            messages.error(request, "Could not cancel subscription. Please try again or contact support.")
            return redirect("portal_billing", slug=slug)
        messages.success(request, "Your subscription will be cancelled at the end of the current billing period.")
        _send_subscription_cancelled_email(restaurant)
    else:
        sub.status = "cancelled"
        sub.save(update_fields=["status"])
        messages.success(request, "Your subscription has been cancelled.")
        _send_subscription_cancelled_email(restaurant)
    return redirect("portal_billing", slug=slug)


@portal_view(require_owner=True)
def portal_billing_checkout(request, slug):
    if request.method != "POST":
        return redirect("portal_billing", slug=slug)

    restaurant = request.restaurant
    sub = _get_or_create_subscription(restaurant)

    if not settings.STRIPE_SECRET_KEY or not settings.STRIPE_PRICE_ID:
        messages.error(request, "Stripe Subscription is not configured yet. Please contact support.")
        return redirect("portal_billing", slug=slug)

    stripe.api_key = settings.STRIPE_SECRET_KEY
    base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"

    # Reuse existing Stripe customer or create a new one
    if sub.stripe_customer_id:
        try:
            cust = stripe.Customer.retrieve(sub.stripe_customer_id)
            if getattr(cust, "deleted", False):
                raise stripe.error.InvalidRequestError("Customer deleted", param="id")
        except stripe.error.InvalidRequestError:
            logger.warning("portal_checkout: stale customer %s, creating new one", sub.stripe_customer_id)
            sub.stripe_customer_id = ""
            sub.stripe_subscription_id = ""
            sub.save(update_fields=["stripe_customer_id", "stripe_subscription_id"])

    if not sub.stripe_customer_id:
        customer = stripe.Customer.create(
            email=restaurant.contact_email or request.user.email,
            name=restaurant.name,
            metadata={"restaurant_id": str(restaurant.pk)},
        )
        sub.stripe_customer_id = customer.id
        sub.save(update_fields=["stripe_customer_id"])

    checkout_kwargs = {
        "customer": sub.stripe_customer_id,
        "line_items": [{"price": settings.STRIPE_PRICE_ID, "quantity": 1}],
        "mode": "subscription",
        "success_url": f"{base_url}/portal/{slug}/billing/?success=1",
        "cancel_url": f"{base_url}/portal/{slug}/billing/?cancelled=1",
        "metadata": {"restaurant_id": str(restaurant.pk), "type": "subscription"},
    }
    trial_days = getattr(settings, "STRIPE_TRIAL_PERIOD_DAYS", 0)
    if trial_days and not sub.stripe_subscription_id and not sub.is_active:
        checkout_kwargs["subscription_data"] = {"trial_period_days": trial_days}

    session = stripe.checkout.Session.create(**checkout_kwargs)
    return redirect(session.url, permanent=False)


@portal_view(require_owner=True)
def portal_billing_topup(request, slug):
    if request.method != "POST":
        return redirect("portal_billing", slug=slug)

    restaurant = request.restaurant
    sub = _get_or_create_subscription(restaurant)

    if not settings.STRIPE_SECRET_KEY:
        messages.error(request, "Stripe is not configured yet. Please contact support.")
        return redirect("portal_billing", slug=slug)

    from decimal import Decimal, InvalidOperation
    MIN_TOPUP = Decimal("3.50")
    try:
        amount = Decimal(request.POST.get("amount", "20"))
    except (InvalidOperation, TypeError):
        amount = Decimal("20")
    if amount < MIN_TOPUP:
        messages.error(request, f"Minimum top-up is ${MIN_TOPUP}.")
        return redirect("portal_billing", slug=slug)

    stripe.api_key = settings.STRIPE_SECRET_KEY
    base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"

    if sub.stripe_customer_id:
        try:
            cust = stripe.Customer.retrieve(sub.stripe_customer_id)
            if getattr(cust, "deleted", False):
                raise stripe.error.InvalidRequestError("Customer deleted", param="id")
        except stripe.error.InvalidRequestError:
            logger.warning("portal_topup: stale customer %s, creating new one", sub.stripe_customer_id)
            sub.stripe_customer_id = ""
            sub.save(update_fields=["stripe_customer_id"])

    if not sub.stripe_customer_id:
        customer = stripe.Customer.create(
            email=restaurant.contact_email or request.user.email,
            name=restaurant.name,
            metadata={"restaurant_id": str(restaurant.pk)},
        )
        sub.stripe_customer_id = customer.id
        sub.save(update_fields=["stripe_customer_id"])

    session = stripe.checkout.Session.create(
        customer=sub.stripe_customer_id,
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Concierge Communication Credits"},
                "unit_amount": int(amount * 100),
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=f"{base_url}/portal/{slug}/billing/?topup_success=1",
        cancel_url=f"{base_url}/portal/{slug}/billing/?cancelled=1",
        metadata={"restaurant_id": str(restaurant.pk), "type": "topup"},
    )
    return redirect(session.url, permanent=False)


@portal_view(require_owner=True)
def portal_billing_portal(request, slug):
    if request.method != "POST":
        return redirect("portal_billing", slug=slug)

    restaurant = request.restaurant
    sub = _get_or_create_subscription(restaurant)

    if not settings.STRIPE_SECRET_KEY:
        messages.error(request, "Stripe is not configured yet. Please contact support.")
        return redirect("portal_billing", slug=slug)

    if not sub.stripe_customer_id:
        messages.error(request, "No billing account found. Please subscribe first.")
        return redirect("portal_billing", slug=slug)

    stripe.api_key = settings.STRIPE_SECRET_KEY
    base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"

    portal_session = stripe.billing_portal.Session.create(
        customer=sub.stripe_customer_id,
        return_url=f"{base_url}/portal/{slug}/billing/",
    )
    return redirect(portal_session.url, permanent=False)


@portal_view(require_owner=True)
def portal_notifications(request, slug):
    restaurant = request.restaurant
    saved = False

    operator_membership = RestaurantMembership.objects.filter(
        restaurant=restaurant, role="operator", is_active=True
    ).select_related("user").first()

    if request.method == "POST":
        restaurant.notify_via_email       = "notify_via_email" in request.POST
        restaurant.notify_email           = request.POST.get("notify_email", "").strip()
        restaurant.notify_on_reservation  = "notify_on_reservation" in request.POST
        restaurant.notify_on_complaint    = "notify_on_complaint" in request.POST
        restaurant.notify_on_followup     = "notify_on_followup" in request.POST
        restaurant.notify_on_non_customer = "notify_on_non_customer" in request.POST
        restaurant.notify_daily_digest    = "notify_daily_digest" in request.POST
        restaurant.save(update_fields=[
            "notify_via_email", "notify_email",
            "notify_on_reservation", "notify_on_complaint",
            "notify_on_followup", "notify_on_non_customer", "notify_daily_digest",
        ])

        if operator_membership:
            operator_membership.notify_email           = request.POST.get("op_notify_email", "").strip()
            operator_membership.notify_on_reservation  = "op_notify_on_reservation" in request.POST
            operator_membership.notify_on_complaint    = "op_notify_on_complaint" in request.POST
            operator_membership.notify_on_followup     = "op_notify_on_followup" in request.POST
            operator_membership.notify_on_non_customer = "op_notify_on_non_customer" in request.POST
            operator_membership.save(update_fields=[
                "notify_email",
                "notify_on_reservation", "notify_on_complaint",
                "notify_on_followup", "notify_on_non_customer",
            ])

        saved = True
        logger.info("portal_notifications: saved prefs | restaurant=%s | email=%s",
                     restaurant.slug, restaurant.notify_via_email)

    return render(request, "portal/notifications.html", {
        "restaurant": restaurant,
        "saved":      saved,
        "operator":   operator_membership,
    })


@portal_view()
def portal_reports_list(request, slug):
    from datetime import timedelta
    from django.utils import timezone

    restaurant = request.restaurant
    reports = WeeklyReport.objects.filter(restaurant=restaurant)

    last_report = reports.order_by("-generated_at").first()
    can_generate = True
    next_available_at = None
    if last_report:
        next_available_at = last_report.generated_at + timedelta(hours=24)
        if timezone.now() < next_available_at:
            can_generate = False
        else:
            next_available_at = None

    return render(request, "portal/reports_list.html", {
        "restaurant":       restaurant,
        "reports":          reports,
        "can_generate":     can_generate,
        "next_available_at": next_available_at,
    })


def _run_generate_report_bg(report_pk, summaries, week_start, week_end):
    """Background thread: call Claude, update the WeeklyReport, close DB connection."""
    from datetime import timedelta
    from django.db import connection as db_connection
    from restaurants.management.commands.send_weekly_report import generate_report as _generate_report
    try:
        report = WeeklyReport.objects.select_related(
            "restaurant", "restaurant__knowledge_base"
        ).get(pk=report_pk)
        kb = getattr(report.restaurant, "knowledge_base", None)
        prev_report = WeeklyReport.objects.filter(
            restaurant=report.restaurant,
            week_start=week_start - timedelta(days=7),
        ).first()
        prev_metrics = prev_report.metrics if prev_report else None
        owner_summary, prompt_suggestions, model_used, cost = _generate_report(
            report.restaurant, report.metrics, summaries, week_start, week_end,
            kb=kb, prev_metrics=prev_metrics,
        )
        report.owner_summary      = owner_summary
        report.prompt_suggestions = prompt_suggestions
        report.model_used         = model_used
        report.generation_cost    = cost
        report.status             = WeeklyReport.STATUS_DONE
        report.save()
    except Exception:
        logger.exception("_run_generate_report_bg failed | report_pk=%s", report_pk)
        try:
            WeeklyReport.objects.filter(pk=report_pk).update(status=WeeklyReport.STATUS_FAILED)
        except Exception:
            pass
    finally:
        db_connection.close()


@portal_view()
def portal_generate_report(request, slug):
    import threading
    from datetime import timedelta, date as date_
    from django.contrib import messages as django_messages
    from django.utils import timezone

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    restaurant = request.restaurant

    if not os.environ.get("ANTHROPIC_API_KEY"):
        django_messages.error(request, "ANTHROPIC_API_KEY not configured.")
        return redirect("portal_reports_list", slug=slug)

    # Cooldown check (skip if there's already a pending report)
    last_report = WeeklyReport.objects.filter(restaurant=restaurant).order_by("-generated_at").first()
    if last_report:
        if last_report.status == WeeklyReport.STATUS_PENDING:
            return redirect("portal_reports_detail", slug=slug, report_id=last_report.pk)
        next_available = last_report.generated_at + timedelta(hours=24)
        if timezone.now() < next_available:
            django_messages.warning(request, "Ya generaste un reporte recientemente. Espera un poco antes de generar otro.")
            return redirect("portal_reports_list", slug=slug)

    # Rolling 7-day window ending today (inclusive)
    today = date_.today()
    week_start = today - timedelta(days=6)
    week_end = today + timedelta(days=1)

    from restaurants.management.commands.send_weekly_report import (
        aggregate_metrics, select_relevant_summaries,
    )

    metrics = aggregate_metrics(restaurant, week_start, week_end)
    if not metrics:
        django_messages.warning(request, "No hay llamadas esta semana para generar un reporte.")
        return redirect("portal_reports_list", slug=slug)

    summaries = select_relevant_summaries(restaurant, week_start, week_end)

    report, _ = WeeklyReport.objects.update_or_create(
        restaurant=restaurant,
        week_start=week_start,
        defaults={
            "week_end":           week_end,
            "metrics":            metrics,
            "status":             WeeklyReport.STATUS_PENDING,
            "owner_summary":      "",
            "prompt_suggestions": "",
            "model_used":         "",
            "generation_cost":    None,
        },
    )

    thread = threading.Thread(
        target=_run_generate_report_bg,
        args=(report.pk, summaries, week_start, week_end),
        daemon=True,
    )
    thread.start()

    return redirect("portal_reports_detail", slug=slug, report_id=report.pk)


@portal_view()
def portal_report_status(request, slug, report_id):
    report = get_object_or_404(WeeklyReport, pk=report_id, restaurant=request.restaurant)
    return JsonResponse({"status": report.status})


@portal_view()
def portal_reports_detail(request, slug, report_id):
    from django.http import HttpResponse
    import csv as csv_module

    restaurant = request.restaurant
    report = get_object_or_404(WeeklyReport, pk=report_id, restaurant=restaurant)

    # CSV export
    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="weekly_report_{report.week_start}.csv"'
        )
        writer = csv_module.writer(response)
        writer.writerow(["Metric", "Value"])
        for k, v in report.metrics.items():
            writer.writerow([k, v])
        return response

    # Calls for the week — top 50 non-spam, deduped by Retell call_id
    _raw_calls = (
        CallDetail.objects
        .filter(
            call_event__restaurant=restaurant,
            call_event__created_at__date__gte=report.week_start,
            call_event__created_at__date__lt=report.week_end,
            is_spam=False,
        )
        .select_related("call_event")
        .order_by("-call_event__created_at")
    )
    _seen_call_ids: set = set()
    week_calls = []
    for _d in _raw_calls:
        _cid = _d.call_event.payload.get("call", {}).get("call_id", "")
        if _cid and _cid in _seen_call_ids:
            continue
        if _cid:
            _seen_call_ids.add(_cid)
        week_calls.append(_d)
        if len(week_calls) >= 50:
            break

    return render(request, "portal/reports_detail.html", {
        "restaurant":  restaurant,
        "report":      report,
        "week_calls":  week_calls,
    })


def _disconnect_retell_phone(restaurant):
    """Disconnect phone number from Retell agent so calls don't reach Retell."""
    if restaurant.retell_api_key and restaurant.retell_phone_number:
        try:
            from restaurants.services.retell_client import RetellClient
            client = RetellClient(api_key=restaurant.retell_api_key)
            client.update_phone_number(restaurant.retell_phone_number, inbound_agents=[])
            logger.info("Retell phone disconnected | restaurant=%s", restaurant.slug)
        except Exception:
            logger.exception("Failed to disconnect Retell phone | restaurant=%s", restaurant.slug)


def _reconnect_retell_phone(restaurant):
    """Reconnect phone number to Retell agent after reactivation."""
    if restaurant.retell_api_key and restaurant.retell_phone_number and restaurant.retell_agent_id:
        try:
            from restaurants.services.retell_client import RetellClient
            client = RetellClient(api_key=restaurant.retell_api_key)
            client.update_phone_number(
                restaurant.retell_phone_number,
                inbound_agents=[{"agent_id": restaurant.retell_agent_id, "weight": 1.0}],
            )
            logger.info("Retell phone reconnected | restaurant=%s", restaurant.slug)
        except Exception:
            logger.exception("Failed to reconnect Retell phone | restaurant=%s", restaurant.slug)


def _notify_service_disconnected(restaurant, reason):
    """Send email notification when service is disconnected."""
    if not restaurant.notify_via_email or not restaurant.notify_email:
        return
    base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"
    ctx = {
        "restaurant_name": restaurant.name,
        "reason": reason,
        "billing_url": f"{base_url}/portal/{restaurant.slug}/billing/",
    }
    try:
        html_body = render_to_string("emails/service_disconnected.html", ctx)
        text_body = (
            f"{restaurant.name} — Servicio desactivado\n\n"
            f"{reason}\n\n"
            f"Reactivar: {ctx['billing_url']}\n"
        )
        msg = EmailMultiAlternatives(
            f"Servicio desactivado — {restaurant.name}",
            text_body, from_email=None, to=[restaurant.notify_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send()
    except Exception:
        logger.exception("Failed to send service_disconnected email | restaurant=%s", restaurant.slug)


def _notify_service_reconnected(restaurant):
    """Send email notification when service is reconnected."""
    if not restaurant.notify_via_email or not restaurant.notify_email:
        return
    ctx = {"restaurant_name": restaurant.name}
    try:
        html_body = render_to_string("emails/service_reconnected.html", ctx)
        text_body = (
            f"{restaurant.name} — Servicio reactivado\n\n"
            "Tu agente de IA está contestando llamadas nuevamente.\n"
        )
        msg = EmailMultiAlternatives(
            f"Servicio reactivado — {restaurant.name}",
            text_body, from_email=None, to=[restaurant.notify_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send()
    except Exception:
        logger.exception("Failed to send service_reconnected email | restaurant=%s", restaurant.slug)


@csrf_exempt
def stripe_webhook(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    payload = request.body
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, settings.STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        logger.warning("Stripe webhook: invalid signature")
        return JsonResponse({"detail": "invalid signature"}, status=400)
    except Exception:
        logger.exception("Stripe webhook: failed to parse payload")
        return JsonResponse({"detail": "invalid payload"}, status=400)

    data = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        metadata = data.get("metadata", {})

        if metadata.get("type") == "topup":
            # Handle one-time payment for communication balance
            restaurant_id = metadata.get("restaurant_id")
            if restaurant_id:
                # We need to find how much was paid.
                # For simplicity, if we have a fixed price ID, we might know the value.
                # Or better, we can get it from the session's total_details or line items.
                amount_total = data.get("amount_total", 0) / 100.0 # cents to USD
                from decimal import Decimal
                sub = Subscription.objects.filter(restaurant_id=restaurant_id).first()
                if sub:
                    balance_before = sub.communication_balance
                    sub.communication_balance += Decimal(str(amount_total))
                    sub.save(update_fields=["communication_balance"])
                    logger.info("Stripe Webhook | Top-up successful | restaurant_id=%s | amount=%.2f",
                                restaurant_id, amount_total)
                    # Reconnect Retell if balance was zero and is now positive
                    if balance_before <= 0 and sub.communication_balance > 0:
                        restaurant = sub.restaurant
                        _reconnect_retell_phone(restaurant)
                        _notify_service_reconnected(restaurant)
        else:
            # Subscription checkout completed
            sub = Subscription.objects.filter(stripe_customer_id=customer_id).first()
            if sub and subscription_id:
                sub.stripe_subscription_id = subscription_id
                sub.status = "active"
                # Fetch period_end from Stripe subscription
                try:
                    stripe.api_key = settings.STRIPE_SECRET_KEY
                    stripe_sub = stripe.Subscription.retrieve(subscription_id)
                    period_end = stripe_sub["items"]["data"][0].get("current_period_end")
                    if period_end:
                        from datetime import datetime as dt
                        sub.current_period_end = dt.fromtimestamp(period_end, tz=timezone.utc)
                except Exception:
                    logger.exception("Stripe webhook | failed to fetch subscription period_end")
                sub.save(update_fields=["stripe_subscription_id", "status", "current_period_end"])
                # Reconnect Retell phone if it was disconnected during inactive period
                _reconnect_retell_phone(sub.restaurant)
                # Send welcome email
                try:
                    _send_subscription_welcome_email(sub.restaurant)
                except Exception:
                    logger.exception("Failed to send welcome email | customer=%s", customer_id)

    elif event["type"] in ("customer.subscription.updated", "customer.subscription.created"):
        customer_id = data.get("customer")
        sub = Subscription.objects.filter(stripe_customer_id=customer_id).first()
        if sub:
            sub.stripe_subscription_id = data["id"]
            sub.status = data["status"]  # active / trialing / past_due / etc.
            items = data.get("items", {}).get("data", [])
            period_end = items[0].get("current_period_end") if items else None
            if period_end:
                from datetime import datetime as dt
                sub.current_period_end = dt.fromtimestamp(
                    period_end, tz=timezone.utc
                )
            sub.save(update_fields=["stripe_subscription_id", "status", "current_period_end"])

    elif event["type"] == "customer.subscription.deleted":
        customer_id = data.get("customer")
        sub = Subscription.objects.filter(stripe_customer_id=customer_id).first()
        if sub:
            sub.status = "cancelled"
            sub.save(update_fields=["status"])
            _disconnect_retell_phone(sub.restaurant)
            _notify_service_disconnected(sub.restaurant, "Tu suscripción ha sido cancelada. Tu agente de IA ya no está contestando llamadas.")

    elif event["type"] == "customer.subscription.paused":
        customer_id = data.get("customer")
        sub = Subscription.objects.filter(stripe_customer_id=customer_id).first()
        if sub:
            sub.status = "inactive"
            sub.save(update_fields=["status"])
            _disconnect_retell_phone(sub.restaurant)
            _notify_service_disconnected(sub.restaurant, "Tu suscripción ha sido pausada. Tu agente de IA ya no está contestando llamadas.")
            logger.info("Stripe webhook | subscription paused | customer=%s", customer_id)

    elif event["type"] == "invoice.paid":
        customer_id = data.get("customer")
        sub = Subscription.objects.filter(stripe_customer_id=customer_id).first()
        if sub:
            sub.status = "active"
            period_end = data.get("lines", {}).get("data", [{}])[0].get("period", {}).get("end")
            if period_end:
                from datetime import datetime as dt
                sub.current_period_end = dt.fromtimestamp(period_end, tz=timezone.utc)
            sub.save(update_fields=["status", "current_period_end"])
            logger.info("Stripe webhook | invoice.paid | customer=%s | status=active", customer_id)

    elif event["type"] == "invoice.payment_failed":
        customer_id = data.get("customer")
        sub = Subscription.objects.filter(stripe_customer_id=customer_id).first()
        if sub:
            sub.status = "past_due"
            sub.save(update_fields=["status"])
            logger.warning("Stripe webhook | invoice.payment_failed | customer=%s", customer_id)
            # Send payment-failed email to restaurant owner
            try:
                restaurant = sub.restaurant
                _send_payment_failed_email(restaurant)
            except Exception:
                logger.exception("Failed to send payment_failed email | customer=%s", customer_id)

    return JsonResponse({"status": "ok"})


def demo_call(request):
    return render(request, "demo_call.html")
