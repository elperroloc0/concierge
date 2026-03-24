from django.urls import path

from . import views

urlpatterns = [
    path("", views.root_redirect),
    # ── Landing ────────────────────────────────────────────────────────────
    path("demo-request/",  views.portal_demo_request, name="portal_demo_request"),
    # ── Auth (no slug — pre-login) ─────────────────────────────────────────
    path("login/",views.portal_login,   name="portal_login"),
    path("logout/",views.portal_logout,  name="portal_logout"),
    path("account/confirm-email/<uuid:token>/", views.portal_confirm_email, name="portal_confirm_email"),

    # ── Restaurant-scoped pages ────────────────────────────────────────────
    path("<slug:slug>/",                    views.portal_dashboard,        name="portal_dashboard"),
    path("<slug:slug>/knowledge-base/",     views.portal_knowledge_base,   name="portal_kb"),
    path("<slug:slug>/calls/",              views.portal_calls,            name="portal_calls"),
    path("<slug:slug>/calls/<int:event_pk>/resolve-followup/", views.portal_resolve_followup, name="portal_resolve_followup"),
    path("<slug:slug>/calls/<int:event_pk>/reservation-status/", views.portal_reservation_status, name="portal_reservation_status"),
    path("<slug:slug>/guests/",                          views.portal_guests,        name="portal_guests"),
    path("<slug:slug>/guests/add/",                      views.portal_guest_create,  name="portal_guest_create"),
    path("<slug:slug>/guests/<int:memory_pk>/",          views.portal_guest_detail,  name="portal_guest_detail"),
    path("<slug:slug>/guests/<int:memory_pk>/delete/",   views.portal_guest_delete,  name="portal_guest_delete"),
    path("<slug:slug>/billing/",            views.portal_billing,          name="portal_billing"),
    path("<slug:slug>/billing/checkout/",   views.portal_billing_checkout, name="portal_billing_checkout"),
    path("<slug:slug>/billing/portal/",     views.portal_billing_portal,   name="portal_billing_portal"),
    path("<slug:slug>/billing/topup/",      views.portal_billing_topup,          name="portal_billing_topup"),
    path("<slug:slug>/billing/cancel/",     views.portal_cancel_subscription,    name="portal_cancel_subscription"),
    path("<slug:slug>/notifications/",      views.portal_notifications,          name="portal_notifications"),
    path("<slug:slug>/account/",            views.portal_account,                name="portal_account"),
    path("<slug:slug>/update-avg-cover/",   views.portal_update_avg_cover,       name="portal_update_avg_cover"),
]
