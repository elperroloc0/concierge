"""
Django test suite for the restaurants app.

BEST PRACTICES EXPLAINED THROUGHOUT THIS FILE:
  - Each TestCase class tests ONE concern (models, one view, etc.)
  - Every test follows Arrange → Act → Assert
  - External services (Retell SDK) are always mocked
  - Test names describe behavior, not just the function name
  - setUp() creates shared fixtures; helpers avoid copy-paste
  - Edge cases and error paths are as important as the happy path
"""

import json
from unittest.mock import MagicMock, patch

from django.forms import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import CallDetail, CallEvent, CallerMemory, Restaurant

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def make_restaurant(**kwargs) -> Restaurant:
    """
    Factory helper: creates and saves a Restaurant with sensible defaults.
    Any field can be overridden via kwargs.

    WHY: avoids repeating 10-field constructor calls in every test.
    You only specify what matters for the scenario under test.
    """
    defaults = {
        "name": "Test Bistro",
        "contact_email": "owner@testbistro.com",
        "retell_api_key": "test-api-key-123",
        "retell_phone_number": "+13051234567",
        "is_active": True,
    }
    defaults.update(kwargs)
    # Using Restaurant() + .save() instead of .create() so that
    # our custom save() logic (slug generation) always runs.
    r = Restaurant(**defaults)
    r.save()
    return r


# ─────────────────────────────────────────────────────────────────────────────
# MODEL TESTS
# ─────────────────────────────────────────────────────────────────────────────

class RestaurantSlugTest(TestCase):
    """
    Tests for the auto-slug generation logic in Restaurant.save().

    BEST PRACTICE: group tests by the behavior they verify, not by method.
    slug generation is its own behavior worth its own class.
    """

    def test_slug_is_auto_generated_from_name(self):
        """When no slug is provided, save() should create one from the name."""
        # Arrange + Act
        r = make_restaurant(name="El Rincon Latino")

        # Assert
        self.assertEqual(r.slug, "el-rincon-latino")

    def test_slug_is_not_overwritten_on_update(self):
        """Re-saving an existing restaurant should not change its slug."""
        # Arrange
        r = make_restaurant(name="El Rincon Latino")
        original_slug = r.slug

        # Act — change a field and save again
        r.contact_person = "Maria"
        r.save()

        # Assert
        self.assertEqual(r.slug, original_slug)

    def test_duplicate_name_gets_numeric_suffix(self):
        """Two restaurants with the same name must get unique slugs."""
        # Arrange + Act
        r1 = make_restaurant(name="Casa Blanca")
        r2 = make_restaurant(name="Casa Blanca")

        # Assert
        self.assertEqual(r1.slug, "casa-blanca")
        self.assertEqual(r2.slug, "casa-blanca-2")

    def test_third_duplicate_increments_further(self):
        """Each additional duplicate keeps incrementing the suffix."""
        r1 = make_restaurant(name="Casa Blanca")
        r2 = make_restaurant(name="Casa Blanca")
        r3 = make_restaurant(name="Casa Blanca")

        self.assertEqual(r1.slug, "casa-blanca")
        self.assertEqual(r2.slug, "casa-blanca-2")
        self.assertEqual(r3.slug, "casa-blanca-3")


class RestaurantValidationTest(TestCase):
    """
    Tests for Restaurant.clean() — the business-rule validation layer.

    BEST PRACTICE: test validation by calling .full_clean() on the model.
    Django's admin and ModelForm call full_clean() automatically;
    testing it directly ensures the rules are enforced regardless of entry point.
    """

    def _assert_raises_for_field(self, model_instance, field_name):
        """
        Small helper to check that ValidationError is raised for a specific field.
        Keeps assertions DRY without hiding what is being tested.
        """
        with self.assertRaises(ValidationError) as ctx:
            model_instance.full_clean()
        self.assertIn(field_name, ctx.exception.message_dict)

    # ── phone_mode ────────────────────────────────────────────────────────────

    def test_existing_phone_mode_requires_existing_ph_numb(self):
        """phone_mode='existing' without a phone number should fail validation."""
        r = make_restaurant(phone_mode="existing", existing_ph_numb="")
        self._assert_raises_for_field(r, "existing_ph_numb")

    def test_existing_phone_mode_passes_when_number_is_provided(self):
        """phone_mode='existing' WITH a phone number should pass validation."""
        r = make_restaurant(phone_mode="existing", existing_ph_numb="+13059876543")
        # full_clean() should not raise
        r.full_clean()

    def test_new_phone_mode_does_not_require_existing_ph_numb(self):
        """phone_mode='new' (default) requires no existing number."""
        r = make_restaurant(phone_mode="new", existing_ph_numb="")
        r.full_clean()  # must not raise

    # ── WhatsApp notifications ────────────────────────────────────────────────

    def test_whatsapp_notifications_require_ws_number(self):
        """Enabling WhatsApp notifications without a number should fail."""
        r = make_restaurant(notify_via_ws=True, notify_ws_numb="")
        self._assert_raises_for_field(r, "notify_ws_numb")

    def test_whatsapp_notifications_pass_when_number_provided(self):
        """Enabling WhatsApp notifications WITH a number should pass."""
        r = make_restaurant(notify_via_ws=True, notify_ws_numb="+13059876543")
        r.full_clean()

    # ── Email notifications ───────────────────────────────────────────────────

    def test_email_notifications_fail_with_no_email_at_all(self):
        """Email notifications enabled but neither email field filled → error."""
        r = make_restaurant(
            notify_via_email=True,
            notify_email="",
            contact_email="",
        )
        self._assert_raises_for_field(r, "notify_email")

    def test_email_notifications_pass_with_notify_email(self):
        """notify_email alone satisfies the email notification requirement."""
        r = make_restaurant(notify_via_email=True, notify_email="alerts@bistro.com", contact_email="")
        r.full_clean()

    def test_email_notifications_pass_with_contact_email(self):
        """contact_email alone also satisfies the email notification requirement."""
        r = make_restaurant(notify_via_email=True, notify_email="", contact_email="owner@bistro.com")
        r.full_clean()


