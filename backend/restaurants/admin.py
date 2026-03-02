from django.conf import settings
from django.contrib import admin, messages

from .models import Restaurant, RestaurantKnowledgeBase
from .services.retell_client import RetellClient

LANG_MAP = {"es": "spanish", "en": "english", "other": "multilingual"}

AGENT_SYSTEM_PROMPT = """You are the AI phone assistant for {{restaurant_name}}.

LOCATION
Address: {{address_full}} — {{location_reference}}
Website: {{website}}

HOURS
Operating hours: {{hours_of_operation}}
Kitchen closes: {{kitchen_closing_time}}
Holiday closures: {{holiday_closure_notes}}

FOOD MENU
{{food_menu_summary}}
Full menu: {{food_menu_url}}

BAR / COCKTAILS
{{bar_menu_summary}}
Full bar menu: {{bar_menu_url}}

HAPPY HOUR
{{happy_hour_details}}

DIETARY OPTIONS (vegan / gluten-free / allergies)
{{dietary_options}}

BILLING & PAYMENTS
Auto-gratuity included: {{auto_gratuity}}
Service charge: {{service_charge_pct}} — applies to: {{service_charge_scope}}
Max cards to split the bill: {{max_cards_to_split}}

RESERVATIONS
Grace period: {{reservation_grace_min}} minutes
No-show fee: {{no_show_fee}}
Large party ({{large_party_min_guests}}+ guests): recommend group reservation

PRIVATE EVENTS & BUYOUTS
Private dining room available: {{has_private_dining}}
Minimum spend: {{private_dining_min_spend}}
Decorations allowed: {{allows_decorations}} | Cleaning fee: {{decoration_cleaning_fee}}
Press / influencer contact: {{press_contact}}

AMBIENCE & EXPERIENCE
Live music / DJ: {{live_music_details}} | Party vibe starts: {{party_vibe_start_time}}
Noise level: {{noise_level}} | Dress code: {{dress_code}} | Cover charge: {{cover_charge}}

FACILITIES
Terrace: {{has_terrace}} | A/C intensity: {{ac_intensity}}
Stroller-friendly: {{stroller_friendly}}
Valet parking: {{has_valet}} ({{valet_cost}}) | Free parking nearby: {{free_parking_info}}

BEHAVIOR RULES
- Open every call with: "{{welcome_phrase}}"
- Language: {{primary_lang}} | Tone: {{conversation_tone}}
- Collect from the caller: {{guest_info_to_collect}}
- If you cannot answer a question, direct the caller to {{website}} or suggest calling back.
- Do not make up information. Only share what is provided above.
- Timezone: {{timezone}}"""


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


@admin.action(description="Retell: 2b — Update Agent webhook URL (requires RETELL_WEBHOOK_URL in .env)")
def retell_update_agent_webhook(modeladmin, request, queryset):
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

        webhook_url = f"{settings.RETELL_WEBHOOK_BASE_URL}/api/retell/webhook/{r.pk}/"
        client = RetellClient(api_key=r.retell_api_key)
        client.update_agent(r.retell_agent_id, inbound_dynamic_variables_webhook_url=webhook_url)
        messages.success(request, f"[{r.slug}] Agent webhook updated → {webhook_url}")


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

        webhook_url = f"{settings.RETELL_WEBHOOK_BASE_URL}/api/retell/webhook/{r.pk}/"
        lang = LANG_MAP.get(r.primary_lang, "multilingual")

        client = RetellClient(api_key=r.retell_api_key)
        agent = client.create_agent(
            agent_name=f"{r.name} — Inbound Agent",
            voice_id=r.retell_voice_id,
            language=lang,
            response_engine={"llm_id": r.retell_llm_id, "type": "retell-llm"},
            inbound_dynamic_variables_webhook_url=webhook_url,
        )
        r.retell_agent_id = agent.agent_id
        r.save(update_fields=["retell_agent_id"])
        messages.success(request, f"[{r.slug}] Agent created: {r.retell_agent_id} | webhook → {webhook_url}")


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

        client = RetellClient(api_key=r.retell_api_key)
        phone = client.create_phone_number(area_code=786, inbound_agent_id=r.retell_agent_id)
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
            "collect_guest_info", "guest_info_to_collect",
        )}),
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
    search_fields = (
        "name", "slug", "retell_agent_id", "retell_phone_number",
        "contact_email", "contact_phone", "address_full",
    )
    readonly_fields = ("created_at", "updated_at", "retell_llm_id", "retell_agent_id", "public_id")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [KnowledgeBaseInline]
    actions = [
        retell_create_llm, retell_update_llm_prompt,
        retell_create_agent, retell_update_agent_webhook,
        retell_create_phone,
    ]
