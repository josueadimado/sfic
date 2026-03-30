from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0025_alter_portalvideo_external_url_help_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="portalvideo",
            name="zoom_recording_url",
            field=models.URLField(
                blank=True,
                max_length=500,
                help_text="Optional: Zoom cloud recording share URL. Shown only to participants while their hub access is active (same as other hub content).",
            ),
        ),
        migrations.AddField(
            model_name="portalvideo",
            name="zoom_passcode",
            field=models.CharField(
                blank=True,
                help_text="Recording passcode if Zoom requires it. Shown only on the signed-in hub.",
                max_length=64,
            ),
        ),
    ]
