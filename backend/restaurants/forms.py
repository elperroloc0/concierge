import re
import urllib.parse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import ValidationError

from .models import Restaurant, RestaurantKnowledgeBase

User = get_user_model()

_TEXT  = {"class": "form-control"}
_SEL   = {"class": "form-select"}
_CHECK = {"class": "form-check-input"}


def _ta(rows=3):
    return {"class": "form-control", "rows": rows}


def _normalize_url(value):
    """Prepend https:// if no scheme is present, then validate the result."""
    value = value.strip()
    if not value:
        return value
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if not parsed.netloc:
        raise forms.ValidationError("Enter a valid URL (e.g. https://example.com).")
    return value


class RestaurantBasicForm(forms.ModelForm):
    # Override as CharField so Django's URLField validator doesn't block input
    # without a scheme. Normalization and validation happen in clean_website().
    website = forms.CharField(required=False, widget=forms.TextInput(attrs=_TEXT))

    class Meta:
        model = Restaurant
        fields = [
            "name", "address_full", "location_reference", "website",
            "timezone", "primary_lang", "conversation_tone", "welcome_phrase",
            "contact_phone", "contact_email",
        ]
        widgets = {
            "name":               forms.TextInput(attrs=_TEXT),
            "address_full":       forms.TextInput(attrs=_TEXT),
            "location_reference": forms.Textarea(attrs=_ta(2)),
            "timezone":           forms.TextInput(attrs=_TEXT),
            "primary_lang":       forms.Select(attrs=_SEL),
            "conversation_tone":  forms.Select(attrs=_SEL),
            "welcome_phrase":     forms.Textarea(attrs=_ta(2)),
            "contact_phone":      forms.TextInput(attrs=_TEXT),
            "contact_email":      forms.EmailInput(attrs=_TEXT),
        }

    def clean_website(self):
        return _normalize_url(self.cleaned_data.get("website", ""))

    def clean_timezone(self):
        value = self.cleaned_data.get("timezone", "").strip()
        if value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, KeyError):
                raise forms.ValidationError(
                    "Invalid timezone. Use a valid IANA name (e.g. America/New_York)."
                )
        return value


