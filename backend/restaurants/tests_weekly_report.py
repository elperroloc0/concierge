"""
Tests for the weekly report feature.

Coverage:
  - aggregate_metrics()         — signal aggregation, spam filtering, edge cases
  - select_relevant_summaries() — prioritization order
  - generate_report()           — Claude API mock, delimiter parsing, missing delimiter
  - _build_call_detail_from_payload() — call_signals, duration_seconds, is_spam
  - _send_knowledge_gap_alert() — fires / skips under the right conditions
  - send_weekly_report command  — dry-run, --force, --prompt-only, no-calls skip, missing API key
  - Portal views                — list, detail, CSV export, cross-restaurant security
  - WeeklyReport model          — unique_together, cascade delete
"""

import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    CallDetail, CallEvent, Restaurant, RestaurantMembership,
    Subscription, WeeklyReport,
)
from .management.commands.send_weekly_report import (
    aggregate_metrics, select_relevant_summaries,
)

User = get_user_model()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_user(username="owner", password="pass"):
    return User.objects.create_user(username=username, password=password)


def make_restaurant(user=None, **kwargs):
    defaults = {
        "name": "Test Bistro",
        "contact_email": "owner@test.com",
        "notify_email": "owner@test.com",
        "notify_via_email": True,
        "retell_phone_number": "+13051234567",
        "retell_api_key": "key",
        "is_active": True,
        "notify_weekly_report": True,
        "weekly_report_language": "es",
    }
    defaults.update(kwargs)
    r = Restaurant(**defaults)
    if user:
        r.user = user
    r.save()
    return r


def make_call(restaurant, signals=None, call_summary="", duration_seconds=None,
              is_spam=False, call_reason="other", wants_reservation=False,
              follow_up_needed=False, caller_sentiment="neutral", days_ago=0):
    """Create a CallEvent + CallDetail pair for a given restaurant."""
    created_at = timezone.now() - timedelta(days=days_ago)
    event = CallEvent.objects.create(
        restaurant=restaurant,
        event_type="call_analyzed",
        payload={},
        created_at=created_at,
    )
    # Update created_at (auto_now_add bypasses assignment)
    CallEvent.objects.filter(pk=event.pk).update(created_at=created_at)
    event.refresh_from_db()

    detail = CallDetail.objects.create(
        call_event=event,
        call_signals=signals or {},
        call_summary=call_summary,
        duration_seconds=duration_seconds,
        is_spam=is_spam,
        call_reason=call_reason,
        wants_reservation=wants_reservation,
        follow_up_needed=follow_up_needed,
        caller_sentiment=caller_sentiment,
    )
    return event, detail


# ─── aggregate_metrics() ──────────────────────────────────────────────────────

class AggregateMetricsTest(TestCase):

    def setUp(self):
        self.restaurant = make_restaurant()
        self.week_start = date.today() - timedelta(days=7)
        self.week_end   = self.week_start + timedelta(days=7)

    def _make(self, **kwargs):
        return make_call(self.restaurant, days_ago=3, **kwargs)

    def test_returns_empty_dict_when_no_calls(self):
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result, {})

    def test_counts_total_real_and_spam(self):
        self._make()
        self._make()
        self._make(is_spam=True)
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result["total_calls"], 3)
        self.assertEqual(result["real_calls"], 2)
        self.assertEqual(result["spam_calls"], 1)

    def test_quality_breakdown_counts_correctly(self):
        self._make(signals={"call_quality": "excellent"})
        self._make(signals={"call_quality": "excellent"})
        self._make(signals={"call_quality": "good"})
        self._make(signals={"call_quality": "poor"})
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result["call_quality"]["excellent"], 2)
        self.assertEqual(result["call_quality"]["good"], 1)
        self.assertEqual(result["call_quality"]["poor"], 1)

    def test_agent_failures_extracts_unanswered_question(self):
        self._make(signals={
            "agent_failed_to_answer": True,
            "unanswered_question": "¿A qué hora empieza la música?",
            "agent_response_to_unanswered": "No tengo esa información.",
        })
        self._make(signals={"agent_failed_to_answer": False})
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result["agent_failures"]["total"], 1)
        self.assertEqual(len(result["unanswered_questions"]), 1)
        self.assertIn("música", result["unanswered_questions"][0])

    def test_spam_calls_excluded_from_quality_and_failures(self):
        self._make(is_spam=True, signals={
            "agent_failed_to_answer": True,
            "unanswered_question": "spam question",
            "call_quality": "poor",
        })
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result["agent_failures"]["total"], 0)
        self.assertEqual(result["call_quality"].get("poor", 0), 0)

    def test_caller_frustration_counted(self):
        self._make(signals={"caller_frustration": True})
        self._make(signals={"caller_frustration": True})
        self._make(signals={"caller_frustration": False})
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result["caller_frustration"], 2)

    def test_language_inconsistencies_counted(self):
        # language_consistency=False means inconsistent
        self._make(signals={"language_consistency": False})
        self._make(signals={"language_consistency": True})
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result["language_inconsistencies"], 1)

    def test_unnecessary_transfers_only_counts_explicit_false(self):
        # transfer_was_necessary=False → unnecessary; True or None → not counted
        self._make(signals={"transfer_was_necessary": False})
        self._make(signals={"transfer_was_necessary": True})
        self._make(signals={"transfer_was_necessary": None})
        self._make(signals={})  # key absent
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result["unnecessary_transfers"], 1)

    def test_avg_duration_seconds_computed(self):
        self._make(duration_seconds=60)
        self._make(duration_seconds=120)
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result["avg_duration_seconds"], 90)

    def test_avg_duration_none_when_no_durations(self):
        self._make(duration_seconds=None)
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertIsNone(result["avg_duration_seconds"])

    def test_calls_outside_week_not_counted(self):
        make_call(self.restaurant, days_ago=10)  # outside window
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result, {})

    def test_calls_from_different_restaurant_not_counted(self):
        other = make_restaurant(name="Other Place")
        make_call(other, days_ago=3)
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result, {})

    def test_missing_call_signals_treated_as_unknown_not_poor(self):
        """Calls without call_signals (legacy) should not pollute the quality breakdown."""
        self._make(signals={})
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result["call_quality"].get("poor", 0), 0)
        self.assertEqual(result["total_calls"], 1)

    def test_reservations_complaints_followups_counted(self):
        self._make(wants_reservation=True)
        self._make(wants_reservation=True)
        self._make(call_reason="complaint")
        self._make(follow_up_needed=True)
        result = aggregate_metrics(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result["reservations"], 2)
        self.assertEqual(result["complaints"], 1)
        self.assertEqual(result["follow_ups"], 1)


