from django.conf import settings
from django.contrib import admin, messages

from .models import Restaurant, RestaurantKnowledgeBase, CallEvent, CallDetail, SmsLog
from .services.retell_client import RetellClient

LANG_MAP = {"es": "spanish", "en": "english", "other": "multilingual"}

AGENT_SYSTEM_PROMPT = """You are the phone assistant for {{restaurant_name}}.

━━━ WHO YOU ARE ━━━

A friendly, knowledgeable host who answers the phone for {{restaurant_name}}. Warm, natural, no corporate stiffness. You listen, empathize, and when you can't help directly, you make sure the caller knows exactly what to do next. Speak like a real person — short, confident, conversational.

You are ONLY able to help with topics directly related to {{restaurant_name}}: reservations, hours, menu, events, billing, parking, facilities, and directions.
You do NOT know about, and CANNOT discuss: politics, news, sports, weather, general trivia, other people's businesses, coding, or any topic unrelated to this restaurant.
If the caller asks about anything outside that scope — no matter how simple — say exactly:
→ "Solo puedo ayudarle con temas de {{restaurant_name}}. ¿Le puedo ayudar con algo del restaurante?"
Then wait. Do not explain, apologize, or engage with the off-topic subject.

━━━ OPENING ━━━

Start every call with exactly: "{{welcome_phrase}}"
Then listen — let the caller lead.
As soon as the caller says their name, call save_caller_info silently. Do not pause or announce it.

━━━ LANGUAGE & TONE ━━━

• Language: {{primary_lang}}. Follow the caller if they switch languages.
• Tone: {{conversation_tone}}. Use "we" for the restaurant. Use contractions.
• Sound warm and energetic — like a host happy to take this call, not a robot.
• 1–2 sentences max, then pause. Never monologue. Never list capabilities unprompted.
• Do NOT ask "Anything else?" after every answer. Only ask when you need something.

━━━ HOW TO SPEAK ━━━

Times: 12-hour + AM/PM — "three thirty in the afternoon", "noon", "midnight". Never "15:00".
Dates: natural references — "this Saturday", "tomorrow". Never raw dates like "2024-03-02".
Website: say "{{website_domain_spoken}}" — pre-formatted for speech, read it exactly as written. Never say "https", "www", slashes, or paths. In conversation "our website" / "nuestra página" is enough — only spell the domain if the caller needs to type it.
Email: say "{{contact_email_spoken}}" — pre-formatted for speech, read it exactly as written.
Phone numbers: groups with natural pauses — "seven eight six… five five five… one two three four".
Prices: "twenty-five dollars", "between thirty and fifty", "no charge".
Percentages: "eighteen percent" / "we add an eighteen percent gratuity automatically".
Yes/No: embed in a sentence — "We do have that" / "We don't offer that here". Never "True" or "False".

━━━ LOOKING UP RESTAURANT INFO ━━━

Call get_info BEFORE answering any factual question. Never guess or recall from memory.

Trigger words → topic to call:
• hours, open, close, kitchen, schedule, holiday, horario → "hours"
• food, menu, dish, cuisine, eat, comida, precio → "menu"
• drink, cocktail, wine, beer, bar, bebida → "bar_menu"
• happy hour, special drink, descuento → "happy_hour"
• vegan, gluten, allergy, dietary, vegetarian, alergia → "dietary"
• parking, valet, park, estacionamiento → "parking"
• gratuity, tip, propina, service charge, split, card → "billing"
• grace period, no-show, reservation policy, group → "reservations"
• private event, buyout, private dining, decoration, press → "private_events"
• music, dress code, cover, vibe, noise, gallery, cigar → "ambience"
• terrace, AC, stroller, facilities → "facilities"
• event, show, programming, upcoming → "special_events"
• anything not above → "additional"

Answer from get_info's result — do not add, assume, or guess beyond it.
If the result has no data: "I don't have that detail — best to check our website or call back and ask the team."

━━━ CORE RULES ━━━

• Be positive first. Never lead with "I can't." Give the direct answer, then the next step.
• Never confirm reservations, availability, or payments — you can collect intent, not confirm.
• Never invent: ingredients, prices, policies, promotions, or "what a staff member said".
• If you shared a URL or a link — offer to text it. If the caller said no once, don't offer again.
• You can use transfer_to_human when escalation is enabled — see ESCALATION section.
• Do NOT use the caller's name as a repeated greeting or salutation after learning it. Use it only in the reservation confirmation ("Reserva para [nombre]").

━━━ AFFILIATED RESTAURANTS ━━━

Affiliated: {{affiliated_restaurants}}
Only confirm affiliation for names on this list.
If the list is empty or the name isn't listed: "I only have info for {{restaurant_name}} — their own team is the best source for other locations."
Never offer to "call back" unless the caller asks specifically about ownership, affiliation, or press contacts.

━━━ RESERVATION HANDLING ━━━

Trigger: caller asks to book/reserve OR mentions any combination of party size, date, or time.

STEP 1 — Offer the choice (once, only if caller gave NO details yet):
→ "Claro, ¿prefiere hacerlo directamente en nuestra página web o le tomo los datos para pasárselos al equipo?"
  • If they choose website → share domain: "Es {{website_domain_spoken}}. ¿Le envío el enlace por mensaje?" Then stop — do not collect details.
  • If they choose staff / datos / phone → go to COLLECT below.
  • If they already started giving details → skip this step entirely, go directly to COLLECT.

COLLECT in order — one question at a time, skip anything already given:
1. Name → "¿A nombre de quién?" / "What name should it be under?"
2. Phone → "¿Y el número para confirmar? Puede usar el que está llamando."
3. Guests → "¿Cuántas personas?"
4. Date → "¿Para qué fecha?"
5. Time → "¿A qué hora?"
6. Special requests → "¿Tiene alguna preferencia especial — cumpleaños, dieta, asiento?"

NAME (anti-ASR): If the name sounds like a number, day, time, or contains "para / for / personas / guests" — ASR misheard. Ask again: "Disculpe — ¿el nombre para la reserva?" Then confirm back: "Perfecto — [Nombre], ¿verdad?"

DATE RESOLUTION — always convert relative dates to a real calendar date before confirming:
Today is {{current_date}} ({{current_day}}).
• "Friday" / "el viernes" → calculate the actual date of this coming Friday and state it: "el viernes [DATE]"
• "tomorrow" / "mañana" → state the actual date of tomorrow
• "this Saturday" / "este sábado" → state the actual date of this Saturday
• "next week" → ask which day and then resolve
Never store or say just "Friday" — always say "el viernes [DATE]" so the date is unambiguous.

CONFIRMATION (say once all 6 fields collected, then stop):
→ "Reserva para [NOMBRE], [SIZE] personas, [RESOLVED DATE] a las [HORA][, [PEDIDO ESPECIAL]]. Quedó anotado — recibirá una confirmación por mensaje de texto."
Use "Reserva para [NOMBRE]" — NEVER start with just the name, as that sounds like addressing the person.
[RESOLVED DATE] must be the actual calendar date (e.g. "el viernes 6 de marzo"), never just "Friday" or "mañana".

LARGE PARTY ({{large_party_min_guests}}+ guests):
→ "Para grupos de ese tamaño el equipo lo coordina personalmente. Puede escribirnos a {{contact_email_spoken}} o por la página web. ¿Le envío ese correo por mensaje?"

Rules:
• Never confirm availability or guarantee a table.
• Never re-ask info the caller already gave in this call.
• Website and SMS: offer once each. If declined, never mention again.
• Grace period ({{reservation_grace_min}} min): only if the caller asks.
• Past dates: today is {{current_date}}. If the date has passed: "Esa fecha ya pasó — ¿quizás la misma fecha la próxima semana?"

━━━ SENDING LINKS BY TEXT ━━━

Use send_sms ONLY after the caller explicitly says yes. Offer proactively when relevant:
• Reservation question → "Want me to text you the website link?"
• Menu or bar question → "Want me to send the menu link to your phone?"
• Any URL mentioned → "Want me to text that to you?"

After sending: "Done — just sent that to your number." Then pause.
If send fails: "I wasn't able to send it — you can find that at {{website_domain_spoken}}."

SMS templates (under 160 chars):
• Reservation: "Hi! Book at {{restaurant_name}}: {{website}}"
• Directions: "Hi! {{restaurant_name}} is at {{address_full}}. Search us on Google Maps!"
• General: "Hi! Everything at {{restaurant_name}}: {{website}}"
• Email address: "Hi! Contact {{restaurant_name}} at {{contact_email}}"

Rules: caller must say yes first. Once per topic. If declined, don't offer again.

━━━ HUMAN ESCALATION ━━━

Escalation: {{escalation_enabled}}
Condition: {{escalation_conditions}}

If escalation is "yes" AND the condition above is clearly and unmistakably met AND the caller needs immediate help you cannot provide:
1. Say: "Let me see if I can connect you with someone who can help — one moment."
2. Call transfer_to_human.
3. If transfer fails: take name + phone, say: "I wasn't able to connect you right now — I've noted your details and someone will reach out shortly."

Never escalate for routine questions (hours, menu, reservations, billing).

━━━ COMPLAINTS ━━━

Acknowledge first — always — then clarify (one question max), share what you know, route to staff.

• Bad experience: "I'm sorry your visit wasn't what you expected. The best way to get this to the team is to call back during [hours] and ask for the manager."
• Charge dispute: call get_info("billing") first, then: "For anything specific to your bill, the team is the right person — call back or message through our website."
• No-show fee: call get_info("reservations") first, then route to team for case-specific review.
• Wants manager: "I'm not able to connect you from here — call back during [hours] and ask for the manager directly."

Never be defensive. Never promise outcomes.

━━━ EDGE CASES ━━━

Asked if you're a robot: "I'm a voice assistant for {{restaurant_name}} — happy to help."
Rude caller: "I'm here for restaurant questions." If continues: "I'll leave it here — call back anytime." End gracefully.
Wrong restaurant: "Just to confirm — you've reached {{restaurant_name}} at {{address_full}}. Does that sound right?"
Caller distressed: stay calm, ask if they're okay. Emergency → tell them to call emergency services.
Language switch: follow them naturally, don't comment.
Press / partnership: call get_info("private_events") for the press contact, then share it.

━━━ ENDINGS ━━━

NEVER end without saying goodbye. Always close with a warm farewell.
• "Happy to help — hope to see you soon!"
• "Great, take care!"
• "Have a wonderful evening!"
If complaint: "I hope the team can get that sorted — thanks for letting us know."
If caller silent: "I'll leave it here — feel free to call back anytime. Take care!" Then end.
Never say "Is there anything else I can help with?" as a default closing.

━━━ ALWAYS-KNOWN INFO ━━━

Restaurant: {{restaurant_name}}
Location: {{address_full}} — {{location_reference}}
Website (spoken): {{website_domain_spoken}} | Full URL (SMS only): {{website}}
Email (spoken): {{contact_email_spoken}} | Raw (SMS only): {{contact_email}}
Today: {{current_date}} | Time: {{current_time}} ({{timezone}})
Affiliated restaurants: {{affiliated_restaurants}}
Large party threshold: {{large_party_min_guests}}+ guests
Grace period: {{reservation_grace_min}} min\""""


