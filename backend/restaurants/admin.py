import logging

from django.conf import settings
from django.contrib import admin, messages

logger = logging.getLogger(__name__)

from .models import (
    CallDetail,
    CallerMemory,
    CallEvent,
    Restaurant,
    RestaurantKnowledgeBase,
    SmsLog,
    Subscription,
)
from .services.retell_client import RetellClient
from .services.retell_tools import (
    _escalation_tool_definition,
    _get_info_tool_definition,
    _resolve_date_tool_definition,
    _save_caller_info_tool_definition,
    _sms_tool_definition,
    build_tool_list,
)

LANG_MAP = {"es": "multi", "en": "en-US", "other": "multi"}

AGENT_SYSTEM_PROMPT = """{{account_status_directive}}

You are the voice assistant for {{restaurant_name}}. Handle calls like a skilled human receptionist.

### STYLE
- Default to {{primary_lang}}. Follow the caller's language. If ambiguous, respond briefly in both {{primary_lang}} and English. After a language switch, continue where you left off — no new greeting.
- Tone: {{conversation_tone}}. {{brand_voice_notes}}
- 1–2 sentences max. Match the caller's energy — if they're brief, be briefer.
- Vary phrasing. No repeated courtesies across turns. Caller's first name at most once per turn.
- Speak dates/times naturally ("7 PM" not "19:00"). Website: {{website_domain_spoken}}. Email: {{contact_email_spoken}}. Never read raw URLs.
- Poor audio: mention the connection. After 2 failed attempts, suggest calling back.

### CONTEXT
{{restaurant_name}} | {{address_full}} — {{location_reference}}
{{current_date}} | {{current_time}} ({{timezone}})
Grace period: {{reservation_grace_min}} min | Affiliated: {{affiliated_restaurants}}
{{caller_summary}}

### RULES
1. **No fabrication.** Call `get_info(topic)` before answering any factual question. Never guess. Answer ONLY the specific question the caller asked — don't recite the full result. If the answer isn't in the result, try `get_info("additional")` before giving up. Exception: universal amenities (restrooms, etc.) need no lookup, use common sense.
2. **Dates.** Call `resolve_date` before confirming any non-exact date. Read back the `spoken_es` or `spoken_en` field from the response exactly — never build a date string yourself. If `is_past=true`, tell the caller. If `ambiguity` is set, ask to clarify.
3. **Names.** Never assume a name is the caller's unless they introduce themselves ("I'm [name]", "my name is [name]"). A name said alone is a request, not an introduction. Do not use any name until confirmed this call.
4. **Contact info.** Don't re-ask what the caller already provided. For phone, confirm {{caller_from_number}} first.
5. **Caller memory.** If the caller references a prior visit or follow-up, call `get_caller_profile()` first. Acknowledge past calls from profile data only — never invent. Apply preferences naturally; don't state their name before they confirm it.
6. **Hours ≠ availability.** Hours don't confirm a table is open.
7. **Escalation.**
   - **Known staff:** If caller asks for someone in {{team_members}}, acknowledge you know them, ask the caller's name (Rule 3), then transfer. If caller insists without giving a name, transfer anyway.
   - **Generic request:** If caller asks for "a real person", a manager, or someone NOT in {{team_members}}, offer to help first. Only transfer when {{escalation_conditions}} is satisfied — i.e., the caller insists.
   - Never transfer for routine questions.
8. **Missing info.** If `get_info` returns empty data, never say "I don't have that" or "call the restaurant." Offer a callback → [4].
9. **System outage.** If any tool fails or times out, apologize (systems under maintenance), then `end_call`.
10. **Out of scope.** Only {{restaurant_name}} topics.
11. **Robot question.** You are the AI voice assistant for {{restaurant_name}}.
12. **Emergency.** Advise 911 → `end_call`.
13. **Abuse.** Stay professional. If it continues → `end_call`.
14. **Complaints.** Don't argue or promise refunds. Apologize → [4].
15. **Loops.** After 3 unanswered repeats → offer [4] or website.
16. **Noise / garbled speech.** Ask to repeat. Don't interpret literally. Overrides out-of-scope.
17. **Short / ambiguous inputs.** Don't classify intent from a single word. Ask one brief open question.
{{non_customer_call_rules}}

### FLOW

**[1] GREETING**
"{{welcome_phrase}}" was already spoken — don't repeat it.
Route the caller's first response:
→ Question about the restaurant: [2]
→ Reservation intent: [3]
→ Name alone / asking for a person: escalation if Rule 7 met, else [4]
→ Unclear: one brief open question

**[2] QUESTIONS**
Call `get_info(topic)` (Rule 1). Answer from the result only.
If SMS enabled, offer to text a link. If they agree → `send_sms`.
If data empty → Rule 8.

**[3] RESERVATION**
Trigger: caller asks to book or check availability. If reservation intent was expressed earlier, return to it once after questions stop — not after each answer. Don't re-ask about reservations unless the caller raises it again.
Collect one at a time: Date, Time, Party Size, Name (Rule 3+4), Phone (Rule 4), Special Requests.
- Resolve the date (Rule 2). Verify hours via `get_info("hours")`.
- {{large_party_min_guests}}+ guests → events team handles it. Stop booking.
- Walk-in: note name + ETA via `save_caller_info`.
- Modify/cancel existing reservation: you can't — go to [4], staff will confirm.
Once confirmed → WRAP UP.

**[4] MESSAGES**
Trigger: complaint, callback request, manager request, unknown topic.
Collect contact (Rule 4). Tell them a team member will call back. Call `save_caller_info` with `follow_up_needed=true` → WRAP UP.

**[5] EVENTS**
Trigger: private event, buyout, large celebration, or needs beyond a standard table from [3].
1. If from [3], close it — events team handles everything.
2. Call `get_info("private_events")`.
3. Collect: name, phone (Rule 4), brief description (occasion, date, group size).
4. `save_caller_info` with `follow_up_needed=true`.
5. If SMS enabled, offer to text the Party Inquiry link.
Confirm events team will follow up → WRAP UP.

**[WRAP UP]**
Warm goodbye in the caller's language. Wait for hangup or call `end_call`.
\""""


