import re

from django import forms

from .models import Restaurant, RestaurantKnowledgeBase

_TEXT  = {"class": "form-control"}
_SEL   = {"class": "form-select"}
_CHECK = {"class": "form-check-input"}


def _ta(rows=3):
    return {"class": "form-control", "rows": rows}


class RestaurantBasicForm(forms.ModelForm):
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
            "website":            forms.URLInput(attrs=_TEXT),
            "timezone":           forms.TextInput(attrs=_TEXT),
            "primary_lang":       forms.Select(attrs=_SEL),
            "conversation_tone":  forms.Select(attrs=_SEL),
            "welcome_phrase":     forms.Textarea(attrs={**_ta(2), "placeholder": "Thank you for calling [Restaurant Name], how can I help you today?"}),
            "contact_phone":      forms.TextInput(attrs=_TEXT),
            "contact_email":      forms.EmailInput(attrs=_TEXT),
        }


class KnowledgeBaseForm(forms.ModelForm):
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
            "bar_concept", "bar_signature_drinks", "bar_wine_beer",
            "happy_hour_details", "dietary_options",
            # Billing
            "auto_gratuity", "service_charge_pct", "service_charge_scope", "max_cards_to_split",
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
            # Agent (owner-facing: call transfer + sister locations only)
            "escalation_enabled", "escalation_conditions", "escalation_transfer_number",
            "affiliated_restaurants",
        ]
        widgets = {
            # Spoken overrides
            "contact_email_spoken": forms.TextInput(attrs={**_TEXT, "placeholder": "admon calle dragones arroba gmail punto com"}),
            # Hours
            "hours_of_operation":     forms.Textarea(attrs={**_ta(4), "placeholder": "Mon–Thu 12pm–midnight, Fri–Sat 12pm–2am, Sun 12pm–11pm"}),
            "kitchen_closing_time":   forms.TextInput(attrs=_TEXT),
            "closes_on_holidays":     forms.CheckboxInput(attrs=_CHECK),
            "holiday_closure_notes":  forms.Textarea(attrs=_ta(2)),
            "private_event_closures": forms.Textarea(attrs={
                **_ta(3),
                "placeholder": "March 15, 2026: closed for private event.\nApril 1, 2026: private buyout — no public dining.",
            }),
            # Menu
            "food_menu_url":          forms.URLInput(attrs={**_TEXT, "placeholder": "https://yourwebsite.com/menu"}),
            "food_menu_summary":      forms.Textarea(attrs={**_ta(6), "placeholder": "We specialize in Latin-Asian fusion. Best sellers: ceviche tostada ($18), short rib tacos ($24). Most dishes between $15–$35."}),
            "bar_menu_url":           forms.URLInput(attrs={**_TEXT, "placeholder": "https://yourwebsite.com/drinks"}),
            "bar_menu_summary":       forms.Textarea(attrs=_ta(6)),
            "happy_hour_details":     forms.Textarea(attrs={**_ta(4), "placeholder": "Mon–Fri 4–7pm. 50% off all cocktails and select beers. Available at bar and lounge seating only."}),
            "dietary_options":        forms.Textarea(attrs=_ta(4)),
            # Billing
            "auto_gratuity":          forms.CheckboxInput(attrs=_CHECK),
            "service_charge_pct":     forms.TextInput(attrs=_TEXT),
            "service_charge_scope":   forms.Select(attrs=_SEL),
            "max_cards_to_split":     forms.NumberInput(attrs=_TEXT),
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
            # Agent
            "affiliated_restaurants":     forms.Textarea(attrs={**_ta(3), "placeholder": "Cuba Ocho\nCalle Dragones Colombia"}),
            "escalation_enabled":         forms.CheckboxInput(attrs=_CHECK),
            "escalation_conditions":      forms.Textarea(attrs={**_ta(3), "placeholder": "Caller asks to speak with a manager.\nCaller reports an emergency on-site."}),
            "escalation_transfer_number": forms.TextInput(attrs={**_TEXT, "placeholder": "+17865551234"}),
        }

    def clean_escalation_transfer_number(self):
        number = self.cleaned_data.get("escalation_transfer_number", "").strip()
        if number and not re.fullmatch(r"\+[1-9]\d{7,14}", number):
            raise forms.ValidationError(
                "Enter a valid E.164 phone number (e.g. +17865551234). "
                "Must start with + followed by 8–15 digits."
            )
        return number
