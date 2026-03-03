"""
Portal UX tests — KB lint, health score, affiliated_restaurants, view context.

Each class tests one concern:
  - KbLintTest: _kb_lint() error/warning logic
  - KbHealthScoreTest: _kb_health_score() percentage calculation
  - AffiliatedRestaurantsFieldTest: model field exists and behaves correctly
  - PortalKbViewContextTest: GET view passes `lint` in template context
  - PortalDashboardViewContextTest: GET view passes `kb_score`/`kb_missing`
"""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import Restaurant, RestaurantKnowledgeBase
from .views import _kb_health_score, _kb_lint

User = get_user_model()


# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_user(username="owner", password="testpass123"):
    return User.objects.create_user(username=username, password=password)


def make_restaurant(user=None, **kwargs):
    defaults = {
        "name": "Test Bistro",
        "contact_email": "owner@testbistro.com",
        "retell_phone_number": "+13051234567",
        "is_active": True,
        "website": "https://testbistro.com",
        "welcome_phrase": "Thank you for calling Test Bistro!",
        "address_full": "123 Main St, Miami FL",
    }
    defaults.update(kwargs)
    r = Restaurant(**defaults)
    if user:
        r.user = user
    r.save()
    return r


def make_kb(restaurant, **kwargs):
    defaults = {
        "hours_of_operation": "Mon–Sun 12pm–10pm",
        "food_menu_summary": "Best sellers include ceviche and short rib tacos.",
    }
    defaults.update(kwargs)
    kb, _ = RestaurantKnowledgeBase.objects.get_or_create(restaurant=restaurant, defaults=defaults)
    for k, v in kwargs.items():
        setattr(kb, k, v)
    kb.save()
    return kb


# ─── _kb_lint ─────────────────────────────────────────────────────────────────

class KbLintTest(TestCase):
    def test_errors_when_website_missing(self):
        r = make_restaurant(website="")
        kb = make_kb(r)
        result = _kb_lint(r, kb)
        self.assertTrue(any("Website" in e for e in result["errors"]))
        self.assertIn("basic", result["error_tabs"])

    def test_errors_when_welcome_phrase_missing(self):
        r = make_restaurant(welcome_phrase="")
        kb = make_kb(r)
        result = _kb_lint(r, kb)
        self.assertTrue(any("greeting" in e.lower() for e in result["errors"]))
        self.assertIn("basic", result["error_tabs"])

    def test_errors_when_hours_missing(self):
        r = make_restaurant()
        kb = make_kb(r, hours_of_operation="")
        result = _kb_lint(r, kb)
        self.assertTrue(any("Hours" in e for e in result["errors"]))
        self.assertIn("hours", result["error_tabs"])

    def test_no_errors_when_all_critical_fields_filled(self):
        r = make_restaurant()
        kb = make_kb(r)
        result = _kb_lint(r, kb)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["error_tabs"], [])

    def test_warning_when_food_menu_summary_too_long(self):
        r = make_restaurant()
        kb = make_kb(r, food_menu_summary="x" * 501)
        result = _kb_lint(r, kb)
        self.assertTrue(any("Food menu" in w for w in result["warnings"]))
        self.assertIn("menu", result["warning_tabs"])

    def test_warning_when_bar_menu_summary_too_long(self):
        r = make_restaurant()
        kb = make_kb(r, bar_menu_summary="x" * 501)
        result = _kb_lint(r, kb)
        self.assertTrue(any("Bar menu" in w for w in result["warnings"]))

    def test_warning_when_happy_hour_too_long(self):
        r = make_restaurant()
        kb = make_kb(r, happy_hour_details="x" * 401)
        result = _kb_lint(r, kb)
        self.assertTrue(any("Happy hour" in w for w in result["warnings"]))

    def test_warning_when_brand_voice_too_long(self):
        r = make_restaurant()
        kb = make_kb(r, brand_voice_notes="x" * 801)
        result = _kb_lint(r, kb)
        self.assertTrue(any("Brand voice" in w for w in result["warnings"]))
        self.assertIn("agent", result["warning_tabs"])

    def test_warning_when_additional_info_too_long(self):
        r = make_restaurant()
        kb = make_kb(r, additional_info="x" * 1501)
        result = _kb_lint(r, kb)
        self.assertTrue(any("Additional info" in w for w in result["warnings"]))
        self.assertIn("other", result["warning_tabs"])

    def test_no_warnings_when_all_within_limits(self):
        r = make_restaurant()
        kb = make_kb(r)
        result = _kb_lint(r, kb)
        self.assertEqual(result["warnings"], [])

    def test_lint_works_with_no_kb(self):
        r = make_restaurant(website="")
        result = _kb_lint(r, None)
        self.assertTrue(len(result["errors"]) > 0)


# ─── _kb_health_score ─────────────────────────────────────────────────────────