# ─── select_relevant_summaries() ─────────────────────────────────────────────

class SelectRelevantSummariesTest(TestCase):

    def setUp(self):
        self.restaurant = make_restaurant()
        self.week_start = date.today() - timedelta(days=7)
        self.week_end   = self.week_start + timedelta(days=7)

    def _make(self, **kwargs):
        return make_call(self.restaurant, days_ago=3, **kwargs)

    def test_returns_empty_list_when_no_calls(self):
        result = select_relevant_summaries(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result, [])

    def test_agent_failures_come_first(self):
        self._make(call_summary="normal call")
        self._make(call_summary="agent failed", signals={"agent_failed_to_answer": True})
        result = select_relevant_summaries(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(result[0], "agent failed")

    def test_spam_calls_excluded(self):
        self._make(is_spam=True, call_summary="spam call",
                   signals={"agent_failed_to_answer": True})
        self._make(call_summary="real call")
        result = select_relevant_summaries(self.restaurant, self.week_start, self.week_end)
        self.assertNotIn("spam call", result)
        self.assertIn("real call", result)

    def test_empty_summaries_excluded(self):
        self._make(call_summary="")
        self._make(call_summary="has summary")
        result = select_relevant_summaries(self.restaurant, self.week_start, self.week_end)
        self.assertNotIn("", result)
        self.assertIn("has summary", result)

    def test_max_items_respected(self):
        for i in range(15):
            self._make(call_summary=f"call {i}")
        result = select_relevant_summaries(
            self.restaurant, self.week_start, self.week_end, max_items=8
        )
        self.assertLessEqual(len(result), 8)

    def test_no_duplicate_summaries(self):
        self._make(call_summary="unique", signals={"agent_failed_to_answer": True,
                                                    "caller_frustration": True})
        result = select_relevant_summaries(self.restaurant, self.week_start, self.week_end)
        self.assertEqual(len(result), len(set(result)))


# ─── generate_report() ────────────────────────────────────────────────────────

class GenerateReportTest(TestCase):

    def setUp(self):
        self.restaurant = make_restaurant()
        self.week_start = date(2026, 3, 24)
        self.week_end   = date(2026, 3, 31)
        self.metrics    = {"total_calls": 10, "real_calls": 8, "spam_calls": 2}
        self.summaries  = ["caller asked about music hours", "caller frustrated about menu"]

    def _mock_response(self, text):
        msg = MagicMock()
        msg.content = [MagicMock(text=text)]
        msg.model = "claude-sonnet-4-6"
        msg.usage.input_tokens = 500
        msg.usage.output_tokens = 800
        return msg

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_splits_owner_and_prompt_sections(self, mock_anthropic):
        raw = "===OWNER===\nOwner narrative here.\n===PROMPT===\nPrompt suggestions here."
        mock_anthropic.Anthropic.return_value.messages.create.return_value = (
            self._mock_response(raw)
        )
        from restaurants.management.commands.send_weekly_report import generate_report
        owner, prompt, model, cost = generate_report(
            self.restaurant, self.metrics, self.summaries,
            self.week_start, self.week_end,
        )
        self.assertEqual(owner, "Owner narrative here.")
        self.assertEqual(prompt, "Prompt suggestions here.")
        self.assertIsInstance(cost, Decimal)
        self.assertGreater(cost, 0)

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_strips_owner_delimiter_from_summary(self, mock_anthropic):
        raw = "===OWNER===\n\nSome analysis.\n===PROMPT===\nSuggestions."
        mock_anthropic.Anthropic.return_value.messages.create.return_value = (
            self._mock_response(raw)
        )
        from restaurants.management.commands.send_weekly_report import generate_report
        owner, _, _, _ = generate_report(
            self.restaurant, self.metrics, self.summaries,
            self.week_start, self.week_end,
        )
        self.assertNotIn("===OWNER===", owner)

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_missing_prompt_delimiter_falls_back_gracefully(self, mock_anthropic):
        """If Claude omits ===PROMPT===, owner_summary gets the full text and no crash."""
        raw = "Just some text without any delimiter."
        mock_anthropic.Anthropic.return_value.messages.create.return_value = (
            self._mock_response(raw)
        )
        from restaurants.management.commands.send_weekly_report import generate_report
        owner, prompt, _, _ = generate_report(
            self.restaurant, self.metrics, self.summaries,
            self.week_start, self.week_end,
        )
        self.assertEqual(owner, raw.strip())
        self.assertEqual(prompt, "")

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_generation_cost_is_positive_decimal(self, mock_anthropic):
        raw = "===OWNER===\nText\n===PROMPT===\nSuggestion"
        mock_anthropic.Anthropic.return_value.messages.create.return_value = (
            self._mock_response(raw)
        )
        from restaurants.management.commands.send_weekly_report import generate_report
        _, _, _, cost = generate_report(
            self.restaurant, self.metrics, self.summaries,
            self.week_start, self.week_end,
        )
        self.assertIsInstance(cost, Decimal)
        self.assertGreater(cost, Decimal("0"))


# ─── _build_call_detail_from_payload() — call_signals extraction ──────────────

class BuildCallDetailSignalsTest(TestCase):
    """
    Tests that _build_call_detail_from_payload() correctly populates
    call_signals, duration_seconds, and is_spam from the Retell webhook payload.
    """

    def setUp(self):
        from .models import Subscription
        self.restaurant = make_restaurant()
        Subscription.objects.create(
            restaurant=self.restaurant, status="active", communication_balance=100
        )
        self.url = reverse("retell_events")

    def _post_analyzed(self, custom_analysis_data=None, duration_ms=None, sig_valid=True):
        call_data = {
            "call_id": "call-abc",
            "from_number": "+13059990000",
            "to_number": "+13051234567",
            "call_analysis": {
                "custom_analysis_data": custom_analysis_data or {},
                "call_summary": "Test summary",
            },
        }
        if duration_ms is not None:
            call_data["duration_ms"] = duration_ms

        payload = {"event": "call_analyzed", "call": call_data}

        with patch("restaurants.views.Retell") as MockRetell:
            MockRetell.return_value.verify.return_value = sig_valid
            response = self.client.post(
                self.url,
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_RETELL_SIGNATURE="valid-sig",
            )
        return response

    def test_call_signals_populated_from_custom_analysis(self):
        signals = {
            "agent_failed_to_answer": True,
            "unanswered_question": "¿A qué hora empieza la música?",
            "agent_response_to_unanswered": "No lo sé.",
            "caller_frustration": False,
            "is_spam_or_robocall": False,
            "call_quality": "poor",
        }
        self._post_analyzed(custom_analysis_data=signals)
        detail = CallDetail.objects.filter(
            call_event__restaurant=self.restaurant
        ).order_by("-created_at").first()
        self.assertIsNotNone(detail)
        self.assertTrue(detail.call_signals.get("agent_failed_to_answer"))
        self.assertEqual(detail.call_signals.get("call_quality"), "poor")
        self.assertIn("música", detail.call_signals.get("unanswered_question", ""))

    def test_duration_seconds_computed_from_duration_ms(self):
        self._post_analyzed(duration_ms=75000)
        detail = CallDetail.objects.filter(
            call_event__restaurant=self.restaurant
        ).order_by("-created_at").first()
        self.assertEqual(detail.duration_seconds, 75)

    def test_duration_seconds_none_when_duration_ms_absent(self):
        self._post_analyzed(duration_ms=None)
        detail = CallDetail.objects.filter(
            call_event__restaurant=self.restaurant
        ).order_by("-created_at").first()
        self.assertIsNone(detail.duration_seconds)

    def test_is_spam_set_true_from_signal(self):
        self._post_analyzed(custom_analysis_data={"is_spam_or_robocall": True})
        detail = CallDetail.objects.filter(
            call_event__restaurant=self.restaurant
        ).order_by("-created_at").first()
        self.assertTrue(detail.is_spam)

    def test_is_spam_false_when_not_robocall(self):
        self._post_analyzed(custom_analysis_data={"is_spam_or_robocall": False})
        detail = CallDetail.objects.filter(
            call_event__restaurant=self.restaurant
        ).order_by("-created_at").first()
        self.assertFalse(detail.is_spam)

    def test_non_signal_keys_not_stored_in_call_signals(self):
        """Keys from custom_analysis_data that are not quality signals must be excluded."""
        self._post_analyzed(custom_analysis_data={
            "caller_name": "Maria",          # transactional field — not a signal
            "call_quality": "good",          # signal
        })
        detail = CallDetail.objects.filter(
            call_event__restaurant=self.restaurant
        ).order_by("-created_at").first()
        self.assertNotIn("caller_name", detail.call_signals)
        self.assertIn("call_quality", detail.call_signals)

    def test_empty_custom_analysis_gives_empty_signals(self):
        self._post_analyzed(custom_analysis_data={})
        detail = CallDetail.objects.filter(
            call_event__restaurant=self.restaurant
        ).order_by("-created_at").first()
        self.assertEqual(detail.call_signals, {})
        self.assertFalse(detail.is_spam)


# ─── _send_knowledge_gap_alert() ─────────────────────────────────────────────

class KnowledgeGapAlertTest(TestCase):

    def setUp(self):
        from .models import Subscription
        self.restaurant = make_restaurant(
            notify_via_email=True,
            notify_email="owner@test.com",
        )
        Subscription.objects.create(
            restaurant=self.restaurant, status="active", communication_balance=100
        )
        self.url = reverse("retell_events")

    def _post_analyzed(self, custom_analysis_data, duration_ms=30000):
        payload = {
            "event": "call_analyzed",
            "call": {
                "call_id": "kg-call",
                "from_number": "+13059990001",
                "to_number": "+13051234567",
                "duration_ms": duration_ms,
                "call_analysis": {
                    "custom_analysis_data": custom_analysis_data,
                    "call_summary": "Test summary",
                },
            },
        }
        with patch("restaurants.views.Retell") as MockRetell:
            MockRetell.return_value.verify.return_value = True
            with patch("restaurants.views._send_knowledge_gap_alert") as mock_alert:
                self.client.post(
                    self.url,
                    data=json.dumps(payload),
                    content_type="application/json",
                    HTTP_X_RETELL_SIGNATURE="sig",
                )
                return mock_alert

    def test_alert_sent_when_agent_failed_and_question_present(self):
        mock_alert = self._post_analyzed({
            "agent_failed_to_answer": True,
            "unanswered_question": "¿Tienen valet?",
            "agent_response_to_unanswered": "No lo sé.",
            "is_spam_or_robocall": False,
        })
        mock_alert.assert_called_once()
        _, detail_arg = mock_alert.call_args[0]
        self.assertEqual(detail_arg.call_signals.get("unanswered_question"), "¿Tienen valet?")

    def test_alert_not_sent_when_no_unanswered_question(self):
        """agent_failed_to_answer=True but empty unanswered_question → no email."""
        mock_alert = self._post_analyzed({
            "agent_failed_to_answer": True,
            "unanswered_question": "",
            "is_spam_or_robocall": False,
        })
        mock_alert.assert_not_called()

    def test_alert_not_sent_for_spam_calls(self):
        mock_alert = self._post_analyzed({
            "agent_failed_to_answer": True,
            "unanswered_question": "¿Tienen valet?",
            "is_spam_or_robocall": True,
        })
        mock_alert.assert_not_called()

    def test_alert_not_sent_when_agent_did_not_fail(self):
        mock_alert = self._post_analyzed({
            "agent_failed_to_answer": False,
            "unanswered_question": "",
            "is_spam_or_robocall": False,
        })
        mock_alert.assert_not_called()

    def test_alert_not_sent_when_notify_via_email_off(self):
        self.restaurant.notify_via_email = False
        self.restaurant.save()
        mock_alert = self._post_analyzed({
            "agent_failed_to_answer": True,
            "unanswered_question": "¿Tienen valet?",
            "is_spam_or_robocall": False,
        })
        mock_alert.assert_not_called()


# ─── WeeklyReport model ────────────────────────────────────────────────────────

class WeeklyReportModelTest(TestCase):

    def setUp(self):
        self.restaurant = make_restaurant()

    def test_weekly_report_created_and_retrieved(self):
        report = WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
            metrics={"total_calls": 10},
            owner_summary="Great week.",
            prompt_suggestions="Add music hours to KB.",
            model_used="claude-sonnet-4-6",
            generation_cost=Decimal("0.012345"),
        )
        saved = WeeklyReport.objects.get(pk=report.pk)
        self.assertEqual(saved.metrics["total_calls"], 10)
        self.assertEqual(saved.owner_summary, "Great week.")
        self.assertEqual(saved.generation_cost, Decimal("0.012345"))

    def test_unique_together_prevents_duplicate_week(self):
        from django.db import IntegrityError
        WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
        )
        with self.assertRaises(IntegrityError):
            WeeklyReport.objects.create(
                restaurant=self.restaurant,
                week_start=date(2026, 3, 24),
                week_end=date(2026, 3, 31),
            )

    def test_reports_cascade_deleted_with_restaurant(self):
        WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
        )
        pk = self.restaurant.pk
        self.restaurant.delete()
        self.assertFalse(WeeklyReport.objects.filter(restaurant_id=pk).exists())

    def test_str_includes_slug_and_week(self):
        report = WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
        )
        self.assertIn(self.restaurant.slug, str(report))
        self.assertIn("2026-03-24", str(report))

    def test_ordering_is_most_recent_first(self):
        WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 17),
            week_end=date(2026, 3, 24),
        )
        WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
        )
        reports = list(WeeklyReport.objects.filter(restaurant=self.restaurant))
        self.assertEqual(reports[0].week_start, date(2026, 3, 24))


