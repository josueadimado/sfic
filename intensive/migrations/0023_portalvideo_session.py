# Portal hub videos belong to a training session (not site-wide).

import django.db.models.deletion
from django.db import migrations, models


def assign_videos_to_sessions(apps, schema_editor):
    PortalVideo = apps.get_model("intensive", "PortalVideo")
    Session = apps.get_model("intensive", "Session")
    first = Session.objects.order_by("start_date").first()
    if first:
        PortalVideo.objects.filter(session__isnull=True).update(session_id=first.id)
    else:
        PortalVideo.objects.filter(session__isnull=True).delete()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0022_registrationmaterial_session"),
    ]

    operations = [
        migrations.AddField(
            model_name="portalvideo",
            name="session",
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="portal_videos",
                to="intensive.session",
            ),
        ),
        migrations.RunPython(assign_videos_to_sessions, noop_reverse),
        migrations.AlterField(
            model_name="portalvideo",
            name="session",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="portal_videos",
                to="intensive.session",
            ),
        ),
    ]