class KbHealthScoreTest(TestCase):
    def test_high_score_when_all_key_fields_filled(self):
        r = make_restaurant()
        make_kb(r,
            hours_of_operation="Mon–Sun 12pm–10pm",
            food_menu_url="https://testbistro.com/menu",
            food_menu_summary="Great food summary here.",
            bar_menu_summary="Great bar summary here.",
            happy_hour_details="Mon–Fri 4–7pm.",
            dietary_options="Vegan, gluten-free.",
            reservation_grace_min=15,
        )
        score, missing = _kb_health_score(r)
        self.assertGreater(score, 70)
        self.assertEqual(missing, [])

    def test_zero_score_when_all_fields_empty(self):
        r = make_restaurant(website="", welcome_phrase="", address_full="")
        make_kb(r, hours_of_operation="", food_menu_url="", food_menu_summary="")
        score, missing = _kb_health_score(r)
        self.assertEqual(score, 0)
        self.assertIn("Website URL", missing)
        self.assertIn("Opening greeting", missing)
        self.assertIn("Address", missing)

    def test_critical_missing_lists_menu_info_when_no_url_or_summary(self):
        r = make_restaurant()
        make_kb(r, food_menu_url="", food_menu_summary="")
        _, missing = _kb_health_score(r)
        self.assertIn("Menu info", missing)

    def test_no_critical_missing_when_summary_filled_but_no_url(self):
        r = make_restaurant()
        make_kb(r, food_menu_url="", food_menu_summary="Great dishes.")
        _, missing = _kb_health_score(r)
        self.assertNotIn("Menu info", missing)

    def test_score_is_percentage_between_0_and_100(self):
        r = make_restaurant()
        make_kb(r)
        score, _ = _kb_health_score(r)
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)


# ─── affiliated_restaurants field ─────────────────────────────────────────────

class AffiliatedRestaurantsFieldTest(TestCase):
    def test_field_exists_on_kb_model(self):
        r = make_restaurant()
        kb = make_kb(r)
        self.assertTrue(hasattr(kb, "affiliated_restaurants"))

    def test_field_defaults_to_empty_string(self):
        r = make_restaurant()
        kb = make_kb(r)
        self.assertEqual(kb.affiliated_restaurants, "")

    def test_field_saves_and_retrieves_correctly(self):
        r = make_restaurant()
        kb = make_kb(r, affiliated_restaurants="Cuba Ocho, Calle Dragones Colombia")
        kb.refresh_from_db()
        self.assertEqual(kb.affiliated_restaurants, "Cuba Ocho, Calle Dragones Colombia")


# ─── Portal KB view context ───────────────────────────────────────────────────

class PortalKbViewContextTest(TestCase):
    def setUp(self):
        self.user = make_user()
        self.restaurant = make_restaurant(user=self.user)
        self.kb = make_kb(self.restaurant)
        self.client = Client()
        self.client.login(username="owner", password="testpass123")
        self.url = reverse("portal_kb", kwargs={"slug": self.restaurant.slug})

    def test_get_includes_lint_in_context(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("lint", response.context)

    def test_lint_has_expected_keys(self):
        response = self.client.get(self.url)
        lint = response.context["lint"]
        self.assertIn("errors", lint)
        self.assertIn("warnings", lint)
        self.assertIn("error_tabs", lint)
        self.assertIn("warning_tabs", lint)

    def test_lint_errors_shown_when_website_missing(self):
        self.restaurant.website = ""
        self.restaurant.save()
        response = self.client.get(self.url)
        lint = response.context["lint"]
        self.assertTrue(len(lint["errors"]) > 0)

    def test_lint_no_errors_when_kb_complete(self):
        response = self.client.get(self.url)
        lint = response.context["lint"]
        self.assertEqual(lint["errors"], [])

    def test_requires_login(self):
        client = Client()
        response = client.get(self.url)
        self.assertEqual(response.status_code, 302)


# ─── Portal Dashboard view context ───────────────────────────────────────────

class PortalDashboardViewContextTest(TestCase):
    def setUp(self):
        self.user = make_user(username="dash_owner")
        self.restaurant = make_restaurant(user=self.user)
        self.kb = make_kb(self.restaurant)
        self.client = Client()
        self.client.login(username="dash_owner", password="testpass123")
        self.url = reverse("portal_dashboard", kwargs={"slug": self.restaurant.slug})

    def test_get_includes_kb_score_in_context(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("kb_score", response.context)

    def test_get_includes_kb_missing_in_context(self):
        response = self.client.get(self.url)
        self.assertIn("kb_missing", response.context)

    def test_kb_score_is_integer(self):
        response = self.client.get(self.url)
        self.assertIsInstance(response.context["kb_score"], int)

    def test_kb_missing_is_list(self):
        response = self.client.get(self.url)
        self.assertIsInstance(response.context["kb_missing"], list)

    def test_kb_missing_lists_website_when_empty(self):
        self.restaurant.website = ""
        self.restaurant.save()
        response = self.client.get(self.url)
        self.assertIn("Website URL", response.context["kb_missing"])
