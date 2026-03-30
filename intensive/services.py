import json
import logging
import mimetypes
import os
import secrets
import string
from datetime import timedelta
from io import BytesIO
from urllib import request as urllib_request

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .models import Donation, Registration, RegistrationStatus, Session, SiteSetting

PORTAL_ACCESS_DAYS = 30

logger = logging.getLogger(__name__)
DONATION_MANAGE_SALT = "sfic-donation-manage"


def _generate_portal_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def send_portal_credentials_email(registration: Registration, plain_password: str) -> bool:
    """Email portal login link, email, and one-time generated password."""
    portal_url = f"{settings.SITE_BASE_URL}{reverse('participant_portal_login')}"
    latest_until = (
        Registration.objects.filter(
            email__iexact=registration.email.strip().lower(),
            status=RegistrationStatus.PAID,
        )
        .order_by("-portal_access_until")
        .values_list("portal_access_until", flat=True)
        .first()
    )
    site = SiteSetting.objects.first()
    context = {
        "registration": registration,
        "plain_password": plain_password,
        "portal_url": portal_url,
        "access_until": latest_until,
        "site_base_url": settings.SITE_BASE_URL,
        "site_name": site.site_name if site else "Set Free In Christ",
    }
    subject = "Your participant portal — resources and training videos"
    text_message = render_to_string("intensive/emails/portal_credentials.txt", context)
    html_message = render_to_string("intensive/emails/portal_credentials.html", context)
    try:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[registration.email],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send portal credentials for %s", registration.id)
        return False
    return True


def provision_portal_access(registration: Registration, *, send_email: bool = True) -> None:
    """
    For PAID registrations: set 30-day portal window (once), create or reuse password,
    email plaintext password only when a new password is created for this person.
    """
    if registration.status != RegistrationStatus.PAID:
        return

    email_key = registration.email.strip().lower()
    now = timezone.now()
    plain: str | None = None

    with transaction.atomic():
        reg = Registration.objects.select_for_update().get(pk=registration.pk)
        if reg.status != RegistrationStatus.PAID:
            return

        siblings = Registration.objects.select_for_update().filter(
            email__iexact=email_key,
            status=RegistrationStatus.PAID,
        ).exclude(pk=reg.pk)

        update_fields: list[str] = []

        if reg.portal_access_until is None:
            reg.portal_access_until = now + timedelta(days=PORTAL_ACCESS_DAYS)
            update_fields.append("portal_access_until")

        if not reg.portal_password_hash:
            donor = siblings.filter(portal_password_hash__gt="").first()
            if donor:
                reg.portal_password_hash = donor.portal_password_hash
            else:
                plain = _generate_portal_password()
                reg.portal_password_hash = make_password(plain)
            update_fields.append("portal_password_hash")

        if update_fields:
            if "updated_at" not in update_fields:
                update_fields.append("updated_at")
            reg.save(update_fields=list(set(update_fields)))

    if plain and send_email:
        registration.refresh_from_db()
        send_portal_credentials_email(registration, plain)


def admin_extend_portal_if_needed(registration: Registration) -> None:
    """Give or restore a 30-day window when sending portal emails from the admin."""
    now = timezone.now()
    if registration.portal_access_until is None or registration.portal_access_until <= now:
        registration.portal_access_until = now + timedelta(days=PORTAL_ACCESS_DAYS)
        registration.save(update_fields=["portal_access_until", "updated_at"])


def admin_send_portal_invite_email(registration: Registration) -> tuple[bool, str]:
    """
    Email portal login details when this paid registrant does not have a portal password yet.
    Does not rotate an existing password (use admin_reset_portal_password_email for that).
    """
    if registration.status != RegistrationStatus.PAID:
        return False, "Only paid registrations can get portal access."
    admin_extend_portal_if_needed(registration)
    registration.refresh_from_db()
    if registration.portal_password_hash:
        return (
            False,
            "This person already has a portal password. Use ‘Reset password and email’ to send a new one.",
        )
    provision_portal_access(registration, send_email=True)
    return True, "Portal invitation email sent with login details."


def admin_reset_portal_password_email(registration: Registration) -> tuple[bool, str]:
    """
    Generate a new portal password and email it.
    Applies to every active (non-expired) paid registration that shares this email.
    """
    if registration.status != RegistrationStatus.PAID:
        return False, "Only paid registrations can use the portal."
    admin_extend_portal_if_needed(registration)
    registration.refresh_from_db()
    if not reset_and_email_portal_password(registration.email):
        return False, "Could not send email (no active portal access for this email)."
    return (
        True,
        "New portal password emailed. If this email has multiple sessions, they all use the same new password.",
    )


