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

from .models import CallEvent, Restaurant

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

    def test_unknown_restaurant_id_returns_404(self):
        """A rest_id that doesn't exist in the DB should return 404."""
        url = reverse("retell_webhook", kwargs={"rest_id": 99999})
        response = self._post(url=url) if False else self.client.post(
            reverse("retell_webhook", kwargs={"rest_id": 99999}),
            data=json.dumps(self.valid_payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_inactive_restaurant_returns_404(self):
        """An inactive restaurant should not process calls."""
        self.restaurant.is_active = False
        self.restaurant.save()

        response = self._post()
        self.assertEqual(response.status_code, 404)

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
        dv = data["dynamic_variables"]

        self.assertEqual(dv["restaurant_name"], "La Perla")
        self.assertEqual(dv["address_full"], "123 Main St")
        self.assertEqual(dv["website"], "https://laperla.com")
        self.assertEqual(dv["welcome_phrase"], "Bienvenidos")
        self.assertEqual(dv["primary_lang"], "es")
        self.assertEqual(dv["timezone"], "America/New_York")

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

    # ── Dev bypass ────────────────────────────────────────────────────────────

    @override_settings(DEBUG=True)
    def test_dev_bypass_skips_signature_check(self):
        """
        When DEBUG=True and the correct bypass header is sent,
        the view should return 200 WITHOUT verifying the Retell signature.

        override_settings() changes Django settings only for this test,
        then restores them automatically. Never rely on global settings in tests.
        """
        with patch.dict("os.environ", {"RETELL_DEV_BYPASS_SECRET": "dev-secret"}):
            # No signature header needed — bypass header replaces it
            response = self._post(headers={"HTTP_X_DEV_BYPASS": "dev-secret"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("dynamic_variables", response.json())

    @override_settings(DEBUG=True)
    def test_dev_bypass_fails_with_wrong_secret(self):
        """Wrong bypass secret should NOT skip signature verification."""
        with patch.dict("os.environ", {"RETELL_DEV_BYPASS_SECRET": "dev-secret"}):
            response = self._post(headers={"HTTP_X_DEV_BYPASS": "wrong-secret"})

        # Falls through to signature check, which returns 401 (no sig header)
        self.assertEqual(response.status_code, 401)


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
