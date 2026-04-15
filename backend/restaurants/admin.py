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
    RestaurantMembership,
    SmsLog,
    Subscription,
)
from .services.retell_client import RetellClient
from .services.retell_tools import (
    _escalation_tool_definition,
    _get_info_tool_definition,
    _resolve_date_tool_definition,
    _sms_tool_definition,
    build_tool_list,
)

LANG_MAP = {"es": "multi", "en": "en-US", "other": "multi"}

AGENT_SYSTEM_PROMPT = """{{account_status_directive}}

## WHO YOU ARE
{{agent_name}}, voice of {{restaurant_name}}. Warm, confident, efficient. Verify facts via tools — never guess.

## SPEECH
- Language: default {{primary_lang}}; match caller after their first full sentence (single words like "ok/hello/bye" don't count).
- Tone: {{conversation_tone}}. {{brand_voice_notes}}
- Answer → stop. Vary phrases. First name ≤1×/turn. Dates: "7 PM" not "19:00". Never read raw URLs.
- Website — ES: {{website_domain_spoken_es}} | EN: {{website_domain_spoken_en}}
- Email — ES: {{contact_email_spoken_es}} | EN: {{contact_email_spoken_en}} — always match conversation language.
- **Escalate immediately** (transfer, else [4]): caller frustrated + unresolved; missing callback mentioned; same answer given twice.
- Mid-call re-engage → continue naturally, never re-greet. Poor audio: note it; after 2 fails → redirect or suggest callback.
- Answer the full question, not a keyword.

## CONTEXT
{{restaurant_name}} | {{address_full}}
{{current_date}} | {{current_time}} ({{timezone}})
{{caller_summary}}

## HARD RULES
1. **Facts:** `get_info(topic)` before answering. Multiple topics → multiple calls. Not found → try `get_info("additional")`. Basic amenities = no lookup needed.
2. **Dates:** `resolve_date` for any non-exact date. Read back `spoken_es`/`spoken_en` exactly. `is_past=true` → tell caller. `ambiguity` → clarify. Unresolvable → tell caller a team member will confirm and follow up.
3. **Names:** Name alone = request, not intro. "I'm [name]"/"my name is" = intro. Use {{team_members}} to ID staff.
4. **Phone:** Caller's number = {{caller_from_number}} — ask if best to reach them; use theirs if different. Don't re-ask known info.
5. **Memory:** Prior visit reference → `get_caller_profile()`. Use naturally; don't state name before confirmed.
6. **Scope:** Only {{restaurant_name}} topics. You are the AI assistant. Emergency → 911 → `end_call`. Abuse → `end_call`.
7. **No dead ends — CRITICAL!!!** After answering, check if the specific detail was actually given. Vague answer ("varies", "depends", range, "check website") → offer transfer or [4]. "I don't have that info" is never a final answer.
8. **Tool errors:** If a tool returns an error, try once more with corrected or simplified parameters. If it fails again, continue the conversation without it — offer to connect a team member or take a message. Never end the call because a tool failed.
9. **No unsolicited offers mid-call.** Exception: WRAP UP for hot topics — once per call max.
{{non_customer_call_rules}}

## FLOW

**[1] GREETING**
"{{welcome_phrase}}" already said — don't repeat.
- Non-customer (vendor/press/sales/robocall): NON-CUSTOMER rules
- Wants a person/staff: TRANSFER, else [4]
- Leave a message: [4]
- Question (non-reservation): [2]
- Mentions reservation (any kind): [3]
- Name alone / asking for someone: transfer if conditions met, else [4]
- Unclear: one brief open question

**[2] QUESTIONS**
0. Garbled/unclear → ask to repeat BEFORE any tool call (one short question only).
1. `get_info(topic)`
2. **ANSWER CHECK — CRITICAL!!!** Have the exact detail? → give it. Don't have it (vague/missing)? → offer to connect someone who can confirm → transfer or [4]. Never end on "I don't have that."
3. SMS enabled: after answering, offer once to text the info. Yes → `send_sms` with matching type: menu → `menu_link` | bar/cocktails → `bar_menu_link` | hours → `hours` | music → `music` | valet/parking → `valet` | social media → `social_media` | location → `address` | events → `event_inquiry` | other → `custom`.

**[3] RESERVATION**
Intent unclear → one clarifying question first.
**DUPLICATE CHECK — CRITICAL!!!** "Last reservation" matches requested date → don't create new. Ask: same, modify, or new? Cite existing date/time/party size.
Collect in order (skip if already known): Date → Time → Party Size → Name (Rule 3) → Phone (Rule 4) → Special Requests.
- Resolve date (Rule 2). Check hours via `get_info("hours")` (schedule ≠ availability).
- ≥{{large_party_min_guests}} guests → [5].
- Walk-in: name + ETA, tell caller the team expects them.
- Modify/cancel/existing: you cannot look up or change directly. Collect name + date + describe change → tell team will verify → WRAP UP.
- Prior reservation interest → revisit once after questions (not after each). If declined, drop.
- All info collected → confirm back (date, time, party size, name) → tell caller: reservation will be processed, confirmation text will come from OpenTable → WRAP UP. No more questions.

**[4] MESSAGES**
Collect contact (Rule 4). Confirm a team member will call back.
SMS enabled → offer useful text → WRAP UP.

**[5] EVENTS**
Private event / buyout / large party from [3]. Caller wants to speak directly → TRANSFER.
1. `get_info("private_events")`
2. Collect: name, phone (Rule 4), brief description.
3. Confirm events team will follow up.
4. SMS enabled → MUST offer; yes → `send_sms(sms_type="event_inquiry")` → WRAP UP.

**[WRAP UP]**
One soft offer max per call, in order (never stack):
1. **Reservation** (hot topics only: menu, bar, cocktails, happy hour, music, ambience, dietary + no reservation taken) → offer once. Yes → [3]. No → goodbye. Never offer after hours/parking/billing/address/events questions. Never insist.
2. **SMS** (skip if step 1 was offered; SMS enabled + none sent yet) → offer once.

Warm goodbye in caller's language → `end_call` immediately. Don't wait for response.
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
        "description": "24-hour HH:MM (e.g. '18:00'). ONLY if the caller explicitly stated a specific time. Empty string if no time was mentioned.",
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
    {
        "name": "caller_sentiment",
        "type": "enum",
        "description": (
            "Overall caller sentiment throughout the call. "
            "'positive': caller was satisfied, friendly, or appreciative. "
            "'neutral': no strong emotion detected. "
            "'frustrated': caller showed signs of frustration or impatience but remained civil. "
            "'upset': caller was clearly upset, confrontational, or the call ended unresolved."
        ),
        "choices": ["positive", "neutral", "frustrated", "upset"],
    },
    # ── Quality signals for weekly report ────────────────────────────────────
    # After deploy: run "Retell: 1c — Configure post-call analysis fields" admin
    # action on each active restaurant to push these fields to Retell.
    {
        "name": "agent_failed_to_answer",
        "type": "boolean",
        "description": (
            "True if the agent was unable to answer a question the caller clearly asked — "
            "responded with uncertainty, vagueness, or admitted not having the information."
        ),
    },
    {
        "name": "unanswered_question",
        "type": "string",
        "description": (
            "If agent_failed_to_answer is true, quote the caller's exact words when they asked "
            "the question the agent couldn't answer. Empty string if agent_failed_to_answer is false."
        ),
    },
    {
        "name": "agent_response_to_unanswered",
        "type": "string",
        "description": (
            "If agent_failed_to_answer is true, quote the agent's exact response that showed "
            "uncertainty or lack of information. Empty string if agent_failed_to_answer is false."
        ),
    },
    {
        "name": "agent_confusion_moment",
        "type": "string",
        "description": (
            "If there was a moment where the agent clearly misunderstood the caller's intent, "
            "describe it in one sentence. Empty string if none."
        ),
    },
    {
        "name": "caller_frustration",
        "type": "boolean",
        "description": (
            "True if the caller showed frustration at any point: repeated themselves, "
            "expressed dissatisfaction, gave up on getting an answer, or showed impatience."
        ),
    },
    {
        "name": "transfer_was_necessary",
        "type": "boolean",
        "description": (
            "If the call was transferred to a human: true if the transfer was truly necessary "
            "and the agent could not have resolved the need. False if the agent could have handled it. "
            "Null/omit if no transfer occurred."
        ),
    },
    {
        "name": "language_consistency",
        "type": "boolean",
        "description": (
            "True if the agent maintained consistent language throughout the entire call "
            "(including greeting, body, and goodbye). False if the agent switched languages "
            "or used the wrong language at any point."
        ),
    },
    {
        "name": "is_spam_or_robocall",
        "type": "boolean",
        "description": (
            "True if this was a robocall, automated message, or commercial spam "
            "rather than a real customer or human caller."
        ),
    },
    {
        "name": "call_quality",
        "type": "enum",
        "description": (
            "Overall quality of the call. Only two values are allowed. "
            "'poor' if ANY of the following occurred: "
            "(1) the caller expressed frustration, impatience, or dissatisfaction at any point; "
            "(2) the agent misunderstood the caller's intent (even if later corrected); "
            "(3) the agent could not answer a question the caller clearly asked; "
            "(4) the caller wanted a reservation but it was left incomplete (missing name, date, time, or party size); "
            "(5) the agent spoke in the wrong language at any point. "
            "'excellent' only if none of the above occurred and the caller's need was fully and correctly addressed."
        ),
        "choices": ["excellent", "poor"],
    },
    {
        "name": "caller_message",
        "type": "string",
        "description": (
            "If the caller explicitly left a message for the team (e.g. callback request, "
            "question for staff, complaint), quote what they said. "
            "Empty string if the caller did not leave a message."
        ),
    },
]


# Injected at the top of the prompt ONLY when escalation is enabled.
# Keeps it completely out of the LLM context when transfer is off.
_ESCALATION_RULE_BLOCK = """
## CALL TRANSFER
Transfer using `transfer_to_human` when: {{escalation_conditions}}.
A name alone is not a transfer request — confirm what they need first.

