from django.db import models
from django.forms import ValidationError
from django.utils.text import slugify

# Create your models here.
class Restaurant(models.Model):
    # identity
    name = models.CharField(max_length=255) 
    slug = models.SlugField(unique=True)
    
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
    # twilio number that receives calls
    twilio_pn_numb = models.CharField(max_length=32, blank=True, default="")
    twilio_in_numb_sid = models.CharField(max_length=64, blank=True, default="", db_index=True)
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
    
    
    # Validation
    def clean(self):
        errors = {}

        if self.phone_mode == "existing" and not self.existing_phone_number:
            errors["existing_phone_number"] = "Required when phone_mode is 'existing'."

        if self.notify_via_whatsapp and not self.notify_whatsapp_number:
            errors["notify_whatsapp_number"] = "Required if WhatsApp notifications are enabled."

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