from django.db import migrations, models


def migrate_niche_fields_to_venue_facts(apps, schema_editor):
    KB = apps.get_model("restaurants", "RestaurantKnowledgeBase")
    for kb in KB.objects.all():
        facts = list(kb.venue_facts) if kb.venue_facts else []
        if kb.art_gallery_info:
            facts.append({"label": "Art gallery", "value": kb.art_gallery_info})
        if kb.cigar_policy:
            facts.append({"label": "Cigar policy", "value": kb.cigar_policy})
        if kb.show_charge_policy:
            facts.append({"label": "Show charge policy", "value": kb.show_charge_policy})
        if facts:
            kb.venue_facts = facts
            kb.save(update_fields=["venue_facts"])


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0023_kb_spoken_overrides"),
    ]

    operations = [
        # 1. Add venue_facts column
        migrations.AddField(
            model_name="restaurantknowledgebase",
            name="venue_facts",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Venue-specific facts as label/value pairs.",
            ),
        ),
        # 2. Migrate existing data from the three niche fields
        migrations.RunPython(
            migrate_niche_fields_to_venue_facts,
            reverse_code=migrations.RunPython.noop,
        ),
        # 3. Remove the three niche dedicated fields
        migrations.RemoveField(
            model_name="restaurantknowledgebase",
            name="art_gallery_info",
        ),
        migrations.RemoveField(
            model_name="restaurantknowledgebase",
            name="cigar_policy",
        ),
        migrations.RemoveField(
            model_name="restaurantknowledgebase",
            name="show_charge_policy",
        ),
    ]
