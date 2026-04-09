from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0066_add_retell_conversation_flow_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="restaurant",
            name="social_media_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="Instagram, Facebook, or any social media link to share with callers.",
            ),
        ),
    ]
