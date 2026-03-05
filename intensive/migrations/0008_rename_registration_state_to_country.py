from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0007_registration_city_registration_state"),
    ]

    operations = [
        migrations.RenameField(
            model_name="registration",
            old_name="state",
            new_name="country",
        ),
    ]
