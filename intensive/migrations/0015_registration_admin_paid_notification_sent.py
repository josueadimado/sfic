from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0014_registration_confirmation_email_sent"),
    ]

    operations = [
        migrations.AddField(
            model_name="registration",
            name="admin_paid_notification_sent",
            field=models.BooleanField(default=False),
        ),
    ]
