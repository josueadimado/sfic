# Server history: aligns Session.event_program_pdf field metadata after branch merge.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0025_merge_20260330_1327"),
    ]

    operations = [
        migrations.AlterField(
            model_name="session",
            name="event_program_pdf",
            field=models.FileField(
                blank=True,
                help_text="Event program for this intensive: public homepage link only while this session is the next upcoming live date; confirmation email attachment; learning hub download for paid registrants when downloads unlock.",
                upload_to="registration_materials/",
            ),
        ),
    ]