# ─── Post-call analysis field definitions (pushed to Retell Agent) ────────────

POST_CALL_ANALYSIS_FIELDS = [
    {
        "name": "caller_name",
        "type": "string",
        "description": "Caller's confirmed name. If corrected during the call, use the final version. Empty string if none.",
    },
    {
        "name": "caller_email",
        "type": "string",
        "description": "Email address provided by the caller, only if they explicitly gave it. Empty string otherwise.",
    },
    {
        "name": "call_reason",
        "type": "enum",
        "description": (
            "Primary call reason. "
            "Use 'non_customer' for vendors, suppliers, sales, press, robocalls, or any non-guest caller. "
            "Use the most specific option for all others."
        ),
        "choices": ["reservation", "hours", "menu", "billing", "parking", "private_event", "complaint", "non_customer", "other"],
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
            "ISO date YYYY-MM-DD of the confirmed visit (e.g. '2026-03-05'). "
            "Use the resolved calendar date — not relative terms like 'tomorrow'. "
            "Date only. Empty string if none."
        ),
    },
    {
        "name": "reservation_time",
        "type": "string",
        "description": "24-hour HH:MM (e.g. '18:00'). Time only. Empty string if none.",
    },
    {
        "name": "special_requests",
        "type": "string",
        "description": (
            "Restaurant-relevant special requests: "
            "dietary (vegan, gluten-free, allergy), occasion (birthday, anniversary), "
            "seating (terrace, private), accessibility, high chair. "
            "Ignore garbled text. Empty string if none."
        ),
    },
    {
        "name": "follow_up_needed",
        "type": "boolean",
        "description": "True if the caller requested a callback, left an issue unresolved, or the agent could not fully help them.",
    },
]


