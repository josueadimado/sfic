# Donation: split donor_name into first/last; add phone and address for tax records.

from django.db import migrations, models


def forwards_split_name(apps, schema_editor):
    Donation = apps.get_model("intensive", "Donation")
    for d in Donation.objects.exclude(donor_name="").iterator():
        raw = (getattr(d, "donor_name", None) or "").strip()
        if not raw:
            continue
        parts = raw.split(None, 1)
        d.donor_first_name = parts[0]
        d.donor_last_name = parts[1] if len(parts) > 1 else ""
        d.save(update_fields=["donor_first_name", "donor_last_name"])


class Migration(migrations.Migration):
    dependencies = [
        ("intensive", "0027_portal_access_starts_on_first_login"),
    ]

    operations = [
        migrations.AddField(
            model_name="donation",
            name="donor_address",
            field=models.TextField(blank=True, help_text="Mailing address for tax acknowledgment purposes."),
        ),
        migrations.AddField(
            model_name="donation",
            name="donor_first_name",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="donation",
            name="donor_last_name",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="donation",
            name="donor_phone",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.RunPython(forwards_split_name, migrations.RunPython.noop),
        migrations.RemoveField(model_name="donation", name="donor_name"),
    ]
