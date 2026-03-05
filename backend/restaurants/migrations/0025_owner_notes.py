from django.db import migrations, models


def migrate_venue_facts_to_owner_notes(apps, schema_editor):
    KB = apps.get_model("restaurants", "RestaurantKnowledgeBase")
    for kb in KB.objects.all():
        if kb.venue_facts:
            lines = []
            for f in kb.venue_facts:
                if not isinstance(f, dict):
                    continue
                label = (f.get("label") or "").strip()
                value = (f.get("value") or "").strip()
                if label and value:
                    lines.append(f"{label}: {value}")
            if lines:
                kb.owner_notes = "\n".join(lines)
                kb.save(update_fields=["owner_notes"])


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0024_venue_facts"),
    ]

    operations = [
        migrations.AddField(
            model_name="restaurantknowledgebase",
            name="owner_notes",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Free-form notes the agent should know: gift cards, Wi-Fi password, "
                    "corkage fee, birthday policy, capacity, etc."
                ),
            ),
        ),
        migrations.RunPython(
            migrate_venue_facts_to_owner_notes,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="restaurantknowledgebase",
            name="venue_facts",
        ),
    ]
