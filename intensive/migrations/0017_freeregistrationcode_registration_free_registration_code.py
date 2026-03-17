# Generated manually for free registration codes

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0016_sitesetting_registration_material_pdf_one_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="FreeRegistrationCode",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(db_index=True, max_length=32, unique=True)),
                ("is_used", models.BooleanField(default=False)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "used_registration",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="free_codes_used",
                        to="intensive.registration",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddField(
            model_name="registration",
            name="free_registration_code",
            field=models.ForeignKey(
                blank=True,
                help_text="One-time free registration code used, if any.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="registrations",
                to="intensive.freeregistrationcode",
            ),
        ),
        migrations.AddIndex(
            model_name="freeregistrationcode",
            index=models.Index(fields=["is_used"], name="intensive_fr_is_used_9f5e2a_idx"),
        ),
    ]
