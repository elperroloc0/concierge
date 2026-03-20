import uuid

from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import JSONField
from django.forms import ValidationError
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

User = get_user_model()

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
    website = models.URLField(blank=True, default="")
    timezone = models.CharField(max_length=100, default="America/New_York")

    # user prefs
    primary_lang = models.CharField(max_length=16, default="es", choices=[("es", "Spanish"), ("en", "English"), ("other", "other"),])
    conversation_tone = models.CharField(max_length=16, default="friendly", choices=[("formal", "Formal"), ("friendly", "Friendly"), ("adaptive", "Adaptive")],)
    welcome_phrase = models.TextField(blank=True, default="")
    phone_mode = models.CharField(
        max_length=16,
        default="new",
        choices=[("new", "New number"), ("existing", "Existing number")],
        help_text="existing = keep public number and forward to Twilio; new = use Twilio number as public",
        )

    existing_ph_numb = models.CharField(max_length=32, blank=True, default="")

    # if restaurant keeps existing number - did i set up forwarding to twilio?
    forwarding_enabled = models.BooleanField(default=False)

    # notifications (summaries and alerts)
    notify_via_email = models.BooleanField(default=True)     # master switch
    notify_email = models.EmailField(blank=True, default="")

    # per-event notification preferences
    notify_on_reservation = models.BooleanField(
        default=True,
        help_text="Email the owner when a caller expresses reservation intent."
    )
    notify_on_complaint = models.BooleanField(
        default=True,
        help_text="Urgent email when the AI detects a complaint."
    )
    notify_on_followup = models.BooleanField(
        default=True,
        help_text="Email when the AI flags a call as needing a callback."
    )
    notify_on_non_customer = models.BooleanField(
        default=True,
        help_text="Email when the AI identifies a non-customer business call (vendor, press, sales, etc.)."
    )
    notify_daily_digest = models.BooleanField(
        default=True,
        help_text="Morning digest email with previous day's call summary."
    )

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
    retell_area_code = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="Area code for purchasing a Retell phone number (e.g. 786 for Miami)."
    )

    # twilio (per-restaurant — each restaurant is billed separately)
    twilio_account_sid = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Twilio Account SID for this restaurant. Leave blank to use the platform default."
    )
    twilio_auth_token = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Twilio Auth Token for this restaurant."
    )
    twilio_from_number = models.CharField(
        max_length=32, blank=True, default="",
        help_text="Twilio phone number to send SMS from (E.164 format, e.g. +17865550000)."
    )
    enable_sms = models.BooleanField(
        default=False,
        help_text="Enable Twilio SMS sending capabilities and the Retell AI SMS tool."
    )

    # Validation
    def clean(self):
        errors = {}

        if self.phone_mode == "existing" and not self.existing_ph_numb:
            errors["existing_ph_numb"] = "Required when phone_mode is 'existing'."

        if self.notify_via_ws and not self.notify_ws_numb:
            errors["notify_ws_numb"] = "Required if WhatsApp notifications are enabled."

        if self.notify_via_email and not self.notify_email and not self.contact_email:
            errors["notify_email"] = "Required if Email notifications are enabled."

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


class Subscription(models.Model):
    STATUS_CHOICES = [
        ("active",    "Active"),
        ("trialing",  "Trialing"),
        ("past_due",  "Past Due"),
        ("cancelled", "Cancelled"),
        ("inactive",  "Inactive"),
    ]

    restaurant             = models.OneToOneField(
        "restaurants.Restaurant", on_delete=models.CASCADE, related_name="subscription"
    )
    stripe_customer_id     = models.CharField(max_length=64, blank=True, default="")
    stripe_subscription_id = models.CharField(max_length=64, blank=True, default="")
    status                 = models.CharField(max_length=16, default="inactive", choices=STATUS_CHOICES, db_index=True)
    current_period_end     = models.DateTimeField(null=True, blank=True)

    # Usage-based billing: communication expenses (synced from Retell)
    communication_balance  = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    communication_markup   = models.DecimalField(
        max_digits=4, decimal_places=2, default=1.30,
        help_text="Multiplier applied to Retell's combined_cost before deducting from balance. "
                  "e.g. 1.30 = 30% markup. Set to 1.00 for no markup."
    )

    created_at             = models.DateTimeField(auto_now_add=True)
    updated_at             = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Sub[{self.restaurant.name}]: {self.status}"

    @property
    def is_active(self):
        return self.status in ("active", "trialing")


