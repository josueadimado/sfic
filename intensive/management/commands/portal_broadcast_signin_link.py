"""
Send a fresh email with the corrected learning-hub sign-in link (one email per address).

Run after deploying SITE_BASE_URL / www fixes. By default only contacts with active (non-expired)
portal access. Use --include-expired to also email anyone who still has a portal password set.

Use --reset-passwords to send the full credentials template with a NEW password for each email
(active portal only; same behavior as “Email me a new password” / admin reset).
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from intensive.models import Registration, RegistrationStatus
from intensive.services import reset_and_email_portal_password, send_portal_link_update_email


class Command(BaseCommand):
    help = (
        "Email each unique paid registrant an updated portal sign-in link (password unchanged). "
        "Optional: --reset-passwords to issue and email a new password (active access only)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print how many recipients would be contacted; do not send.",
        )
        parser.add_argument(
            "--include-expired",
            action="store_true",
            help="Include paid registrations whose portal_access_until has passed (link-only mode only).",
        )
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help="Generate a new shared portal password per email and send the full credentials email "
            "(only registrations with portal_access_until in the future).",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        include_expired: bool = options["include_expired"]
        reset_passwords: bool = options["reset_passwords"]

        now = timezone.now()
        qs = Registration.objects.filter(
            status=RegistrationStatus.PAID,
        ).exclude(portal_password_hash="")

        if not include_expired:
            qs = qs.filter(portal_access_until__gt=now)

        qs = qs.order_by("email", "id")

        seen: set[str] = set()
        recipients: list[Registration] = []
        for reg in qs.iterator(chunk_size=200):
            key = reg.email.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            recipients.append(reg)

        if not recipients:
            self.stdout.write(self.style.WARNING("No matching registrations to email."))
            return

        self.stdout.write(
            f"{'Would email' if dry_run else 'Mailing'} {len(recipients)} unique address(es) "
            f"({'reset passwords' if reset_passwords else 'link update only'})."
        )

        if dry_run:
            for reg in recipients[:15]:
                self.stdout.write(f"  - {reg.email} (registration {reg.id})")
            if len(recipients) > 15:
                self.stdout.write(f"  … and {len(recipients) - 15} more")
            return

        ok_n = 0
        err_n = 0
        for reg in recipients:
            if reset_passwords:
                if not reset_and_email_portal_password(reg.email):
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skip or failed (no active portal?): {reg.email}",
                        ),
                    )
                    err_n += 1
                    continue
            else:
                if not send_portal_link_update_email(reg):
                    err_n += 1
                    continue
            ok_n += 1
            self.stdout.write(f"Sent: {reg.email}")

        self.stdout.write(self.style.SUCCESS(f"Done. Sent: {ok_n}. Failed/skipped: {err_n}."))