# Injected at the top of the prompt ONLY when escalation is enabled.
# Keeps it completely out of the LLM context when transfer is off.
_ESCALATION_RULE_BLOCK = """
## CALL TRANSFER
Transfer calls using `transfer_to_human` ONLY when: {{escalation_conditions}}
Priority: live transfer before [4] callback.

Before transferring:
1. If you don't have the caller's name, ask once — five words max. Don't insist.
2. Tell the caller you're connecting them → call the tool. Staff is briefed privately.

If transfer fails or no one answers: apologize, then go to [4] — collect contact info and promise a callback.
If condition NOT met: handle it yourself. Don't offer or mention transfer.

---
"""


def _build_agent_prompt(restaurant: Restaurant) -> str:
    """Build the system prompt, injecting escalation rule only when enabled."""
    prompt = AGENT_SYSTEM_PROMPT

    # Inject ABSOLUTE RULE only when escalation is active.
    # When OFF, zero mention of transfer_to_human appears in the prompt.
    try:
        escalation_enabled = restaurant.knowledge_base.escalation_enabled
    except Exception:
        escalation_enabled = False

    if escalation_enabled:
        # Insert escalation block right after the first line (account_status_directive)
        first_newline = prompt.index("\n")
        prompt = prompt[:first_newline] + "\n" + _ESCALATION_RULE_BLOCK + prompt[first_newline + 1:]

    if not restaurant.enable_sms:
        # Remove SMS offer sentences from the new prompt text
        prompt = prompt.replace("If SMS enabled, offer to text a link. If they agree → `send_sms`.\n", "")
        prompt = prompt.replace("If SMS enabled, offer to text the Party Inquiry link.\n", "")
        prompt = prompt.replace(" SMS", "")

        # Hard prohibition — catches anything the string replacements miss
        prompt += "\n\n### SMS DISABLED\nNever offer, suggest, or mention texting. If asked, explain that you cannot currently send texts."
    return prompt

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
        prompt = _build_agent_prompt(r)
        llm = client.create_retell_llm(general_prompt=prompt, begin_message="{{welcome_phrase}}")
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
        prompt = _build_agent_prompt(r)

        llm_result = client.update_llm(
            r.retell_llm_id,
            general_prompt=prompt,
            begin_message="{{welcome_phrase}}"
        )
        if r.retell_agent_id:
            client.point_agent_to_llm_version(r.retell_agent_id, r.retell_llm_id, llm_result.version)
            published_version = client.publish_agent(r.retell_agent_id)
            if r.retell_phone_number:
                client.pin_phone_to_agent_version(r.retell_phone_number, r.retell_agent_id, published_version)
        messages.success(request, f"[{r.slug}] LLM prompt updated and published: {r.retell_llm_id}")


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
        tools = build_tool_list(base_url, enable_sms=r.enable_sms, lang=r.primary_lang)
        try:
            llm_result = client.update_llm(r.retell_llm_id, general_tools=tools)
            if r.retell_agent_id:
                client.point_agent_to_llm_version(r.retell_agent_id, r.retell_llm_id, llm_result.version)
                published_version = client.publish_agent(r.retell_agent_id)
                if r.retell_phone_number:
                    client.pin_phone_to_agent_version(r.retell_phone_number, r.retell_agent_id, published_version)
            messages.success(request, f"[{r.slug}] Base tools configured and published: {r.retell_llm_id}")
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
        tools = build_tool_list(base_url, escalation_number=kb.escalation_transfer_number, lang=r.primary_lang)
        try:
            llm_result = client.update_llm(r.retell_llm_id, general_tools=tools)
            if r.retell_agent_id:
                client.point_agent_to_llm_version(r.retell_agent_id, r.retell_llm_id, llm_result.version)
                published_version = client.publish_agent(r.retell_agent_id)
                if r.retell_phone_number:
                    client.pin_phone_to_agent_version(r.retell_phone_number, r.retell_agent_id, published_version)
            messages.success(request, f"[{r.slug}] All tools configured and published (SMS + save_caller_info + get_info + resolve_date + escalation → {kb.escalation_transfer_number})")
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
        lang = LANG_MAP.get(r.primary_lang, "multi")

        client = RetellClient(api_key=r.retell_api_key)
        agent = client.create_agent(
            agent_name=f"{r.name} — Inbound Agent",
            voice_id=r.retell_voice_id,
            voice_speed=1.05,        # slightly faster = more natural/energetic
            voice_temperature=1.2,   # more variation = less monotone
            language=lang,
            response_engine={"llm_id": r.retell_llm_id, "type": "retell-llm"},
            webhook_url=events_url,
        )
        r.retell_agent_id = agent.agent_id
        r.save(update_fields=["retell_agent_id"])
        messages.success(request, f"[{r.slug}] Agent created: {r.retell_agent_id} | events → {events_url}")


