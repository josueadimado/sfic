from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0023_portalvideo_session"),
    ]

    operations = [
        migrations.AddField(
            model_name="registration",
            name="portal_last_login_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Last successful learning-hub sign-in (updated when they log in with email + password).",
                null=True,
            ),
        ),
    ]
