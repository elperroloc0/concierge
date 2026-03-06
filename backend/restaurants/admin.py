import logging

from django.conf import settings
from django.contrib import admin, messages

logger = logging.getLogger(__name__)

from .models import CallDetail, CallEvent, Restaurant, RestaurantKnowledgeBase, SmsLog
from .services.retell_client import RetellClient
from .services.retell_tools import (
    _sms_tool_definition,
    _save_caller_info_tool_definition,
    _get_info_tool_definition,
    _resolve_date_tool_definition,
    _escalation_tool_definition,
    build_tool_list,
)

LANG_MAP = {"es": "spanish", "en": "english", "other": "multilingual"}

AGENT_SYSTEM_PROMPT = """You are the phone assistant for {{restaurant_name}}.

━━━ SCOPE ━━━

You can ONLY help with topics directly related to {{restaurant_name}}: reservations, hours, menu/bar, happy hour, dietary/allergies, parking, billing/gratuity, reservation policy, private events/press, ambience/dress code, facilities, special events, directions, affiliated restaurants.

If the caller asks about anything outside that scope, say exactly:
"Solo puedo ayudarle con temas de {{restaurant_name}}. ¿Le puedo ayudar con algo del restaurante?"
Then stop and wait. No explanations.

━━━ OPENING ━━━

Start every call with exactly: "{{welcome_phrase}}"
Then listen.
When the caller says their name, silently call save_caller_info. Never announce it or pause the conversation.
If the caller corrects their name at any point, immediately call save_caller_info again with the corrected name — the last name given overrides any earlier one.
If a name sounds unusual or unclear, confirm the spelling before hanging up: "Just to make sure I have it right — how do you spell that?"

━━━ LANGUAGE & VOICE ━━━

• Speak {{primary_lang}}. Mirror the caller if they switch languages.
• Tone: {{conversation_tone}}. Use "we" for the restaurant. Use contractions.
• 1–2 sentences max, then pause. Never monologue. Never list capabilities unprompted.
• Times: 12-hour AM/PM ("three thirty PM", "noon"). Never "15:00".
• Dates: natural references when speaking — for reservations always confirm with an unambiguous calendar date from resolve_date (e.g. "el viernes 6 de marzo").
• Website: say "{{website_domain_spoken}}" exactly — never "https", "www", or slashes. In conversation "our website" is enough; only spell the domain if the caller needs to type it.
• Email: say "{{contact_email_spoken}}" exactly.
• Phone numbers: group with natural pauses — "seven eight six… five five five… one two three four".
• Prices: "twenty-five dollars", "eighteen percent". Never symbols.
• Yes/No: embed in a sentence. Never "True" or "False".
• Never address the caller by name at any point in the conversation.

━━━ RESTAURANT INFO — ALWAYS LOOK UP ━━━

Before answering any factual question about the restaurant, call get_info(topic). Never guess.

Topics:
• hours — opening hours, kitchen closing, holiday/private-event closures
• menu — food dishes, cuisine type, best sellers, prices, categories
• bar_menu — drinks, cocktails, wine, beer
• happy_hour — happy hour deals and times
• dietary — vegetarian, vegan, gluten-free, allergies
• parking — valet, street parking, garage
• billing — gratuity, service charge, card split
• reservations — booking policy, grace period, no-show fee, large parties
• private_events — private dining, buyouts, press contact, décor policy
• ambience — live music, dress code, noise level, cover charge, vibe
• facilities — terrace, AC, stroller access, accessibility
• special_events — upcoming events, themed nights, entertainment schedule
• additional — concept, story, affiliation, capacity, gift cards, Wi-Fi, corkage, birthday policy, art gallery, cigar policy, show charges, and anything not covered above

Answer only from the returned info. If there's no info: "I don't have that detail — best to check our website or call back and ask the team."

━━━ RESERVATIONS ━━━

Trigger: caller wants to book OR mentions party size/date/time.
Goal: collect 6 fields (name, phone, guests, date, time, special_requests). Save any details they give; ask only what's missing (one question per turn).

Rules:
• Never guarantee/confirm a table. Say "I'll log this / pass it to the team / you'll get a text confirmation."
• Don't re-ask info already given (only confirm if unclear).
• Offer website once; offer SMS once; don't repeat if declined.
• Mention grace period ({{reservation_grace_min}} min) only if asked.

Functions (these are background actions — fire as soon as triggered, independent of which collection step you are on):

• DATE HEARD → call resolve_date(text="<caller exact words>") immediately, even while collecting other fields.
  Do NOT ask the caller to repeat or spell out the date — use resolve_date to parse whatever they said.
  Confirm back using spoken_en (e.g. "Friday March 15, 2026 — got it.").
  - if is_past → "That date already passed — did you mean the same day next week?"
  - if ambiguity → ask ONE closed clarification (e.g. "Did you mean March or April?")

• PRE-CHECK (fires right after resolve_date, before asking for time):
  Call get_info("hours"). If the result explicitly says that date is fully closed or a private buyout →
  "We're closed that day for an event. What other date works?" Re-collect date. Do NOT ask for time first.

• HOURS CHECK (fires once date + time are both known):
  Call get_info("hours"). Verify (1) regular hours and (2) any date-specific closure/change.
  If fail → "We're not open then. [relevant hours]. Different day or time?" Re-collect date/time only.
  If hours unavailable → proceed.

Flow:
1) If caller gave NO details yet: ask once:
   "Website or should I take the details for the team?"
   - Website: "{{website_domain_spoken}}. Want the link by text?" yes → send + STOP, no → STOP.
2) Collect missing fields in this order:
   a) guests: "How many people?"
      - if guests >= {{large_party_min_guests}}: "Groups that size are handled by our team. Email {{contact_email_spoken}} or website. Text you the email?" yes → send + STOP, no → STOP.
   b) date: "What date?" → resolve_date → pre-check
   c) time: "What time?" → hours check (date+time)
   d) name: "What name is it under?" (if it sounds like number/day/time or includes "for/guests/people" → re-ask) confirm: "[NAME], right?"
   e) phone: "Best number to confirm? Can be the one you're calling from."
   f) special_requests: "Any special requests?"
      Valid requests: dietary (allergy, vegan, gluten-free), occasion (birthday, anniversary, surprise), seating (terrace, private, window), accessibility, high chair.
      If what the caller says doesn't fit any of these categories or sounds unclear, ask once: "Sorry, I didn't catch that — could you repeat your request?" If still unclear, skip it and move on.

Confirm (only when all 6 fields + hours OK, or hours unavailable):
"I'll log this: [NAME], [GUESTS], [SPOKEN DATE] at [TIME][, SPECIAL_REQUESTS]. You'll receive a text confirmation." STOP.

━━━ SMS ━━━

Use send_sms ONLY after the caller explicitly says yes. Offer proactively when relevant:
• Reservation / website → "¿Le envío el enlace por mensaje?"
• Food menu question → "¿Le mando el link del menú a su teléfono?"
• Bar / drinks question → "¿Le mando el link de la carta de bebidas?"
• Any URL mentioned → "¿Se lo envío por mensaje?"

After sending: "Done — just sent that to your number."
If send fails: "I wasn't able to send it — you can find that at {{website_domain_spoken}}."

Templates (under 160 chars):
• Reservation: "Hi! Book at {{restaurant_name}}: {{website}}"
• Food menu: "Hi! Here's the {{restaurant_name}} menu: {{food_menu_url}}"
• Bar / drinks menu: "Hi! Here's the {{restaurant_name}} drinks menu: {{bar_menu_url}}"
• Directions: "Hi! {{restaurant_name}} is at {{address_full}}. Search us on Google Maps!"
• Email contact: "Hi! Contact {{restaurant_name}} at {{contact_email}}"
• General: "Hi! Everything at {{restaurant_name}}: {{website}}"

Rules: caller must say yes first. Once per topic. Never offer again if declined.

━━━ CALL TRANSFER ━━━

Condition to transfer: {{escalation_conditions}}

When the condition is clearly met AND you cannot resolve the issue yourself:
• If transfer_to_human is available:
  1. "Let me connect you with someone — one moment."
  2. Call transfer_to_human.
  3. If transfer fails → go to TAKE-A-MESSAGE below.
• If transfer_to_human is NOT available:
  → TAKE-A-MESSAGE.

TAKE-A-MESSAGE procedure:
1. "I want to make sure the right person follows up with you. Can I get your name and a number to reach you?"
2. Call save_caller_info(caller_name=...).
3. Confirm the callback number by repeating it back.
4. "Perfect — someone from the team will be in touch shortly. Have a great day." → call end_call.

Never transfer for routine questions (hours, menu, reservations, billing).

IMPORTANT:
if call transfer is not afailable. Softly hang up

━━━ STUCK CONVERSATION ━━━

3-strike rule: if you've given the same answer 3+ times with no resolution, or said "I don't have that info" twice on the same topic:
1. "For more detail on that, the best option is to reach us at {{contact_email_spoken}} or check our website."
2. "Is there anything else I can help with today?"
3. If no new topic → warm goodbye → call end_call.

Out-of-scope persistence: if the caller pushes on an out-of-scope topic after your second refusal:
→ "I can only help with {{restaurant_name}} topics. Have a great day!" → call end_call.

━━━ COMPLAINTS ━━━

Acknowledge first, then one clarifying question max.
• Bad experience: apologize sincerely → TAKE-A-MESSAGE (so the team can follow up).
• Charge dispute: call get_info("billing") first → if unresolved → TAKE-A-MESSAGE.
• No-show fee: call get_info("reservations") first → if unresolved → TAKE-A-MESSAGE.
Never be defensive. Never promise outcomes.

━━━ EDGE CASES ━━━

• "Are you a robot?" → "I'm a voice assistant for {{restaurant_name}} — happy to help."
• Wrong restaurant → confirm name + address, offer to help anyway.
• Rude caller → one warm redirect; if continues → "Take care — feel free to call back." → end_call.
• Distressed caller → stay calm, listen, → TAKE-A-MESSAGE so team can follow up.
• Emergency (fire, medical) → "Please call emergency services right away — 911." → end_call.
• Press / partnership → call get_info("private_events") for the press contact.
• Affiliated restaurants: {{affiliated_restaurants}} — only confirm if listed. If not: "I only have info for {{restaurant_name}}."

━━━ ENDINGS ━━━

Always speak a warm goodbye before calling end_call.
• "Happy to help — hope to see you soon!"
• "Take care — have a wonderful evening!"
• If complaint: "I hope the team can get that sorted — thanks for letting us know."
• If silence for 10+ seconds: "I'll leave it here — feel free to call back anytime. Take care!" → end_call.
Never use "Is there anything else?" as a default closing.
Always call end_call after the goodbye — never leave the call open.

━━━ ALWAYS-KNOWN INFO ━━━

Restaurant: {{restaurant_name}}
Location: {{address_full}} — {{location_reference}}
Website (spoken): {{website_domain_spoken}} | Full URL (SMS only): {{website}}
Food menu link (SMS only): {{food_menu_url}}
Bar/drinks menu link (SMS only): {{bar_menu_url}}
Email (spoken): {{contact_email_spoken}} | Raw (SMS only): {{contact_email}}
Today: {{current_date}} | Time: {{current_time}} ({{timezone}})
Affiliated: {{affiliated_restaurants}}
Large party threshold: {{large_party_min_guests}}+ guests
Grace period: {{reservation_grace_min}} min\""""


