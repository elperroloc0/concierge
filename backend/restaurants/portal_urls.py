from django.urls import path

from . import views

urlpatterns = [
    path("", views.root_redirect),
    # ── Landing ────────────────────────────────────────────────────────────
    path("demo-request/",  views.portal_demo_request, name="portal_demo_request"),
    # ── Auth (no slug — pre-login) ─────────────────────────────────────────
    path("login/",views.portal_login,   name="portal_login"),
    path("logout/",views.portal_logout,  name="portal_logout"),
    path("select-restaurant/", views.portal_select_restaurant, name="portal_select_restaurant"),
    path("password-reset/", views.portal_password_reset_request, name="portal_password_reset_request"),
    path("password-reset/<uidb64>/<token>/", views.portal_password_reset_confirm, name="portal_password_reset_confirm"),
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
    path("<slug:slug>/account/add-operator/",    views.portal_add_operator,    name="portal_add_operator"),
    path("<slug:slug>/account/remove-operator/", views.portal_remove_operator, name="portal_remove_operator"),
    path("<slug:slug>/account/update-operator/", views.portal_update_operator, name="portal_update_operator"),
    path("<slug:slug>/update-avg-cover/",   views.portal_update_avg_cover,       name="portal_update_avg_cover"),
    path("<slug:slug>/reports/",                         views.portal_reports_list,   name="portal_reports_list"),
    path("<slug:slug>/reports/generate/",                views.portal_generate_report, name="portal_generate_report"),
    path("<slug:slug>/reports/<int:report_id>/",         views.portal_reports_detail, name="portal_reports_detail"),
]
