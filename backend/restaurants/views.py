import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

import stripe
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
from .models import CallDetail, CallEvent, Restaurant, RestaurantKnowledgeBase, SmsLog, Subscription
from .services.retell_client import RetellClient
from .services.retell_tools import build_tool_list


# ─── Global Redirects ─────────────────────────────────────────────────────────

def root_redirect(request):
    """Redirect root '/' to the portal login or dashboard."""
    if request.user.is_authenticated:
        # If user is logged in, try to find their restaurant
        restaurant = Restaurant.objects.filter(user=request.user).first()
        if restaurant:
            return redirect("portal_dashboard", slug=restaurant.slug)
    return redirect("portal_login")


# ─── Retell Webhook Helpers ───────────────────────────────────────────────────

def _friendly_url(url: str) -> str:
    """Return just the domain for spoken use: 'https://foo.com/menu' → 'foo.com'"""
    if not url:
        return ""
    return (
        url.replace("https://", "").replace("http://", "").replace("www.", "")
        .split("/")[0].strip()
    )


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
        "address_full":          restaurant.address_full,
        "location_reference":    restaurant.location_reference,
        "website":               restaurant.website,
        "website_domain":        domain,
        "website_domain_spoken": _spoken_domain(domain, lang),
        "contact_email":         restaurant.contact_email or "",
        "contact_email_spoken":  (
            kb.contact_email_spoken
            if kb and kb.contact_email_spoken
            else _spoken_email(restaurant.contact_email or "", lang)
        ),
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
        # Pre-compute effective menu URLs: use KB-specific URL if set, else fall back to restaurant website
        _site = restaurant.website or ""
        dyn.update({
            "affiliated_restaurants": kb.affiliated_restaurants,
            "reservation_grace_min":  str(kb.reservation_grace_min) if kb.reservation_grace_min else "N/A",
            "large_party_min_guests": str(kb.large_party_min_guests) if kb.large_party_min_guests else "N/A",
            "escalation_enabled":     "yes" if kb.escalation_enabled else "no",
            "escalation_conditions":  kb.escalation_conditions or "",
            "food_menu_url":          kb.food_menu_url or _site,
            "bar_menu_url":           kb.bar_menu_url  or _site,
        })
    else:
        dyn.update({
            "affiliated_restaurants": "",
            "reservation_grace_min":  "N/A",
            "large_party_min_guests": "N/A",
            "escalation_enabled":     "no",
            "escalation_conditions":  "",
            "food_menu_url":          restaurant.website or "",
            "bar_menu_url":           restaurant.website or "",
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
    ("private_event", ["evento", "event", "private", "privado", "buyout", "party", "fiesta", "celebration"]),
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

    if day_num is not None:
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
    # Here we default to what was already saved; never override a True value.
    existing_flag = _get_bool("follow_up_needed", False)
    result["follow_up_needed"] = existing_flag

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
    analysis   = call.get("call_analysis") or {}
    transcript = (call.get("transcript") or "").strip()
    fallback   = _parse_transcript_for_guest_info(transcript) if transcript else {}

    # Third fallback: name saved real-time by save_caller_info tool during the call
    call_id = call.get("call_id", "")
    if call_id and not analysis.get("caller_name") and not fallback.get("caller_name"):
        mid = (CallDetail.objects
               .filter(call_event__payload__call__call_id=call_id)
               .exclude(call_event=call_event)
               .order_by("created_at").first())
        if mid and mid.caller_name:
            fallback["caller_name"] = mid.caller_name

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
            "follow_up_needed":  _get_bool("follow_up_needed", False),
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
    logger.info("Retell inbound webhook | Raw payload: %s", raw_str)

    try:
        payload = json.loads(raw_str)
    except json.JSONDecodeError:
        logger.error("Retell inbound webhook | Invalid JSON payload")
        return JsonResponse({"detail": "invalid json"}, status=400)

    restaurant = get_object_or_404(Restaurant, id=rest_id, is_active=True)
    sub = getattr(restaurant, "subscription", None)

    # ── Check Subscription Status & Communication Balance ─────────────────────
    if not sub or not sub.is_active:
        logger.warning("Retell inbound webhook | Subscription inactive | restaurant=%s", restaurant.slug)
        return JsonResponse({"detail": "Subscription inactive. Please renew in the portal."}, status=402)

    if sub.communication_balance <= 0:
        logger.warning("Retell inbound webhook | Insufficient balance (%.2f) | restaurant=%s",
                       sub.communication_balance, restaurant.slug)
        return JsonResponse({"detail": "Insufficient communication balance. Please top up."}, status=402)

    to_number = (payload.get("to_number") or "").strip()

    logger.warning("Retell inbound webhook | restaurant=%s | to_number=%r | payload_keys=%s",
                   restaurant.slug, to_number, list(payload.keys()))

    dyn_response = {"call_inbound": {"dynamic_variables": _build_dynamic_variables(restaurant)}}

    # In DEBUG mode skip signature verification (new inbound webhook doesn't send x-retell-signature)
    if settings.DEBUG:
        return JsonResponse(dyn_response, status=200)

    # Production: verify Retell signature
    signature = request.headers.get("x-retell-signature", "")
    if signature and restaurant.retell_api_key:
        retell_client = Retell(api_key=restaurant.retell_api_key)
        if not retell_client.verify(raw_str, restaurant.retell_api_key, signature):
            logger.warning("Retell inbound webhook | Invalid signature | restaurant=%s", restaurant.slug)
            return JsonResponse({"detail": "invalid signature"}, status=401)

    return JsonResponse(dyn_response, status=200)


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
) -> None:
    """Shared helper: render and send an event-driven alert email to the restaurant owner."""
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.utils.timezone import localtime

    notify_email = restaurant.notify_email or (restaurant.user.email if restaurant.user else "")
    if not restaurant.notify_via_email or not notify_email:
        return

    try:
        detail = call_event.detail
    except CallDetail.DoesNotExist:
        detail = None

    call_payload       = call_event.payload.get("call", {})
    caller_phone       = (call_payload.get("from_number") or "").strip()
    caller_name        = detail.caller_name  if detail else ""
    caller_email_val   = detail.caller_email if detail else ""
    transcript_raw     = call_payload.get("transcript", "") or ""
    transcript_snippet = transcript_raw[-400:].strip() if transcript_raw else ""
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
        msg = EmailMultiAlternatives(subject, text_body, settings.DEFAULT_FROM_EMAIL, [notify_email])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        logger.info("call_alert: [%s] sent to %s | restaurant=%s", reason_display, notify_email, restaurant.slug)
    except Exception:
        logger.exception("call_alert: [%s] failed | restaurant=%s", reason_display, restaurant.slug)


def _send_followup_alert_email(call_event: CallEvent, restaurant: Restaurant) -> None:
    """Send follow-up alert if the preference flag is on."""
    if not restaurant.notify_on_followup:
        return
    _send_call_alert_email(
        call_event, restaurant,
        subject_prefix="⚠️ Follow-up Needed",
        reason_display="Follow-up Required",
        reason_bg="#fee2e2", reason_color="#b91c1c", reason_border="#fca5a5",
        text_body_extra="The caller asked to be called back or requested a human agent.\n",
    )


def _send_reservation_alert_email(call_event: CallEvent, restaurant: Restaurant) -> None:
    """Send reservation-intent alert if the preference flag is on."""
    if not restaurant.notify_on_reservation:
        return
    _send_call_alert_email(
        call_event, restaurant,
        subject_prefix="📅 Reservation Request",
        reason_display="Reservation Intent",
        reason_bg="#dbeafe", reason_color="#1e40af", reason_border="#93c5fd",
        text_body_extra="A caller expressed interest in making a reservation.\n",
    )


def _send_complaint_alert_email(call_event: CallEvent, restaurant: Restaurant) -> None:
    """Send complaint alert if the preference flag is on."""
    if not restaurant.notify_on_complaint:
        return
    _send_call_alert_email(
        call_event, restaurant,
        subject_prefix="🚨 Complaint Received",
        reason_display="Complaint",
        reason_bg="#fee2e2", reason_color="#991b1b", reason_border="#fca5a5",
        text_body_extra="A caller raised a complaint. Immediate attention may be required.\n",
    )


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


def _send_post_call_sms(call_event: CallEvent, restaurant: Restaurant) -> None:
    """Send a contextual follow-up SMS after call_ended, but only if no SMS was already sent."""
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
        if detail.reservation_time:  parts.append(f"at {detail.reservation_time.strftime('%-I:%M %p')}")
        if parts:
            summary = ", ".join(parts)
            message = f"{greeting} Your request at {restaurant.name} ({summary}) is noted. Book instantly: {website}"
        else:
            message = f"{greeting} Thanks for calling {restaurant.name}! Reserve your table at {website}"
    elif detail and detail.call_reason == "menu":
        menu_url = (kb.food_menu_url if kb else "") or restaurant.website or ""
        message  = f"{greeting} Here's the {restaurant.name} menu: {menu_url}"
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
    logger.info("Retell events webhook | Raw payload: %s", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Retell events webhook | Invalid JSON payload")
        return JsonResponse({"detail": "invalid json"}, status=400)

    to_number = (data.get("to_number") or data.get("call", {}).get("to_number") or "").strip()
    restaurant = Restaurant.objects.filter(retell_phone_number=to_number, is_active=True).first()
    if not restaurant:
        logger.warning("Retell events webhook | Unknown number: %r", to_number)
        return JsonResponse({"detail": "unknown number"}, status=404)

    if not settings.DEBUG:
        if not sig or not restaurant.retell_api_key:
            return JsonResponse({"detail": "unauthorized"}, status=401)
        retell_client = Retell(api_key=restaurant.retell_api_key)
        if not retell_client.verify(raw, restaurant.retell_api_key, sig):
            logger.warning("Retell events webhook | Invalid signature | restaurant=%s", restaurant.slug)
            return JsonResponse({"detail": "invalid signature"}, status=401)

    # Retell sends "event" (not "event_type") — fall back for safety
    event_type = data.get("event") or data.get("event_type", "")
    call_event = CallEvent.objects.create(restaurant=restaurant, event_type=event_type, payload=data)

    if event_type == "call_ended":
        # ── Subtract Call Cost from Communication Balance ─────────────────────
        call_payload = data.get("call", {})
        combined_cost = call_payload.get("combined_cost") # in USD
        if combined_cost is not None:
            try:
                from decimal import Decimal
                sub = getattr(restaurant, "subscription", None)
                if sub:
                    cost_decimal = Decimal(str(combined_cost))
                    sub.communication_balance -= cost_decimal
                    sub.save(update_fields=["communication_balance"])
                    logger.info("Retell call_ended | Subtracted cost: %.4f | New balance: %.4f | restaurant=%s",
                                combined_cost, sub.communication_balance, restaurant.slug)
            except Exception:
                logger.exception("Failed to update communication balance for restaurant=%s", restaurant.slug)

        try:
            _build_call_detail_from_payload(call_event)
        except Exception:
            logger.exception("Failed to build CallDetail for CallEvent pk=%s", call_event.pk)

        # ── Send event-driven email alerts ───────────────────────────────────
        try:
            detail = getattr(call_event, "detail", None)
            if detail:
                if detail.follow_up_needed:
                    _send_followup_alert_email(call_event, restaurant)
                if detail.wants_reservation:
                    _send_reservation_alert_email(call_event, restaurant)
                if detail.call_reason == "complaint":
                    _send_complaint_alert_email(call_event, restaurant)
        except Exception:
            logger.exception("Failed to send alert email(s) for CallEvent pk=%s", call_event.pk)

        try:
            _send_post_call_sms(call_event, restaurant)
        except Exception:
            logger.exception("Failed to send post-call SMS for CallEvent pk=%s", call_event.pk)

    return JsonResponse({"status": "ok"}, status=200)


# ─── get_info Tool ────────────────────────────────────────────────────────────

def _format_kb_topic(kb, topic: str) -> str:
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

    elif topic == "reservations":
        add("Grace period", f"{kb.reservation_grace_min} minutes" if kb.reservation_grace_min else None)
        add("No-show fee", kb.no_show_fee)
        add("Large party threshold", f"{kb.large_party_min_guests}+ guests" if kb.large_party_min_guests else None)

    elif topic == "private_events":
        lines.append(f"Private dining: {'Available' if kb.has_private_dining else 'Not available'}")
        add("Minimum spend", kb.private_dining_min_spend)
        lines.append(f"Decorations: {'Allowed' if kb.allows_decorations else 'Not allowed'}")
        add("Cleaning fee", kb.decoration_cleaning_fee)
        add("Press / partnerships", kb.press_contact)

    elif topic == "ambience":
        if kb.has_live_music:
            add("Live music", kb.live_music_details)
            add("Party vibe starts", kb.party_vibe_start_time)
        add("Noise level", kb.get_noise_level_display() if kb.noise_level else None)
        add("Dress code", kb.dress_code)
        add("Cover charge", kb.cover_charge)

    elif topic == "facilities":
        lines.append(f"Terrace: {'Yes' if kb.has_terrace else 'No'}")
        add("Air conditioning", kb.get_ac_intensity_display() if kb.ac_intensity else None)
        lines.append(f"Stroller-friendly: {'Yes' if kb.stroller_friendly else 'No'}")

    elif topic == "special_events":
        add("Special events & entertainment", kb.special_events_info)

    elif topic == "additional":
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

    result = _format_kb_topic(kb, topic)
    logger.info("get_info: restaurant=%s topic=%r → %d chars", restaurant.slug, topic, len(result))
    return JsonResponse({"result": result})


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
    message   = data.get("args", {}).get("message", "").strip()[:320]  # hard cap at 2 segments

    if not to_number or not message:
        return JsonResponse({"result": "error: missing to_number or message"})

    restaurant = Restaurant.objects.filter(retell_phone_number=to_retell, is_active=True).first()
    call_id    = call.get("call_id")
    call_event = (
        CallEvent.objects.filter(payload__call__call_id=call_id).first()
        if call_id else None
    )

    log = SmsLog(
        restaurant=restaurant,
        call_event=call_event,
        to_number=to_number,
        message=message,
    )

    try:
        from twilio.rest import Client as TwilioClient
        # Use restaurant-specific credentials; fall back to platform defaults
        # Only use restaurant credentials if ALL three are present — mixing accounts
        # causes "Mismatch between From number and account" errors.
        if restaurant and restaurant.twilio_account_sid and restaurant.twilio_auth_token and restaurant.twilio_from_number:
            account_sid = restaurant.twilio_account_sid
            auth_token  = restaurant.twilio_auth_token
            from_number = restaurant.twilio_from_number
        else:
            account_sid = settings.TWILIO_ACCOUNT_SID
            auth_token  = settings.TWILIO_AUTH_TOKEN
            from_number = settings.TWILIO_FROM_NUMBER
        callback_url = (
            f"{settings.RETELL_WEBHOOK_BASE_URL}/api/retell/twilio/sms-status/"
            if settings.RETELL_WEBHOOK_BASE_URL else None
        )
        twilio_client = TwilioClient(account_sid, auth_token)
        create_kwargs = {"body": message, "from_": from_number, "to": to_number}
        if callback_url:
            create_kwargs["status_callback"] = callback_url
        msg = twilio_client.messages.create(**create_kwargs)
        log.status     = SmsLog.STATUS_SENT
        log.twilio_sid = msg.sid
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
    if not settings.DEBUG:
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
        if len(kb.food_menu_summary) > 500:
            warnings.append(
                f"Food menu summary is long ({len(kb.food_menu_summary)} chars). "
                "Keep it under 3 sentences for clearer phone answers — the agent will summarize naturally."
            )
            if "menu" not in warning_tabs:
                warning_tabs.append("menu")
        if len(kb.bar_menu_summary) > 500:
            warnings.append(
                f"Bar menu summary is long ({len(kb.bar_menu_summary)} chars). "
                "Keep it under 3 sentences."
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

def portal_login(request):
    if request.user.is_authenticated:
        try:
            return redirect("portal_dashboard", slug=request.user.restaurant.slug)
        except Exception:
            pass

    error = None
    if request.method == "POST":
        user = authenticate(
            request,
            username=request.POST.get("username", "").strip(),
            password=request.POST.get("password", "").strip(),
        )
        if user is not None:
            login(request, user)
            try:
                return redirect("portal_dashboard", slug=user.restaurant.slug)
            except Exception:
                return redirect("portal_login")
        error = "Invalid username or password."

    return render(request, "portal/login.html", {"error": error})


def portal_logout(request):
    logout(request)
    return redirect("portal_login")


@login_required
def portal_dashboard(request, slug):
    restaurant = get_object_or_404(Restaurant, slug=slug, user=request.user, is_active=True)

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

    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    leads_today = CallDetail.objects.filter(
        call_event__restaurant=restaurant,
        wants_reservation=True,
        created_at__gte=today_start,
    ).count()
    recent_reservation_inquiries = (
        CallDetail.objects
        .filter(call_event__restaurant=restaurant, wants_reservation=True)
        .order_by("-created_at")[:5]
    )

    kb_score, kb_missing = _kb_health_score(restaurant)

    context = {
        "restaurant": restaurant,
        "total_calls": total_calls,
        "unique_callers": unique_callers,
        "repeat_callers": repeat_callers,
        "leads_today": leads_today,
        "recent_reservation_inquiries": recent_reservation_inquiries,
        "calls_by_day_labels": json.dumps(list(calls_by_day.keys())),
        "calls_by_day_data": json.dumps(list(calls_by_day.values())),
        "topic_labels": json.dumps(list(topic_counter.keys())),
        "topic_data": json.dumps(list(topic_counter.values())),
        "outcome_labels": json.dumps(list(outcome_counter.keys())),
        "outcome_data": json.dumps(list(outcome_counter.values())),
        "kb_score": kb_score,
        "kb_missing": kb_missing,
    }
    return render(request, "portal/dashboard.html", context)


def _sync_retell_tools(request, restaurant: Restaurant, kb: RestaurantKnowledgeBase) -> None:
    """Push the current tool list (with or without escalation) to Retell after KB save."""
    base_url = settings.RETELL_WEBHOOK_BASE_URL
    if not base_url:
        messages.warning(request, "Call transfer could not be synced: RETELL_WEBHOOK_BASE_URL is not configured.")
        return
    if not restaurant.retell_api_key or not restaurant.retell_llm_id:
        messages.warning(request, "Call transfer settings saved, but Retell is not yet configured for this account. Contact support to activate.")
        return

    escalation_number = kb.escalation_transfer_number if kb.escalation_enabled else None
    tools = build_tool_list(base_url, escalation_number=escalation_number)

    try:
        client = RetellClient(api_key=restaurant.retell_api_key)
        client.update_llm(restaurant.retell_llm_id, general_tools=tools)
        if kb.escalation_enabled and escalation_number:
            messages.success(request, f"Call transfer activated — calls will be forwarded to {escalation_number} when conditions are met.")
        else:
            messages.info(request, "Call transfer deactivated.")
    except Exception as exc:
        logger.error("Failed to sync Retell tools for %s: %s", restaurant.slug, exc)
        messages.error(request, f"Settings saved, but failed to sync call transfer with Retell: {exc}")


@login_required
def portal_knowledge_base(request, slug):
    restaurant = get_object_or_404(Restaurant, slug=slug, user=request.user, is_active=True)
    kb, _ = RestaurantKnowledgeBase.objects.get_or_create(restaurant=restaurant)

    if request.method == "POST":
        basic_form = RestaurantBasicForm(request.POST, instance=restaurant)
        kb_form = KnowledgeBaseForm(request.POST, instance=kb)
        if basic_form.is_valid() and kb_form.is_valid():
            # Capture old escalation state before saving
            old_enabled = kb.escalation_enabled
            old_number  = kb.escalation_transfer_number

            basic_form.save()
            kb_form.save()
            kb.refresh_from_db()

            # Auto-push tools to Retell if escalation settings changed
            escalation_changed = (
                kb.escalation_enabled != old_enabled
                or kb.escalation_transfer_number != old_number
            )
            if escalation_changed:
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
    })


@login_required
def portal_calls(request, slug):
    restaurant = get_object_or_404(Restaurant, slug=slug, user=request.user, is_active=True)

    # ── Filters from GET params ────────────────────────────────────────────────
    reason_filter      = request.GET.get("reason", "")
    followup_filter    = request.GET.get("follow_up", "")
    reservation_filter = request.GET.get("reservation", "")
    date_from          = request.GET.get("date_from", "")
    date_to            = request.GET.get("date_to", "")
    phone_filter       = request.GET.get("phone", "").strip()

    # Base queryset — all ended calls, joined with CallDetail + SmsLog
    base_qs = (
        CallEvent.objects
        .filter(restaurant=restaurant, event_type="call_ended")
        .select_related("detail")
        .prefetch_related("sms_logs")
        .order_by("-created_at")
    )

    # ── Unfiltered stats (always reflect totals, not current filter) ───────────
    total_calls         = base_qs.count()
    reservation_intents = base_qs.filter(detail__wants_reservation=True).count()
    follow_ups_pending  = base_qs.filter(detail__follow_up_needed=True).count()
    sms_sent            = SmsLog.objects.filter(
        call_event__restaurant=restaurant, status="sent"
    ).count()

    # ── Apply filters to paginated queryset ───────────────────────────────────
    qs = base_qs
    if reason_filter:
        qs = qs.filter(detail__call_reason=reason_filter)
    if followup_filter == "1":
        qs = qs.filter(detail__follow_up_needed=True)
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
            "sms_logs":     list(event.sms_logs.all()),
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
        "reservation_intents": reservation_intents,
        "follow_ups_pending":  follow_ups_pending,
        "sms_sent":            sms_sent,
    })


