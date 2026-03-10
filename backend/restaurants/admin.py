import logging

from django.conf import settings
from django.contrib import admin, messages

logger = logging.getLogger(__name__)

from .models import CallDetail, CallEvent, Restaurant, RestaurantKnowledgeBase, SmsLog, Subscription
from .services.retell_client import RetellClient
from .services.retell_tools import (
    _sms_tool_definition,
    _save_caller_info_tool_definition,
    _get_info_tool_definition,
    _resolve_date_tool_definition,
    _escalation_tool_definition,
    build_tool_list,
)
LANG_MAP = {"es": "es-419", "en": "en-US", "other": "multi"}

AGENT_SYSTEM_PROMPT = """{{account_status_directive}}

You are the professional, friendly, and human-like voice assistant for {{restaurant_name}}.
You handle calls naturally and efficiently, exactly like a great human receptionist.

### VOICE & BEHAVIOR
- Language: {{primary_lang}}. Mirror the caller if they switch.
- Tone: {{conversation_tone}}. Warm, hospitable, and conversational.
- Brand Voice & Style Notes: {{brand_voice_notes}}
- Keep responses short (1-2 sentences). Allow the caller to speak.
- Avoid robotic phrases. Never say "How may I assist you today?" if you already greeted them.
- When reading times/dates, use natural speech (e.g., "7 PM", not "19:00").
- Do not make up information. Always use your tools.

### RESTAURANT CONTEXT
- Name: {{restaurant_name}}
- Address: {{address_full}} — {{location_reference}}
- Current Date/Time: {{current_date}} | {{current_time}} ({{timezone}})
- Website: {{website_domain_spoken}}
- Email: {{contact_email_spoken}}
- Grace period: {{reservation_grace_min}} min
- Affiliated restaurants: {{affiliated_restaurants}}

### GUARDRAILS & EDGE CASES (STRICT ADHERENCE)
- **System Outage:** If you attempt to call ANY tool (such as `get_info`) and it fails or times out, you MUST assume the backend is down. Say: "I apologize, but our systems are currently undergoing maintenance. Please call back later." and then use the `end_call` tool to hang up.
- Out of Scope: You ONLY help with {{restaurant_name}} topics. If asked about unrelated things, politely say you can only assist with restaurant matters.
- Are you a robot?: If asked, proudly but naturally state you are the AI voice assistant for {{restaurant_name}}.
- Emergencies: If an emergency is mentioned, immediately advise them to hang up and dial 911, then use the `end_call` tool.
- Rude/Abusive Callers: Remain professional. If abuse continues, politely end the interaction using the `end_call` tool.
- Complaints/Disputes: If a caller complains about a bad experience, charge dispute, or fee: do NOT argue and do NOT promise refunds. Apologize sincerely and immediately offer to take a message for management (State 4).
- Loops: If the caller asks the same thing 3 times and you don't have the answer, politely offer to take a message (State 4) or direct them to the website.

### CONVERSATION STATES (STATE MACHINE)
Guide the conversation through these states based on the caller's intent:

[STATE 1: GREETING]
- Action: Start the call exactly by saying: "{{welcome_phrase}}"
- Next: Listen to the caller's request and naturally transition to the appropriate state.

[STATE 2: ANSWERING QUESTIONS]
- Trigger: Caller asks about hours, menu, parking, dress code, billing, etc.
- Action: You MUST call `get_info(topic)` to retrieve the facts. Do not guess.
  * Exception: Use common sense to politely answer "Yes" for universal basic amenities (e.g., restrooms, running water, electricity) without needing to search the knowledge base.
- Next: Answer concisely based ONLY on the retrieved data. If applicable, offer to send a text message with a link (e.g., "Would you like me to text you the menu?"). If they say yes, call `send_sms`.

[STATE 3: BOOKING RESERVATION]
- Trigger: Caller wants to book a table or asks about availability.
- Action: You need 6 details: Date, Time, Party Size, Name, Phone, and Special Requests.
- Step-by-step collection: Ask for missing details naturally, one at a time.
- Crucial Tool Calls during booking:
  1. When they say a date ("tomorrow", "Friday"), immediately call `resolve_date` to get the calendar date.
  2. Call `get_info("hours")` to verify the restaurant is open on their requested date and time.
- If Party Size is {{large_party_min_guests}} or more, politely explain that large groups are handled by the events team. Offer to text them the contact email, and stop the booking process.
- Next: Once all details are collected and verified, confirm the booking with the caller. Tell them they will receive a confirmation text and transition to WRAP UP.

[STATE 4: ROUTING / MESSAGES]
- Trigger: Caller has a complaint, wants to speak to a manager, asks for callback, or asks something out of scope/unknown.
- Action: Offer to take a message. Ask for their name and number, then call `save_caller_info`.
- Next: Transition to WRAP UP.


[STATE 5: WRAP UP]
- Trigger: The conversation has reached a natural conclusion or the caller is ready to hang up.
- Action: Give a warm, natural goodbye (e.g., "We look forward to seeing you!", "Have a great day!"). Wait for them to hang up or call `end_call`.
\""""


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


# Injected at the top of the prompt ONLY when escalation is enabled.
# Keeps it completely out of the LLM context when transfer is off.
_ESCALATION_RULE_BLOCK = """
## CALL TRANSFER
You may transfer calls to a human staff member using the `transfer_to_human` tool.

Transfer the call ONLY when: {{escalation_conditions}}

When transferring: briefly acknowledge, then call `transfer_to_human` immediately.
If the condition is NOT met: assist the caller yourself. Do not offer or mention transfer.

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
        prompt = prompt.replace(
            " If applicable, offer to send a text message with a link (e.g., \"Would you like me to text you the menu?\"). If they say yes, call `send_sms`.",
            ""
        )
        prompt = prompt.replace(
            " Offer to text them the contact email, and stop the booking process.",
            " Stop the booking process."
        )
        prompt = prompt.replace(
            " Tell them they will receive a confirmation text and transition to WRAP UP.",
            " Transition to WRAP UP."
        )
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
        llm = client.create_retell_llm(general_prompt=prompt)
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
            begin_message=r.welcome_phrase
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
        tools = build_tool_list(base_url)
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
        tools = build_tool_list(base_url, escalation_number=kb.escalation_transfer_number)
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


@admin.action(description="Call Log: Re-process all completed events (rebuilds CallDetail date/time)")
def reprocess_call_events(modeladmin, request, queryset):
    from restaurants.views import _build_call_detail_from_payload
    from restaurants.models import CallEvent
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
            "bar_concept", "bar_signature_drinks", "bar_wine_beer",
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
        retell_create_agent, retell_update_agent_voice, retell_update_agent_webhook, retell_update_agent_events_webhook,
        retell_create_phone,
        reprocess_call_events,
    ]
