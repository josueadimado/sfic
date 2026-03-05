from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0005_alter_session_location_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="speaker",
            name="photo_image",
            field=models.ImageField(blank=True, upload_to="speakers/"),
        ),
    ]
