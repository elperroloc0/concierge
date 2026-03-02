from django.urls import path

from . import views

urlpatterns = [
    path("",                views.portal_dashboard,        name="portal_dashboard"),
    path("knowledge-base/", views.portal_knowledge_base,   name="portal_kb"),
    path("calls/",          views.portal_calls,            name="portal_calls"),
    path("login/",          views.portal_login,            name="portal_login"),
    path("logout/",         views.portal_logout,           name="portal_logout"),
]
