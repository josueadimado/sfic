# Merge production server history with Didasko payment-method registration branch.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0027_merge_20260330_1835"),
        ("intensive", "0029_merge_20260709_0055"),
    ]

    operations = []