@login_required
def portal_resolve_followup(request, slug, event_pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    restaurant = get_object_or_404(Restaurant, slug=slug, user=request.user, is_active=True)
    event = get_object_or_404(CallEvent, pk=event_pk, restaurant=restaurant)
    try:
        event.detail.follow_up_needed = False
        event.detail.save(update_fields=["follow_up_needed"])
    except CallDetail.DoesNotExist:
        pass
    return JsonResponse({"ok": True})


@login_required
def portal_guests(request, slug):
    """Redirect to unified Call Log — kept for backwards-compat with bookmarks."""
    return redirect("portal_calls", slug=slug)


# ─── Billing (Stripe) ────────────────────────────────────────────────────────

def _get_or_create_subscription(restaurant):
    sub, _ = Subscription.objects.get_or_create(restaurant=restaurant)
    return sub


@login_required
def portal_billing(request, slug):
    restaurant = get_object_or_404(Restaurant, slug=slug, user=request.user, is_active=True)
    sub = _get_or_create_subscription(restaurant)
    return render(request, "portal/billing.html", {
        "restaurant": restaurant,
        "sub": sub,
        "stripe_publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
        "features": [
            "AI phone agent available 24/7",
            "Multilingual support — answers in the caller's language",
            "Full knowledge base (hours, menu, billing, events)",
            "Call history & transcripts",
            "Business analytics dashboard",
            "Monthly call reports via email",
        ],
    })


@login_required
def portal_billing_checkout(request, slug):
    if request.method != "POST":
        return redirect("portal_billing", slug=slug)

    restaurant = get_object_or_404(Restaurant, slug=slug, user=request.user, is_active=True)
    sub = _get_or_create_subscription(restaurant)

    stripe.api_key = settings.STRIPE_SECRET_KEY
    base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"

    # Reuse existing Stripe customer or create a new one
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
        payment_method_types=["card"],
        line_items=[{"price": settings.STRIPE_PRICE_ID, "quantity": 1}],
        mode="subscription",
        success_url=f"{base_url}/portal/{slug}/billing/?success=1",
        cancel_url=f"{base_url}/portal/{slug}/billing/?cancelled=1",
        metadata={"restaurant_id": str(restaurant.pk), "type": "subscription"},
    )
    return redirect(session.url, permanent=False)


