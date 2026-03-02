from django.urls import path

from . import views

urlpatterns = [
    # ── Auth (no slug — pre-login) ─────────────────────────────────────────
    path("login/",   views.portal_login,   name="portal_login"),
    path("logout/",  views.portal_logout,  name="portal_logout"),

    # ── Restaurant-scoped pages ────────────────────────────────────────────
    path("<slug:slug>/",                    views.portal_dashboard,        name="portal_dashboard"),
    path("<slug:slug>/knowledge-base/",     views.portal_knowledge_base,   name="portal_kb"),
    path("<slug:slug>/calls/",              views.portal_calls,            name="portal_calls"),
    path("<slug:slug>/billing/",            views.portal_billing,          name="portal_billing"),
    path("<slug:slug>/billing/checkout/",   views.portal_billing_checkout, name="portal_billing_checkout"),
    path("<slug:slug>/billing/portal/",     views.portal_billing_portal,   name="portal_billing_portal"),
]
