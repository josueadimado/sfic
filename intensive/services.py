import logging
from io import BytesIO

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .models import Registration, Session

logger = logging.getLogger(__name__)


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
        ("Location", session.location),
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
    subject = "Your Freedom Intensive reservation is confirmed"
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
        email.send(fail_silently=False)
    except Exception:
        logger.exception("Failed to send registration confirmation email for %s", registration.id)
        return False
    return True
