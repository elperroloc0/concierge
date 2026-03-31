from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0061_membership_operator_notify_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="calldetail",
            name="call_signals",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="calldetail",
            name="duration_seconds",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="calldetail",
            name="is_spam",
            field=models.BooleanField(default=False),
        ),
    ]
