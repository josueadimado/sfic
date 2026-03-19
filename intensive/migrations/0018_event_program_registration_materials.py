# Migration: Replace pdf_one/pdf_two with event_program_pdf + RegistrationMaterial

from django.db import migrations, models


def migrate_to_new_structure(apps, schema_editor):
    SiteSetting = apps.get_model("intensive", "SiteSetting")
    RegistrationMaterial = apps.get_model("intensive", "RegistrationMaterial")
    ss = SiteSetting.objects.first()
    if not ss:
        return
    # Copy pdf_one to event_program_pdf
    if hasattr(ss, "registration_material_pdf_one") and ss.registration_material_pdf_one:
        ss.event_program_pdf = ss.registration_material_pdf_one
        ss.save(update_fields=["event_program_pdf"])
    # Copy pdf_two to RegistrationMaterial
    if hasattr(ss, "registration_material_pdf_two") and ss.registration_material_pdf_two:
        RegistrationMaterial.objects.create(
            file=ss.registration_material_pdf_two,
            display_order=1,
        )


def reverse_migrate(apps, schema_editor):
    SiteSetting = apps.get_model("intensive", "SiteSetting")
    RegistrationMaterial = apps.get_model("intensive", "RegistrationMaterial")
    ss = SiteSetting.objects.first()
    if not ss:
        return
    # Reverse: copy event_program_pdf back to pdf_one, first material back to pdf_two
    if ss.event_program_pdf and hasattr(SiteSetting, "registration_material_pdf_one"):
        ss.registration_material_pdf_one = ss.event_program_pdf
        ss.save(update_fields=["registration_material_pdf_one"])
    first = RegistrationMaterial.objects.order_by("display_order", "id").first()
    if first and first.file and hasattr(SiteSetting, "registration_material_pdf_two"):
        ss.registration_material_pdf_two = first.file
        ss.save(update_fields=["registration_material_pdf_two"])


class Migration(migrations.Migration):
    dependencies = [
        ("intensive", "0017_freeregistrationcode_registration_free_registration_code"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesetting",
            name="event_program_pdf",
            field=models.FileField(
                blank=True,
                help_text="Event program PDF: shown as download link on homepage and attached to confirmation emails.",
                upload_to="registration_materials/",
            ),
        ),
        migrations.CreateModel(
            name="RegistrationMaterial",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to="registration_materials/")),
                ("display_order", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["display_order", "id"]},
        ),
        migrations.RunPython(migrate_to_new_structure, reverse_migrate),
        migrations.RemoveField(
            model_name="sitesetting",
            name="registration_material_pdf_one",
        ),
        migrations.RemoveField(
            model_name="sitesetting",
            name="registration_material_pdf_two",
        ),
    ]
