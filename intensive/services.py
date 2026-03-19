import logging
import json
import os
import mimetypes
from urllib import request as urllib_request
from io import BytesIO

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .models import Donation, Registration, Session, SiteSetting

logger = logging.getLogger(__name__)
DONATION_MANAGE_SALT = "sfic-donation-manage"


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
