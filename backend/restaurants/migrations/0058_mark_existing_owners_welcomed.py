from django.db import migrations


def mark_owners_welcomed(apps, schema_editor):
    """Existing owners don't need the welcome banner — mark them as already welcomed."""
    RestaurantMembership = apps.get_model("restaurants", "RestaurantMembership")
    RestaurantMembership.objects.filter(role="owner").update(welcomed=True)


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0057_add_operator_notify_prefs"),
    ]

    operations = [
        migrations.RunPython(mark_owners_welcomed, migrations.RunPython.noop),
    ]