# ─── Post-call analysis field definitions (pushed to Retell Agent) ────────────

POST_CALL_ANALYSIS_FIELDS = [
    {
        "name": "caller_name",
        "type": "string",
        "description": "The name confirmed for the reservation at the end of the call. If the caller corrected their name at any point, use the final corrected name — NOT the first name heard. Empty string if no name was given.",
    },
    {
        "name": "caller_email",
        "type": "string",
        "description": "Email address provided by the caller, only if they explicitly gave it. Empty string otherwise.",
    },
    {
        "name": "call_reason",
        "type": "enum",
        "description": "Primary reason the caller contacted the restaurant.",
        "choices": ["reservation", "hours", "menu", "billing", "parking", "private_event", "complaint", "other"],
    },
    {
        "name": "wants_reservation",
        "type": "boolean",
        "description": "True if the caller expressed intent to make a reservation.",
    },
    {
        "name": "party_size",
        "type": "number",
        "description": "Number of guests mentioned by the caller. 0 if not mentioned.",
    },
    {
        "name": "reservation_date",
        "type": "string",
        "description": (
            "Date of the visit in ISO format YYYY-MM-DD (e.g. '2026-03-05'). "
            "Use the specific calendar date the agent confirmed — the agent always resolves relative words "
            "like 'hoy', 'mañana', 'Friday' into a concrete date before confirming. "
            "Use that resolved date. Date only — do NOT include the time. "
            "Empty string if no date was mentioned."
        ),
    },
    {
        "name": "reservation_time",
        "type": "string",
        "description": (
            "Time of the visit in 24-hour HH:MM format (e.g. '18:00', '20:30'). "
            "Time only — do NOT include the date or day name. "
            "Empty string if no time was mentioned."
        ),
    },
    {
        "name": "special_requests",
        "type": "string",
        "description": (
            "Meaningful special requests from the caller that fall into these categories: "
            "dietary needs (vegan, gluten-free, allergy, seafood), "
            "occasion (birthday, anniversary, surprise), seating (terrace, window, private, quiet), "
            "accessibility, or high chair. "
            "Only include requests that clearly make sense in a restaurant context. "
            "Ignore any garbled, unintelligible, or nonsensical text. "
            "Empty string if no valid request was mentioned."
        ),
    },
    {
        "name": "follow_up_needed",
        "type": "boolean",
        "description": "True if the caller requested a callback, left an issue unresolved, or the agent could not fully help them.",
    },
]


