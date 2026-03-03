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
        exclude = ["restaurant"]
        widgets = {
            # Hours
            "hours_of_operation":     forms.Textarea(attrs={**_ta(4), "placeholder": "Mon–Thu 12pm–midnight, Fri–Sat 12pm–2am, Sun 12pm–11pm"}),
            "kitchen_closing_time":   forms.TextInput(attrs=_TEXT),
            "closes_on_holidays":     forms.CheckboxInput(attrs=_CHECK),
            "holiday_closure_notes":  forms.Textarea(attrs=_ta(2)),
            "private_event_closures": forms.Textarea(attrs=_ta(2)),
            # Menu
            "food_menu_url":          forms.URLInput(attrs=_TEXT),
            "food_menu_summary":      forms.Textarea(attrs={**_ta(6), "placeholder": "We specialize in Latin-Asian fusion. Best sellers: ceviche tostada ($18), short rib tacos ($24). Most dishes between $15–$35."}),
            "bar_menu_url":           forms.URLInput(attrs=_TEXT),
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
            # Ambience (additional)
            "art_gallery_info":       forms.Textarea(attrs=_ta(3)),
            "cigar_policy":           forms.TextInput(attrs=_TEXT),
            "show_charge_policy":     forms.TextInput(attrs=_TEXT),
            # Agent
            "affiliated_restaurants": forms.Textarea(attrs={**_ta(2), "placeholder": "Cuba Ocho, Calle Dragones Colombia"}),
            "collect_guest_info":     forms.CheckboxInput(attrs=_CHECK),
            "guest_info_to_collect":  forms.Textarea(attrs=_ta(2)),
            "brand_voice_notes":      forms.Textarea(attrs={**_ta(6), "placeholder": "Use a warm, casual tone. Avoid formal greetings..."}),
            # Other
            "additional_info":        forms.Textarea(attrs=_ta(8)),
        }