class KnowledgeBaseForm(forms.ModelForm):
    # Override URL fields as CharField so normalization runs before validation.
    food_menu_url = forms.CharField(required=False, widget=forms.TextInput(attrs=_TEXT))
    bar_menu_url  = forms.CharField(required=False, widget=forms.TextInput(attrs=_TEXT))

    class Meta:
        model = RestaurantKnowledgeBase
        # Explicit allowlist — brand_voice_notes, collect_guest_info, guest_info_to_collect
        # are developer-only and intentionally excluded from owner portal access.
        fields = [
            # Basic spoken overrides
            "contact_email_spoken",
            # Hours
            "hours_of_operation", "kitchen_closing_time",
            "closes_on_holidays", "holiday_closure_notes", "private_event_closures",
            # Menu
            "food_menu_url", "food_menu_summary",
            "menu_cuisine_type", "menu_best_sellers", "menu_price_range", "menu_categories",
            "bar_menu_url", "bar_menu_summary",
            "bar_concept", "bar_signature_drinks", "bar_wine_beer", "bottle_service",
            "happy_hour_details", "dietary_options",
            # Billing
            "auto_gratuity", "service_charge_pct", "service_charge_scope", "max_cards_to_split", "corkage_policy",
            # Reservations
            "reservation_grace_min", "no_show_fee", "large_party_min_guests",
            # Private events
            "has_private_dining", "private_dining_min_spend",
            "allows_decorations", "decoration_cleaning_fee",
            "press_contact", "special_events_info",
            # Ambience
            "has_live_music", "live_music_details", "party_vibe_start_time",
            "noise_level", "dress_code", "cover_charge",
            # Facilities
            "has_terrace", "ac_intensity", "stroller_friendly",
            "has_valet", "valet_cost", "free_parking_info",
            # Agent — non-customer call handling
            "partner_companies", "partner_call_handling", "partner_call_ask_urgency",
            "vendor_call_handling", "vendor_call_ask_urgency",
            "press_call_handling", "press_call_ask_urgency",
            "service_call_handling", "service_call_ask_urgency",
            "sales_call_handling",
            "financial_call_handling",
            "spam_call_handling",
            "urgent_call_action",
            # Agent (owner-facing: team + call transfer + sister locations)
            "team_members",
            "escalation_enabled", "escalation_conditions", "escalation_transfer_number",
            "affiliated_restaurants",
            # ROI
            "avg_revenue_per_cover",
            # Custom info
            "owner_notes",
        ]
        widgets = {
            # Spoken overrides
            "contact_email_spoken": forms.TextInput(attrs=_TEXT),
            # Hours
            "hours_of_operation":     forms.Textarea(attrs=_ta(4)),
            "kitchen_closing_time":   forms.TextInput(attrs=_TEXT),
            "closes_on_holidays":     forms.CheckboxInput(attrs=_CHECK),
            "holiday_closure_notes":  forms.Textarea(attrs=_ta(2)),
            "private_event_closures": forms.Textarea(attrs=_ta(3)),
            # Menu
            "food_menu_summary":      forms.Textarea(attrs=_ta(6)),
            "bar_menu_summary":       forms.Textarea(attrs=_ta(6)),
            "bottle_service":         forms.Textarea(attrs=_ta(3)),
            "happy_hour_details":     forms.Textarea(attrs=_ta(4)),
            "dietary_options":        forms.Textarea(attrs=_ta(4)),
            # Billing
            "auto_gratuity":          forms.CheckboxInput(attrs=_CHECK),
            "service_charge_pct":     forms.TextInput(attrs=_TEXT),
            "service_charge_scope":   forms.Select(attrs=_SEL),
            "max_cards_to_split":     forms.NumberInput(attrs=_TEXT),
            "corkage_policy":         forms.Textarea(attrs=_ta(2)),
            # Reservations
            "reservation_grace_min":  forms.NumberInput(attrs=_TEXT),
            "no_show_fee":            forms.TextInput(attrs=_TEXT),
            "large_party_min_guests": forms.NumberInput(attrs=_TEXT),
            # Private events
            "has_private_dining":       forms.CheckboxInput(attrs=_CHECK),
            "private_dining_min_spend": forms.TextInput(attrs=_TEXT),
            "allows_decorations":       forms.CheckboxInput(attrs=_CHECK),
            "decoration_cleaning_fee":  forms.TextInput(attrs=_TEXT),
            "press_contact":            forms.TextInput(attrs=_TEXT),
            "special_events_info":      forms.Textarea(attrs=_ta(5)),
            # Ambience
            "has_live_music":         forms.CheckboxInput(attrs=_CHECK),
            "live_music_details":     forms.Textarea(attrs=_ta(3)),
            "party_vibe_start_time":  forms.TextInput(attrs=_TEXT),
            "noise_level":            forms.Select(attrs=_SEL),
            "dress_code":             forms.TextInput(attrs=_TEXT),
            "cover_charge":           forms.TextInput(attrs=_TEXT),
            # Facilities
            "has_terrace":            forms.CheckboxInput(attrs=_CHECK),
            "ac_intensity":           forms.Select(attrs=_SEL),
            "stroller_friendly":      forms.CheckboxInput(attrs=_CHECK),
            "has_valet":              forms.CheckboxInput(attrs=_CHECK),
            "valet_cost":             forms.TextInput(attrs=_TEXT),
            "free_parking_info":      forms.Textarea(attrs=_ta(2)),
            # Agent — non-customer call handling
            "partner_companies":        forms.Textarea(attrs=_ta(3)),
            "partner_call_handling":    forms.Select(attrs=_SEL),
            "partner_call_ask_urgency": forms.CheckboxInput(attrs=_CHECK),
            "vendor_call_handling":     forms.Select(attrs=_SEL),
            "vendor_call_ask_urgency":  forms.CheckboxInput(attrs=_CHECK),
            "press_call_handling":      forms.Select(attrs=_SEL),
            "press_call_ask_urgency":   forms.CheckboxInput(attrs=_CHECK),
            "service_call_handling":    forms.Select(attrs=_SEL),
            "service_call_ask_urgency": forms.CheckboxInput(attrs=_CHECK),
            "sales_call_handling":      forms.Select(attrs=_SEL),
            "financial_call_handling":  forms.Select(attrs=_SEL),
            "spam_call_handling":       forms.Select(attrs=_SEL),
            "urgent_call_action":       forms.Select(attrs=_SEL),
            # Agent — team + call transfer + sister locations
            "team_members":               forms.Textarea(attrs=_ta(3)),
            "affiliated_restaurants":     forms.Textarea(attrs=_ta(3)),
            "escalation_enabled":         forms.CheckboxInput(attrs=_CHECK),
            "escalation_conditions":      forms.Textarea(attrs=_ta(3)),
            "escalation_transfer_number": forms.TextInput(attrs=_TEXT),
            # ROI
            "avg_revenue_per_cover": forms.NumberInput(attrs={**_TEXT, "step": "0.01", "min": "0"}),
            # Custom info
            "owner_notes": forms.Textarea(attrs=_ta(6)),
        }

    def clean_food_menu_url(self):
        return _normalize_url(self.cleaned_data.get("food_menu_url", ""))

    def clean_bar_menu_url(self):
        return _normalize_url(self.cleaned_data.get("bar_menu_url", ""))

    def clean_escalation_transfer_number(self):
        number = self.cleaned_data.get("escalation_transfer_number", "").strip()
        if number and not re.fullmatch(r"\+[1-9]\d{7,14}", number):
            raise forms.ValidationError(
                "Enter a valid E.164 phone number (e.g. +17865551234). "
                "Must start with + followed by 8–15 digits."
            )
        return number


class AccountEmailForm(forms.Form):
    current_password = forms.CharField(
        label="Current Password",
        widget=forms.PasswordInput(attrs=_TEXT),
        required=True
    )
    new_email = forms.EmailField(
        label="New Email",
        widget=forms.EmailInput(attrs=_TEXT),
        required=True
    )

    def __init__(self, *args, user=None, restaurant=None, **kwargs):
        self.user = user
        self.restaurant = restaurant
        super().__init__(*args, **kwargs)

    def clean_current_password(self):
        current_password = self.cleaned_data.get("current_password")
        if not self.user.check_password(current_password):
            raise forms.ValidationError("Incorrect current password.")
        return current_password

    def clean_new_email(self):
        new_email = self.cleaned_data.get("new_email", "").strip().lower()
        # Check against restaurant contact email or user email/username
        if self.restaurant and self.restaurant.contact_email:
            current_email = self.restaurant.contact_email.lower()
        else:
            current_email = (self.user.email or self.user.username).lower()

        if new_email == current_email:
            raise forms.ValidationError("This is already your current email address.")
        if User.objects.filter(email__iexact=new_email).exclude(pk=self.user.pk).exists():
            raise forms.ValidationError("This email is already in use by another account.")
        if User.objects.filter(username__iexact=new_email).exclude(pk=self.user.pk).exists():
            raise forms.ValidationError("This email is already in use by another account.")
        return new_email


class PasswordUpdateForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update(_TEXT)