# ─── Admin Actions ────────────────────────────────────────────────────────────

@admin.action(description="Retell: 1 — Create LLM (with system prompt)")
def retell_create_llm(modeladmin, request, queryset):
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] retell_api_key is empty.")
            continue
        if r.retell_llm_id:
            messages.info(request, f"[{r.slug}] LLM already exists: {r.retell_llm_id}")
            continue

        client = RetellClient(api_key=r.retell_api_key)
        llm = client.create_retell_llm(general_prompt=AGENT_SYSTEM_PROMPT)
        r.retell_llm_id = llm.llm_id
        r.save(update_fields=["retell_llm_id"])
        messages.success(request, f"[{r.slug}] LLM created: {r.retell_llm_id}")


@admin.action(description="Retell: 1b — Update LLM prompt (overwrites existing)")
def retell_update_llm_prompt(modeladmin, request, queryset):
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_llm_id:
            messages.error(request, f"[{r.slug}] No LLM ID — run 'Create LLM' first.")
            continue

        client = RetellClient(api_key=r.retell_api_key)
        client.update_llm(r.retell_llm_id, general_prompt=AGENT_SYSTEM_PROMPT)
        messages.success(request, f"[{r.slug}] LLM prompt updated: {r.retell_llm_id}")


@admin.action(description="Retell: 1c — Configure post-call analysis fields (call_analysis)")
def retell_configure_call_analysis(modeladmin, request, queryset):
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] retell_api_key is empty.")
            continue
        if not r.retell_agent_id:
            messages.error(request, f"[{r.slug}] No Agent ID — run 'Create Agent' first.")
            continue

        client = RetellClient(api_key=r.retell_api_key)
        try:
            client.update_agent(r.retell_agent_id, post_call_analysis_data=POST_CALL_ANALYSIS_FIELDS)
            messages.success(
                request,
                f"[{r.slug}] post_call_analysis_data configured ({len(POST_CALL_ANALYSIS_FIELDS)} fields) on Agent {r.retell_agent_id}."
            )
        except Exception as exc:
            messages.error(request, f"[{r.slug}] Failed to update Agent: {exc}")


