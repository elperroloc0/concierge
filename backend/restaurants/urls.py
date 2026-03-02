from django.urls import path

from . import views


urlpatterns = [
    path("", views.index, name="index"),
    path("account", views.account, name="account"),
    path("webhook/<str:rest_id>/", views.retell_inbound_webhook, name="retell_webhook"),
    path("events/", views.retell_events_webhook, name="retell_events"),
]