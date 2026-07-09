# Server history: merges parallel branches before session PDF alter migration.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0020_rename_intensive_fr_is_used_9f5e2a_idx_intensive_f_is_used_b348f9_idx_and_more"),
        ("intensive", "0024_registration_portal_last_login_at"),
    ]

    operations = []
