from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("intensive", "0027_portal_access_starts_on_first_login"),
    ]

    operations = [
        migrations.AlterField(
            model_name="registration",
            name="payment_provider",
            field=models.CharField(
                choices=[
                    ("STRIPE", "Stripe"),
                    ("FREE_CODE", "Free Registration Code"),
                    ("MANUAL", "Manual / offline payment"),
                ],
                default="STRIPE",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="registration",
            name="requested_payment_method",
            field=models.CharField(
                choices=[
                    ("online_card", "Credit / Debit Card (online)"),
                    ("bank_zelle_ach", "Bank / Zelle / ACH"),
                    ("check_mail", "Check by Mail"),
                    ("online_giving", "Online Giving"),
                ],
                default="online_card",
                help_text="How the participant chose to pay at registration.",
                max_length=32,
            ),
        ),
    ]