def reset_and_email_portal_password(email: str) -> bool:
    """Set a new portal password for every active (paid, non-expired) registration with this email."""
    email_key = (email or "").strip().lower()
    if not email_key:
        return False
    now = timezone.now()
    active = list(
        Registration.objects.filter(
            email__iexact=email_key,
            status=RegistrationStatus.PAID,
            portal_access_until__gt=now,
        )
    )
    if not active:
        return False
    plain = _generate_portal_password()
    new_hash = make_password(plain)
    Registration.objects.filter(
        email__iexact=email_key,
        status=RegistrationStatus.PAID,
        portal_access_until__gt=now,
    ).update(portal_password_hash=new_hash)
    send_portal_credentials_email(active[0], plain)
    return True


def _build_confirmation_pdf(registration: Registration, session: Session, amount_paid: str) -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4
    left = 20 * mm
    top = page_height - 22 * mm

    pdf.setTitle("Freedom Intensive Confirmation")
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(left, top, "Reservation Confirmation")

    pdf.setFont("Helvetica", 10)
    pdf.drawString(left, top - 8 * mm, "Set Free In Christ")
    pdf.drawString(left, top - 13 * mm, f"Reference: {registration.payment_ref or '-'}")
    pdf.drawString(left, top - 18 * mm, f"Status: {registration.status}")

    y = top - 30 * mm
    rows = [
        ("Registrant", registration.full_name),
        ("Email", registration.email),
        ("Phone", registration.phone),
        ("City/Country", f"{registration.city}, {registration.country}"),
        ("Session", session.title),
        ("LOCATION", session.location),
        ("Dates", f"{session.start_date} to {session.end_date}"),
        ("Amount Paid", amount_paid),
    ]

    for label, value in rows:
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(left, y, f"{label}:")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(left + 34 * mm, y, value or "-")
        y -= 8 * mm

    pdf.setFont("Helvetica-Oblique", 9)
    pdf.drawString(left, y - 5 * mm, "This document is generated as your payment confirmation receipt.")
    pdf.showPage()
    pdf.save()

    data = buffer.getvalue()
    buffer.close()
    return data


def send_registration_confirmation(registration: Registration) -> bool:
    session: Session = registration.session
    subject = "Your 3-Day Spiritual Warfare Intensive registration is confirmed"
    amount_paid = f"{registration.amount_paid / 100:.2f} {registration.currency}"
    context = {
        "registration": registration,
        "session": session,
        "amount_paid": amount_paid,
        "site_base_url": settings.SITE_BASE_URL,
    }
    text_message = render_to_string("intensive/emails/registration_confirmation.txt", context)
    html_message = render_to_string("intensive/emails/registration_confirmation.html", context)
    try:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[registration.email],
        )
        email.attach_alternative(html_message, "text/html")
        pdf_bytes = _build_confirmation_pdf(registration, session, amount_paid)
        filename = f"freedom-intensive-confirmation-{registration.id}.pdf"
        email.attach(filename, pdf_bytes, "application/pdf")

        site_settings = SiteSetting.objects.first()
        if site_settings:
            # Event program PDF
            program_pdf = site_settings.event_program_pdf
            if program_pdf:
                try:
                    attachment_name = program_pdf.name.split("/")[-1] or "event-program.pdf"
                    mime_type, _ = mimetypes.guess_type(attachment_name)
                    content_type = mime_type or "application/octet-stream"
                    with program_pdf.open("rb") as handle:
                        email.attach(attachment_name, handle.read(), content_type)
                except Exception:
                    logger.exception(
                        "Failed to attach event program '%s' for %s",
                        getattr(program_pdf, "name", ""),
                        registration.id,
                    )
            # Additional registration materials
            from .models import RegistrationMaterial

            for material in RegistrationMaterial.objects.order_by("display_order", "id"):
                if not material.file:
                    continue
                try:
                    attachment_name = material.file.name.split("/")[-1] or "material.pdf"
                    mime_type, _ = mimetypes.guess_type(attachment_name)
                    content_type = mime_type or "application/octet-stream"
                    with material.file.open("rb") as handle:
                        email.attach(attachment_name, handle.read(), content_type)
                except Exception:
                    logger.exception(
                        "Failed to attach material '%s' for %s",
                        getattr(material.file, "name", ""),
                        registration.id,
                    )
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send registration confirmation email for %s", registration.id)
        return False
    return True


def send_payment_retry_email(registration: Registration) -> bool:
    session: Session = registration.session
    resume_url = f"{settings.SITE_BASE_URL}{reverse('resume_checkout', args=[registration.id])}"
    amount_due = f"{registration.amount_paid / 100:.2f} {registration.currency.upper()}"
    context = {
        "registration": registration,
        "session": session,
        "resume_url": resume_url,
        "amount_due": amount_due,
    }
    subject = "Complete your Freedom Intensive payment"
    text_message = render_to_string("intensive/emails/payment_retry.txt", context)
    html_message = render_to_string("intensive/emails/payment_retry.html", context)
    try:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[registration.email],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send retry payment email for %s", registration.id)
        return False
    return True