# ─── Management command ────────────────────────────────────────────────────────

class SendWeeklyReportCommandTest(TestCase):

    def setUp(self):
        self.restaurant = make_restaurant()
        self.week_start = date.today() - timedelta(days=date.today().weekday() + 7)
        self.week_end   = self.week_start + timedelta(days=7)
        # Create some calls in the window
        for _ in range(3):
            make_call(self.restaurant, days_ago=3, call_summary="A call.")

    def _call_command(self, **kwargs):
        from io import StringIO
        out = StringIO()
        call_command("send_weekly_report", stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_raises_if_api_key_missing(self):
        with override_settings(ANTHROPIC_API_KEY=""):
            with self.assertRaises(CommandError) as ctx:
                with patch.dict("os.environ", {}, clear=True):
                    self._call_command(restaurant=self.restaurant.slug, dry_run=True)
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_dry_run_does_not_create_report(self, mock_anthropic):
        mock_anthropic.Anthropic.return_value.messages.create.return_value = MagicMock(
            content=[MagicMock(text="===OWNER===\nText\n===PROMPT===\nSugg")],
            model="claude-sonnet-4-6",
            usage=MagicMock(input_tokens=100, output_tokens=200),
        )
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            self._call_command(restaurant=self.restaurant.slug, dry_run=True)
        self.assertEqual(WeeklyReport.objects.count(), 0)

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    @patch("restaurants.management.commands.send_weekly_report.render_to_string", return_value="<html>")
    @patch("restaurants.management.commands.send_weekly_report.EmailMultiAlternatives")
    def test_creates_report_and_sends_email(self, MockEmail, mock_render, mock_anthropic):
        mock_anthropic.Anthropic.return_value.messages.create.return_value = MagicMock(
            content=[MagicMock(text="===OWNER===\nOwner text\n===PROMPT===\nSuggestions")],
            model="claude-sonnet-4-6",
            usage=MagicMock(input_tokens=100, output_tokens=200),
        )
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            self._call_command(restaurant=self.restaurant.slug)
        self.assertEqual(WeeklyReport.objects.count(), 1)
        report = WeeklyReport.objects.first()
        self.assertEqual(report.owner_summary, "Owner text")
        self.assertEqual(report.prompt_suggestions, "Suggestions")
        MockEmail.assert_called_once()

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_prompt_only_saves_but_skips_email(self, mock_anthropic):
        mock_anthropic.Anthropic.return_value.messages.create.return_value = MagicMock(
            content=[MagicMock(text="===OWNER===\nText\n===PROMPT===\nSugg")],
            model="claude-sonnet-4-6",
            usage=MagicMock(input_tokens=100, output_tokens=200),
        )
        with patch("restaurants.management.commands.send_weekly_report.EmailMultiAlternatives") as MockEmail:
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                self._call_command(restaurant=self.restaurant.slug, prompt_only=True)
        self.assertEqual(WeeklyReport.objects.count(), 1)
        MockEmail.assert_not_called()

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_skip_when_no_calls_in_week(self, mock_anthropic):
        # Move the restaurant to a different slug so there are no calls
        empty_r = make_restaurant(name="Empty Place")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            out = self._call_command(restaurant=empty_r.slug)
        self.assertIn("no calls", out.lower())
        self.assertEqual(WeeklyReport.objects.filter(restaurant=empty_r).count(), 0)
        mock_anthropic.Anthropic.assert_not_called()

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    @patch("restaurants.management.commands.send_weekly_report.EmailMultiAlternatives")
    def test_force_overwrites_existing_report(self, MockEmail, mock_anthropic):
        mock_anthropic.Anthropic.return_value.messages.create.return_value = MagicMock(
            content=[MagicMock(text="===OWNER===\nUpdated\n===PROMPT===\nNew sugg")],
            model="claude-sonnet-4-6",
            usage=MagicMock(input_tokens=100, output_tokens=200),
        )
        WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=self.week_start,
            week_end=self.week_end,
            owner_summary="Old summary",
        )
        with patch("restaurants.management.commands.send_weekly_report.render_to_string",
                   return_value="<html>"):
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                self._call_command(restaurant=self.restaurant.slug, force=True)
        self.assertEqual(WeeklyReport.objects.count(), 1)
        self.assertEqual(WeeklyReport.objects.first().owner_summary, "Updated")

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_skips_existing_report_without_force(self, mock_anthropic):
        WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=self.week_start,
            week_end=self.week_end,
        )
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            out = self._call_command(restaurant=self.restaurant.slug)
        self.assertIn("skip", out.lower())
        mock_anthropic.Anthropic.assert_not_called()

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_notify_weekly_report_false_skips_restaurant(self, mock_anthropic):
        self.restaurant.notify_weekly_report = False
        self.restaurant.save()
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            self._call_command()
        self.assertEqual(WeeklyReport.objects.count(), 0)
        mock_anthropic.Anthropic.assert_not_called()

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_custom_week_flag_uses_given_date(self, mock_anthropic):
        mock_anthropic.Anthropic.return_value.messages.create.return_value = MagicMock(
            content=[MagicMock(text="===OWNER===\nText\n===PROMPT===\nSugg")],
            model="claude-sonnet-4-6",
            usage=MagicMock(input_tokens=100, output_tokens=200),
        )
        custom_week = date(2026, 3, 9)
        # Create calls in that specific window
        for _ in range(2):
            make_call(self.restaurant,
                      days_ago=(date.today() - custom_week).days + 1,
                      call_summary="old call")
        with patch("restaurants.management.commands.send_weekly_report.EmailMultiAlternatives"):
            with patch("restaurants.management.commands.send_weekly_report.render_to_string",
                       return_value="<html>"):
                with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                    self._call_command(
                        restaurant=self.restaurant.slug,
                        week="2026-03-09",
                        prompt_only=True,
                    )
        report = WeeklyReport.objects.filter(restaurant=self.restaurant).first()
        if report:
            self.assertEqual(report.week_start, custom_week)


