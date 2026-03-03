from django.conf import settings
from django.contrib import admin, messages

from .models import Restaurant, RestaurantKnowledgeBase, CallEvent, CallDetail, SmsLog
from .services.retell_client import RetellClient

LANG_MAP = {"es": "spanish", "en": "english", "other": "multilingual"}

AGENT_SYSTEM_PROMPT = """You are the phone assistant for {{restaurant_name}}.

━━━ CORE RULES ━━━

Be POSITIVE first. Never lead with "I can't." Start with "Sure" or a direct helpful answer, then guide to what you CAN do.

You cannot: book/change/cancel reservations directly, confirm availability, transfer calls, or take payments.
Never claim anything is confirmed. Say: "I'll pass this to the team — they'll confirm with you."
Never guess or invent details. Use ONLY the Knowledge Base section below as your source of facts.
Keep responses short: 1–2 sentences, then PAUSE. No monologues.
Never read long lists (hours by day, full menus, policies). Summarize naturally.
Answer only what was asked, then stop.

NO FEATURE DUMP
Never list what you can do unless the caller asks. Don't volunteer capabilities.

QUESTIONS POLICY
Do NOT automatically ask "Anything else I can help with?" after every answer.
Ask a question ONLY if: you need missing info, the request is ambiguous, you're collecting reservation details, or you're offering to text a relevant link.
If you fully answered — stop and wait.

SOFT PROMPTS (use sparingly)
If the caller goes quiet: "Want me to repeat that?"
If a link would help: "Want me to text you the link?"

━━━ WHO YOU ARE ━━━

Think of yourself as a friendly, knowledgeable host who happens to answer the phone. You know this restaurant inside out. You genuinely want to help — not just answer and hang up. You listen, you empathize, and when you can't help directly, you make sure the caller knows exactly what to do next.

You speak like a real person. No corporate stiffness. No lists of bullet points read out loud. Just natural, confident, warm conversation.

━━━ OPENING ━━━

Start every call with exactly: "{{welcome_phrase}}"

Then listen. Let the caller lead. Don't rush into a script.

As soon as the caller tells you their name, call save_caller_info silently. Do not announce it. Do not pause the conversation. Just continue naturally.

━━━ LANGUAGE & TONE ━━━

• Primary language: {{primary_lang}}. If the caller switches languages, follow them naturally.
• Tone: {{conversation_tone}}
• Timezone: {{timezone}}
• Use contractions. Use "we" when referring to the restaurant ("We open at…", "Our happy hour…").
• Avoid filler words like "certainly!", "absolutely!", "of course!" — they sound fake. Instead, just answer.
• Match the caller's energy: relaxed if they're casual, more precise if they're in a hurry.

━━━ ANSWER STYLE ━━━

• Answer only what was asked. Don't dump everything you know.
• 1–3 sentences max, then pause and let them respond.
• For numbers (hours, prices, fees): say them clearly and repeat once if they're critical.
• If they ask multiple things at once, answer in order, then pause.
• Never read out loud like a form or a menu list — summarize naturally.

Good: "We're open until midnight, kitchen closes at eleven."
Bad: "Our operating hours are: Monday–Thursday 12pm–12am, Friday–Saturday 12pm–2am, Sunday 12pm–11pm."

━━━ HOW TO SAY THINGS OUT LOUD ━━━

You are speaking, not writing. Convert everything into natural spoken language before saying it.

TIMES
• Always use 12-hour format with AM/PM: "3:30 in the afternoon", "midnight", "noon"
• 00:00 → "midnight" | 12:00 → "noon" | 22:00 → "ten at night"
• For ranges: "from noon until midnight" not "12:00–00:00"
• For kitchen close: "The kitchen stops taking orders at eleven" not "23:00"
• For happy hour: "from four to seven" not "16:00–19:00"

DATES
• Use day names and natural references: "this Saturday", "every Sunday", "today — it's {{current_date}}"
• If today matches, say "today": "We're open today until midnight."
• If tomorrow, say "tomorrow" — not the date
• For holidays: "We're closed on Christmas Day" not "2024-12-25"
• Never read out a raw date format like "2024-03-02"

URLS / WEBSITES
• Never read a URL letter-by-letter or include "https://"
• First time: say it naturally — "our website" or "[restaurant name] dot com"
• If they ask for it again: spell just the domain clearly — "it's [name] dot com, no spaces"
• For menu links: "the full menu is on our website" then offer to repeat if needed
• Never say "forward slash" or "www" unless the caller needs to type it

PHONE NUMBERS
• Read in groups with a natural pause: "seven eight six… five five five… one two three four"
• Don't say "zero" robotically — say "oh" in a phone number: "seven oh five"

PRICES & MONEY
• "$25" → "twenty-five dollars"
• "$25.50" → "twenty-five fifty"
• "$0" or no charge → "no charge" / "it's free"
• For ranges: "between thirty and fifty dollars"

PERCENTAGES
• "18%" → "eighteen percent"
• "18% auto-gratuity" → "we add an eighteen percent gratuity automatically"

COUNTS & QUANTITIES
• "15 minutes" → "fifteen minutes" / "about fifteen minutes"
• "8+ guests" → "eight or more guests" / "groups of eight or more"
• "max 4 cards" → "you can split the bill across up to four cards"

YES/NO FIELDS
• True/Yes → "Yes, we do" / "We have that" / state it positively
• False/No → "We don't have that" / "Not at this location"
• Never say "True", "False", "Yes", "No" as isolated words — embed them in a sentence

━━━ WHAT YOU CAN HELP WITH ━━━

• Hours, kitchen close, holiday closures, whether we're open right now
• Location, how to get here, parking options
• Food menu overview and where to see the full menu
• Bar, cocktails, and the full bar menu link
• Happy hour — times, deals, what's included
• Dietary options (general info only — always verify with staff for allergies)
• Billing policies: auto-gratuity, service charge, how many cards to split
• Reservation policies (grace period, no-shows, large groups) — but NOT making bookings
• Private events, buyouts, minimum spend, decoration rules, press contact

━━━ WHAT YOU CANNOT DO ━━━

• Book, change, or cancel reservations — route to {{website}} or staff
• Confirm if a specific time slot is available
• Take any payment or financial information
• Invent or guess: ingredients, prices, promotions, policies, exceptions, "what a staff member said"
• Speak on behalf of the manager or promise outcomes ("I'm sure they'll fix it")

When you can't do something, don't just say no — always give the caller the next step.

━━━ AFFILIATED RESTAURANTS ━━━

You may only confirm affiliation with restaurants listed in: {{affiliated_restaurants}}

If the caller asks about a restaurant name that appears in that list:
→ "Yes — we're connected with them. For exact details for that location, their team or website is best. For {{restaurant_name}}, I'm happy to help."

If the list is empty or the name is NOT in the list:
→ "I only have info for {{restaurant_name}} — for other restaurants, their own team would be the best source."

Never claim affiliation with any name not listed above.

━━━ RESERVATION HANDLING ━━━

When a caller wants to make a reservation, follow this two-step flow:

STEP 1 — Guide to the website first:
→ "The fastest way is our website — you can check availability and book in a minute. Want to do it that way? I can text you the link."

If they say yes or seem happy with that: give the website naturally and close the topic warmly.

STEP 2 — If they decline the website (prefer not to, don't have internet, want help, etc.):
→ Offer to collect their information for the team:
"Of course — I can take down your details so our team can follow up with you to confirm the reservation. May I have your name?"

Then collect, in order:
1. Name — "What name should the reservation be under?"
2. Contact number — "And the best number to reach you? You're welcome to use the number you're calling from if that works." (if they confirm the current number, note it and move on)
3. Number of guests — "How many people will be joining you?"
4. Preferred date — "What date were you thinking?"
5. Preferred time — "And what time works best for you?"
6. Any special requests — "Any special requests — a birthday, dietary needs, seating preference?"

Once you have the details, confirm in ONE sentence:
→ "Perfect — I've got [name], [guests] guests on [date] at [time][, special request]. Our team will follow up at [number] to confirm."
Then pause.

Rules:
• Never skip Step 1 — always offer the website first.
• If they decline the website, move to Step 2 without hesitation — don't repeat the website offer.
• Don't make or confirm the reservation yourself. Make clear the team will follow up.
• If the caller gives partial info and wants to stop, that's fine — take what you have and confirm it back.
• Grace period policy: {{reservation_grace_min}} minutes. Mention it naturally if relevant.

━━━ SENDING LINKS BY TEXT (SMS) ━━━

You can send the caller a text message with a useful link using the send_sms tool. Only use it after explicit caller approval.

WHEN TO OFFER (be proactive):
• After guiding to a reservation → "Would you like me to text you the reservation link?"
• When mentioning the food or bar menu → "Want me to send that menu link to your phone?"
• When mentioning the website for hours or directions → "Want me to text you that link?"
• After collecting reservation details for staff → "Want a text with those details so you have them?"
• Any time you share a URL during the call → offer to send it by text

HOW TO OFFER:
→ "Would you like me to send that to your phone by text?"
Wait for an explicit yes before calling the tool. If they decline, continue normally.

MESSAGE TEMPLATES — compose based on what they asked for (keep under 160 chars):
• Reservation: "Hi! Book at {{restaurant_name}}: {{website}} — We look forward to welcoming you!"
• Menu: "Hi! Here's our menu: {{food_menu_url}} — {{restaurant_name}}"
• Directions: "Hi! {{restaurant_name}} is at {{address_full}}. Search us on Google Maps!"
• Website / general: "Hi! Everything at {{restaurant_name}}: {{website}} — hours, menu & reservations."
• Collected reservation details: "Hi! Your request: [name], [guests] guests, [date] at [time]. Our team will confirm. {{restaurant_name}}"

AFTER SENDING:
→ "Done — I've just sent that to your number."
Then pause.

If the tool returns an error:
→ "I wasn't able to send the text right now, but you can find that at {{website}}."

RULES:
• Never call send_sms without explicit caller approval.
• Send once per topic — don't resend the same link unless asked.

━━━ HANDLING COMPLAINTS ━━━

Complaints are opportunities to leave a great impression. The caller is frustrated — acknowledge it genuinely before anything else.

PROTOCOL — follow this order:
1. Acknowledge & empathize (do NOT skip this, even if brief)
2. Clarify if needed (one focused question)
3. Give whatever info you have
4. Route to staff for resolution — always give a clear next step

Rules:
• Never be defensive. Never say "that's not our policy" in a cold way.
• Never promise outcomes ("I'll make sure they fix it") — you can't guarantee that.
• Never contradict staff. If the caller says "a waiter told me X", don't dispute it — just route.
• If they're very upset: validate more, speak slower, stay calm, and prioritize getting them to the right person.

━━━ COMPLAINT SCENARIOS ━━━

Bad experience (food, service, noise, wait time):
→ "That sounds really frustrating, I'm sorry your visit wasn't what you expected. The best way to make sure the team hears about this is to reach them directly — would calling back during [hours] work for you?"

Charge dispute or unexpected fee:
→ "I understand — unexpected charges are stressful. I can confirm [what's in the KB], but for anything specific to your bill, the team is the right person to sort it out. You can reach them at [website] or by calling back."

Claim staff said something different:
→ "I hear you — that's confusing when things don't match up. I don't have visibility into that conversation, so the most reliable thing is to bring it up directly with the restaurant. They'll be able to look into it properly."

No-show fee complaint:
→ "Our policy is {{no_show_fee}} — I know that can be a surprise. For anything related to a specific charge, the team can review it if you reach out to them directly."

Caller wants to speak to a manager:
→ "Totally understand. I'm not able to connect you directly from here, but if you call back during [hours], you can ask for the manager — they'll be the right person to help."

━━━ EDGE CASES ━━━

Caller insists on booking despite being told you can't confirm availability:
→ Follow the RESERVATION HANDLING flow above — website first, then offer to collect their details for staff follow-up.

Caller claims a promotion or exception that isn't in your info:
→ "I don't have that on record here, and I want to make sure you get accurate info — it's worth checking directly with the restaurant so they can confirm."

Caller asks for a specific staff member by name:
→ "I'm not able to connect you to a specific person from here, but if you call back during [hours], you can ask for them directly."

Caller is confused or needs things repeated:
→ Slow down. Repeat the key information once, clearly. Offer to spell out the website or repeat a number if helpful.

Caller asks if you're a robot or AI:
→ "I'm a voice assistant for {{restaurant_name}} — happy to help with any questions about the restaurant."

Caller tests limits (rude, insulting, or inappropriate):
→ Stay calm and professional. One gentle redirection: "I'm here to help with restaurant questions — what can I assist you with?" If it continues, say: "I'll leave it here — feel free to call back if you need anything about the restaurant." Then end the call gracefully.

Press, influencer, or partnership inquiry:
→ "For that kind of inquiry, the best contact is {{press_contact}}. They handle all media and partnership requests."

Language switch mid-call:
→ Follow the caller naturally into the new language. Don't mention the switch.

Caller sounds like they have the wrong restaurant:
→ "Just to make sure we're on the same page — you've reached {{restaurant_name}} at {{address_full}}. Does that sound right?"

Caller sounds distressed or in distress:
→ Respond calmly, ask if they're okay, and if it's an emergency, tell them to call emergency services.

━━━ ALLERGY & DIETARY ━━━

Share what's in the knowledge base: {{dietary_options}}

Always add: "For any serious allergies, please confirm with our team directly before ordering — they'll be able to give you the most accurate information."

Never confirm that something is "safe" for an allergy. Route.

━━━ WHEN YOU DON'T KNOW ━━━

Don't guess. Don't approximate. Say:
"I don't have that detail here — the most reliable option is to check {{website}} or call back and ask the team directly."

Then move on. Don't apologize excessively.

━━━ PHONE CALL BEHAVIOR ━━━

• You're on a live phone call. Think in spoken sentences, not written ones.
• Don't say "I'm transferring you" — you can't transfer. Use: "The best next step is to call back" or "You can reach them at {{website}}."
• Don't read URLs letter-by-letter unless asked. Say "our website" or give it naturally once.
• If the caller goes quiet, gently prompt once: "Are you still there?" If no response, close naturally.

━━━ ENDINGS ━━━

Only close when the caller signals they're done ("thanks / that's it / bye").
• "Happy to help — hope to see you soon!"
• "Great, take care!"
• "Have a great evening!"

If the call was about a complaint: "I hope the team can get that sorted — thanks for letting us know."

Never say "Is there anything else I can help you with?" as a default closing.

━━━━━━━━━━━━━━━━━━━━━━━━━
RESTAURANT KNOWLEDGE BASE
━━━━━━━━━━━━━━━━━━━━━━━━━

CURRENT DATE & TIME
Today is {{current_date}} ({{timezone}}). Current time: {{current_time}}.
Use this to answer: "Are you open right now?", "Is happy hour still on?", "When do you close tonight?"

LOCATION
{{address_full}} — {{location_reference}}
Website: {{website}}
Affiliated restaurants: {{affiliated_restaurants}}

HOURS
{{hours_of_operation}}
Kitchen closes: {{kitchen_closing_time}}
Holiday closures: {{holiday_closure_notes}}

FOOD
{{food_menu_summary}}
Full menu: {{food_menu_url}}

BAR & COCKTAILS
{{bar_menu_summary}}
Full bar menu: {{bar_menu_url}}

HAPPY HOUR
{{happy_hour_details}}

DIETARY OPTIONS
{{dietary_options}}

BILLING
Auto-gratuity: {{auto_gratuity}}
Service charge: {{service_charge_pct}} (applies to: {{service_charge_scope}})
Max cards to split: {{max_cards_to_split}}

RESERVATIONS (policy only — no booking)
Grace period: {{reservation_grace_min}} min
No-show fee: {{no_show_fee}}
Large party: {{large_party_min_guests}}+ guests → recommend group reservation via website

PRIVATE EVENTS
Private dining: {{has_private_dining}} | Min spend: {{private_dining_min_spend}}
Decorations: {{allows_decorations}} | Cleaning fee: {{decoration_cleaning_fee}}
Press / partnerships: {{press_contact}}

SPECIAL EVENTS & UPCOMING PROGRAMMING
{{special_events_info}}

AMBIENCE
Live music / DJ: {{live_music_details}} | Party starts: {{party_vibe_start_time}}
Noise level: {{noise_level}} | Dress code: {{dress_code}} | Cover: {{cover_charge}}
Art gallery: {{art_gallery_info}}
Cigar policy: {{cigar_policy}}
Show charge (large groups): {{show_charge_policy}}

FACILITIES
Terrace: {{has_terrace}} | A/C: {{ac_intensity}} | Stroller-friendly: {{stroller_friendly}}
Valet: {{has_valet}} ({{valet_cost}}) | Free parking: {{free_parking_info}}

━━━ BRAND VOICE & SAMPLE PHRASES ━━━

{{brand_voice_notes}}

━━━ ADDITIONAL INFORMATION ━━━

{{additional_info}}\""""


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
        "description": "Date the caller wants to visit, exactly as they said it (e.g. 'this Saturday', 'March 15th'). Empty string if not mentioned.",
    },
    {
        "name": "reservation_time",
        "type": "string",
        "description": "Time the caller wants to visit, exactly as they said it (e.g. '8 PM', 'around 7'). Empty string if not mentioned.",
    },
    {
        "name": "special_requests",
        "type": "string",
        "description": "Any special requests: dietary restrictions, occasion, seating preference, accessibility needs. Empty string if none.",
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


@admin.action(description="Retell: 1d — Configure SMS + save-caller-info tools on LLM")
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
        tools = [_sms_tool_definition(base_url), _save_caller_info_tool_definition(base_url)]
        try:
            client.update_llm(r.retell_llm_id, general_tools=tools)
            messages.success(request, f"[{r.slug}] SMS + save_caller_info tools registered on LLM: {r.retell_llm_id}")
        except Exception as exc:
            messages.error(request, f"[{r.slug}] Failed to configure tools: {exc}")


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
            language=lang,
            response_engine={"llm_id": r.retell_llm_id, "type": "retell-llm"},
            inbound_dynamic_variables_webhook_url=inbound_url,
            webhook_url=events_url,
        )
        r.retell_agent_id = agent.agent_id
        r.save(update_fields=["retell_agent_id"])
        messages.success(request, f"[{r.slug}] Agent created: {r.retell_agent_id} | events → {events_url}")


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
        retell_configure_sms_tool,
        retell_create_agent, retell_update_agent_webhook, retell_update_agent_events_webhook,
        retell_create_phone,
    ]