class CallEvent(models.Model):
    restaurant = models.ForeignKey("restaurants.Restaurant", on_delete=models.CASCADE, related_name="call_events")
    event_type = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class CallDetail(models.Model):
    CALL_REASON_CHOICES = [
        ("reservation",   "Reservation"),
        ("hours",         "Hours / Schedule"),
        ("menu",          "Menu / Food"),
        ("bar_menu",      "Bar Menu / Drinks"),
        ("happy_hour",    "Happy Hour"),
        ("billing",       "Billing / Payment"),
        ("parking",       "Parking / Valet"),
        ("private_event", "Private Event"),
        ("complaint",     "Complaint"),
        ("non_customer",  "Non-Customer / Business Call"),
        ("other",         "Other / General"),
    ]

    call_event = models.OneToOneField(
        "restaurants.CallEvent", on_delete=models.CASCADE, related_name="detail"
    )

    # Caller identity
    caller_name  = models.CharField(max_length=255, blank=True, default="")
    caller_phone = models.CharField(max_length=32,  blank=True, default="")
    caller_email = models.EmailField(max_length=255, blank=True, default="")

    # Intent
    call_reason       = models.CharField(max_length=32, blank=True, default="other", choices=CALL_REASON_CHOICES)
    wants_reservation = models.BooleanField(null=True, blank=True)

    # Reservation details
    party_size       = models.PositiveSmallIntegerField(null=True, blank=True)
    reservation_date = models.DateField(null=True, blank=True)
    reservation_time = models.TimeField(null=True, blank=True)
    special_requests = models.TextField(blank=True, default="")

    # Follow-up
    follow_up_needed = models.BooleanField(default=True)
    notes            = models.TextField(blank=True, default="")

    # Reservation confirmation (manually set by staff)
    RESERVATION_STATUS_CHOICES = [
        ("pending",   "Pending"),
        ("confirmed", "Confirmed"),
        ("lost",      "Lost"),
    ]
    reservation_status = models.CharField(
        max_length=16, choices=RESERVATION_STATUS_CHOICES, default="pending",
        help_text="Staff-confirmed outcome for reservation leads."
    )
    reservation_confirmed_at = models.DateTimeField(null=True, blank=True)

    # Cost tracking
    call_cost = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text="Combined cost of this call in USD (voice + LLM)."
    )

    # Audio recording URL from Retell AI
    recording_url = models.URLField(max_length=500, blank=True, default="")

    # AI-generated call summary from Retell (populated from call_ended event)
    call_summary = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Detail[{self.caller_name or self.caller_phone or 'Unknown'}]"


