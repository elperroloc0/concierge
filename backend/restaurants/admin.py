from django.contrib import admin
from .models import Restaurant

# Register your models here.
@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    exclude = ("retell_api_key",)
    
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
    readonly_fields = ("created_at", "updated_at")
    prepopulated_fields = {"slug": ("name",)}
                    