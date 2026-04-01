from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0063_weeklyreport"),
    ]

    operations = [
        migrations.AddField(
            model_name="weeklyreport",
            name="status",
            field=models.CharField(
                choices=[("pending", "Pending"), ("done", "Done"), ("failed", "Failed")],
                default="done",
                max_length=16,
            ),
        ),
    ]
