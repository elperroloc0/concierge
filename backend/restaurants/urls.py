from django.urls import path

from . import views


urlpatterns = [
    path("", views.index, name="index"),
    path("account", views.account, name="account"),
    path("webhook/<str:rest_id>/", views.retell_inbound_webhook, name="retell_webhook"),
    path("events/", views.retell_events_webhook, name="retell_events"),
    path("tools/send-sms/", views.retell_tool_send_sms, name="retell_tool_send_sms"),
    path("tools/save-caller-info/", views.retell_tool_save_caller_info, name="retell_tool_save_caller_info"),
    path("tools/get-info/", views.retell_tool_get_info, name="retell_tool_get_info"),
    path("tools/get-caller-profile/", views.retell_tool_get_caller_profile, name="retell_tool_get_caller_profile"),
    path("tools/resolve-date/", views.retell_tool_resolve_date, name="retell_tool_resolve_date"),
    path("twilio/sms-status/", views.twilio_sms_status_webhook, name="twilio_sms_status"),
]