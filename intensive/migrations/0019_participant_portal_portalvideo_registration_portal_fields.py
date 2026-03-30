from datetime import timedelta

import django.utils.timezone
from django.db import migrations, models


def backfill_portal_access_until(apps, schema_editor):
    Registration = apps.get_model("intensive", "Registration")
    now = django.utils.timezone.now()
    until = now + timedelta(days=30)
    Registration.objects.filter(status="PAID", portal_access_until__isnull=True).update(
        portal_access_until=until
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0018_event_program_registration_materials"),
    ]

    operations = [
        migrations.CreateModel(
            name="PortalVideo",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                (
                    "video_file",
                    models.FileField(
                        blank=True,
                        help_text="Uploaded video (MP4 recommended). For very large files, use External URL instead.",
                        upload_to="portal_videos/",
                    ),
                ),
                (
                    "external_url",
                    models.URLField(
                        blank=True,
                        help_text="Optional: YouTube/Vimeo or other link. If set, viewers watch here without file upload.",
                    ),
                ),
                ("display_order", models.PositiveIntegerField(default=1)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["display_order", "id"],
            },
        ),
        migrations.AddField(
            model_name="registration",
            name="portal_access_until",
            field=models.DateTimeField(
                blank=True,
                help_text="After this time, portal login and downloads stop for this registration.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="registration",
            name="portal_password_hash",
            field=models.CharField(
                blank=True,
                help_text="Hashed password for the participant resource portal.",
                max_length=128,
            ),
        ),
        migrations.AddIndex(
            model_name="registration",
            index=models.Index(
                fields=["portal_access_until"],
                name="intensive_reg_portal_until",
            ),
        ),
        migrations.RunPython(backfill_portal_access_until, noop_reverse),
    ]
