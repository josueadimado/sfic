# Tie additional materials to a training session (same idea as event program PDF).

import django.db.models.deletion
from django.db import migrations, models


def assign_materials_to_sessions(apps, schema_editor):
    RegistrationMaterial = apps.get_model("intensive", "RegistrationMaterial")
    Session = apps.get_model("intensive", "Session")
    first = Session.objects.order_by("start_date").first()
    if first:
        RegistrationMaterial.objects.filter(session__isnull=True).update(session_id=first.id)
    else:
        RegistrationMaterial.objects.filter(session__isnull=True).delete()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0021_session_event_program_pdf_remove_sitesetting_pdf"),
    ]

    operations = [
        migrations.AddField(
            model_name="registrationmaterial",
            name="session",
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="registration_materials",
                to="intensive.session",
            ),
        ),
        migrations.RunPython(assign_materials_to_sessions, noop_reverse),
        migrations.AlterField(
            model_name="registrationmaterial",
            name="session",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="registration_materials",
                to="intensive.session",
            ),
        ),
    ]