1. If you don't have their name, ask once (5 words max).
2. Tell them you're connecting → call `transfer_to_human`.
3. If transfer fails: tell them, then ask "¿Intento de nuevo o prefiere dejar un mensaje?"
   - Retry → go to step 2.
   - Leave message → collect their name and reason, confirm a team member will call back.

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
        # Remove SMS offer sentences from the prompt
        prompt = prompt.replace(
            "If SMS enabled: AFTER giving your answer, ALWAYS offer to send the info by text. "
            "Say something like \"¿Le envío eso por mensaje de texto?\" then wait. "
            "If yes → call `send_sms` with the matching type: menu → `menu_link` | "
            "bar/cocktails → `bar_menu_link` | hours → `hours` | music → `music` | "
            "valet/parking → `valet` | social media → `social_media` | location → `address` | "
            "events → `event_inquiry` | other → `custom`. Only offer once per call.\n",
            "",
        )
        prompt = prompt.replace(
            '4. If SMS enabled → MUST offer: "¿Le envío el contacto de eventos por texto?" → if yes: `send_sms(sms_type="event_inquiry")`.\n',
            "",
        )
        prompt = prompt.replace(
            "If SMS enabled → offer to send something useful by text before wrapping up → WRAP UP.\n",
            "→ WRAP UP.\n",
        )
        prompt = prompt.replace(
            "If SMS enabled AND no SMS was sent during the call: offer once to send something useful by text "
            "(menu, address, social media, etc.) before saying goodbye. If caller declines or nothing relevant, skip.\n",
            "",
        )

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


