# Server history: merges session PDF branch with portal video branch.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0026_alter_session_event_program_pdf"),
        ("intensive", "0026_portalvideo_zoom_recording"),
    ]

    operations = []