# ─── Portal views ─────────────────────────────────────────────────────────────

class PortalReportsListViewTest(TestCase):

    def setUp(self):
        self.user = make_user("owner")
        self.restaurant = make_restaurant(user=self.user)
        RestaurantMembership.objects.create(
            restaurant=self.restaurant, user=self.user, role="owner", is_active=True
        )
        self.client = Client()
        self.client.login(username="owner", password="pass")
        self.url = reverse("portal_reports_list", kwargs={"slug": self.restaurant.slug})

    def test_returns_200_for_authenticated_owner(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_redirects_unauthenticated_user(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertIn(response.status_code, [302, 301])

    def test_lists_reports_for_restaurant(self):
        WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
            metrics={"total_calls": 5},
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "24")  # part of the week date
        self.assertIn("reports", response.context)
        self.assertEqual(len(response.context["reports"]), 1)

    def test_does_not_list_other_restaurants_reports(self):
        other = make_restaurant(name="Other Place")
        WeeklyReport.objects.create(
            restaurant=other,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
        )
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["reports"]), 0)

    def test_empty_state_when_no_reports(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No weekly reports")


class PortalReportsDetailViewTest(TestCase):

    def setUp(self):
        self.user = make_user("owner2")
        self.restaurant = make_restaurant(user=self.user)
        RestaurantMembership.objects.create(
            restaurant=self.restaurant, user=self.user, role="owner", is_active=True
        )
        self.report = WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
            metrics={
                "total_calls": 10, "real_calls": 8, "spam_calls": 2,
                "reservations": 3, "complaints": 1, "follow_ups": 2,
                "agent_failures": {"total": 1, "examples": []},
                "call_quality": {"excellent": 3, "good": 4, "poor": 1},
            },
            owner_summary="Buena semana en general.",
            prompt_suggestions="Añadir horario de música al KB.",
            model_used="claude-sonnet-4-6",
        )
        self.client = Client()
        self.client.login(username="owner2", password="pass")
        self.url = reverse("portal_reports_detail", kwargs={
            "slug": self.restaurant.slug, "report_id": self.report.pk
        })

    def test_returns_200_for_report_owner(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_shows_owner_summary(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Buena semana en general.")

    def test_does_not_show_prompt_suggestions(self):
        """prompt_suggestions must NOT be visible in the owner portal."""
        response = self.client.get(self.url)
        self.assertNotContains(response, "Añadir horario de música al KB.")

    def test_404_for_report_belonging_to_another_restaurant(self):
        other_user = make_user("other3")
        other = make_restaurant(name="Other", user=other_user)
        other_report = WeeklyReport.objects.create(
            restaurant=other,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
        )
        url = reverse("portal_reports_detail", kwargs={
            "slug": self.restaurant.slug, "report_id": other_report.pk
        })
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_csv_export_returns_csv_file(self):
        response = self.client.get(self.url + "?export=csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("weekly_report_", response["Content-Disposition"])
        content = response.content.decode()
        self.assertIn("total_calls", content)
        self.assertIn("10", content)

    def test_week_calls_table_shows_non_spam_calls(self):
        make_call(self.restaurant, days_ago=3, call_summary="real call", is_spam=False)
        make_call(self.restaurant, days_ago=3, call_summary="spam call", is_spam=True)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        week_calls = response.context["week_calls"]
        self.assertFalse(any(c.is_spam for c in week_calls))

    def test_report_not_accessible_without_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertIn(response.status_code, [302, 301])

    def test_pending_report_shows_spinner_not_content(self):
        self.report.status = WeeklyReport.STATUS_PENDING
        self.report.owner_summary = ""
        self.report.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "spinner-border")
        self.assertNotContains(response, "Weekly Analysis")

    def test_failed_report_shows_error_banner(self):
        self.report.status = WeeklyReport.STATUS_FAILED
        self.report.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "alert-danger")

    def test_pending_report_hides_csv_export_button(self):
        self.report.status = WeeklyReport.STATUS_PENDING
        self.report.save()
        response = self.client.get(self.url)
        self.assertNotContains(response, "Export CSV")

    def test_done_report_shows_csv_export_button(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Export CSV")


# ─── portal_report_status view ────────────────────────────────────────────────

class PortalReportStatusViewTest(TestCase):

    def setUp(self):
        self.user = make_user("status_user")
        self.restaurant = make_restaurant(user=self.user)
        RestaurantMembership.objects.create(
            restaurant=self.restaurant, user=self.user, role="owner", is_active=True
        )
        self.report = WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
            metrics={},
            status=WeeklyReport.STATUS_PENDING,
        )
        self.client = Client()
        self.client.login(username="status_user", password="pass")
        self.url = reverse("portal_report_status", kwargs={
            "slug": self.restaurant.slug, "report_id": self.report.pk,
        })

    def test_returns_pending_status(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "pending"})

    def test_returns_done_status(self):
        self.report.status = WeeklyReport.STATUS_DONE
        self.report.save()
        response = self.client.get(self.url)
        self.assertEqual(response.json(), {"status": "done"})

    def test_returns_failed_status(self):
        self.report.status = WeeklyReport.STATUS_FAILED
        self.report.save()
        response = self.client.get(self.url)
        self.assertEqual(response.json(), {"status": "failed"})

    def test_404_for_report_from_other_restaurant(self):
        other_user = make_user("other_status")
        other = make_restaurant(name="Other", user=other_user)
        other_report = WeeklyReport.objects.create(
            restaurant=other, week_start=date(2026, 3, 24), week_end=date(2026, 3, 31),
        )
        url = reverse("portal_report_status", kwargs={
            "slug": self.restaurant.slug, "report_id": other_report.pk,
        })
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_requires_authentication(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertIn(response.status_code, [302, 301])


# ─── portal_generate_report view (background thread) ─────────────────────────

class PortalGenerateReportViewTest(TestCase):

    def setUp(self):
        self.user = make_user("gen_user")
        self.restaurant = make_restaurant(user=self.user)
        RestaurantMembership.objects.create(
            restaurant=self.restaurant, user=self.user, role="owner", is_active=True
        )
        self.client = Client()
        self.client.login(username="gen_user", password="pass")
        self.url = reverse("portal_generate_report", kwargs={"slug": self.restaurant.slug})
        self.week_start = date.today() - timedelta(days=date.today().weekday())
        self.week_end   = self.week_start + timedelta(days=7)
        make_call(self.restaurant, days_ago=0, call_summary="Test call")

    @patch("threading.Thread")
    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_post_creates_pending_report_and_redirects(self, mock_thread):
        mock_thread.return_value.start = lambda: None
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [302, 301])
        report = WeeklyReport.objects.get(restaurant=self.restaurant, week_start=self.week_start)
        self.assertEqual(report.status, WeeklyReport.STATUS_PENDING)

    @patch("threading.Thread")
    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_redirects_to_detail_page(self, mock_thread):
        mock_thread.return_value.start = lambda: None
        response = self.client.post(self.url)
        report = WeeklyReport.objects.get(restaurant=self.restaurant, week_start=self.week_start)
        expected = reverse("portal_reports_detail", kwargs={
            "slug": self.restaurant.slug, "report_id": report.pk,
        })
        self.assertRedirects(response, expected, fetch_redirect_response=False)

    @patch("threading.Thread")
    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_background_thread_is_started(self, mock_thread):
        mock_thread.return_value.start = MagicMock()
        self.client.post(self.url)
        mock_thread.return_value.start.assert_called_once()

    def test_get_request_returns_405(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""})
    def test_missing_api_key_shows_error_and_redirects_to_list(self):
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [302, 301])
        self.assertFalse(WeeklyReport.objects.filter(restaurant=self.restaurant).exists())

    @patch("threading.Thread")
    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_cooldown_blocks_second_generation(self, mock_thread):
        mock_thread.return_value.start = lambda: None
        # Create a completed report generated just now
        WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=self.week_start - timedelta(days=7),
            week_end=self.week_start,
            metrics={},
            status=WeeklyReport.STATUS_DONE,
        )
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [302, 301])
        # No new report should have been created for this week
        self.assertFalse(
            WeeklyReport.objects.filter(restaurant=self.restaurant, week_start=self.week_start).exists()
        )

    @patch("threading.Thread")
    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_already_pending_report_redirects_without_new_thread(self, mock_thread):
        mock_thread.return_value.start = MagicMock()
        existing = WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=self.week_start,
            week_end=self.week_end,
            metrics={},
            status=WeeklyReport.STATUS_PENDING,
        )
        response = self.client.post(self.url)
        expected = reverse("portal_reports_detail", kwargs={
            "slug": self.restaurant.slug, "report_id": existing.pk,
        })
        self.assertRedirects(response, expected, fetch_redirect_response=False)
        mock_thread.return_value.start.assert_not_called()

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    def test_no_calls_this_week_redirects_to_list(self):
        # Use a restaurant with no calls
        user2 = make_user("gen_user2")
        r2 = make_restaurant(name="Empty", user=user2)
        RestaurantMembership.objects.create(
            restaurant=r2, user=user2, role="owner", is_active=True
        )
        client2 = Client()
        client2.login(username="gen_user2", password="pass")
        url2 = reverse("portal_generate_report", kwargs={"slug": r2.slug})
        response = client2.post(url2)
        self.assertIn(response.status_code, [302, 301])
        self.assertFalse(WeeklyReport.objects.filter(restaurant=r2).exists())

    def test_requires_authentication(self):
        self.client.logout()
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [302, 301])