@admin.action(description="Retell: 1d — Configure base tools (get-info + SMS + resolve-date) on LLM")
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
            messages.success(request, f"[{r.slug}] All tools configured and published (get_info + resolve_date + SMS + escalation → {kb.escalation_transfer_number})")
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


@admin.action(description="Retell: CF-1 — Create Agent (Conversation Flow) — set retell_conversation_flow_id first")
def retell_create_agent_cf(modeladmin, request, queryset):
    """Create a new Retell agent using an existing Conversation Flow as the response engine."""
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_conversation_flow_id:
            messages.error(request, f"[{r.slug}] retell_conversation_flow_id is empty — fill it in and save first.")
            continue
        if not settings.RETELL_WEBHOOK_BASE_URL:
            messages.error(request, f"[{r.slug}] RETELL_WEBHOOK_BASE_URL not set in .env — cannot build webhook URL.")
            continue

        events_url = f"{settings.RETELL_WEBHOOK_BASE_URL}/api/retell/events/"
        lang = LANG_MAP.get(r.primary_lang, "multi")

        client = RetellClient(api_key=r.retell_api_key)
        agent = client.create_agent(
            agent_name=f"{r.name} — Inbound Agent (CF)",
            voice_id=r.retell_voice_id,
            voice_speed=1.05,
            voice_temperature=1.2,
            language=lang,
            response_engine={"type": "conversation-flow", "conversation_flow_id": r.retell_conversation_flow_id},
            webhook_url=events_url,
        )
        r.retell_agent_id = agent.agent_id
        r.save(update_fields=["retell_agent_id"])
        messages.success(request, f"[{r.slug}] CF Agent created: {r.retell_agent_id} | events → {events_url}")


