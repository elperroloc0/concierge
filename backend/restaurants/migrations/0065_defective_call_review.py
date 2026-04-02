from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0064_weeklyreport_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="calldetail",
            name="needs_review",
            field=models.BooleanField(default=False, db_index=True),
        ),
        migrations.AddField(
            model_name="calldetail",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="restaurant",
            name="notify_on_defective_call",
            field=models.BooleanField(
                default=True,
                help_text="Urgent email when the agent fails to complete a reservation (missing date, time, party size, or name).",
            ),
        ),
    ]
