from django.urls import path

from . import views

urlpatterns = [
    path("", views.root_redirect),
    # ── Auth (no slug — pre-login) ─────────────────────────────────────────
    path("login/",views.portal_login,   name="portal_login"),
    path("logout/",views.portal_logout,  name="portal_logout"),

    # ── Restaurant-scoped pages ────────────────────────────────────────────
    path("<slug:slug>/",                    views.portal_dashboard,        name="portal_dashboard"),
    path("<slug:slug>/knowledge-base/",     views.portal_knowledge_base,   name="portal_kb"),
    path("<slug:slug>/calls/",              views.portal_calls,            name="portal_calls"),
    path("<slug:slug>/calls/<int:event_pk>/resolve-followup/", views.portal_resolve_followup, name="portal_resolve_followup"),
    path("<slug:slug>/guests/",             views.portal_guests,           name="portal_guests"),
    path("<slug:slug>/billing/",            views.portal_billing,          name="portal_billing"),
    path("<slug:slug>/billing/checkout/",   views.portal_billing_checkout, name="portal_billing_checkout"),
    path("<slug:slug>/billing/portal/",     views.portal_billing_portal,   name="portal_billing_portal"),
    path("<slug:slug>/billing/topup/",      views.portal_billing_topup,    name="portal_billing_topup"),
    path("<slug:slug>/notifications/",      views.portal_notifications,    name="portal_notifications"),
]