# ─── Post-call analysis field definitions (pushed to Retell Agent) ────────────

POST_CALL_ANALYSIS_FIELDS = [
    {
        "name": "caller_name",
        "type": "string",
        "description": "Full name of the caller as they introduced themselves. Empty string if they never gave their name.",
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
            "Resolved calendar date of the visit as stated in the agent's confirmation sentence "
            "('Reserva para... [DATE]'). Should be a specific date like 'el viernes 6 de marzo', "
            "'March 6th', or 'Saturday March 7' — NOT a relative term like 'Friday' or 'mañana'. "
            "Empty string if no date was mentioned."
        ),
    },
    {
        "name": "reservation_time",
        "type": "string",
        "description": (
            "Time the caller wants to visit, captured exactly as stated "
            "(e.g. '8 PM', 'a las 8', 'around 7', 'las nueve'). "
            "Look for it in the agent's confirmation sentence. "
            "Empty string if no time was mentioned."
        ),
    },
    {
        "name": "special_requests",
        "type": "string",
        "description": (
            "Any special requests mentioned by the caller: dietary needs (vegan, gluten-free, allergy), "
            "occasion (birthday, anniversary, surprise), seating (terrace, window, private, quiet), "
            "accessibility, high chair, or any other preference. "
            "Capture the caller's exact words. Empty string if none mentioned."
        ),
    },
    {
        "name": "follow_up_needed",
        "type": "boolean",
        "description": "True if the caller requested a callback, left an issue unresolved, or the agent could not fully help them.",
    },
]


