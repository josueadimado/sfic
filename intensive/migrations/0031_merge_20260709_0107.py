# Final merge: server rename branch + Didasko production branch.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        (
            "intensive",
            "0019_rename_intensive_fr_is_used_9f5e2a_idx_intensive_f_is_used_b348f9_idx_and_more",
        ),
        ("intensive", "0030_merge_20260709_0103"),
    ]

    operations = []