@admin.action(description="Retell: CF-2 — Switch existing agent to Conversation Flow")
def retell_attach_conversation_flow(modeladmin, request, queryset):
    """Switch an existing agent's response engine to conversation-flow."""
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_agent_id:
            messages.error(request, f"[{r.slug}] No Agent ID — run 'Create Agent' first.")
            continue
        if not r.retell_conversation_flow_id:
            messages.error(request, f"[{r.slug}] retell_conversation_flow_id is empty — fill it in and save first.")
            continue

        client = RetellClient(api_key=r.retell_api_key)
        client.update_agent(
            r.retell_agent_id,
            response_engine={"type": "conversation-flow", "conversation_flow_id": r.retell_conversation_flow_id},
        )
        messages.success(request, f"[{r.slug}] Agent switched to Conversation Flow: {r.retell_conversation_flow_id}")


@admin.action(description="Retell: CF-3 — Revert agent to Single Prompt (retell-llm)")
def retell_detach_conversation_flow(modeladmin, request, queryset):
    """Revert agent's response engine back to retell-llm (single prompt)."""
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_agent_id:
            messages.error(request, f"[{r.slug}] No Agent ID.")
            continue
        if not r.retell_llm_id:
            messages.error(request, f"[{r.slug}] No LLM ID — cannot revert to single prompt.")
            continue

        client = RetellClient(api_key=r.retell_api_key)
        client.update_agent(
            r.retell_agent_id,
            response_engine={"type": "retell-llm", "llm_id": r.retell_llm_id},
        )
        messages.success(request, f"[{r.slug}] Agent reverted to single prompt (retell-llm): {r.retell_llm_id}")


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


class MembershipInline(admin.TabularInline):
    model = RestaurantMembership
    extra = 0
    fields = ("user", "role", "is_active", "can_edit_kb", "created_at")
    readonly_fields = ("created_at",)


