"""Email portal passwords to paid registrants who do not have one yet (e.g. after deploying the portal)."""

from django.core.management.base import BaseCommand

from intensive.models import Registration, RegistrationStatus
from intensive.services import provision_portal_access


class Command(BaseCommand):
    help = "Generate portal passwords and email them to PAID registrations missing a portal password."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-email",
            action="store_true",
            help="Create passwords only; do not send emails (not usually useful).",
        )

    def handle(self, *args, **options):
        send = not options["no_email"]
        qs = Registration.objects.filter(status=RegistrationStatus.PAID).filter(portal_password_hash="")
        count = qs.count()
        if count == 0:
            self.stdout.write(self.style.WARNING("No paid registrations need a portal password."))
            return
        for reg in qs.iterator():
            provision_portal_access(reg, send_email=send)
            self.stdout.write(f"Provisioned portal for {reg.email} ({reg.id})")
        self.stdout.write(self.style.SUCCESS(f"Done. Processed {count} registration(s)."))