@admin.action(description="Retell: 1d — Configure base tools (SMS + save-caller-info + get-info) on LLM")
def retell_configure_sms_tool(modeladmin, request, queryset):
    base_url = settings.RETELL_WEBHOOK_BASE_URL
    if not base_url:
        messages.error(request, "RETELL_WEBHOOK_URL not set in .env — cannot build tool URL.")
        return
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_llm_id:
            messages.error(request, f"[{r.slug}] No LLM ID — run 'Create LLM' first.")
            continue
        client = RetellClient(api_key=r.retell_api_key)
        tools = build_tool_list(base_url)
        try:
            client.update_llm(r.retell_llm_id, general_tools=tools)
            messages.success(request, f"[{r.slug}] Base tools (SMS + save_caller_info + get_info + resolve_date) registered on LLM: {r.retell_llm_id}")
        except Exception as exc:
            messages.error(request, f"[{r.slug}] Failed to configure tools: {exc}")


@admin.action(description="Retell: 1e — Configure escalation (transfer) tool on LLM")
def retell_configure_escalation_tool(modeladmin, request, queryset):
    base_url = settings.RETELL_WEBHOOK_BASE_URL
    if not base_url:
        messages.error(request, "RETELL_WEBHOOK_URL not set in .env — cannot build tool URL.")
        return
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_llm_id:
            messages.error(request, f"[{r.slug}] No LLM ID — run 'Create LLM' first.")
            continue
        try:
            kb = r.knowledge_base
        except Exception:
            messages.error(request, f"[{r.slug}] No knowledge base — configure escalation settings first.")
            continue
        if not kb.escalation_enabled:
            messages.warning(request, f"[{r.slug}] Escalation is disabled — enable it in KB settings first.")
            continue
        if not kb.escalation_transfer_number:
            messages.error(request, f"[{r.slug}] No transfer number set — add it in KB → Escalation tab.")
            continue

        client = RetellClient(api_key=r.retell_api_key)
        tools = build_tool_list(base_url, escalation_number=kb.escalation_transfer_number)
        try:
            client.update_llm(r.retell_llm_id, general_tools=tools)
            messages.success(request, f"[{r.slug}] All tools configured (SMS + save_caller_info + get_info + resolve_date + escalation → {kb.escalation_transfer_number})")
        except Exception as exc:
            messages.error(request, f"[{r.slug}] Failed to configure escalation tool: {exc}")


