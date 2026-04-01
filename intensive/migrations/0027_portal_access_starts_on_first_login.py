# Clear pre-scheduled hub end dates for anyone who has never signed in,
# so their 30-day window starts at first login instead of at payment/invite.

from django.db import migrations, models


def forwards(apps, schema_editor):
    Registration = apps.get_model("intensive", "Registration")
    Registration.objects.filter(
        status="PAID",
        portal_last_login_at__isnull=True,
    ).update(portal_access_until=None)


class Migration(migrations.Migration):
    dependencies = [
        ("intensive", "0026_portalvideo_zoom_recording"),
    ]

    operations = [
        migrations.AlterField(
            model_name="registration",
            name="portal_access_until",
            field=models.DateTimeField(
                blank=True,
                help_text="End of the 30-day hub window. Left blank until the learner’s first successful sign-in; then set to sign-in time + 30 days.",
                null=True,
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
