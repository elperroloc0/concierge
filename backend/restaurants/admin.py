from django.contrib import admin, messages
from .models import Restaurant
from .services.retell_client import RetellClient


@admin.action(description="Retell: create LLM, if missing.")
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



# Register your models here.
@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    
    list_display = (
        "name", "slug", "is_active", "phone_mode",
        "retell_agent_id", "retell_phone_number_id", 
        "contact_email", "created_at",
    )
    list_filter = ("is_active", "phone_mode", "primary_lang", "timezone")
    search_fields = (
        "name", "slug", "retell_agent_id", "retell_phone_number_id", 
        "contact_email", "contact_phone", "address_full",
    )
    readonly_fields = ("created_at", "updated_at", "retell_llm_id", "retell_agent_id", "retell_phone_number_id")
    prepopulated_fields = {"slug": ("name",)}
    
    actions = [retell_create_llm]
                    