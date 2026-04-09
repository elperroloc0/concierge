from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0067_restaurant_social_media_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscription",
            name="sms_unit_cost",
            field=models.DecimalField(
                max_digits=6,
                decimal_places=4,
                default=0.0100,
                help_text=(
                    "Fixed amount deducted from balance per SMS sent using platform Twilio. "
                    "Set to 0.00 to not charge for SMS. "
                    "e.g. 0.01 = $0.01/message. No charge if restaurant uses its own Twilio credentials."
                ),
            ),
        ),
    ]