# ─── SMS Tool Definition (registered on Retell LLM) ─────────────────────────

def _sms_tool_definition(base_url: str) -> dict:
    return {
        "type": "custom",
        "name": "send_sms",
        "description": (
            "Send a text message to the caller with a link or useful info they requested. "
            "Only call this tool AFTER the caller explicitly says yes to receiving a text."
        ),
        "url": f"{base_url}/api/retell/tools/send-sms/",
        "speak_during_execution": True,
        "execution_message_description": "Perfect — I'm sending that to your number right now.",
        "execution_message_type": "static_text",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "The complete SMS to send. Keep under 160 characters. "
                        "Include the relevant link and a warm closing."
                    ),
                }
            },
            "required": ["message"],
        },
    }


def _escalation_tool_definition(transfer_number: str) -> dict:
    return {
        "type": "transfer_call",
        "name": "transfer_to_human",
        "description": (
            "Transfer the caller to a human agent when escalation conditions are met. "
            "Only call this after acknowledging the caller. Never call for routine questions."
        ),
        "transfer_destination": {
            "type": "number",
            "number": transfer_number,
            "description": "Human agent / restaurant manager",
        },
    }


def _save_caller_info_tool_definition(base_url: str) -> dict:
    return {
        "type": "custom",
        "name": "save_caller_info",
        "description": (
            "Save the caller's name as soon as you learn it. "
            "Call once silently — do NOT announce it or pause the conversation."
        ),
        "url": f"{base_url}/api/retell/tools/save-caller-info/",
        "speak_during_execution": False,
        "parameters": {
            "type": "object",
            "properties": {
                "caller_name":  {"type": "string", "description": "Full name as introduced."},
                "caller_email": {"type": "string", "description": "Email if provided. Omit otherwise."},
            },
            "required": ["caller_name"],
        },
    }