@admin.action(description="Retell: 2b — Update phone webhook URL (requires RETELL_WEBHOOK_URL in .env)")
def retell_update_agent_webhook(modeladmin, request, queryset):
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_phone_number:
            messages.error(request, f"[{r.slug}] No phone number — run 'Purchase phone number' first.")
            continue
        if not settings.RETELL_WEBHOOK_BASE_URL:
            messages.error(request, f"[{r.slug}] RETELL_WEBHOOK_URL not set in .env.")
            continue

        webhook_url = f"{settings.RETELL_WEBHOOK_BASE_URL}/api/retell/webhook/{r.pk}/"
        client = RetellClient(api_key=r.retell_api_key)
        client.update_phone_number(r.retell_phone_number, inbound_webhook_url=webhook_url)
        messages.success(request, f"[{r.slug}] Phone webhook updated → {webhook_url}")


@admin.action(description="Retell: 2 — Create Agent (requires LLM)")
def retell_create_agent(modeladmin, request, queryset):
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_llm_id:
            messages.error(request, f"[{r.slug}] LLM ID missing — run 'Create LLM' first.")
            continue
        if not settings.RETELL_WEBHOOK_BASE_URL:
            messages.error(request, f"[{r.slug}] RETELL_WEBHOOK_URL not set in .env — cannot build webhook URL.")
            continue

        inbound_url = f"{settings.RETELL_WEBHOOK_BASE_URL}/api/retell/webhook/{r.pk}/"
        events_url = f"{settings.RETELL_WEBHOOK_BASE_URL}/api/retell/events/"
        lang = LANG_MAP.get(r.primary_lang, "multilingual")

        client = RetellClient(api_key=r.retell_api_key)
        agent = client.create_agent(
            agent_name=f"{r.name} — Inbound Agent",
            voice_id=r.retell_voice_id,
            voice_speed=1.05,        # slightly faster = more natural/energetic
            voice_temperature=1.2,   # more variation = less monotone
            language=lang,
            response_engine={"llm_id": r.retell_llm_id, "type": "retell-llm"},
            inbound_dynamic_variables_webhook_url=inbound_url,
            webhook_url=events_url,
        )
        r.retell_agent_id = agent.agent_id
        r.save(update_fields=["retell_agent_id"])
        messages.success(request, f"[{r.slug}] Agent created: {r.retell_agent_id} | events → {events_url}")


