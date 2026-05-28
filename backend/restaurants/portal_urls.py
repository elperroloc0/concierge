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
    path("<slug:slug>/calls/<int:event_pk>/send-sms/",         views.portal_send_sms,         name="portal_send_sms"),
    path("<slug:slug>/calls/<int:event_pk>/reservation-status/", views.portal_reservation_status, name="portal_reservation_status"),
    path("<slug:slug>/calls/<int:event_pk>/note/",               views.portal_call_note,        name="portal_call_note"),
    path("<slug:slug>/calls/<int:event_pk>/dismiss-action/",     views.portal_dismiss_action,   name="portal_dismiss_action"),
    path("<slug:slug>/calls/<int:event_pk>/set-reason/",         views.portal_call_set_reason,  name="portal_call_set_reason"),
    path("<slug:slug>/calls/<int:event_pk>/set-spam/",           views.portal_call_set_spam,    name="portal_call_set_spam"),
    path("<slug:slug>/calls/<int:event_pk>/reopen/",             views.portal_call_reopen,      name="portal_call_reopen"),
    path("<slug:slug>/calls/<int:event_pk>/set-status/",         views.portal_call_set_status,  name="portal_call_set_status"),
    path("<slug:slug>/calls/<int:event_pk>/mark-viewed/",        views.portal_mark_call_viewed, name="portal_mark_call_viewed"),
    path("<slug:slug>/calls/<int:event_pk>/mark-reviewed/", views.portal_mark_reviewed, name="portal_mark_reviewed"),
    path("<slug:slug>/guests/",                          views.portal_guests,        name="portal_guests"),
    path("<slug:slug>/guests/add/",                      views.portal_guest_create,  name="portal_guest_create"),
    path("<slug:slug>/guests/<int:memory_pk>/",          views.portal_guest_detail,  name="portal_guest_detail"),
    path("<slug:slug>/guests/<int:memory_pk>/sms/",      views.portal_guest_sms,     name="portal_guest_sms"),
    path("<slug:slug>/guests/<int:memory_pk>/activity/", views.portal_guest_activity, name="portal_guest_activity"),
    path("<slug:slug>/guests/<int:memory_pk>/delete/",   views.portal_guest_delete,  name="portal_guest_delete"),
    path("<slug:slug>/billing/",            views.portal_billing,          name="portal_billing"),
    path("<slug:slug>/billing/checkout/",   views.portal_billing_checkout, name="portal_billing_checkout"),
    path("<slug:slug>/billing/portal/",     views.portal_billing_portal,   name="portal_billing_portal"),
    path("<slug:slug>/billing/topup/",      views.portal_billing_topup,          name="portal_billing_topup"),
    path("<slug:slug>/billing/cancel/",     views.portal_cancel_subscription,    name="portal_cancel_subscription"),
    path("<slug:slug>/notifications/",      views.portal_notifications,          name="portal_notifications"),
    path("<slug:slug>/push/subscribe/",     views.portal_push_subscribe,         name="portal_push_subscribe"),
    path("<slug:slug>/push/unsubscribe/",   views.portal_push_unsubscribe,       name="portal_push_unsubscribe"),
    path("<slug:slug>/push/test/",          views.portal_push_test,              name="portal_push_test"),

    # ── One-tap response page (no login — secured by single-use token) ─────
    path("<slug:slug>/r/<str:token>/",         views.call_action_page,    name="call_action_page"),
    path("<slug:slug>/r/<str:token>/respond/", views.call_action_respond, name="call_action_respond"),
    path("<slug:slug>/r/<str:token>/resolve/", views.call_action_resolve, name="call_action_resolve"),

    # ── Pending queue polling (for dashboard live updates) ─────────────────
    path("<slug:slug>/pending-actions/count/", views.portal_pending_actions_count, name="portal_pending_actions_count"),
    path("<slug:slug>/account/",            views.portal_account,                name="portal_account"),
    path("<slug:slug>/account/add-operator/",    views.portal_add_operator,    name="portal_add_operator"),
    path("<slug:slug>/account/remove-operator/", views.portal_remove_operator, name="portal_remove_operator"),
    path("<slug:slug>/account/update-operator/", views.portal_update_operator, name="portal_update_operator"),
    path("<slug:slug>/update-avg-cover/",   views.portal_update_avg_cover,       name="portal_update_avg_cover"),
    path("<slug:slug>/reports/",                                views.portal_reports_list,    name="portal_reports_list"),
    path("<slug:slug>/reports/generate/",                       views.portal_generate_report,  name="portal_generate_report"),
    path("<slug:slug>/reports/<int:report_id>/",                views.portal_reports_detail,   name="portal_reports_detail"),
    path("<slug:slug>/reports/<int:report_id>/status/",         views.portal_report_status,    name="portal_report_status"),
]
