"""
Management command: force-push current tool list (including transfer_to_human)
and updated LLM prompt to Retell for all configured restaurants.

Usage:
    python manage.py sync_retell_tools
    python manage.py sync_retell_tools --slug calle-dragones
"""
from django.core.management.base import BaseCommand
from django.conf import settings

from restaurants.models import Restaurant
from restaurants.services.retell_client import RetellClient
from restaurants.services.retell_tools import build_tool_list


class Command(BaseCommand):
    help = "Force-sync Retell LLM tools and prompt for all (or one) restaurant(s)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--slug", type=str, default=None,
            help="Limit sync to a single restaurant slug.",
        )

    def handle(self, *args, **options):
        from restaurants.admin import _build_agent_prompt  # local import to avoid circular

        base_url = settings.RETELL_WEBHOOK_BASE_URL
        if not base_url:
            self.stderr.write(self.style.ERROR("RETELL_WEBHOOK_BASE_URL is not set."))
            return

        qs = Restaurant.objects.filter(is_active=True)
        if options["slug"]:
            qs = qs.filter(slug=options["slug"])

        for r in qs:
            if not r.retell_api_key:
                self.stdout.write(self.style.WARNING(f"[{r.slug}] No API key — skipping."))
                continue
            if not r.retell_llm_id:
                self.stdout.write(self.style.WARNING(f"[{r.slug}] No LLM ID — skipping."))
                continue

            try:
                kb = r.knowledge_base
            except Exception:
                self.stdout.write(self.style.WARNING(f"[{r.slug}] No knowledge base — skipping."))
                continue

            escalation_number = kb.escalation_transfer_number if kb.escalation_enabled else None
            tools = build_tool_list(
                base_url,
                escalation_number=escalation_number,
                enable_sms=r.enable_sms,
            )
            prompt = _build_agent_prompt(r)

            try:
                client = RetellClient(api_key=r.retell_api_key)
                llm_result = client.update_llm(
                    r.retell_llm_id,
                    general_tools=tools,
                    general_prompt=prompt,
                )
                if r.retell_agent_id:
                    client.point_agent_to_llm_version(
                        r.retell_agent_id, r.retell_llm_id, llm_result.version
                    )
                    published_version = client.publish_agent(r.retell_agent_id)
                    if r.retell_phone_number:
                        client.pin_phone_to_agent_version(
                            r.retell_phone_number,
                            r.retell_agent_id,
                            published_version,
                        )
                tool_names = [t.get("name", "?") for t in tools]
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[{r.slug}] ✅ Synced — tools: {tool_names} | "
                        f"transfer: {'ON → ' + escalation_number if escalation_number else 'OFF'}"
                    )
                )
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"[{r.slug}] ❌ Failed: {exc}"))

