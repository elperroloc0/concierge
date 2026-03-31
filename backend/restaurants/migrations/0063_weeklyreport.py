import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0062_calldetail_quality_signals"),
    ]

    operations = [
        migrations.AddField(
            model_name="restaurant",
            name="notify_weekly_report",
            field=models.BooleanField(default=True, help_text="Monday morning weekly call quality report."),
        ),
        migrations.AddField(
            model_name="restaurant",
            name="weekly_report_language",
            field=models.CharField(
                choices=[("es", "Español"), ("en", "English")],
                default="es",
                max_length=2,
                help_text="Language for the Claude-generated weekly report narrative.",
            ),
        ),
        migrations.CreateModel(
            name="WeeklyReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("restaurant", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="weekly_reports",
                    to="restaurants.restaurant",
                )),
                ("week_start", models.DateField()),
                ("week_end", models.DateField()),
                ("generated_at", models.DateTimeField(auto_now_add=True)),
                ("metrics", models.JSONField(default=dict)),
                ("owner_summary", models.TextField(blank=True)),
                ("prompt_suggestions", models.TextField(blank=True)),
                ("model_used", models.CharField(blank=True, max_length=64)),
                ("generation_cost", models.DecimalField(
                    blank=True, decimal_places=6, max_digits=8, null=True
                )),
            ],
            options={
                "ordering": ["-week_start"],
                "unique_together": {("restaurant", "week_start")},
            },
        ),
    ]
