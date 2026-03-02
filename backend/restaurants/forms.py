from django import forms

from .models import Restaurant, RestaurantKnowledgeBase


class RestaurantBasicForm(forms.ModelForm):
    class Meta:
        model = Restaurant
        fields = [
            "name", "address_full", "location_reference", "website",
            "timezone", "primary_lang", "conversation_tone", "welcome_phrase",
            "contact_phone", "contact_email",
        ]
        widgets = {
            "welcome_phrase": forms.Textarea(attrs={"rows": 2}),
            "location_reference": forms.Textarea(attrs={"rows": 2}),
        }


class KnowledgeBaseForm(forms.ModelForm):
    class Meta:
        model = RestaurantKnowledgeBase
        exclude = ["restaurant"]
        widgets = {
            "hours_of_operation":     forms.Textarea(attrs={"rows": 4}),
            "holiday_closure_notes":  forms.Textarea(attrs={"rows": 2}),
            "private_event_closures": forms.Textarea(attrs={"rows": 2}),
            "food_menu_summary":      forms.Textarea(attrs={"rows": 6}),
            "bar_menu_summary":       forms.Textarea(attrs={"rows": 6}),
            "happy_hour_details":     forms.Textarea(attrs={"rows": 4}),
            "dietary_options":        forms.Textarea(attrs={"rows": 4}),
            "live_music_details":     forms.Textarea(attrs={"rows": 3}),
            "free_parking_info":      forms.Textarea(attrs={"rows": 2}),
            "guest_info_to_collect":  forms.Textarea(attrs={"rows": 2}),
        }