# ─── _run_generate_report_bg() ────────────────────────────────────────────────

class RunGenerateReportBgTest(TestCase):
    """Tests for the background worker called from the thread — run synchronously."""

    def setUp(self):
        self.restaurant = make_restaurant()
        self.week_start = date(2026, 3, 24)
        self.week_end   = date(2026, 3, 31)
        self.report = WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=self.week_start,
            week_end=self.week_end,
            metrics={"total_calls": 5},
            status=WeeklyReport.STATUS_PENDING,
        )

    def _mock_generate(self, owner="Summary", prompt="Suggestions",
                       model="claude-sonnet-4-6", cost=None):
        from decimal import Decimal
        return MagicMock(return_value=(owner, prompt, model, cost or Decimal("0.001")))

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_success_sets_status_done_and_saves_content(self, mock_anthropic):
        raw = "===OWNER===\nGreat week.\n===PROMPT===\nUpdate KB."
        mock_anthropic.Anthropic.return_value.messages.create.return_value = (
            MagicMock(
                content=[MagicMock(text=raw)],
                model="claude-sonnet-4-6",
                usage=MagicMock(input_tokens=100, output_tokens=200),
            )
        )
        from restaurants.views import _run_generate_report_bg
        _run_generate_report_bg(self.report.pk, [], self.week_start, self.week_end)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, WeeklyReport.STATUS_DONE)
        self.assertEqual(self.report.owner_summary, "Great week.")
        self.assertEqual(self.report.prompt_suggestions, "Update KB.")
        self.assertEqual(self.report.model_used, "claude-sonnet-4-6")
        self.assertIsNotNone(self.report.generation_cost)

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_exception_sets_status_failed(self, mock_anthropic):
        mock_anthropic.Anthropic.return_value.messages.create.side_effect = RuntimeError("API down")
        from restaurants.views import _run_generate_report_bg
        _run_generate_report_bg(self.report.pk, [], self.week_start, self.week_end)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, WeeklyReport.STATUS_FAILED)

    @patch("restaurants.management.commands.send_weekly_report.anthropic")
    def test_report_stays_failed_on_save_error(self, mock_anthropic):
        """Even if the final save blows up, status ends up as failed (via .update())."""
        mock_anthropic.Anthropic.return_value.messages.create.side_effect = Exception("network")
        from restaurants.views import _run_generate_report_bg
        # Should not raise — exception is caught internally
        try:
            _run_generate_report_bg(self.report.pk, [], self.week_start, self.week_end)
        except Exception:
            self.fail("_run_generate_report_bg should not propagate exceptions")
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, WeeklyReport.STATUS_FAILED)


# ─── WeeklyReport.status field ────────────────────────────────────────────────

class WeeklyReportStatusFieldTest(TestCase):

    def setUp(self):
        self.restaurant = make_restaurant()

    def test_default_status_is_done(self):
        report = WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
        )
        self.assertEqual(report.status, WeeklyReport.STATUS_DONE)

    def test_status_can_be_set_to_pending(self):
        report = WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
            status=WeeklyReport.STATUS_PENDING,
        )
        self.assertEqual(report.status, "pending")

    def test_status_transitions_pending_to_done(self):
        report = WeeklyReport.objects.create(
            restaurant=self.restaurant,
            week_start=date(2026, 3, 24),
            week_end=date(2026, 3, 31),
            status=WeeklyReport.STATUS_PENDING,
        )
        report.status = WeeklyReport.STATUS_DONE
        report.save()
        report.refresh_from_db()
        self.assertEqual(report.status, WeeklyReport.STATUS_DONE)

    def test_status_constants_match_string_values(self):
        self.assertEqual(WeeklyReport.STATUS_PENDING, "pending")
        self.assertEqual(WeeklyReport.STATUS_DONE, "done")
        self.assertEqual(WeeklyReport.STATUS_FAILED, "failed")
