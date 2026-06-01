"""
URL configuration for concierge_ai project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include

from restaurants import views as restaurant_views

handler400 = "restaurants.views.bad_request"
handler403 = "restaurants.views.permission_denied"
handler404 = "restaurants.views.page_not_found"
handler500 = "restaurants.views.server_error"

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/retell/', include('restaurants.urls')),
    path('api/stripe/webhook/', restaurant_views.stripe_webhook, name='stripe_webhook'),
    # Public landing-page endpoints
    path('api/demo/trigger-call/', restaurant_views.demo_trigger_call, name='demo_trigger_call'),
    path('api/waitlist/',          restaurant_views.waitlist_create,   name='waitlist_create'),
    # Service worker + manifest must live at root so SW can claim scope "/"
    path('sw.js',         restaurant_views.push_service_worker, name='push_service_worker'),
    path('manifest.json', restaurant_views.pwa_manifest,        name='pwa_manifest'),
    path('', restaurant_views.root_redirect),
    path('portal/', include('restaurants.portal_urls')),
    path('demo/', restaurant_views.demo_call, name='demo_call'),
    path('help/cancel-forwarding/', restaurant_views.help_cancel_forwarding, name='help_cancel_forwarding'),
]
