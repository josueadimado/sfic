from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0015_registration_admin_paid_notification_sent"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesetting",
            name="registration_material_pdf_one",
            field=models.FileField(
                blank=True,
                help_text="Optional PDF attached to registration confirmation emails.",
                upload_to="registration_materials/",
            ),
        ),
        migrations.AddField(
            model_name="sitesetting",
            name="registration_material_pdf_two",
            field=models.FileField(
                blank=True,
                help_text="Optional second PDF attached to registration confirmation emails.",
                upload_to="registration_materials/",
            ),
        ),
    ]