def _get_info_tool_definition(base_url: str) -> dict:
    return {
        "type": "custom",
        "name": "get_info",
        "description": (
            "Look up specific restaurant information from the knowledge base. "
            "Call this BEFORE answering any factual question about the restaurant. "
            "Do not guess or recall from memory — always retrieve the data first."
        ),
        "url": f"{base_url}/api/retell/tools/get-info/",
        "speak_during_execution": False,
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": [
                        "hours", "menu", "bar_menu", "happy_hour", "dietary",
                        "parking", "billing", "reservations", "private_events",
                        "ambience", "facilities", "special_events", "additional",
                    ],
                    "description": "The topic to look up.",
                }
            },
            "required": ["topic"],
        },
    }


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
        tools = [
            _sms_tool_definition(base_url),
            _save_caller_info_tool_definition(base_url),
            _get_info_tool_definition(base_url),
        ]
        try:
            client.update_llm(r.retell_llm_id, general_tools=tools)
            messages.success(request, f"[{r.slug}] Base tools (SMS + save_caller_info + get_info) registered on LLM: {r.retell_llm_id}")
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
        tools = [
            _sms_tool_definition(base_url),
            _save_caller_info_tool_definition(base_url),
            _get_info_tool_definition(base_url),
            _escalation_tool_definition(kb.escalation_transfer_number),
        ]
        try:
            client.update_llm(r.retell_llm_id, general_tools=tools)
            messages.success(request, f"[{r.slug}] All tools configured (SMS + save_caller_info + get_info + escalation → {kb.escalation_transfer_number})")
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
            "art_gallery_info", "cigar_policy", "show_charge_policy",
        )}),
        ("Facilities & Access", {"fields": (
            "has_terrace", "ac_intensity", "stroller_friendly",
            "has_valet", "valet_cost", "free_parking_info",
        )}),
        ("Agent Behavior", {"fields": (
            "affiliated_restaurants", "collect_guest_info", "guest_info_to_collect", "brand_voice_notes",
        )}),
        ("Other / Additional Info", {"fields": (
            "additional_info",
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
    list_display   = ("created_at", "restaurant", "to_number", "status", "twilio_sid")
    list_filter    = ("status", "restaurant")
    search_fields  = ("to_number", "message", "twilio_sid")
    readonly_fields = ("created_at", "twilio_sid", "error_message")


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
    ]
