from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0024_registration_portal_last_login_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="portalvideo",
            name="external_url",
            field=models.URLField(
                blank=True,
                help_text=(
                    "Optional: YouTube or Vimeo. Paste a normal watch link, youtu.be link, the "
                    "11-character YouTube ID, or an embed URL — it is stored as a hub-safe embed link."
                ),
            ),
        ),
    ]