class SmsLog(models.Model):
    STATUS_PENDING     = "pending"
    STATUS_SENT        = "sent"
    STATUS_DELIVERED   = "delivered"
    STATUS_FAILED      = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING,   "Pending"),
        (STATUS_SENT,      "Sent (in transit)"),
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_FAILED,    "Failed"),
    ]

    restaurant    = models.ForeignKey(
        "restaurants.Restaurant", on_delete=models.CASCADE, related_name="sms_logs"
    )
    call_event    = models.ForeignKey(
        "restaurants.CallEvent", on_delete=models.SET_NULL, null=True, blank=True, related_name="sms_logs"
    )
    to_number     = models.CharField(max_length=32)
    message       = models.TextField()
    status        = models.CharField(max_length=16, default=STATUS_PENDING, choices=STATUS_CHOICES)
    twilio_sid    = models.CharField(max_length=64, blank=True, db_index=True)
    error_message = models.TextField(blank=True, default="")
    delivered_at  = models.DateTimeField(null=True, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"SMS→{self.to_number} [{self.status}]"


class RestaurantKnowledgeBase(models.Model):
    restaurant = models.OneToOneField(
        "restaurants.Restaurant", on_delete=models.CASCADE, related_name="knowledge_base"
    )

    # ── Spoken pronunciation overrides ────────────────────────────────────
    # When set, these override the auto-generated spoken forms in the agent prompt.
    contact_email_spoken = models.CharField(
        max_length=255, blank=True, default="",
        help_text=(
            "How the agent should say the email address aloud. "
            "Leave blank to auto-generate. "
            "Example (ES): 'admon calle dragones arroba gmail punto com' "
            "Example (EN): 'admon calle dragones at gmail dot com'"
        )
    )

    # ── Hours & Availability ──────────────────────────────────────────────
    hours_of_operation     = models.TextField(blank=True, default="")
    kitchen_closing_time   = models.CharField(max_length=128, blank=True, default="")
    closes_on_holidays     = models.BooleanField(default=False)
    holiday_closure_notes  = models.TextField(blank=True, default="")
    private_event_closures = models.TextField(
        blank=True, default="",
        help_text=(
            "Dates when the restaurant is fully or partially closed due to private events or buyouts. "
            "Use format: Month D, YYYY — description. One entry per line. "
            "Example: March 15, 2026: closed for private event."
        )
    )

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

    # ── Structured menu fields (columns added by migration 0022) ──────────
    menu_cuisine_type    = models.TextField(blank=True, default="",
        help_text="Cuisine type & concept. e.g. 'Latin-Asian fusion, tapas-style sharing plates'.")
    menu_best_sellers    = models.TextField(blank=True, default="",
        help_text="Signature dishes with prices. One item per line.")
    menu_price_range     = models.CharField(max_length=100, blank=True, default="",
        help_text="Typical price range. e.g. '$15–$35 per dish'.")
    menu_categories      = models.CharField(max_length=255, blank=True, default="",
        help_text="Menu sections. e.g. 'Starters, Dumplings, Wok & fried rice, Mains, Sides'.")
    bar_concept          = models.TextField(blank=True, default="",
        help_text="Bar specialty & concept. e.g. 'Craft cocktails, extensive rum & mezcal selection'.")
    bar_signature_drinks = models.TextField(blank=True, default="",
        help_text="Signature cocktails with prices. One item per line.")
    bar_wine_beer        = models.TextField(blank=True, default="",
        help_text="Wine & beer selection highlights.")

    happy_hour_details = models.TextField(blank=True, default="")
    dietary_options    = models.TextField(
        blank=True, default="",
        help_text="Vegan, gluten-free, nut-free, and other dietary options available."
    )

    # ── ROI / Business Metrics ────────────────────────────────────────────
    avg_revenue_per_cover = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Average revenue per guest per visit (e.g. 45.00). Used to estimate ROI on the dashboard."
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
    special_events_info      = models.TextField(
        blank=True, default="",
        help_text=(
            "Upcoming or recurring special events at the restaurant "
            "(e.g. themed nights, holiday dinners, live shows, pop-ups). "
            "The AI agent will use this to inform callers about what's coming up."
        )
    )

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
    affiliated_restaurants = models.TextField(
        blank=True, default="",
        help_text=(
            "Comma-separated restaurant names the agent may confirm affiliation with. "
            "Example: Cuba Ocho, Calle Dragones Colombia. "
            "Leave blank to disable affiliation confirmation."
        )
    )
    collect_guest_info    = models.BooleanField(default=True)
    guest_info_to_collect = models.TextField(
        blank=True, default="name, party size, date, time, phone number"
    )
    brand_voice_notes = models.TextField(
        blank=True, default="",
        help_text=(
            "Restaurant-specific style, sample phrases, and language notes for the AI agent. "
            "Include example responses in the restaurant's preferred language, key brand phrases, "
            "and any custom routing instructions unique to this restaurant."
        )
    )

    # ── Other / Free-form ─────────────────────────────────────────────────
    owner_notes = models.TextField(
        blank=True, default="",
        help_text=(
            "Free-form notes the agent should know: gift cards, Wi-Fi password, "
            "corkage fee, birthday policy, capacity, etc."
        )
    )
    additional_info = models.TextField(
        blank=True, default="",
        help_text=(
            "Any additional information the AI agent should know that isn't covered by the fields above. "
            "Use this for special policies, FAQs, promotions, or anything unique to your restaurant."
        )
    )

    # ── Non-Customer Call Handling ─────────────────────────────────────────
    # Choices
    _NC_PARTNER  = [("ignore","Ignore (handle normally)"), ("message","Take a message"), ("transfer","Transfer to staff")]
    _NC_VENDOR   = [("ignore","Ignore (handle normally)"), ("decline","Decline politely"), ("message","Take a message"), ("transfer","Transfer to staff")]
    _NC_SALES    = [("ignore","Ignore (handle normally)"), ("decline","Decline politely"), ("message","Take a message")]
    _NC_PRESS    = [("ignore","Ignore (handle normally)"), ("give_contact","Give press contact"), ("message","Take a message"), ("transfer","Transfer to staff")]
    _NC_SERVICE  = [("ignore","Ignore (handle normally)"), ("decline","Decline politely"), ("message","Take a message"), ("transfer","Transfer to staff")]
    _NC_FINANCIAL= [("ignore","Ignore (handle normally)"), ("message","Take a message"), ("transfer","Transfer to staff")]
    _NC_SPAM     = [("decline","Decline politely"), ("end_call","End call immediately")]
    _NC_URGENT   = [("message_urgent","Take urgent message"), ("transfer","Transfer to staff")]

    # Partner companies (known business relationships)
    partner_companies          = models.TextField(blank=True, default="",
        help_text="Companies with an active relationship (e.g. DoorDash, OpenTable, Toast). One per line. The agent will recognize callers from these companies.")
    partner_call_handling      = models.CharField(max_length=16, default="message",    choices=_NC_PARTNER,   help_text="How to handle calls from listed partner companies.")
    partner_call_ask_urgency   = models.BooleanField(default=True,  help_text="Before acting, ask the caller if the matter is urgent.")

    # Vendors / Suppliers (unknown or unlisted)
    vendor_call_handling       = models.CharField(max_length=16, default="message",    choices=_NC_VENDOR,    help_text="How to handle calls from vendors or suppliers not on the partner list.")
    vendor_call_ask_urgency    = models.BooleanField(default=True,  help_text="Before acting, ask the caller if the matter is urgent.")

    # Press / Media / Influencers
    press_call_handling        = models.CharField(max_length=16, default="give_contact", choices=_NC_PRESS,   help_text="How to handle calls from press, media, or influencers.")
    press_call_ask_urgency     = models.BooleanField(default=False, help_text="Before acting, ask the caller if the matter is urgent.")

    # External services (plumbers, cleaners, pest control, etc.)
    service_call_handling      = models.CharField(max_length=16, default="message",    choices=_NC_SERVICE,   help_text="How to handle calls from external service providers.")
    service_call_ask_urgency   = models.BooleanField(default=True,  help_text="Before acting, ask the caller if the matter is urgent.")

    # Sales & Marketing (unsolicited)
    sales_call_handling        = models.CharField(max_length=16, default="decline",    choices=_NC_SALES,     help_text="How to handle unsolicited sales or marketing calls.")

    # Financial / Legal / Collections
    financial_call_handling    = models.CharField(max_length=16, default="message",    choices=_NC_FINANCIAL, help_text="How to handle calls from banks, collection agencies, or legal firms.")

    # Robocalls / Spam
    spam_call_handling         = models.CharField(max_length=16, default="end_call",   choices=_NC_SPAM,      help_text="How to handle automated or spam calls.")

    # Global urgency outcome
    urgent_call_action         = models.CharField(max_length=16, default="message_urgent", choices=_NC_URGENT,
        help_text="What to do when a non-customer caller confirms their matter is urgent.")

    # ── Human Escalation ──────────────────────────────────────────────────
    escalation_enabled = models.BooleanField(
        default=False,
        help_text="Allow the agent to transfer the call to a human when escalation conditions are met."
    )
    escalation_conditions = models.TextField(
        blank=True,
        default="If the caller is physically at or outside the restaurant and it is currently outside working hours.",
        help_text="Describe when the agent should escalate. The agent monitors for these conditions on every call."
    )
    escalation_transfer_number = models.CharField(
        max_length=30, blank=True, default="",
        help_text="Phone number to transfer to (E.164 format, e.g. +17865551234). Leave blank to take a message instead of transferring."
    )

    def __str__(self):
        return f"KB: {self.restaurant.name}"


class PendingEmailChange(models.Model):
    """Stores a requested email change until the user clicks the confirmation link."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="pending_email_changes")
    new_email = models.EmailField()
    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def save(self, *args, **kwargs):
        if not self.pk:
            # Token expires in 24 hours from creation
            self.expires_at = timezone.now() + timezone.timedelta(hours=24)
        super().save(*args, **kwargs)

    def is_valid(self):
        return timezone.now() < self.expires_at

    def __str__(self):
        return f"Pending change for {self.user.email} -> {self.new_email}"


class CallerMemory(models.Model):
    """
    Persistent per-caller profile, keyed by (restaurant, phone).
    Built automatically from call_ended events; editable by staff.
    """
    restaurant = models.ForeignKey(
        "restaurants.Restaurant",
        on_delete=models.CASCADE,
        related_name="caller_memories",
    )
    # Caller identifier — E.164 format, e.g. "+13055551234"
    phone = models.CharField(max_length=32, db_index=True)

    # Identity — updated with each call, last confirmed value wins
    name  = models.CharField(max_length=255, blank=True, default="")
    email = models.EmailField(max_length=255, blank=True, default="")

    # Call history
    call_count    = models.PositiveIntegerField(default=0)
    last_call_at  = models.DateTimeField(null=True, blank=True)
    # AI-generated summary of the most recent call (from Retell)
    last_call_summary = models.TextField(blank=True, default="")

    # Staff-editable annotations
    preferences = models.TextField(
        blank=True, default="",
        help_text="e.g. 'prefers terrace seating, gluten-free'"
    )
    staff_notes = models.TextField(
        blank=True, default="",
        help_text="e.g. 'VIP', 'do not call before noon'"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["restaurant", "phone"]]
        ordering = ["-last_call_at"]

    def __str__(self):
        label = self.name or self.phone
        return f"CallerMemory[{self.restaurant.slug} | {label}]"