class SubscriptionInline(admin.StackedInline):
    model = Subscription
    can_delete = False
    extra = 0
    fields = (
        "status", "communication_balance", "communication_markup", "sms_unit_cost",
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

    def change_view(self, request, object_id, form_url="", extra_context=None):
        try:
            obj = self.get_object(request, object_id)
            if obj:
                restaurant = obj.restaurant
                if restaurant.is_active and not obj.is_active:
                    self.message_user(
                        request,
                        f"⚠ Restaurant is_active=True but subscription is '{obj.status}' — "
                        "the agent will reject all calls. Activate the subscription to restore service.",
                        level=messages.WARNING,
                    )
                elif not restaurant.is_active and obj.is_active:
                    self.message_user(
                        request,
                        f"⚠ Subscription is active ('{obj.status}') but restaurant is_active=False — "
                        "Retell is disconnected. Set restaurant is_active=True to restore service.",
                        level=messages.WARNING,
                    )
        except Exception:
            pass
        return super().change_view(request, object_id, form_url, extra_context)

    @admin.action(description="Stripe: Show Webhook Configuration URL")
    def show_webhook_url(self, request, queryset):
        domain = request.get_host()
        messages.info(request, f"Set your Stripe Webhook URL to: https://{domain}/api/stripe/webhook/")

    @admin.action(description="Stripe: Reset/Clear Stripe IDs (use when switching Test/Live modes)")
    def reset_stripe_ids(self, request, queryset):
        count = queryset.update(stripe_customer_id="", stripe_subscription_id="")
        messages.success(request, f"Successfully cleared Stripe IDs for {count} subscriptions. New IDs will be generated on next payment attempt.")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and "status" in form.changed_data:
            from restaurants.views import (
                _disconnect_retell_phone,
                _reconnect_retell_phone,
            )
            active_statuses = ("active", "trialing")
            old_status = form.initial.get("status", "")
            new_status = obj.status
            if new_status in active_statuses and old_status not in active_statuses:
                _reconnect_retell_phone(obj.restaurant)
                self.message_user(request, f"Retell phone reconnected for {obj.restaurant.name}.")
            elif new_status not in active_statuses and old_status in active_statuses:
                _disconnect_retell_phone(obj.restaurant)
                self.message_user(request, f"Retell phone disconnected for {obj.restaurant.name}.")


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


@admin.action(description="Re-send follow-up alert email")
def resend_followup_email(_modeladmin, request, queryset):
    from .views import _send_followup_alert_email
    sent = skipped = failed = 0
    for event in queryset.select_related("restaurant", "detail"):
        detail = getattr(event, "detail", None)
        if not detail or not detail.follow_up_needed:
            skipped += 1
            continue
        try:
            _send_followup_alert_email(event, event.restaurant)
            sent += 1
        except Exception as exc:
            failed += 1
            messages.error(request, f"[{event.pk}] Failed: {exc}")
    if sent:
        messages.success(request, f"Sent {sent} follow-up email(s).")
    if skipped:
        messages.warning(request, f"Skipped {skipped} (no follow-up flag).")
    if failed:
        messages.error(request, f"{failed} failed — see errors above.")


@admin.register(CallEvent)
class CallEventAdmin(admin.ModelAdmin):
    list_display  = ("restaurant", "event_type", "created_at")
    list_filter   = ("event_type", "restaurant")
    readonly_fields = ("created_at",)
    inlines       = [CallDetailInline]
    actions       = [resend_followup_email]


@admin.register(CallDetail)
class CallDetailAdmin(admin.ModelAdmin):
    list_display  = (
        "caller_name", "caller_phone", "call_reason",
        "wants_reservation", "party_size", "call_cost", "follow_up_needed", "created_at",
    )
    list_filter   = ("call_reason", "wants_reservation", "follow_up_needed")
    search_fields = ("caller_name", "caller_phone", "caller_email", "notes")
    readonly_fields = ("created_at", "updated_at")


from .views import _send_sms_via_twilio  # noqa: E402 — defined after models are loaded


@admin.action(description="Send corrected SMS")
def send_corrected_sms(modeladmin, request, queryset):
    if queryset.count() != 1:
        modeladmin.message_user(request, "Select exactly one SMS log entry.", level="error")
        return

    log = queryset.first()

    if "send_corrected" in request.POST:
        message = request.POST.get("corrected_message", "").strip()[:320]
        if not message:
            modeladmin.message_user(request, "Message cannot be empty.", level="error")
            return
        try:
            sid = _send_sms_via_twilio(log.restaurant, log.to_number, message)
            SmsLog.objects.create(
                restaurant=log.restaurant,
                call_event=log.call_event,
                to_number=log.to_number,
                message=message,
                status=SmsLog.STATUS_SENT,
                twilio_sid=sid,
            )
            modeladmin.message_user(request, f"Corrected SMS sent to {log.to_number} (sid={sid}).")
        except Exception as exc:
            modeladmin.message_user(request, f"Failed to send: {exc}", level="error")
        return

    from django.http import HttpResponse
    html = f"""<!DOCTYPE html><html><head>
<title>Send corrected SMS</title>
<link rel="stylesheet" href="/static/admin/css/base.css">
</head><body id="django-admin-body" class="default">
<div id="content-main" style="padding:20px;max-width:600px">
  <h1>Send corrected SMS</h1>
  <p><strong>To:</strong> {log.to_number}</p>
  <p><strong>Original message:</strong><br><em>{log.message}</em></p>
  <form method="post">
    <input type="hidden" name="csrfmiddlewaretoken" value="{request.META.get('CSRF_COOKIE', '')}">
    <input type="hidden" name="action" value="send_corrected_sms">
    <input type="hidden" name="_selected_action" value="{log.pk}">
    <input type="hidden" name="send_corrected" value="1">
    <p><label><strong>Corrected message (max 320 chars):</strong><br>
    <textarea name="corrected_message" rows="4" cols="60" maxlength="320">{log.message}</textarea>
    </label></p>
    <input type="submit" value="Send corrected SMS" class="button default">
    &nbsp;<a href=".." class="button">Cancel</a>
  </form>
</div></body></html>"""
    from django.middleware.csrf import get_token
    get_token(request)  # ensure CSRF cookie is set
    return HttpResponse(html)


@admin.register(SmsLog)
class SmsLogAdmin(admin.ModelAdmin):
    list_display   = ("created_at", "restaurant", "to_number", "status", "delivered_at", "twilio_sid")
    list_filter    = ("status", "restaurant")
    search_fields  = ("to_number", "message", "twilio_sid")
    readonly_fields = ("created_at", "delivered_at", "twilio_sid", "error_message")
    actions        = [send_corrected_sms]


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
            "website", "social_media_url", "contact_email", "contact_phone",
            "address_full", "location_reference",
            "welcome_phrase",
            "phone_mode", "existing_ph_numb",
            "notify_via_email", "notify_email",
            "notify_via_ws", "notify_ws_numb",
            "notify_weekly_report", "weekly_report_language",
            "created_at", "updated_at",
        )}),
        ("Retell — Single Prompt", {"fields": (
            "retell_api_key", "retell_llm_id", "retell_agent_id",
            "retell_phone_number", "retell_voice_id", "retell_area_code",
        )}),
        ("Retell — Conversation Flow", {"fields": (
            "retell_conversation_flow_id",
        ), "description": "Set the Conversation Flow ID (from Retell dashboard), then use the CF actions to create or switch the agent."}),
        ("Twilio SMS (per-restaurant billing)", {"fields": (
            "enable_sms", "twilio_account_sid", "twilio_auth_token", "twilio_from_number",
        ), "description": "Enable Twilio integration, or leave credentials blank to use the platform-level Twilio from .env."}),
    )
    inlines = [KnowledgeBaseInline, MembershipInline, SubscriptionInline]
    actions = [
        retell_create_llm, retell_update_llm_prompt, retell_configure_call_analysis,
        retell_configure_sms_tool, retell_configure_escalation_tool,
        retell_create_agent, retell_update_agent_language, retell_update_agent_voice, retell_update_agent_webhook, retell_update_agent_events_webhook,
        retell_create_agent_cf, retell_attach_conversation_flow, retell_detach_conversation_flow,
        retell_create_phone,
        reprocess_call_events,
        "clear_call_history",
    ]

    def change_view(self, request, object_id, form_url="", extra_context=None):
        try:
            obj = self.get_object(request, object_id)
            if obj:
                sub = getattr(obj, "subscription", None)
                sub_active = sub and sub.is_active if sub else False
                if obj.is_active and not sub_active:
                    self.message_user(
                        request,
                        f"⚠ Restaurant is_active=True but subscription is '{sub.status if sub else 'missing'}' — "
                        "the agent will reject all calls. Activate the subscription to restore service.",
                        level=messages.WARNING,
                    )
                elif not obj.is_active and sub_active:
                    self.message_user(
                        request,
                        f"⚠ Subscription is active ('{sub.status}') but restaurant is_active=False — "
                        "Retell is disconnected. Set is_active=True to restore service.",
                        level=messages.WARNING,
                    )
        except Exception:
            pass
        return super().change_view(request, object_id, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change and "is_active" in form.changed_data:
            from restaurants.views import (
                _disconnect_retell_phone,
                _reconnect_retell_phone,
            )
            if obj.is_active:
                _reconnect_retell_phone(obj)
                self.message_user(request, f"Retell phone reconnected for {obj.name}.")
            else:
                _disconnect_retell_phone(obj)
                self.message_user(request, f"Retell phone disconnected for {obj.name}.")

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


# ─── WeeklyReport Admin ───────────────────────────────────────────────────────

from .models import WeeklyReport  # noqa: E402


@admin.register(WeeklyReport)
class WeeklyReportAdmin(admin.ModelAdmin):
    list_display = (
        "restaurant", "week_start", "week_end", "generated_at",
        "model_used", "generation_cost",
        "has_owner_summary", "has_prompt_suggestions",
    )
    list_filter = ("restaurant",)
    search_fields = ("restaurant__name",)
    ordering = ("-week_start",)
    readonly_fields = (
        "generated_at", "model_used", "generation_cost",
        "owner_summary", "prompt_suggestions", "metrics",
    )

    @admin.display(boolean=True, description="Owner Summary")
    def has_owner_summary(self, obj):
        return bool(obj.owner_summary)

    @admin.display(boolean=True, description="Prompt Suggestions")
    def has_prompt_suggestions(self, obj):
        return bool(obj.prompt_suggestions)
