from django.contrib import admin, messages
from .models import Restaurant
from .services.retell_client import RetellClient


@admin.action(description="Retell: Create LLM")
def retell_create_llm(modeladmin, request, queryset):
    for r in queryset:  
        if not r.retell_api_key:
            messages.info(request, f"[{r.slug}] retell_api_key is empty")
            continue
        
        if r.retell_llm_id:
            messages.info(request, f"[{r.slug}] llm already exists: {r.retell_llm_id}")
            continue
        
        client = RetellClient(api_key=r.retell_api_key)
        llm = client.create_retell_llm()
        r.retell_llm_id = llm.llm_id
        r.save(update_fields=["retell_llm_id"])
        messages.success(request, f"[{r.slug}] OK: llm_id={r.retell_llm_id}")


@admin.action(description="Retell: Create Agent (requires llm_id)")
def retell_create_agent(modeladmin, request, queryset):
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue
        if not r.retell_llm_id:
            messages.error(request, f"[{r.slug}] LLM ID missing. Run 'Retell: Create LLM' first.")
            continue
        
        client = RetellClient(api_key=r.retell_api_key)
        agent = client.create_agent(
            agent_name=f"{r.name} - Inbound Agent",
            voice_id=r.retell_voice_id, 
            response_engine={"llm_id": r.retell_llm_id, "type": "retell-llm"},
        )
        r.retell_agent_id = agent.agent_id
        r.save(update_fields=["retell_agent_id"])
        messages.success(request, f"[{r.slug}] OK: agent id = {r.retell_agent_id}")
        
        
@admin.action(description="Retell: Create phone number")
def retell_create_phone(modeladmin, request, queryset):
    for r in queryset:
        if not r.retell_api_key:
            messages.error(request, f"[{r.slug}] API key is empty.")
            continue 
        if not r.retell_agent_id:
            messages.error(request, f"[{r.slug}] agent id missing. Run 'Retell: Create Agent' first.")
            continue
        if r.retell_phone_number:
            messages.error(request, f"[{r.slug}] already has a phone number: {r.retell_phone_number}.")
            continue 
        
        client = RetellClient(api_key=r.retell_api_key)
        phone = client.create_phone_number(area_code=786, inbound_agent_id=r.retell_agent_id)
        r.retell_phone_number = phone.phone_number
        r.save(update_fields=["retell_phone_number"])
        messages.success(request, f"[{r.slug}] OK: Phone {r.retell_phone_numer} successfully created.")     


@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    
    list_display = (
        "name", "slug", "is_active",
        "retell_agent_id", "retell_phone_number", 
        "contact_email", "created_at",
    )
    list_filter = ("is_active", "phone_mode", "primary_lang", "timezone")
    search_fields = (
        "name", "slug", "retell_agent_id", "retell_phone_number", 
        "contact_email", "contact_phone", "address_full",
    )
    readonly_fields = ("created_at", "updated_at", "retell_llm_id", "retell_agent_id")
    prepopulated_fields = {"slug": ("name",)}
    
    actions = [retell_create_llm, retell_create_agent, retell_create_phone]
                    