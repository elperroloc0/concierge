import json
import os

from backend import settings
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from retell import Retell

from .models import CallEvent, Restaurant


# Create your views here.
def index(request):
    return HttpResponse("Hello1")


def account(request):
    return HttpResponse("account page")

@csrf_exempt
def retell_inbound_webhook(request, rest_id):
    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)

    raw_bytes = request.body
    raw_str = raw_bytes.decode("utf-8")

    try:
        payload = json.loads(raw_str)
    except json.JSONDecodeError:
        return JsonResponse({"detail": "invalid json"}, status=400)

    # find restaurant by id
    restaurant = get_object_or_404(Restaurant, id=rest_id, is_active=True)

    # check phone number
    to_number = (payload.get("to_number") or "").strip()
    if not to_number or not restaurant.retell_phone_number:
        return JsonResponse({"detail": "missing to_numbe or mismatch"}, status=400)


    if settings.DEBUG and request.headers.get("X-DEV-BYPASS") == os.environ.get("RETELL_DEV_BYPASS_SECRET",""):
        return JsonResponse(
            {
                "dynamic_variables": {
                    "restaurant_name": restaurant.name,
                    "address_full": restaurant.address_full,
                    "website": restaurant.website,
                    "welcome_phrase": restaurant.welcome_phrase,
                    "primary_lang": restaurant.primary_lang,
                    "timezone": restaurant.timezone
                }
             }, status=200
        )

    # check retell signature
    signature = request.headers.get("x-retell-signature", "")
    if not signature:
        return JsonResponse({"detail": "missing signature"}, status=401)

    if not restaurant.retell_api_key:
        return JsonResponse({"detail": "retell api key not set for this restaurant"}, status=500)
    # find restaurant by number
    restaurant = Restaurant.objects.filter(retell_phone_number=to_number, is_active=True).first()
    if not restaurant:
        return JsonResponse({"detail": "unknown number"}, status=404)

    retell_client = Retell(api_key=restaurant.retell_api_key)
    if not retell_client.verify(raw_str, restaurant.retell_api_key, signature):
        return JsonResponse({"detail": "invalid signature"}, status=401)

    return JsonResponse(
        {
            "dynamic_variables": {
                "restaurant_name": restaurant.name,
                "address_full": restaurant.address_full,
                "website": restaurant.website,
                "welcome_phrase": restaurant.welcome_phrase,
                "primary_lang": restaurant.primary_lang,
                "timezone": restaurant.timezone,
            }
        },
        status=200,
    )


@csrf_exempt
def retell_events_webhook(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    raw = request.body.decode("utf-8")
    sig = request.headers.get("x-retell-signature","")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return JsonResponse({"detail":"invalid json"}, status=400)

    # выбираем ресторан, чтобы проверить подпись ключом ЕГО workspace (модель A)
    to_number = (data.get("to_number") or data.get("call", {}).get("to_number") or "").strip()

    restaurant = Restaurant.objects.filter(retell_phone_number=to_number, is_active=True).first()
    if not restaurant:
        return JsonResponse({"detail":"unknown number"}, status=404)

    if not sig or not restaurant.retell_api_key:
        return JsonResponse({"detail":"unauthorized"}, status=401)

    retell_client = Retell(api_key=restaurant.retell_api_key)

    if not retell_client.verify(raw, restaurant.retell_api_key, sig):
        return JsonResponse({"detail":"invalid signature"}, status=401)

    event_type = data.get("event_type","")

    CallEvent.objects.create(restaurant=restaurant, event_type=event_type, payload=data)  # оптимально: сохраняем сырое событие в БД для аудита/ретраев/отладки, а тяжёлую обработку выносим в фон (celery)
    return JsonResponse({"status":"ok"}, status=200)