class RestaurantStrTest(TestCase):
    def test_str_returns_name(self):
        """__str__ should return the restaurant name for readable admin displays."""
        r = make_restaurant(name="La Palma")
        self.assertEqual(str(r), "La Palma")


class CallEventModelTest(TestCase):
    """Tests for the CallEvent model."""

    def setUp(self):
        """
        setUp() runs before EVERY test in the class.
        Create shared objects here so each test starts with a clean slate.
        Django wraps each test in a transaction that is rolled back afterward,
        so data does NOT leak between tests.
        """
        self.restaurant = make_restaurant()

    def test_call_event_is_created_with_correct_fields(self):
        """CallEvent should persist its fields as-is."""
        payload = {"call_id": "abc123", "event": "call_ended"}

        event = CallEvent.objects.create(
            restaurant=self.restaurant,
            event_type="call_ended",
            payload=payload,
        )

        # Re-fetch from DB to confirm persistence
        saved = CallEvent.objects.get(pk=event.pk)
        self.assertEqual(saved.event_type, "call_ended")
        self.assertEqual(saved.payload["call_id"], "abc123")
        self.assertEqual(saved.restaurant, self.restaurant)

    def test_call_events_are_deleted_when_restaurant_is_deleted(self):
        """
        ForeignKey(on_delete=CASCADE) means deleting a restaurant
        must also delete its events.
        """
        CallEvent.objects.create(
            restaurant=self.restaurant,
            event_type="call_started",
            payload={},
        )

        restaurant_pk = self.restaurant.pk
        self.restaurant.delete()

        self.assertFalse(CallEvent.objects.filter(restaurant_id=restaurant_pk).exists())


# ─────────────────────────────────────────────────────────────────────────────
# VIEW TESTS  — retell_inbound_webhook
# ─────────────────────────────────────────────────────────────────────────────