@login_required
def portal_billing_topup(request, slug):
    if request.method != "POST":
        return redirect("portal_billing", slug=slug)

    restaurant = get_object_or_404(Restaurant, slug=slug, user=request.user, is_active=True)
    sub = _get_or_create_subscription(restaurant)

    stripe.api_key = settings.STRIPE_SECRET_KEY
    base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"

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
        payment_method_types=["card"],
        line_items=[{"price": settings.STRIPE_COMMUNICATION_PRICE_ID, "quantity": 1}],
        mode="payment",
        success_url=f"{base_url}/portal/{slug}/billing/?topup_success=1",
        cancel_url=f"{base_url}/portal/{slug}/billing/?cancelled=1",
        metadata={"restaurant_id": str(restaurant.pk), "type": "topup"},
    )
    return redirect(session.url, permanent=False)


@login_required
def portal_billing_portal(request, slug):
    if request.method != "POST":
        return redirect("portal_billing", slug=slug)

    restaurant = get_object_or_404(Restaurant, slug=slug, user=request.user, is_active=True)
    sub = _get_or_create_subscription(restaurant)

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


@login_required
def portal_notifications(request, slug):
    restaurant = get_object_or_404(Restaurant, slug=slug, user=request.user, is_active=True)
    saved = False

    if request.method == "POST":
        restaurant.notify_via_email       = "notify_via_email" in request.POST
        restaurant.notify_email           = request.POST.get("notify_email", "").strip()
        restaurant.notify_on_reservation  = "notify_on_reservation" in request.POST
        restaurant.notify_on_complaint    = "notify_on_complaint" in request.POST
        restaurant.notify_on_followup     = "notify_on_followup" in request.POST
        restaurant.notify_daily_digest    = "notify_daily_digest" in request.POST
        restaurant.save(update_fields=[
            "notify_via_email", "notify_email",
            "notify_on_reservation", "notify_on_complaint",
            "notify_on_followup", "notify_daily_digest",
        ])
        saved = True
        logger.info("portal_notifications: saved prefs | restaurant=%s | email=%s",
                     restaurant.slug, restaurant.notify_via_email)

    return render(request, "portal/notifications.html", {
        "restaurant": restaurant,
        "saved":      saved,
    })


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
    except stripe.errors.SignatureVerificationError:
        return JsonResponse({"detail": "invalid signature"}, status=400)
    except Exception:
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
                    sub.communication_balance += Decimal(str(amount_total))
                    sub.save(update_fields=["communication_balance"])
                    logger.info("Stripe Webhook | Top-up successful | restaurant_id=%s | amount=%.2f",
                                restaurant_id, amount_total)
        else:
            # Legacy or subscription logic
            sub = Subscription.objects.filter(stripe_customer_id=customer_id).first()
            if sub and subscription_id:
                sub.stripe_subscription_id = subscription_id
                sub.status = "active"
                sub.save(update_fields=["stripe_subscription_id", "status"])

    elif event["type"] in ("customer.subscription.updated", "customer.subscription.created"):
        customer_id = data.get("customer")
        sub = Subscription.objects.filter(stripe_customer_id=customer_id).first()
        if sub:
            sub.stripe_subscription_id = data["id"]
            sub.status = data["status"]  # active / trialing / past_due / etc.
            period_end = data.get("current_period_end")
            if period_end:
                from django.utils.timezone import datetime as tz_datetime
                sub.current_period_end = tz_datetime.fromtimestamp(
                    period_end, tz=timezone.utc
                )
            sub.save(update_fields=["stripe_subscription_id", "status", "current_period_end"])

    elif event["type"] == "customer.subscription.deleted":
        customer_id = data.get("customer")
        sub = Subscription.objects.filter(stripe_customer_id=customer_id).first()
        if sub:
            sub.status = "cancelled"
            sub.save(update_fields=["status"])

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