@admin.action(description="Retell: 2b — Update Agent voice settings (speed + temperature)")
def retell_update_agent_voice(modeladmin, request, queryset):
    """Push voice_speed=1.05 and voice_temperature=1.2 to fix flat/sad-sounding agent."""
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_agent_id:
            messages.error(request, f"[{r.slug}] No Agent ID — run 'Create Agent' first.")
            continue
        client = RetellClient(api_key=r.retell_api_key)
        client.update_agent(
            r.retell_agent_id,
            voice_id=r.retell_voice_id,
            voice_speed=1.05,
            voice_temperature=1.2,
        )
        messages.success(request, f"[{r.slug}] Voice updated: speed=1.05, temperature=1.2")


@admin.action(description="Retell: 2c — Update Agent events webhook URL (fixes missing call history)")
def retell_update_agent_events_webhook(modeladmin, request, queryset):
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_agent_id:
            messages.error(request, f"[{r.slug}] No Agent ID — run 'Create Agent' first.")
            continue
        if not settings.RETELL_WEBHOOK_BASE_URL:
            messages.error(request, f"[{r.slug}] RETELL_WEBHOOK_URL not set in .env.")
            continue

        events_url = f"{settings.RETELL_WEBHOOK_BASE_URL}/api/retell/events/"
        client = RetellClient(api_key=r.retell_api_key)
        client.update_agent(r.retell_agent_id, webhook_url=events_url)
        messages.success(request, f"[{r.slug}] Agent events webhook updated → {events_url}")


@admin.action(description="Retell: 3 — Purchase phone number (requires Agent)")
def retell_create_phone(modeladmin, request, queryset):
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_agent_id:
            messages.error(request, f"[{r.slug}] Agent ID missing — run 'Create Agent' first.")
            continue
        if r.retell_phone_number:
            messages.warning(request, f"[{r.slug}] Already has a phone number: {r.retell_phone_number}")
            continue

        if not r.retell_area_code:
            messages.error(request, f"[{r.slug}] retell_area_code is empty — set it in the restaurant record first.")
            continue
        webhook_url = (
            f"{settings.RETELL_WEBHOOK_BASE_URL}/api/retell/webhook/{r.pk}/"
            if settings.RETELL_WEBHOOK_BASE_URL else None
        )
        client = RetellClient(api_key=r.retell_api_key)
        phone = client.create_phone_number(
            area_code=r.retell_area_code,
            inbound_agent_id=r.retell_agent_id,
            inbound_webhook_url=webhook_url,
        )
        r.retell_phone_number = phone.phone_number
        r.save(update_fields=["retell_phone_number"])
        messages.success(request, f"[{r.slug}] Phone purchased: {r.retell_phone_number}")


@admin.action(description="Call Log: Re-process all call_ended events (rebuilds CallDetail date/time)")
def reprocess_call_events(modeladmin, request, queryset):
    from restaurants.views import _build_call_detail_from_payload
    from restaurants.models import CallEvent
    ok = err = 0
    for restaurant in queryset:
        events = CallEvent.objects.filter(restaurant=restaurant, event_type="call_ended")
        for event in events:
            try:
                _build_call_detail_from_payload(event)
                ok += 1
            except Exception as exc:
                err += 1
                logger.error("reprocess_call_events: event %s — %s", event.pk, exc)
    messages.success(request, f"Re-processed {ok} call events. Errors: {err}.")


# ─── Inlines ──────────────────────────────────────────────────────────────────

class KnowledgeBaseInline(admin.StackedInline):
    model = RestaurantKnowledgeBase
    can_delete = False
    extra = 1
    fieldsets = (
        ("Hours & Availability", {"fields": (
            "hours_of_operation", "kitchen_closing_time",
            "closes_on_holidays", "holiday_closure_notes", "private_event_closures",
        )}),
        ("Menu & Food", {"fields": (
            "food_menu_url", "food_menu_summary",
            "bar_menu_url", "bar_menu_summary",
            "happy_hour_details", "dietary_options",
        )}),
        ("Billing & Payments", {"fields": (
            "auto_gratuity", "service_charge_pct", "service_charge_scope", "max_cards_to_split",
        )}),
        ("Reservations & Groups", {"fields": (
            "reservation_grace_min", "no_show_fee", "large_party_min_guests",
        )}),
        ("Private Events", {"fields": (
            "has_private_dining", "private_dining_min_spend",
            "allows_decorations", "decoration_cleaning_fee", "press_contact",
            "special_events_info",
        )}),
        ("Ambience & Experience", {"fields": (
            "has_live_music", "live_music_details", "party_vibe_start_time",
            "noise_level", "dress_code", "cover_charge",
        )}),
        ("Facilities & Access", {"fields": (
            "has_terrace", "ac_intensity", "stroller_friendly",
            "has_valet", "valet_cost", "free_parking_info",
        )}),
        ("Agent Behavior", {"fields": (
            "affiliated_restaurants", "collect_guest_info", "guest_info_to_collect", "brand_voice_notes",
        )}),
        ("Other / Additional Info", {"fields": (
            "owner_notes", "additional_info",
        )}),
    )