def send_admin_new_registration_notification(registration: Registration) -> bool:
    admin_email = (settings.ADMIN_NOTIFICATION_EMAIL or "").strip()
    if not admin_email:
        return False

    session: Session = registration.session
    amount_due = f"{registration.amount_paid / 100:.2f} {registration.currency.upper()}"
    dashboard_url = f"{settings.SITE_BASE_URL}/dashboard/registrations/{registration.id}/"
    context = {
        "registration": registration,
        "session": session,
        "amount_due": amount_due,
        "dashboard_url": dashboard_url,
    }
    subject = f"New registration: {registration.full_name}"
    text_message = render_to_string("intensive/emails/admin_new_registration.txt", context)
    html_message = render_to_string("intensive/emails/admin_new_registration.html", context)
    try:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[admin_email],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send admin new registration email for %s", registration.id)
        return False
    return True


def send_admin_paid_registration_notification(registration: Registration) -> bool:
    admin_email = (settings.ADMIN_NOTIFICATION_EMAIL or "").strip()
    if not admin_email:
        return False

    session: Session = registration.session
    amount_paid = f"{registration.amount_paid / 100:.2f} {registration.currency.upper()}"
    dashboard_url = f"{settings.SITE_BASE_URL}/dashboard/registrations/{registration.id}/"
    context = {
        "registration": registration,
        "session": session,
        "amount_paid": amount_paid,
        "dashboard_url": dashboard_url,
    }
    subject = f"Payment confirmed: {registration.full_name}"
    text_message = render_to_string("intensive/emails/admin_registration_paid.txt", context)
    html_message = render_to_string("intensive/emails/admin_registration_paid.html", context)
    try:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[admin_email],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send admin paid-registration email for %s", registration.id)
        return False
    return True


def send_student_discount_code_email(
    *,
    to_email: str,
    full_name: str,
    student_id: str,
    code: str,
    discount_percent: int,
) -> bool:
    if not to_email:
        return False
    context = {
        "full_name": full_name,
        "student_id": student_id,
        "code": code,
        "discount_percent": discount_percent,
    }
    subject = "Your one-time student discount code"
    text_message = render_to_string("intensive/emails/student_discount_code.txt", context)
    html_message = render_to_string("intensive/emails/student_discount_code.html", context)
    try:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to_email],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send student discount code email to %s", to_email)
        return False
    return True


def send_donation_thank_you(donation: Donation) -> bool:
    email_to = (donation.donor_email or "").strip()
    if not email_to:
        return False
    context = {
        "donation": donation,
        "amount": donation.display_amount,
        "manage_url": build_donation_manage_url(donation),
    }
    subject = "Thank you for your donation"
    text_message = render_to_string("intensive/emails/donation_thank_you.txt", context)
    html_message = render_to_string("intensive/emails/donation_thank_you.html", context)
    try:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email_to],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send donation thank-you email for %s", donation.id)
        return False
    return True


def send_admin_donation_notification(donation: Donation) -> bool:
    admin_email = (settings.ADMIN_NOTIFICATION_EMAIL or "").strip()
    if not admin_email:
        return False
    context = {
        "donation": donation,
        "amount": donation.display_amount,
        "dashboard_url": f"{settings.SITE_BASE_URL}/dashboard/donations/",
    }
    subject = "New donation received"
    text_message = render_to_string("intensive/emails/admin_donation_notification.txt", context)
    html_message = render_to_string("intensive/emails/admin_donation_notification.html", context)
    try:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[admin_email],
        )
        email.attach_alternative(html_message, "text/html")
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send admin donation notification for %s", donation.id)
        return False
    return True


def forward_donation_to_donor_elf(donation: Donation) -> bool:
    tracking_url = (os.getenv("DONOR_ELF_TRACKING_URL", "") or "").strip()
    if not tracking_url:
        return False

    payload = {
        "provider": donation.provider,
        "provider_ref": donation.provider_ref,
        "status": donation.status,
        "frequency": donation.frequency,
        "is_anonymous": donation.is_anonymous,
        "donor_name": donation.donor_name,
        "donor_email": donation.donor_email,
        "amount": donation.amount / 100,
        "currency": donation.currency,
        "note": donation.note,
        "created_at": donation.created_at.isoformat(),
    }
    secret = (os.getenv("DONOR_ELF_TRACKING_SECRET", "") or "").strip()
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        tracking_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if secret:
        req.add_header("X-Donor-Elf-Secret", secret)
    try:
        with urllib_request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception:
        logger.exception("Failed to forward donation %s to Donor Elf tracking URL", donation.id)
        return False


def build_donation_manage_url(donation: Donation) -> str:
    if donation.frequency != "MONTHLY" or not donation.stripe_customer_id:
        return ""
    token = signing.dumps({"donation_id": donation.id}, salt=DONATION_MANAGE_SALT)
    return f"{settings.SITE_BASE_URL}{reverse('donation_manage', args=[token])}"