class InboundWebhookTest(TestCase):
    """
    Tests for POST /api/retell/webhook/<rest_id>/

    BEST PRACTICE: use Django's test Client, not direct function calls.
    The Client exercises the full Django request/response cycle including
    middleware and URL routing, which is what actually runs in production.

    BEST PRACTICE: always mock external services.
    We never want real HTTP calls to Retell during tests — they are slow,
    require credentials, and make tests non-deterministic.
    """

    def setUp(self):
        self.client = Client()
        self.restaurant = make_restaurant(
            name="La Perla",
            address_full="123 Main St",
            website="https://laperla.com",
            welcome_phrase="Bienvenidos",
            primary_lang="es",
            timezone="America/New_York",
            retell_api_key="secret-key",
            retell_phone_number="+13051234567",
        )
        # Add active subscription for general tests
        from .models import Subscription
        self.subscription = Subscription.objects.create(restaurant=self.restaurant, status="active", communication_balance=100.00)

        self.url = reverse("retell_webhook", kwargs={"rest_id": self.restaurant.id})

        # Default valid payload matching the restaurant's phone number
        self.valid_payload = {"to_number": "+13051234567"}

    def _post(self, payload=None, headers=None):
        """
        Small helper to POST JSON to the webhook.
        Reduces boilerplate in each test to only the varying parts.
        """
        headers = headers or {}
        body = json.dumps(payload if payload is not None else self.valid_payload)
        return self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            **headers,
        )

    # ── Method enforcement ────────────────────────────────────────────────────

    def test_get_request_returns_405(self):
        """Webhook must only accept POST; GET should return 405 Method Not Allowed."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    # ── Request body validation ───────────────────────────────────────────────

    def test_invalid_json_returns_400(self):
        """Malformed JSON body should return 400 Bad Request."""
        response = self.client.post(
            self.url,
            data="this is not json {{{",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("invalid json", response.json()["detail"])

    # ── Restaurant lookup ─────────────────────────────────────────────────────

    def test_unknown_restaurant_id_returns_200_inactive(self):
        """A rest_id that doesn't exist in the DB should return 200 with inactive status."""
        response = self.client.post(
            reverse("retell_webhook", kwargs={"rest_id": 99999}),
            data=json.dumps(self.valid_payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["call_inbound"]["dynamic_variables"]["account_status"], "inactive")

    def test_inactive_restaurant_returns_200_inactive(self):
        """An inactive restaurant should return 200 with inactive status."""
        self.restaurant.is_active = False
        self.restaurant.save()

        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["call_inbound"]["dynamic_variables"]["account_status"], "inactive")

    # ── Payload validation ────────────────────────────────────────────────────

    def test_missing_to_number_returns_400(self):
        """Payload without to_number should return 400."""
        response = self._post(payload={})  # no to_number key
        self.assertEqual(response.status_code, 400)

    def test_empty_to_number_returns_400(self):
        """Empty string to_number should also return 400."""
        response = self._post(payload={"to_number": "   "})
        self.assertEqual(response.status_code, 400)

    def test_restaurant_without_retell_phone_returns_400(self):
        """If the restaurant has no phone number configured, return 400."""
        self.restaurant.retell_phone_number = ""
        self.restaurant.save()

        response = self._post()
        self.assertEqual(response.status_code, 400)

    # ── Signature enforcement ─────────────────────────────────────────────────

    def test_missing_signature_header_returns_401(self):
        """No x-retell-signature header should return 401 Unauthorized."""
        response = self._post()  # no signature header
        self.assertEqual(response.status_code, 401)
        self.assertIn("missing signature", response.json()["detail"])

    def test_restaurant_without_api_key_returns_500(self):
        """
        If the restaurant has no API key, we can't verify the signature.
        The view should return 500 to indicate a server-side misconfiguration.
        """
        self.restaurant.retell_api_key = ""
        self.restaurant.save()

        response = self._post(headers={"HTTP_X_RETELL_SIGNATURE": "any-sig"})
        self.assertEqual(response.status_code, 500)

    def test_phone_number_not_matching_any_restaurant_returns_404(self):
        """
        The webhook validates the to_number against the DB a second time.
        A number that doesn't match any restaurant → 404.
        """
        response = self._post(
            payload={"to_number": "+19999999999"},  # number not in DB
            headers={"HTTP_X_RETELL_SIGNATURE": "any-sig"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("unknown number", response.json()["detail"])

    # ── Signature verification (mocked) ──────────────────────────────────────

    def test_invalid_signature_returns_401(self):
        """
        BEST PRACTICE: mock the Retell SDK's verify() to return False.
        We test OUR code's reaction to a failed verification,
        not the SDK's internal logic (which has its own tests).

        patch() temporarily replaces Retell.verify with a MagicMock.
        The 'with' block restores the original after the test.
        """
        with patch("restaurants.views.Retell") as MockRetell:
            # Configure the mock: any instance's verify() returns False
            MockRetell.return_value.verify.return_value = False

            response = self._post(headers={"HTTP_X_RETELL_SIGNATURE": "bad-sig"})

        self.assertEqual(response.status_code, 401)
        self.assertIn("invalid signature", response.json()["detail"])

    def test_valid_request_returns_200_with_dynamic_variables(self):
        """
        HAPPY PATH: valid restaurant, correct phone, valid signature.
        The response should include all dynamic variables the AI agent needs.
        """
        with patch("restaurants.views.Retell") as MockRetell:
            MockRetell.return_value.verify.return_value = True  # signature OK

            response = self._post(headers={"HTTP_X_RETELL_SIGNATURE": "valid-sig"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        dv = data.get("call_inbound", {}).get("dynamic_variables", {})

        self.assertEqual(dv["restaurant_name"], "La Perla")
        self.assertEqual(dv["address_full"], "123 Main Calle")
        self.assertEqual(dv["website"], "https://laperla.com")
        self.assertEqual(dv["welcome_phrase"], "Bienvenidos")
        self.assertEqual(dv["primary_lang"], "es")
        self.assertEqual(dv["timezone"], "America/New_York")

    def test_inactive_subscription_returns_200_inactive(self):
        """If the restaurant has no active subscription, return 200 inactive."""
        from .models import Subscription
        sub = self.subscription
        sub.status = "inactive"
        sub.save()

        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["call_inbound"]["dynamic_variables"]["account_status"], "inactive")

    def test_insufficient_balance_returns_200_inactive(self):
        """If communication_balance <= 0, return 200 inactive."""
        from .models import Subscription
        sub = self.subscription
        sub.communication_balance = 0.00
        sub.save()

        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["call_inbound"]["dynamic_variables"]["account_status"], "inactive")

    def test_valid_request_calls_verify_with_correct_arguments(self):
        """
        Beyond checking the HTTP response, assert that our code called the SDK
        with the exact arguments we expect. This catches silent contract breaks.
        """
        raw_payload = json.dumps(self.valid_payload)

        with patch("restaurants.views.Retell") as MockRetell:
            mock_instance = MagicMock()
            mock_instance.verify.return_value = True
            MockRetell.return_value = mock_instance

            self.client.post(
                self.url,
                data=raw_payload,
                content_type="application/json",
                HTTP_X_RETELL_SIGNATURE="valid-sig",
            )

        mock_instance.verify.assert_called_once_with(
            raw_payload,
            self.restaurant.retell_api_key,
            "valid-sig",
        )



# ─────────────────────────────────────────────────────────────────────────────
# VIEW TESTS  — retell_events_webhook
# ─────────────────────────────────────────────────────────────────────────────

class EventsWebhookTest(TestCase):
    """
    Tests for retell_events_webhook.

    NOTE: This view is not yet registered in urls.py, so we can't use
    reverse() or the test Client. Instead we use RequestFactory, which
    builds a request object without going through URL routing.

    BEST PRACTICE: RequestFactory is useful for testing view functions
    in isolation — especially views not yet wired to URLs.
    Once you add the URL, switch these tests to use Client + reverse().
    """

    def setUp(self):
        from django.test import RequestFactory
        self.factory = RequestFactory()
        self.restaurant = make_restaurant(
            retell_phone_number="+13051234567",
            retell_api_key="events-key",
        )
        # Add active subscription
        from .models import Subscription
        self.subscription = Subscription.objects.create(restaurant=self.restaurant, status="active", communication_balance=100.00)

    def _make_request(self, payload, signature=""):
        from restaurants.views import retell_events_webhook
        body = json.dumps(payload)
        request = self.factory.post(
            "/api/retell/events/",       # path doesn't matter for RequestFactory
            data=body,
            content_type="application/json",
        )
        if signature:
            request.META["HTTP_X_RETELL_SIGNATURE"] = signature
        return retell_events_webhook(request)

    def test_invalid_json_returns_400(self):
        from django.test import RequestFactory
        from restaurants.views import retell_events_webhook
        request = RequestFactory().post(
            "/api/retell/events/",
            data="not-json",
            content_type="application/json",
        )
        response = retell_events_webhook(request)
        self.assertEqual(response.status_code, 400)

    def test_unknown_phone_number_returns_404(self):
        payload = {"to_number": "+19999999999", "event_type": "call_ended"}
        response = self._make_request(payload, signature="sig")
        self.assertEqual(response.status_code, 404)

    def test_missing_signature_returns_401(self):
        payload = {"to_number": "+13051234567", "event_type": "call_ended"}
        response = self._make_request(payload, signature="")  # no sig
        self.assertEqual(response.status_code, 401)

    def test_invalid_signature_returns_401(self):
        payload = {"to_number": "+13051234567", "event_type": "call_ended"}
        with patch("restaurants.views.Retell") as MockRetell:
            MockRetell.return_value.verify.return_value = False
            response = self._make_request(payload, signature="bad")
        self.assertEqual(response.status_code, 401)

    def test_valid_event_is_saved_and_returns_200(self):
        """
        On a valid request, the view should persist a CallEvent and return 200.
        We verify both the HTTP response AND the side-effect (DB write).
        """
        payload = {"to_number": "+13051234567", "event_type": "call_ended"}
        with patch("restaurants.views.Retell") as MockRetell:
            MockRetell.return_value.verify.return_value = True
            response = self._make_request(payload, signature="valid")

        self.assertEqual(response.status_code, 200)

        # Assert the side-effect: one CallEvent was created
        self.assertEqual(CallEvent.objects.count(), 1)
        event = CallEvent.objects.first()
        self.assertEqual(event.event_type, "call_ended")
        self.assertEqual(event.restaurant, self.restaurant)

    def test_call_ended_updates_communication_balance(self):
        """On call_ended, the combined_cost should be subtracted from the balance."""
        from .models import Subscription
        from decimal import Decimal
        sub = self.subscription
        sub.communication_balance = Decimal("10.00")
        sub.communication_markup = Decimal("1.00")
        sub.save()

        payload = {
            "to_number": "+13051234567",
            "event": "call_ended",
            "call": {
                "call_cost": {
                    "combined_cost": 50
                }
            }
        }

        with patch("restaurants.views.Retell") as MockRetell:
            MockRetell.return_value.verify.return_value = True
            self._make_request(payload, signature="valid")

        sub.refresh_from_db()
        self.assertEqual(sub.communication_balance, Decimal("9.50"))

# ─────────────────────────────────────────────────────────────────────────────
# CALLER MEMORY MODEL TESTS
# ─────────────────────────────────────────────────────────────────────────────

class CallerMemoryModelTest(TestCase):
    """Tests for CallerMemory model: creation, uniqueness, __str__, defaults."""

    def setUp(self):
        self.restaurant = make_restaurant(name="La Perla")

    def _make_memory(self, **kwargs):
        defaults = {
            "restaurant": self.restaurant,
            "phone": "+13055550001",
            "name": "Mavi",
            "call_count": 1,
            "last_call_summary": "Asked about a reservation for 17 people.",
        }
        defaults.update(kwargs)
        return CallerMemory.objects.create(**defaults)

    def test_creation_stores_all_fields(self):
        """CallerMemory should persist all provided fields."""
        mem = self._make_memory(email="mavi@example.com", preferences="prefers terrace")
        mem.refresh_from_db()
        self.assertEqual(mem.phone, "+13055550001")
        self.assertEqual(mem.name, "Mavi")
        self.assertEqual(mem.email, "mavi@example.com")
        self.assertEqual(mem.preferences, "prefers terrace")
        self.assertEqual(mem.call_count, 1)

    def test_unique_together_prevents_duplicate_phone_per_restaurant(self):
        """Two records with the same (restaurant, phone) must raise IntegrityError."""
        from django.db import IntegrityError
        self._make_memory()
        with self.assertRaises(IntegrityError):
            CallerMemory.objects.create(
                restaurant=self.restaurant,
                phone="+13055550001",
                call_count=2,
            )

    def test_same_phone_different_restaurant_is_allowed(self):
        """Same phone number on a different restaurant should succeed."""
        other = make_restaurant(name="El Patio")
        self._make_memory()
        mem2 = CallerMemory.objects.create(
            restaurant=other,
            phone="+13055550001",
            call_count=1,
        )
        self.assertEqual(CallerMemory.objects.filter(phone="+13055550001").count(), 2)

    def test_str_uses_name_when_available(self):
        mem = self._make_memory(name="Mavi")
        self.assertIn("Mavi", str(mem))
        self.assertIn(self.restaurant.slug, str(mem))

    def test_str_falls_back_to_phone_when_no_name(self):
        mem = self._make_memory(name="")
        self.assertIn("+13055550001", str(mem))

    def test_blank_defaults(self):
        """Fields with default='' should not require explicit values."""
        mem = CallerMemory.objects.create(
            restaurant=self.restaurant,
            phone="+13055550002",
        )
        self.assertEqual(mem.name, "")
        self.assertEqual(mem.email, "")
        self.assertEqual(mem.preferences, "")
        self.assertEqual(mem.staff_notes, "")
        self.assertEqual(mem.last_call_summary, "")
        self.assertEqual(mem.call_count, 0)

    def test_call_detail_has_call_summary_field(self):
        """CallDetail.call_summary field must exist and default to empty string."""
        event = CallEvent.objects.create(
            restaurant=self.restaurant,
            event_type="call_ended",
            payload={},
        )
        detail = CallDetail.objects.create(call_event=event)
        self.assertEqual(detail.call_summary, "")


# ─────────────────────────────────────────────────────────────────────────────
# _get_caller_summary TESTS
# ─────────────────────────────────────────────────────────────────────────────

class GetCallerSummaryTest(TestCase):
    """Tests for _get_caller_summary(): formatting and edge cases."""

    def setUp(self):
        from django.utils import timezone
        self.restaurant = make_restaurant(name="La Perla")
        self.now = timezone.now()

    def _make_memory(self, **kwargs):
        from .models import CallerMemory
        defaults = {
            "restaurant": self.restaurant,
            "phone": "+13055550001",
            "name": "Mavi",
            "call_count": 3,
            "last_call_at": self.now,
            "last_call_summary": "Asked about a reservation for 17 people.",
        }
        defaults.update(kwargs)
        return CallerMemory.objects.create(**defaults)

    def _call(self, phone):
        from .views import _get_caller_summary
        return _get_caller_summary(phone, self.restaurant)

    def test_empty_string_when_no_memory_exists(self):
        """First-time caller — no CallerMemory record — should return empty string."""
        result = self._call("+13055550001")
        self.assertEqual(result, "")

    def test_empty_string_when_phone_is_blank(self):
        """Blank from_number should return empty string without querying DB."""
        result = self._call("")
        self.assertEqual(result, "")

    def test_includes_name_and_call_count(self):
        self._make_memory()
        result = self._call("+13055550001")
        self.assertIn("Mavi", result)
        self.assertIn("3", result)

    def test_includes_last_call_summary(self):
        self._make_memory()
        result = self._call("+13055550001")
        self.assertIn("reservation for 17 people", result)

    def test_includes_preferences_when_set(self):
        self._make_memory(preferences="prefers terrace seating")
        result = self._call("+13055550001")
        self.assertIn("prefers terrace seating", result)

    def test_excludes_preferences_when_blank(self):
        self._make_memory(preferences="")
        result = self._call("+13055550001")
        self.assertNotIn("preferences", result.lower().split("known")[1] if "known" in result.lower() else result)

    def test_includes_staff_notes_when_set(self):
        self._make_memory(staff_notes="VIP client")
        result = self._call("+13055550001")
        self.assertIn("VIP client", result)

    def test_returns_returning_caller_header(self):
        self._make_memory()
        result = self._call("+13055550001")
        self.assertIn("RETURNING CALLER", result)

    def test_no_memory_for_different_restaurant(self):
        """CallerMemory for a different restaurant should NOT appear."""
        other = make_restaurant(name="El Patio")
        CallerMemory.objects.create(
            restaurant=other, phone="+13055550001",
            name="Mavi", call_count=1,
        )
        result = self._call("+13055550001")
        self.assertEqual(result, "")

    def test_inbound_webhook_includes_caller_summary_key(self):
        """The inbound webhook response must always include caller_summary key."""
        from unittest.mock import patch
        url = reverse("retell_webhook", kwargs={"rest_id": self.restaurant.id})
        from .models import Subscription
        Subscription.objects.create(restaurant=self.restaurant, status="active", communication_balance=50)
        payload = json.dumps({"to_number": self.restaurant.retell_phone_number})
        with patch("restaurants.views.Retell") as MockRetell:
            MockRetell.return_value.verify.return_value = True
            resp = self.client.post(url, data=payload, content_type="application/json",
                                    HTTP_X_RETELL_SIGNATURE="sig")
        dyn = resp.json()["call_inbound"]["dynamic_variables"]
        self.assertIn("caller_summary", dyn)


# ─────────────────────────────────────────────────────────────────────────────
# get_caller_profile TOOL ENDPOINT TESTS
# ─────────────────────────────────────────────────────────────────────────────

class GetCallerProfileToolTest(TestCase):
    """
    Tests for POST /api/retell/tools/get-caller-profile/

    Security focus: caller must be identified from call.from_number only,
    never from agent-supplied args. Endpoint is strictly read-only.
    """

    def setUp(self):
        self.client = Client()
        self.restaurant = make_restaurant(
            name="La Perla",
            retell_phone_number="+13051234567",
        )
        self.url = reverse("retell_tool_get_caller_profile")

    def _post(self, from_number="+13055550001", to_number="+13051234567", args=None):
        payload = {
            "call": {"from_number": from_number, "to_number": to_number},
            "args": args or {},
        }
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    def _make_memory(self, **kwargs):
        defaults = {
            "restaurant": self.restaurant,
            "phone": "+13055550001",
            "name": "Mavi",
            "call_count": 3,
            "last_call_summary": "Wanted to book for 17 people.",
            "preferences": "prefers terrace",
            "staff_notes": "VIP",
        }
        defaults.update(kwargs)
        return CallerMemory.objects.create(**defaults)

    def test_only_accepts_post(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_invalid_json_returns_400(self):
        response = self.client.post(self.url, data="bad json", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_returns_profile_for_known_caller(self):
        self._make_memory()
        response = self._post()
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertIn("Mavi", result)
        self.assertIn("Wanted to book for 17 people", result)
        self.assertIn("prefers terrace", result)
        self.assertIn("VIP", result)

    def test_returns_no_profile_message_for_unknown_caller(self):
        response = self._post(from_number="+10000000000")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No profile", response.json()["result"])

    def test_returns_error_when_from_number_missing(self):
        payload = {"call": {"to_number": "+13051234567"}, "args": {}}
        response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No caller number", response.json()["result"])

    def test_cannot_spoof_caller_via_args(self):
        """Agent-supplied args must have no effect on which caller is looked up."""
        # Mavi's record exists for from_number +13055550001
        self._make_memory()
        # Attacker tries to pass a spoofed phone in args — should be ignored
        response = self._post(
            from_number="+10000000000",   # unknown real caller
            args={"phone": "+13055550001"},  # spoofed arg — must be ignored
        )
        # Should NOT return Mavi's profile
        self.assertNotIn("Mavi", response.json()["result"])

    def test_unknown_restaurant_returns_not_found_message(self):
        response = self._post(to_number="+19999999999")
        self.assertIn("not found", response.json()["result"].lower())

    def test_profile_does_not_leak_across_restaurants(self):
        """A CallerMemory for restaurant A must not be visible to restaurant B."""
        other = make_restaurant(name="El Patio", retell_phone_number="+10001112222")
        CallerMemory.objects.create(
            restaurant=other, phone="+13055550001", name="Secret", call_count=1,
        )
        # Query against La Perla (self.restaurant) — should not see El Patio's data
        response = self._post()
        self.assertNotIn("Secret", response.json()["result"])


# ─────────────────────────────────────────────────────────────────────────────
# _upsert_caller_memory TESTS
# ─────────────────────────────────────────────────────────────────────────────

class UpsertCallerMemoryTest(TestCase):
    """Tests for _upsert_caller_memory(): creation, merging, and idempotency."""

    def setUp(self):
        self.restaurant = make_restaurant(name="La Perla")

    def _make_event(self, from_number="+13055550001", caller_name="", call_summary="", caller_email=""):
        """Create a call_analyzed CallEvent with the given fields."""
        event = CallEvent.objects.create(
            restaurant=self.restaurant,
            event_type="call_analyzed",
            payload={
                "call": {
                    "from_number": from_number,
                    "call_analysis": {
                        "call_summary": call_summary,
                        "custom_analysis_data": {
                            "caller_name": caller_name,
                            "caller_email": caller_email,
                        },
                    },
                }
            },
        )
        return event

    def _upsert(self, event):
        from .views import _upsert_caller_memory
        _upsert_caller_memory(event, self.restaurant)

    def test_creates_new_record_for_first_call(self):
        event = self._make_event(caller_name="Mavi", call_summary="Wanted to book for 17.")
        self._upsert(event)
        mem = CallerMemory.objects.get(phone="+13055550001", restaurant=self.restaurant)
        self.assertEqual(mem.name, "Mavi")
        self.assertEqual(mem.call_count, 1)
        self.assertIn("book for 17", mem.last_call_summary)

    def test_increments_call_count_on_subsequent_calls(self):
        event1 = self._make_event(caller_name="Mavi", call_summary="First call.")
        event2 = self._make_event(caller_name="Mavi", call_summary="Second call.")
        self._upsert(event1)
        self._upsert(event2)
        mem = CallerMemory.objects.get(phone="+13055550001", restaurant=self.restaurant)
        self.assertEqual(mem.call_count, 2)

    def test_updates_summary_on_subsequent_call(self):
        event1 = self._make_event(call_summary="Old summary.")
        event2 = self._make_event(call_summary="New summary.")
        self._upsert(event1)
        self._upsert(event2)
        mem = CallerMemory.objects.get(phone="+13055550001", restaurant=self.restaurant)
        self.assertEqual(mem.last_call_summary, "New summary.")

    def test_does_not_overwrite_name_with_empty_string(self):
        """If the new call has no caller_name, existing name must be preserved."""
        event1 = self._make_event(caller_name="Mavi")
        event2 = self._make_event(caller_name="")   # no name captured this time
        self._upsert(event1)
        self._upsert(event2)
        mem = CallerMemory.objects.get(phone="+13055550001", restaurant=self.restaurant)
        self.assertEqual(mem.name, "Mavi")

    def test_updates_name_when_new_call_provides_one(self):
        event1 = self._make_event(caller_name="Mavi")
        event2 = self._make_event(caller_name="Maria")
        self._upsert(event1)
        self._upsert(event2)
        mem = CallerMemory.objects.get(phone="+13055550001", restaurant=self.restaurant)
        self.assertEqual(mem.name, "Maria")

    def test_skips_when_from_number_is_missing(self):
        """If the call has no from_number, no CallerMemory record should be created."""
        event = self._make_event(from_number="")
        self._upsert(event)
        self.assertFalse(CallerMemory.objects.filter(restaurant=self.restaurant).exists())

    def test_staff_notes_and_preferences_are_not_overwritten(self):
        """Staff-curated fields must survive post-call upserts."""
        CallerMemory.objects.create(
            restaurant=self.restaurant, phone="+13055550001",
            preferences="prefers terrace", staff_notes="VIP", call_count=1,
        )
        event = self._make_event(caller_name="Mavi", call_summary="New call.")
        self._upsert(event)
        mem = CallerMemory.objects.get(phone="+13055550001", restaurant=self.restaurant)
        self.assertEqual(mem.preferences, "prefers terrace")
        self.assertEqual(mem.staff_notes, "VIP")

    def test_call_summary_stored_on_call_detail(self):
        """call_summary from Retell must be stored on CallDetail.call_summary."""
        event = self._make_event(call_summary="Called about the menu.")
        # _build_call_detail_from_payload needs a proper call_ended structure
        event.payload["call"]["from_number"] = "+13055550001"
        event.save()
        from .views import _build_call_detail_from_payload
        _build_call_detail_from_payload(event)
        detail = CallDetail.objects.get(call_event=event)
        self.assertEqual(detail.call_summary, "Called about the menu.")


# ─────────────────────────────────────────────────────────────────────────────
# caller_type UPSERT TESTS
# ─────────────────────────────────────────────────────────────────────────────

class CallerTypeUpsertTest(TestCase):
    """Tests for caller_type assignment logic in _upsert_caller_memory."""

    def setUp(self):
        self.restaurant = make_restaurant(name="La Perla")

    def _make_event(self, from_number="+13055550001", call_reason="other"):
        return CallEvent.objects.create(
            restaurant=self.restaurant,
            event_type="call_analyzed",
            payload={"call": {
                "from_number": from_number,
                "call_analysis": {"call_summary": "", "custom_analysis_data": {"call_reason": call_reason}},
            }},
        )

    def _upsert(self, event):
        from .views import _upsert_caller_memory
        _upsert_caller_memory(event, self.restaurant)

    def test_non_customer_call_creates_business_type(self):
        self._upsert(self._make_event(call_reason="non_customer"))
        mem = CallerMemory.objects.get(phone="+13055550001", restaurant=self.restaurant)
        self.assertEqual(mem.caller_type, CallerMemory.CALLER_TYPE_BUSINESS)

    def test_regular_call_creates_guest_type(self):
        for reason in ("reservation", "hours", "menu", "complaint", "other"):
            CallerMemory.objects.filter(restaurant=self.restaurant).delete()
            self._upsert(self._make_event(call_reason=reason))
            mem = CallerMemory.objects.get(phone="+13055550001", restaurant=self.restaurant)
            self.assertEqual(mem.caller_type, CallerMemory.CALLER_TYPE_GUEST, msg=f"Failed for reason={reason}")

    def test_guest_is_never_downgraded_to_business(self):
        """If a guest calls again and is classified as non_customer, type stays guest."""
        self._upsert(self._make_event(call_reason="reservation"))
        self._upsert(self._make_event(call_reason="non_customer"))
        mem = CallerMemory.objects.get(phone="+13055550001", restaurant=self.restaurant)
        self.assertEqual(mem.caller_type, CallerMemory.CALLER_TYPE_GUEST)

    def test_business_can_be_upgraded_to_guest(self):
        """If a business contact later calls as a guest, they become a guest."""
        self._upsert(self._make_event(call_reason="non_customer"))
        self._upsert(self._make_event(call_reason="reservation"))
        mem = CallerMemory.objects.get(phone="+13055550001", restaurant=self.restaurant)
        self.assertEqual(mem.caller_type, CallerMemory.CALLER_TYPE_GUEST)

    def test_default_caller_type_is_guest(self):
        """CallerMemory created manually without caller_type must default to guest."""
        mem = CallerMemory.objects.create(restaurant=self.restaurant, phone="+13055550002")
        self.assertEqual(mem.caller_type, CallerMemory.CALLER_TYPE_GUEST)


# ─────────────────────────────────────────────────────────────────────────────
# PORTAL GUEST CRM VIEW TESTS
# ─────────────────────────────────────────────────────────────────────────────

class PortalGuestsListTest(TestCase):
    """Tests for portal_guests list view."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user("owner", password="pass")
        self.restaurant = make_restaurant(name="La Perla", retell_phone_number="+13051234567")
        self.restaurant.user = self.user
        self.restaurant.save()
        self.client.force_login(self.user)
        self.url = reverse("portal_guests", kwargs={"slug": self.restaurant.slug})

    def _make_memory(self, phone, caller_type="guest", name=""):
        return CallerMemory.objects.create(
            restaurant=self.restaurant, phone=phone,
            caller_type=caller_type, name=name, call_count=1,
        )

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.get(self.url)
        self.assertNotEqual(resp.status_code, 200)

    def test_returns_200_for_authenticated_owner(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

    def test_shows_guests_tab_by_default(self):
        self._make_memory("+13055550001", caller_type="guest", name="Mavi")
        self._make_memory("+13055550002", caller_type="business", name="Sysco Rep")
        resp = self.client.get(self.url)
        self.assertContains(resp, "Mavi")
        self.assertNotContains(resp, "Sysco Rep")

    def test_business_tab_shows_business_contacts(self):
        self._make_memory("+13055550001", caller_type="guest", name="Mavi")
        self._make_memory("+13055550002", caller_type="business", name="Sysco Rep")
        resp = self.client.get(self.url + "?tab=business")
        self.assertNotContains(resp, "Mavi")
        self.assertContains(resp, "Sysco Rep")

    def test_search_filters_by_name(self):
        self._make_memory("+13055550001", name="Mavi")
        self._make_memory("+13055550002", name="Carlos")
        resp = self.client.get(self.url + "?q=Mavi")
        self.assertContains(resp, "Mavi")
        self.assertNotContains(resp, "Carlos")

    def test_other_restaurant_records_not_visible(self):
        other = make_restaurant(name="El Patio")
        CallerMemory.objects.create(restaurant=other, phone="+10001112222", name="Intruder", call_count=1)
        resp = self.client.get(self.url)
        self.assertNotContains(resp, "Intruder")


class PortalGuestDetailTest(TestCase):
    """Tests for portal_guest_detail view: GET display and POST save."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user("owner2", password="pass")
        self.restaurant = make_restaurant(name="La Perla 2")
        self.restaurant.user = self.user
        self.restaurant.save()
        self.client.force_login(self.user)
        self.memory = CallerMemory.objects.create(
            restaurant=self.restaurant, phone="+13055550099",
            name="Mavi", call_count=2, caller_type="guest",
        )
        self.url = reverse("portal_guest_detail", kwargs={
            "slug": self.restaurant.slug, "memory_pk": self.memory.pk
        })

    def test_get_returns_200_with_profile(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Mavi")
        self.assertContains(resp, "+13055550099")

    def test_post_saves_preferences_and_staff_notes(self):
        resp = self.client.post(self.url, {
            "action": "save",
            "preferences": "prefers terrace",
            "staff_notes": "VIP",
            "caller_type": "guest",
        })
        self.assertEqual(resp.status_code, 200)
        self.memory.refresh_from_db()
        self.assertEqual(self.memory.preferences, "prefers terrace")
        self.assertEqual(self.memory.staff_notes, "VIP")

    def test_save_name_action_updates_name(self):
        resp = self.client.post(self.url, {
            "action": "save_name",
            "name": "Mavi González",
        })
        self.assertEqual(resp.status_code, 200)
        self.memory.refresh_from_db()
        self.assertEqual(self.memory.name, "Mavi González")

    def test_post_can_change_caller_type(self):
        resp = self.client.post(self.url, {
            "action": "save",
            "preferences": "",
            "staff_notes": "",
            "caller_type": "business",
        })
        self.memory.refresh_from_db()
        self.assertEqual(self.memory.caller_type, "business")

    def test_cannot_access_other_restaurant_profile(self):
        other_rest = make_restaurant(name="Other")
        other_mem  = CallerMemory.objects.create(
            restaurant=other_rest, phone="+10001112222", call_count=1,
        )
        url = reverse("portal_guest_detail", kwargs={
            "slug": other_rest.slug, "memory_pk": other_mem.pk
        })
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)


class PortalGuestDeleteTest(TestCase):
    """Tests for portal_guest_delete: POST-only, ownership, redirect."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user("owner3", password="pass")
        self.restaurant = make_restaurant(name="La Perla 3")
        self.restaurant.user = self.user
        self.restaurant.save()
        self.client.force_login(self.user)
        self.memory = CallerMemory.objects.create(
            restaurant=self.restaurant, phone="+13055550077", call_count=1,
        )

    def _url(self, pk=None):
        return reverse("portal_guest_delete", kwargs={
            "slug": self.restaurant.slug,
            "memory_pk": pk or self.memory.pk,
        })

    def test_get_returns_405(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 405)

    def test_post_deletes_record_and_redirects(self):
        resp = self.client.post(self._url())
        self.assertRedirects(resp, reverse("portal_guests", kwargs={"slug": self.restaurant.slug}))
        self.assertFalse(CallerMemory.objects.filter(pk=self.memory.pk).exists())

    def test_cannot_delete_other_restaurant_record(self):
        other_rest = make_restaurant(name="Other 2")
        other_mem  = CallerMemory.objects.create(restaurant=other_rest, phone="+10001112333", call_count=1)
        url = reverse("portal_guest_delete", kwargs={
            "slug": other_rest.slug, "memory_pk": other_mem.pk
        })
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(CallerMemory.objects.filter(pk=other_mem.pk).exists())