class CallDetailInline(admin.StackedInline):
    model = CallDetail
    can_delete = False
    extra = 0
    readonly_fields = ("created_at", "updated_at")
    fields = (
        "caller_name", "caller_phone", "caller_email",
        "call_reason", "wants_reservation",
        "party_size", "reservation_date", "reservation_time",
        "special_requests", "follow_up_needed", "notes",
        "created_at", "updated_at",
    )


@admin.register(CallEvent)
class CallEventAdmin(admin.ModelAdmin):
    list_display  = ("restaurant", "event_type", "created_at")
    list_filter   = ("event_type", "restaurant")
    readonly_fields = ("created_at",)
    inlines       = [CallDetailInline]


@admin.register(CallDetail)
class CallDetailAdmin(admin.ModelAdmin):
    list_display  = (
        "caller_name", "caller_phone", "call_reason",
        "wants_reservation", "party_size", "follow_up_needed", "created_at",
    )
    list_filter   = ("call_reason", "wants_reservation", "follow_up_needed")
    search_fields = ("caller_name", "caller_phone", "caller_email", "notes")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SmsLog)
class SmsLogAdmin(admin.ModelAdmin):
    list_display   = ("created_at", "restaurant", "to_number", "status", "delivered_at", "twilio_sid")
    list_filter    = ("status", "restaurant")
    search_fields  = ("to_number", "message", "twilio_sid")
    readonly_fields = ("created_at", "delivered_at", "twilio_sid", "error_message")


# ─── Restaurant Admin ─────────────────────────────────────────────────────────

@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    list_display = (
        "name", "slug", "is_active",
        "retell_agent_id", "retell_phone_number",
        "contact_email", "created_at", "public_id",
    )
    list_filter = ("is_active", "phone_mode", "primary_lang", "timezone")
    search_fields = (
        "name", "slug", "retell_agent_id", "retell_phone_number",
        "contact_email", "contact_phone", "address_full",
    )
    readonly_fields = ("created_at", "updated_at", "retell_llm_id", "retell_agent_id", "public_id")
    prepopulated_fields = {"slug": ("name",)}
    fieldsets = (
        (None, {"fields": (
            "name", "slug", "is_active", "public_id",
            "primary_lang", "conversation_tone", "timezone",
            "website", "contact_email", "contact_phone",
            "address_full", "location_reference",
            "welcome_phrase",
            "phone_mode", "existing_ph_numb",
            "notify_via_email", "notify_email",
            "notify_via_ws", "notify_ws_numb",
            "created_at", "updated_at",
        )}),
        ("Retell", {"fields": (
            "retell_api_key", "retell_llm_id", "retell_agent_id",
            "retell_phone_number", "retell_voice_id", "retell_area_code",
        )}),
        ("Twilio SMS (per-restaurant billing)", {"fields": (
            "twilio_account_sid", "twilio_auth_token", "twilio_from_number",
        ), "description": "Leave blank to use the platform-level Twilio credentials from .env."}),
    )
    inlines = [KnowledgeBaseInline]
    actions = [
        retell_create_llm, retell_update_llm_prompt, retell_configure_call_analysis,
        retell_configure_sms_tool, retell_configure_escalation_tool,
        retell_create_agent, retell_update_agent_voice, retell_update_agent_webhook, retell_update_agent_events_webhook,
        retell_create_phone,
        reprocess_call_events,
    ]