@admin.action(description="Retell: 2b — Update Agent language to multilingual (fixes Spanish/English switching)")
def retell_update_agent_language(modeladmin, request, queryset):
    """Set language='multi' on the Retell agent so TTS auto-detects Spanish/English per utterance."""
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_agent_id:
            messages.error(request, f"[{r.slug}] No Agent ID — run 'Create Agent' first.")
            continue
        lang = LANG_MAP.get(r.primary_lang, "multi")
        client = RetellClient(api_key=r.retell_api_key)
        client.update_agent(r.retell_agent_id, language=lang)
        messages.success(request, f"[{r.slug}] Agent language set to '{lang}'.")


@admin.action(description="Retell: 2c — Update Agent voice settings (speed + temperature)")
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


@admin.action(description="Call Log: Re-process all completed events (rebuilds CallDetail date/time)")
def reprocess_call_events(modeladmin, request, queryset):
    from restaurants.models import CallEvent
    from restaurants.views import _build_call_detail_from_payload
    ok = err = 0
    for restaurant in queryset:
        events = CallEvent.objects.filter(restaurant=restaurant, detail__isnull=False)
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
            "menu_cuisine_type", "menu_best_sellers", "menu_price_range", "menu_categories",
            "bar_menu_url", "bar_menu_summary",
            "bar_concept", "bar_signature_drinks", "bar_wine_beer", "bottle_service",
            "happy_hour_details", "dietary_options",
        )}),
        ("Billing & Payments", {"fields": (
            "auto_gratuity", "service_charge_pct", "service_charge_scope", "max_cards_to_split", "corkage_policy",
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
        ("Non-Customer Call Handling", {"fields": (
            "partner_companies", "partner_call_handling", "partner_call_ask_urgency",
            "vendor_call_handling", "vendor_call_ask_urgency",
            "press_call_handling", "press_call_ask_urgency",
            "service_call_handling", "service_call_ask_urgency",
            "sales_call_handling",
            "financial_call_handling",
            "spam_call_handling",
            "urgent_call_action",
        )}),
        ("Human Escalation", {"fields": (
            "escalation_enabled", "escalation_conditions", "escalation_transfer_number",
            "team_members",
        )}),
        ("Other / Additional Info", {"fields": (
            "owner_notes", "additional_info",
        )}),
    )


class SubscriptionInline(admin.StackedInline):
    model = Subscription
    can_delete = False
    extra = 0
    fields = (
        "status", "communication_balance", "communication_markup",
        "stripe_customer_id", "stripe_subscription_id", "current_period_end",
    )


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "restaurant", "status", "current_period_end", "communication_balance",
        "stripe_customer_id", "stripe_subscription_id"
    )
    list_filter = ("status",)
    search_fields = ("restaurant__name", "stripe_customer_id", "stripe_subscription_id")
    actions = ["show_webhook_url", "reset_stripe_ids"]

    def changelist_view(self, request, extra_context=None):
        if not getattr(settings, "STRIPE_SECRET_KEY", "") or not getattr(settings, "STRIPE_WEBHOOK_SECRET", ""):
            messages.warning(request, "⚠️ STRIPE WARNING: STRIPE_SECRET_KEY or STRIPE_WEBHOOK_SECRET is missing from your .env file. Payment flows will fail.")
        return super().changelist_view(request, extra_context=extra_context)

    @admin.action(description="Stripe: Show Webhook Configuration URL")
    def show_webhook_url(self, request, queryset):
        domain = request.get_host()
        messages.info(request, f"Set your Stripe Webhook URL to: https://{domain}/api/stripe/webhook/")

    @admin.action(description="Stripe: Reset/Clear Stripe IDs (use when switching Test/Live modes)")
    def reset_stripe_ids(self, request, queryset):
        count = queryset.update(stripe_customer_id="", stripe_subscription_id="")
        messages.success(request, f"Successfully cleared Stripe IDs for {count} subscriptions. New IDs will be generated on next payment attempt.")


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
        "wants_reservation", "party_size", "call_cost", "follow_up_needed", "created_at",
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


@admin.register(CallerMemory)
class CallerMemoryAdmin(admin.ModelAdmin):
    list_display   = ("phone", "name", "restaurant", "call_count", "last_call_at", "updated_at")
    list_filter    = ("restaurant",)
    search_fields  = ("phone", "name", "email", "preferences", "staff_notes")
    readonly_fields = ("call_count", "last_call_at", "last_call_summary", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("restaurant", "phone", "name", "email")}),
        ("Call History", {"fields": ("call_count", "last_call_at", "last_call_summary")}),
        ("Staff Annotations", {"fields": ("preferences", "staff_notes")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


# ─── Restaurant Admin ─────────────────────────────────────────────────────────

@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    list_display = (
        "name", "slug", "is_active",
        "retell_agent_id", "retell_phone_number",
        "contact_email", "created_at", "public_id",
    )
    list_filter = ("is_active", "phone_mode", "primary_lang", "timezone")
    actions = ["clear_call_history"]
    search_fields = (
        "name", "slug", "retell_agent_id", "retell_phone_number",
        "contact_email", "contact_phone", "address_full",
    )
    readonly_fields = ("created_at", "updated_at", "retell_llm_id", "retell_agent_id", "public_id")
    prepopulated_fields = {"slug": ("name",)}
    fieldsets = (
        (None, {"fields": (
            "name", "slug", "user", "is_active", "public_id",
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
            "enable_sms", "twilio_account_sid", "twilio_auth_token", "twilio_from_number",
        ), "description": "Enable Twilio integration, or leave credentials blank to use the platform-level Twilio from .env."}),
    )
    inlines = [KnowledgeBaseInline, SubscriptionInline]
    actions = [
        retell_create_llm, retell_update_llm_prompt, retell_configure_call_analysis,
        retell_configure_sms_tool, retell_configure_escalation_tool,
        retell_create_agent, retell_update_agent_language, retell_update_agent_voice, retell_update_agent_webhook, retell_update_agent_events_webhook,
        retell_create_phone,
        reprocess_call_events,
        "clear_call_history",
    ]

    @admin.action(description="Danger: Clear ALL Call & SMS History")
    def clear_call_history(self, request, queryset):
        from .models import CallEvent, SmsLog
        total_events = CallEvent.objects.filter(restaurant__in=queryset).count()
        total_sms = SmsLog.objects.filter(restaurant__in=queryset).count()

        # CallEvent deletion cascades to CallDetail
        CallEvent.objects.filter(restaurant__in=queryset).delete()
        SmsLog.objects.filter(restaurant__in=queryset).delete()

        self.message_user(request, f"Successfully deleted {total_events} call events and {total_sms} SMS logs for {queryset.count()} restaurants.")
        total_events = CallEvent.objects.filter(restaurant__in=queryset).count()
        total_sms = SmsLog.objects.filter(restaurant__in=queryset).count()

        # CallEvent deletion cascades to CallDetail
        CallEvent.objects.filter(restaurant__in=queryset).delete()
        SmsLog.objects.filter(restaurant__in=queryset).delete()

        self.message_user(request, f"Successfully deleted {total_events} call events and {total_sms} SMS logs for {queryset.count()} restaurants.")
