import uuid

from django.contrib.auth import get_user_model
from django.db import models
from django.forms import ValidationError
from django.utils.text import slugify

# Create your models here.
class Restaurant(models.Model):
    # identity
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    user = models.OneToOneField(
        get_user_model(), null=True, blank=True,
        on_delete=models.SET_NULL, related_name="restaurant"
    )
    
    # contacts
    contact_person = models.CharField(max_length=255, blank=True, default="")
    contact_email = models.EmailField(blank=True, default="")
    contact_phone = models.CharField(max_length=32, blank=True, default="")
    
    # location 
    address_full = models.CharField(max_length=512, blank=True, default="")
    location_reference = models.TextField(blank=True, default="")
    website =  models.URLField(blank=True,default="")
    timezone = models.CharField(max_length=100, default="America/New_York")
    
    # user prefs
    primary_lang =  models.CharField(max_length=16, default="es", choices=[("es", "Spanish"), ("en", "English"), ("other", "other"),])
    conversation_tone =  models.CharField(max_length=16, default="friendly", choices=[("formal", "Formal"), ("friendly", "Friendly"), ("adaptive", "Adaptive")],)
    welcome_phrase = models.TextField(blank=True, default="")
    
    # phone strategy 
    phone_mode = models.CharField(
        max_length=16, 
        default="new", 
        choices=[("new", "New number"), ("existing", "Existing number")], 
        help_text="existing = keep public number and forward to Twilio; new = use Twilio number as public",
        )
    
    existing_ph_numb = models.CharField(max_length=32, blank=True, default="")

    # if restaurant keeps existing number - did i set up forwarding to twilio?
    forwarding_enabled = models.BooleanField(default=False)
    
    # notifications (summaries ans alerts)
    notify_via_email = models.BooleanField(default=True)
    notify_email = models.EmailField(blank=True, default="")
    
    # whatsapp notifications
    notify_via_ws = models.BooleanField(default=False)
    notify_ws_numb = models.CharField(max_length=32, blank=True, default="")
    
    notify_other = models.CharField(max_length=64, blank=True, default="")
    
    # status
    is_active = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # retell
    retell_api_key = models.CharField(max_length=128, blank=True, default="")
    retell_agent_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    retell_phone_number = models.CharField(max_length=64, blank=True, default="", db_index=True)
    retell_llm_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    retell_voice_id = models.CharField(max_length=64, blank=True, default="retell-Claudia")
    
    # Validation
    def clean(self):
        errors = {}

        if self.phone_mode == "existing" and not self.existing_ph_numb:
            errors["existing_ph_numb"] = "Required when phone_mode is 'existing'."

        if self.notify_via_ws and not self.notify_ws_numb:
            errors["notify_ws_numb"] = "Required if WhatsApp notifications are enabled."

        # If email notifications enabled, require at least one email
        if self.notify_via_email and not (self.notify_email or self.contact_email):
            errors["notify_email"] = "Provide notify_email or contact_email for email notifications."

        if errors:
            raise ValidationError(errors)


    def save(self, *args, **kwargs):
        # Generate base slug once (only if slug not manually set)
        if not self.slug:
            base = slugify(self.name)[:240] or "restaurant"  # запас под суффикс
            slug = base
            n = 2

            # Ensure uniqueness
            while Restaurant.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                suffix = f"-{n}"
                slug = f"{base[:255 - len(suffix)]}{suffix}"
                n += 1

            self.slug = slug

        super().save(*args, **kwargs)

    def __str__(self):
        return self.name
    
    
class CallEvent(models.Model):
    restaurant = models.ForeignKey("restaurants.Restaurant", on_delete=models.CASCADE, related_name="call_events")
    event_type = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class RestaurantKnowledgeBase(models.Model):
    restaurant = models.OneToOneField(
        "restaurants.Restaurant", on_delete=models.CASCADE, related_name="knowledge_base"
    )

    # ── Hours & Availability ──────────────────────────────────────────────
    hours_of_operation     = models.TextField(blank=True, default="")
    kitchen_closing_time   = models.CharField(max_length=128, blank=True, default="")
    closes_on_holidays     = models.BooleanField(default=False)
    holiday_closure_notes  = models.TextField(blank=True, default="")
    private_event_closures = models.TextField(blank=True, default="")

    # ── Menu & Food ───────────────────────────────────────────────────────
    food_menu_url     = models.URLField(blank=True, default="")
    food_menu_summary = models.TextField(
        blank=True, default="",
        help_text="~300 word curated summary: best sellers, price range, categories. Used by the AI on calls."
    )
    bar_menu_url      = models.URLField(blank=True, default="")
    bar_menu_summary  = models.TextField(
        blank=True, default="",
        help_text="Cocktail/wine/beer highlights with prices. Used by the AI on calls."
    )
    happy_hour_details = models.TextField(blank=True, default="")
    dietary_options    = models.TextField(
        blank=True, default="",
        help_text="Vegan, gluten-free, nut-free, and other dietary options available."
    )

    # ── Billing & Payments ────────────────────────────────────────────────
    auto_gratuity        = models.BooleanField(default=False)
    service_charge_pct   = models.CharField(max_length=32, blank=True, default="")
    service_charge_scope = models.CharField(
        max_length=32, blank=True, default="",
        choices=[("all", "All tables"), ("large_parties", "Large parties only"), ("", "N/A")]
    )
    max_cards_to_split   = models.PositiveSmallIntegerField(null=True, blank=True)

    # ── Reservations & Groups ─────────────────────────────────────────────
    reservation_grace_min  = models.PositiveSmallIntegerField(null=True, blank=True)
    no_show_fee            = models.CharField(max_length=128, blank=True, default="")
    large_party_min_guests = models.PositiveSmallIntegerField(null=True, blank=True)

    # ── Private Events ────────────────────────────────────────────────────
    has_private_dining       = models.BooleanField(default=False)
    private_dining_min_spend = models.CharField(max_length=128, blank=True, default="")
    allows_decorations       = models.BooleanField(default=False)
    decoration_cleaning_fee  = models.CharField(max_length=128, blank=True, default="")
    press_contact            = models.CharField(max_length=255, blank=True, default="")

    # ── Ambience & Experience ─────────────────────────────────────────────
    has_live_music        = models.BooleanField(default=False)
    live_music_details    = models.TextField(blank=True, default="")
    party_vibe_start_time = models.CharField(max_length=64, blank=True, default="")
    noise_level           = models.CharField(
        max_length=16, blank=True, default="",
        choices=[("quiet", "Quiet"), ("moderate", "Moderate"), ("loud", "Loud"), ("very_loud", "Very Loud")]
    )
    dress_code   = models.CharField(max_length=255, blank=True, default="")
    cover_charge = models.CharField(max_length=128, blank=True, default="")

    # ── Facilities & Access ───────────────────────────────────────────────
    has_terrace       = models.BooleanField(default=False)
    ac_intensity      = models.CharField(
        max_length=16, blank=True, default="",
        choices=[("mild", "Mild"), ("moderate", "Moderate"), ("strong", "Strong / Cold")]
    )
    stroller_friendly = models.BooleanField(default=False)
    has_valet         = models.BooleanField(default=False)
    valet_cost        = models.CharField(max_length=64, blank=True, default="")
    free_parking_info = models.TextField(blank=True, default="")

    # ── Agent Behavior ────────────────────────────────────────────────────
    collect_guest_info    = models.BooleanField(default=True)
    guest_info_to_collect = models.TextField(
        blank=True, default="name, party size, date, time, phone number"
    )

    def __str__(self):
        return f"KB: {self.restaurant.name}"