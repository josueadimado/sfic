import csv
import json
import math
import mimetypes
import os
from collections import defaultdict
from io import BytesIO
import re
import secrets
import string
import uuid
from datetime import timedelta
from urllib.parse import urlencode
from decimal import Decimal, ROUND_UP

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core import signing
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .forms import (
    DonationForm,
    PortalVideoForm,
    RegistrationForm,
    SessionManageForm,
    SiteSettingForm,
    SpeakerForm,
    TrainingScheduleItemForm,
)
from .models import (
    Donation,
    DonationFrequency,
    DonationStatus,
    FreeRegistrationCode,
    PaymentTransaction,
    PaymentProvider,
    PortalVideo,
    Registration,
    RegistrationMaterial,
    RegistrationStatus,
    Session,
    SiteSetting,
    Speaker,
    StudentDiscountCode,
    TransactionType,
    TrainingScheduleItem,
)
from .services import (
    admin_reset_portal_password_email,
    admin_send_portal_invite_email,
    build_donation_manage_url,
    forward_donation_to_donor_elf,
    DONATION_MANAGE_SALT,
    provision_portal_access,
    send_admin_donation_notification,
    send_admin_new_registration_notification,
    send_admin_paid_registration_notification,
    send_donation_thank_you,
    send_payment_retry_email,
    send_registration_confirmation,
    send_student_discount_code_email,
)

stripe.api_key = settings.STRIPE_SECRET_KEY
DEFAULT_VENUE_ADDRESS = "Freedom Revival Center, 1200 Main St, Dallas, TX 75202, USA"
STRIPE_FEE_PERCENT = Decimal("0.029")
STRIPE_FEE_FIXED_CENTS = Decimal("30")


def _payload_get(payload: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _to_cents(raw_amount) -> int:
    if raw_amount in (None, ""):
        return 0
    text = str(raw_amount).strip().replace(",", "")
    if not text:
        return 0
    try:
        if "." in text:
            return max(int(round(float(text) * 100)), 0)
        value = int(text)
        if value > 1000:
            return max(value, 0)
        return max(value * 100, 0)
    except ValueError:
        return 0


def _map_donation_status(raw_status: str) -> str:
    status = (raw_status or "").strip().lower()
    if status in {"succeeded", "success", "completed", "complete", "paid"}:
        return DonationStatus.COMPLETED
    if status in {"failed", "declined", "error"}:
        return DonationStatus.FAILED
    if status in {"canceled", "cancelled", "void"}:
        return DonationStatus.CANCELED
    return DonationStatus.PENDING


def _gross_up_amount_for_processing_fee(base_amount_cents: int) -> tuple[int, int]:
    """
    Return (total_charge_cents, processing_fee_cents).
    Formula assumes standard Stripe card pricing: 2.9% + 30 cents.
    """
    if base_amount_cents <= 0:
        return 0, 0
    base = Decimal(base_amount_cents)
    gross = (base + STRIPE_FEE_FIXED_CENTS) / (Decimal("1") - STRIPE_FEE_PERCENT)
    total_charge = int(gross.to_integral_value(rounding=ROUND_UP))
    fee = max(total_charge - base_amount_cents, 0)
    return total_charge, fee


def _generate_student_discount_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        if not StudentDiscountCode.objects.filter(code=code).exists():
            return code


def _generate_free_registration_code(length: int = 10) -> str:
    """Generate a unique code for FreeRegistrationCode (avoid StudentDiscountCode collisions)."""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            not FreeRegistrationCode.objects.filter(code=code).exists()
            and not StudentDiscountCode.objects.filter(code=code).exists()
        ):
            return code


def _ensure_registration_confirmation_email(registration: Registration) -> None:
    if registration.confirmation_email_sent:
        return
    sent = send_registration_confirmation(registration)
    if sent:
        registration.confirmation_email_sent = True
        registration.save(update_fields=["confirmation_email_sent", "updated_at"])


def _ensure_admin_paid_registration_notification(registration: Registration) -> None:
    if (
        registration.status != RegistrationStatus.PAID
        or registration.admin_paid_notification_sent
    ):
        return
    sent = send_admin_paid_registration_notification(registration)
    if sent:
        registration.admin_paid_notification_sent = True
        registration.save(update_fields=["admin_paid_notification_sent", "updated_at"])


def _create_payment_transaction_once(
    *,
    registration: Registration | None,
    session: Session | None,
    transaction_type: str,
    status: str,
    provider: str,
    amount: int,
    currency: str,
    payment_ref: str = "",
    stripe_payment_intent: str = "",
    note: str = "",
) -> None:
    payment_ref = str(payment_ref or "")
    stripe_payment_intent = str(stripe_payment_intent or "")
    existing = PaymentTransaction.objects.filter(
        registration=registration,
        transaction_type=transaction_type,
        provider=provider,
        payment_ref=payment_ref,
        stripe_payment_intent=stripe_payment_intent,
        amount=amount,
        currency=str(currency).upper(),
        status=status,
    ).exists()
    if existing:
        return
    PaymentTransaction.objects.create(
        registration=registration,
        session=session,
        transaction_type=transaction_type,
        status=status,
        provider=provider,
        amount=amount,
        currency=str(currency).upper(),
        payment_ref=payment_ref,
        stripe_payment_intent=stripe_payment_intent,
        note=note,
    )


def _sync_pending_registration_from_stripe(registration: Registration, note: str) -> bool:
    if (
        registration.status == RegistrationStatus.PAID
        or not registration.payment_ref
        or not settings.STRIPE_SECRET_KEY
    ):
        return False
    try:
        checkout = stripe.checkout.Session.retrieve(registration.payment_ref)
    except stripe.error.StripeError:
        return False

    payment_status = str(checkout.get("payment_status", "")).lower()
    checkout_status = str(checkout.get("status", "")).lower()
    if payment_status != "paid" and checkout_status != "complete":
        return False

    registration.status = RegistrationStatus.PAID
    registration.payment_ref = str(checkout.get("id", registration.payment_ref))
    registration.amount_paid = checkout.get("amount_total", registration.amount_paid) or registration.amount_paid
    registration.currency = str(checkout.get("currency", registration.currency)).upper()
    registration.save(update_fields=["status", "payment_ref", "amount_paid", "currency", "updated_at"])

    _create_payment_transaction_once(
        registration=registration,
        session=registration.session,
        transaction_type=TransactionType.PAYMENT_COMPLETED,
        status=registration.status,
        provider=PaymentProvider.STRIPE,
        amount=registration.amount_paid,
        currency=registration.currency.upper(),
        payment_ref=registration.payment_ref,
        stripe_payment_intent=str(checkout.get("payment_intent", "")),
        note=note,
    )

    _ensure_registration_confirmation_email(registration)
    _ensure_admin_paid_registration_notification(registration)
    provision_portal_access(registration)
    return True


def _mark_donation_completed_from_checkout(donation: Donation, checkout, note: str) -> None:
    was_completed = donation.status == DonationStatus.COMPLETED
    donation.status = DonationStatus.COMPLETED
    donation.provider = "STRIPE"
    donation.provider_ref = str(checkout.get("id", donation.provider_ref))
    donation.stripe_checkout_id = str(checkout.get("id", donation.stripe_checkout_id))
    donation.stripe_payment_intent = str(checkout.get("payment_intent", donation.stripe_payment_intent))
    donation.stripe_subscription_id = str(checkout.get("subscription", donation.stripe_subscription_id))
    donation.stripe_customer_id = str(checkout.get("customer", donation.stripe_customer_id))
    donation.amount = checkout.get("amount_total", donation.amount) or donation.amount
    donation.currency = str(checkout.get("currency", donation.currency)).upper()
    donation.note = note
    donation.raw_payload = checkout
    donation.save()
    forward_donation_to_donor_elf(donation)
    if not was_completed:
        send_admin_donation_notification(donation)
        send_donation_thank_you(donation)


def _sync_pending_donation_from_stripe(donation: Donation, note: str) -> bool:
    if (
        donation.status in {DonationStatus.COMPLETED, DonationStatus.CANCELED, DonationStatus.FAILED}
        or not donation.stripe_checkout_id
        or not settings.STRIPE_SECRET_KEY
    ):
        return False
    try:
        checkout = stripe.checkout.Session.retrieve(donation.stripe_checkout_id)
    except stripe.error.StripeError:
        return False
    payment_status = str(checkout.get("payment_status", "")).lower()
    checkout_status = str(checkout.get("status", "")).lower()
    if payment_status == "paid" or checkout_status == "complete":
        _mark_donation_completed_from_checkout(donation, checkout, note)
        return True
    if checkout_status == "expired":
        donation.status = DonationStatus.CANCELED
        donation.note = "Donation checkout expired before payment completion."
        donation.raw_payload = checkout
        donation.save(update_fields=["status", "note", "raw_payload", "updated_at"])
        forward_donation_to_donor_elf(donation)
        return True
    if checkout_status in {"open", ""}:
        # Stripe can keep abandoned sessions "open" for a long time.
        # Mark old open sessions as canceled so dashboard status is clearer.
        if donation.created_at <= timezone.now() - timedelta(minutes=45):
            donation.status = DonationStatus.CANCELED
            donation.note = "Donation was abandoned before payment completion."
            donation.raw_payload = checkout
            donation.save(update_fields=["status", "note", "raw_payload", "updated_at"])
            forward_donation_to_donor_elf(donation)
            return True
    return False


def _build_checkout_session(registration: Registration, session: Session) -> stripe.checkout.Session:
    return stripe.checkout.Session.create(
        mode="payment",
        customer_email=registration.email,
        line_items=[
            {
                "price_data": {
                    "currency": session.currency.lower(),
                    "unit_amount": registration.amount_paid,
                    "product_data": {"name": f"3 Day Freedom Intensive - {session.title}"},
                },
                "quantity": 1,
            }
        ],
        metadata={
            "registration_id": str(registration.id),
            "session_id": str(session.id),
        },
        payment_intent_data={
            "metadata": {
                "registration_id": str(registration.id),
                "session_id": str(session.id),
            }
        },
        success_url=f"{settings.SITE_BASE_URL}/success?ref={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.SITE_BASE_URL}/cancel?registration_id={registration.id}",
    )


def _build_donation_checkout_session(donation: Donation) -> stripe.checkout.Session:
    price_data = {
        "currency": donation.currency.lower(),
        "unit_amount": donation.amount,
        "product_data": {"name": "Donation - Set Free In Christ Mission"},
    }
    if donation.frequency == DonationFrequency.MONTHLY:
        price_data["recurring"] = {"interval": "month"}
    line_item = {"price_data": price_data, "quantity": 1}
    common = {
        "customer_email": donation.donor_email or None,
        "line_items": [line_item],
        "metadata": {
            "donation_id": str(donation.id),
            "frequency": donation.frequency,
            "is_anonymous": "1" if donation.is_anonymous else "0",
            **(
                {}
                if donation.is_anonymous
                else {
                    "donor_first_name": (donation.donor_first_name or "")[:80],
                    "donor_last_name": (donation.donor_last_name or "")[:80],
                }
            ),
        },
        "success_url": f"{settings.SITE_BASE_URL}/donation/success?ref={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{settings.SITE_BASE_URL}/donation/cancel?donation_id={donation.id}",
    }
    if donation.frequency == DonationFrequency.MONTHLY:
        return stripe.checkout.Session.create(
            mode="subscription",
            subscription_data={
                "metadata": {
                    "donation_id": str(donation.id),
                    "frequency": donation.frequency,
                }
            },
            **common,
        )
    return stripe.checkout.Session.create(
        mode="payment",
        payment_intent_data={"metadata": {"donation_id": str(donation.id)}},
        **common,
    )


def _next_scheduled_intensive_session() -> Session | None:
    """Next active intensive by start date whose last day has not passed (local date)."""
    today = timezone.localdate()
    return (
        Session.objects.filter(is_active=True, end_date__gte=today).order_by("start_date").first()
    )


@require_GET
def public_event_program(request: HttpRequest, session_id: uuid.UUID) -> HttpResponse:
    """
    Serve the event program file only when the public site would show the download button.
    Avoids exposing a guessable static media URL; checks are re-evaluated on every request.
    """
    session = get_object_or_404(Session, pk=session_id, is_active=True)
    next_session = _next_scheduled_intensive_session()
    if (
        next_session is None
        or next_session.pk != session.pk
        or not session.allows_public_event_program_link()
    ):
        raise Http404("Program not available.")
    pdf = session.event_program_pdf
    if not pdf:
        raise Http404("Program not available.")
    name = pdf.name.split("/")[-1] if pdf.name else "event-program.pdf"
    mime, _ = mimetypes.guess_type(name)
    return FileResponse(
        pdf.open("rb"),
        as_attachment=True,
        filename=name,
        content_type=mime or "application/pdf",
    )


def _home_context(reg_form: dict | None = None, focus_register: bool = False) -> dict:
    sessions = Session.objects.filter(is_active=True).order_by("start_date")
    schedule_items = TrainingScheduleItem.objects.filter(is_active=True).order_by("display_order")
    today = timezone.localdate()
    upcoming_sessions = sessions.filter(end_date__gte=today)
    past_sessions = sessions.filter(end_date__lt=today)
    next_session = _next_scheduled_intensive_session()
    upcoming_session = next_session or sessions.first()
    speakers = Speaker.objects.filter(is_active=True).prefetch_related("sessions")
    if upcoming_session:
        session_speakers = speakers.filter(sessions=upcoming_session)
        if session_speakers.exists():
            speakers = session_speakers
    site_setting = SiteSetting.objects.first()
    public_program_session = None
    if next_session and next_session.allows_public_event_program_link():
        public_program_session = next_session
    return {
        "sessions": sessions,
        "upcoming_sessions": upcoming_sessions,
        "past_sessions": past_sessions,
        "schedule_items": schedule_items,
        "countdown_session": next_session,
        "speakers": speakers,
        "speakers_session": upcoming_session,
        "venue_address": site_setting.venue_address if site_setting else DEFAULT_VENUE_ADDRESS,
        "donation_url": site_setting.donation_url if site_setting and site_setting.donation_url else "",
        "student_discount_percent": site_setting.student_discount_percent if site_setting else 0,
        "public_program_session": public_program_session,
        "stripe_publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
        "reg_form": reg_form or {},
        "focus_register": focus_register,
        "country_choices": RegistrationForm.COUNTRY_CHOICES,
        "country_options": RegistrationForm.COUNTRY_OPTIONS,
    }


def _donation_form_values_from_post(post) -> dict:
    return {
        "amount": post.get("amount", ""),
        "frequency": post.get("frequency", DonationFrequency.ONE_TIME),
        "is_anonymous": post.get("is_anonymous", ""),
        "first_name": post.get("first_name", ""),
        "last_name": post.get("last_name", ""),
        "phone": post.get("phone", ""),
        "address": post.get("address", ""),
        "email": post.get("email", ""),
        "message": post.get("message", ""),
    }


def _donation_context(donation_form: dict | None = None) -> dict:
    return {
        "donation_form": donation_form or {},
    }


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    return render(request, "intensive/home.html", _home_context())


@require_GET
def donate(request: HttpRequest) -> HttpResponse:
    return render(request, "intensive/donate.html", _donation_context())


@require_POST
@login_required
def dashboard_logout(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("home")


@require_GET
def robots_txt(request: HttpRequest) -> HttpResponse:
    base = settings.SITE_BASE_URL.rstrip("/")
    content = "\n".join(
        [
            "User-agent: *",
            "Disallow: /dashboard/",
            "Disallow: /admin/",
            "Disallow: /checkout/",
            "Disallow: /success",
            "Disallow: /cancel",
            f"Sitemap: {base}/sitemap.xml",
        ]
    )
    return HttpResponse(content, content_type="text/plain")


@require_GET
def sitemap_xml(request: HttpRequest) -> HttpResponse:
    base = settings.SITE_BASE_URL.rstrip("/")
    home_url = f"{base}{reverse('home')}"
    today = timezone.localdate().isoformat()
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{home_url}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
"""
    return HttpResponse(content, content_type="application/xml")


@require_POST
def create_checkout(request: HttpRequest) -> HttpResponse:
    form = RegistrationForm(request.POST)
    if not form.is_valid():
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        raw = request.POST
        form_values = {
            "full_name": raw.get("full_name", ""),
            "email": raw.get("email", ""),
            "phone": raw.get("phone", ""),
            "city": raw.get("city", ""),
            "country": raw.get("country", ""),
            "church": raw.get("church", ""),
            "is_student": raw.get("is_student", ""),
            "student_id": raw.get("student_id", ""),
            "student_discount_code": raw.get("student_discount_code", ""),
            "discount_code": raw.get("discount_code", ""),
            "session_id": raw.get("session_id", ""),
        }
        return render(request, "intensive/home.html", _home_context(form_values, focus_register=True))

    session = get_object_or_404(Session, id=form.cleaned_data["session_id"], is_active=True)
    email = form.cleaned_data["email"].strip().lower()

    # Prevent duplicate registrations for the same session/email.
    existing_paid = Registration.objects.filter(
        session=session,
        email__iexact=email,
        status=RegistrationStatus.PAID,
    ).order_by("-created_at").first()
    if existing_paid:
        messages.info(
            request,
            "You already have a paid registration for this session with this email.",
        )
        if existing_paid.payment_ref:
            return redirect(f"{reverse('success')}?ref={existing_paid.payment_ref}")
        return redirect(f"{reverse('success')}?reg_id={existing_paid.id}")

    existing_unpaid = Registration.objects.filter(
        session=session,
        email__iexact=email,
    ).exclude(status=RegistrationStatus.PAID).order_by("-created_at").first()
    if existing_unpaid:
        existing_unpaid.full_name = form.cleaned_data["full_name"]
        existing_unpaid.phone = form.cleaned_data["phone"]
        existing_unpaid.city = form.cleaned_data["city"]
        existing_unpaid.country = form.cleaned_data["country"]
        existing_unpaid.church = form.cleaned_data["church"]
        existing_unpaid.save(update_fields=["full_name", "phone", "city", "country", "church", "updated_at"])
        messages.info(
            request,
            "You already started registration for this session. Continue payment with your existing record.",
        )
        return redirect("resume_checkout", registration_id=existing_unpaid.id)

    paid_count = session.registrations.filter(status=RegistrationStatus.PAID).count()
    if paid_count >= session.capacity:
        messages.error(request, "This session is already full. Please pick another one.")
        form_values = {
            "full_name": form.cleaned_data["full_name"],
            "email": form.cleaned_data["email"],
            "phone": form.cleaned_data["phone"],
            "city": form.cleaned_data["city"],
            "country": form.cleaned_data["country"],
            "church": form.cleaned_data["church"],
            "is_student": form.cleaned_data.get("is_student", False),
            "student_id": form.cleaned_data.get("student_id", ""),
            "student_discount_code": form.cleaned_data.get("student_discount_code", ""),
            "discount_code": form.cleaned_data.get("discount_code", ""),
            "session_id": str(form.cleaned_data["session_id"]),
        }
        return render(request, "intensive/home.html", _home_context(form_values, focus_register=True))

    # Free registration code: 100% discount, no Stripe. One-time use only.
    discount_code = form.cleaned_data.get("discount_code", "").strip().upper()
    if discount_code:
        free_reg_id: str | None = None
        with transaction.atomic():
            free_code = FreeRegistrationCode.objects.filter(
                code=discount_code,
                is_used=False,
            ).select_for_update().first()
            if free_code:
                registration = Registration.objects.create(
                    full_name=form.cleaned_data["full_name"],
                    email=form.cleaned_data["email"],
                    phone=form.cleaned_data["phone"],
                    city=form.cleaned_data["city"],
                    country=form.cleaned_data["country"],
                    church=form.cleaned_data["church"],
                    is_student=False,
                    student_id="",
                    student_discount_code="",
                    discount_amount=session.price,
                    session=session,
                    status=RegistrationStatus.PAID,
                    payment_provider=PaymentProvider.FREE_CODE,
                    payment_ref="",
                    amount_paid=0,
                    currency=session.currency,
                    free_registration_code=free_code,
                )
                free_code.is_used = True
                free_code.used_at = timezone.now()
                free_code.used_registration = registration
                free_code.save(update_fields=["is_used", "used_at", "used_registration"])
                _create_payment_transaction_once(
                    registration=registration,
                    session=session,
                    transaction_type=TransactionType.PAYMENT_COMPLETED,
                    status=RegistrationStatus.PAID,
                    provider=PaymentProvider.FREE_CODE,
                    amount=0,
                    currency=session.currency.upper(),
                    payment_ref=f"FREE-{registration.id}",
                    note="100% discount via free registration code.",
                )
                free_reg_id = str(registration.id)
        if free_reg_id:
            paid_free = Registration.objects.select_related("session").get(id=free_reg_id)
            send_admin_new_registration_notification(paid_free)
            _ensure_registration_confirmation_email(paid_free)
            _ensure_admin_paid_registration_notification(paid_free)
            provision_portal_access(paid_free)
            return redirect(f"{reverse('success')}?reg_id={free_reg_id}")

        existing_row = FreeRegistrationCode.objects.filter(code=discount_code).first()
        if existing_row is None:
            messages.error(
                request,
                "That code is not a free registration code. Copy it from Dashboard → Free "
                "Registration Codes. (Student codes from email go in the student field only.)",
            )
        elif existing_row.is_used:
            messages.error(
                request,
                "This free registration code has already been used. Each code works only once—"
                "pick an unused one from your list or generate new codes.",
            )
        else:
            messages.error(
                request,
                "This code could not be applied right now. Try again, or use another unused code.",
            )
        form_values = {
            "full_name": form.cleaned_data["full_name"],
            "email": form.cleaned_data["email"],
            "phone": form.cleaned_data["phone"],
            "city": form.cleaned_data["city"],
            "country": form.cleaned_data["country"],
            "church": form.cleaned_data["church"],
            "is_student": form.cleaned_data.get("is_student", False),
            "student_id": form.cleaned_data.get("student_id", ""),
            "student_discount_code": form.cleaned_data.get("student_discount_code", ""),
            "discount_code": discount_code,
            "session_id": str(form.cleaned_data["session_id"]),
        }
        return render(request, "intensive/home.html", _home_context(form_values, focus_register=True))

    site_setting = SiteSetting.objects.first()
    discount_percent = site_setting.student_discount_percent if site_setting else 0
    is_student = form.cleaned_data.get("is_student", False)
    student_id = form.cleaned_data.get("student_id", "")
    student_discount_code = form.cleaned_data.get("student_discount_code", "")
    verified_discount_code = None

    if is_student:
        if not student_discount_code:
            messages.error(
                request,
                "Enter your one-time student code before payment. Use the 'Verify ID & Send Code' button first.",
            )
            form_values = {
                "full_name": form.cleaned_data["full_name"],
                "email": form.cleaned_data["email"],
                "phone": form.cleaned_data["phone"],
                "city": form.cleaned_data["city"],
                "country": form.cleaned_data["country"],
                "church": form.cleaned_data["church"],
                "is_student": True,
                "student_id": student_id,
                "student_discount_code": "",
                "session_id": str(form.cleaned_data["session_id"]),
            }
            return render(request, "intensive/home.html", _home_context(form_values, focus_register=True))

        verified_discount_code = StudentDiscountCode.objects.filter(
            code=student_discount_code,
            email__iexact=email,
            student_id=student_id,
            is_used=False,
        ).first()
        if not verified_discount_code:
            messages.error(request, "Invalid or already-used student discount code.")
            form_values = {
                "full_name": form.cleaned_data["full_name"],
                "email": form.cleaned_data["email"],
                "phone": form.cleaned_data["phone"],
                "city": form.cleaned_data["city"],
                "country": form.cleaned_data["country"],
                "church": form.cleaned_data["church"],
                "is_student": True,
                "student_id": student_id,
                "student_discount_code": student_discount_code,
                "session_id": str(form.cleaned_data["session_id"]),
            }
            return render(request, "intensive/home.html", _home_context(form_values, focus_register=True))

    discount_amount = 0
    amount_due = session.price
    if is_student and verified_discount_code and discount_percent > 0:
        discount_amount = (session.price * discount_percent) // 100
        if discount_amount >= session.price:
            discount_amount = max(session.price - 1, 0)
        amount_due = max(session.price - discount_amount, 0)

    registration = Registration.objects.create(
        full_name=form.cleaned_data["full_name"],
        email=form.cleaned_data["email"],
        phone=form.cleaned_data["phone"],
        city=form.cleaned_data["city"],
        country=form.cleaned_data["country"],
        church=form.cleaned_data["church"],
        is_student=is_student,
        student_id=student_id if is_student else "",
        student_discount_code=verified_discount_code.code if verified_discount_code else "",
        discount_amount=discount_amount,
        session=session,
        status=RegistrationStatus.PENDING,
        payment_provider=PaymentProvider.STRIPE,
        amount_paid=amount_due,
        currency=session.currency,
    )
    if verified_discount_code:
        verified_discount_code.is_used = True
        verified_discount_code.used_at = timezone.now()
        verified_discount_code.used_registration = registration
        verified_discount_code.save(update_fields=["is_used", "used_at", "used_registration"])
    send_admin_new_registration_notification(registration)

    if not settings.STRIPE_SECRET_KEY:
        messages.error(request, "Stripe is not configured yet. Please add keys in .env.")
        registration.status = RegistrationStatus.CANCELED
        registration.save(update_fields=["status", "updated_at"])
        _create_payment_transaction_once(
            registration=registration,
            session=session,
            transaction_type=TransactionType.PAYMENT_ERROR,
            status=registration.status,
            provider=PaymentProvider.STRIPE,
            amount=registration.amount_paid,
            currency=session.currency.upper(),
            payment_ref=registration.payment_ref,
            note="Stripe key missing in environment.",
        )
        form_values = {
            "full_name": form.cleaned_data["full_name"],
            "email": form.cleaned_data["email"],
            "phone": form.cleaned_data["phone"],
            "city": form.cleaned_data["city"],
            "country": form.cleaned_data["country"],
            "church": form.cleaned_data["church"],
            "is_student": is_student,
            "student_id": student_id,
            "student_discount_code": student_discount_code,
            "session_id": str(form.cleaned_data["session_id"]),
        }
        return render(request, "intensive/home.html", _home_context(form_values, focus_register=True))

    checkout_session = _build_checkout_session(registration, session)
    registration.payment_ref = checkout_session.id
    registration.save(update_fields=["payment_ref", "updated_at"])
    _create_payment_transaction_once(
        registration=registration,
        session=session,
        transaction_type=TransactionType.CHECKOUT_CREATED,
        status=registration.status,
        provider=PaymentProvider.STRIPE,
        amount=registration.amount_paid,
        currency=session.currency.upper(),
        payment_ref=checkout_session.id,
        stripe_payment_intent=str(checkout_session.get("payment_intent", "")),
        note="Checkout session created.",
    )
    return redirect(checkout_session.url)


@require_POST
def request_student_discount_code(request: HttpRequest) -> HttpResponse:
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    raw = request.POST
    form_values = {
        "full_name": raw.get("full_name", "").strip(),
        "email": raw.get("email", "").strip(),
        "phone": raw.get("phone", "").strip(),
        "city": raw.get("city", "").strip(),
        "country": raw.get("country", "").strip(),
        "church": raw.get("church", "").strip(),
        "is_student": raw.get("is_student", ""),
        "student_id": raw.get("student_id", "").strip(),
        "student_discount_code": raw.get("student_discount_code", "").strip(),
        "session_id": raw.get("session_id", ""),
    }

    if not form_values["is_student"]:
        message = "Select 'I am an Andrews University student' before requesting a code."
        if is_ajax:
            return JsonResponse({"ok": False, "message": message}, status=400)
        messages.error(request, message)
        return render(request, "intensive/home.html", _home_context(form_values, focus_register=True))

    student_id = form_values["student_id"]
    if not re.fullmatch(r"\d{6}", student_id):
        message = "Andrews University student ID must be exactly 6 digits."
        if is_ajax:
            return JsonResponse({"ok": False, "message": message}, status=400)
        messages.error(request, message)
        return render(request, "intensive/home.html", _home_context(form_values, focus_register=True))

    email = form_values["email"]
    try:
        validate_email(email)
    except ValidationError:
        message = "Enter a valid email address so we can send your one-time code."
        if is_ajax:
            return JsonResponse({"ok": False, "message": message}, status=400)
        messages.error(request, message)
        return render(request, "intensive/home.html", _home_context(form_values, focus_register=True))

    site_setting = SiteSetting.objects.first()
    discount_percent = site_setting.student_discount_percent if site_setting else 0

    existing = StudentDiscountCode.objects.filter(
        student_id=student_id,
        email__iexact=email,
        is_used=False,
    ).order_by("-created_at").first()
    code = existing.code if existing else _generate_student_discount_code()
    if not existing:
        StudentDiscountCode.objects.create(
            student_id=student_id,
            email=email.lower(),
            code=code,
        )

    sent = send_student_discount_code_email(
        to_email=email,
        full_name=form_values["full_name"],
        student_id=student_id,
        code=code,
        discount_percent=discount_percent,
    )
    if sent:
        message = "Student code sent. Check your email and enter it below before payment."
        if is_ajax:
            return JsonResponse({"ok": True, "message": message})
        messages.success(request, message)
    else:
        message = "Could not send your code email right now. Please try again."
        if is_ajax:
            return JsonResponse({"ok": False, "message": message}, status=500)
        messages.error(request, message)
    return render(request, "intensive/home.html", _home_context(form_values, focus_register=True))


@require_POST
def create_donation_checkout(request: HttpRequest) -> HttpResponse:
    form = DonationForm(request.POST)
    if not form.is_valid():
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return render(request, "intensive/donate.html", _donation_context(_donation_form_values_from_post(request.POST)))

    if not settings.STRIPE_SECRET_KEY:
        messages.error(request, "Stripe is not configured yet. Please contact support.")
        return render(request, "intensive/donate.html", _donation_context(_donation_form_values_from_post(request.POST)))

    base_amount_cents = int(form.cleaned_data["amount"] * 100)
    amount_cents, processing_fee_cents = _gross_up_amount_for_processing_fee(base_amount_cents)
    is_anonymous = form.cleaned_data.get("is_anonymous", False)
    donation = Donation.objects.create(
        provider="STRIPE",
        frequency=form.cleaned_data["frequency"],
        is_anonymous=is_anonymous,
        donor_first_name="" if is_anonymous else form.cleaned_data.get("first_name", ""),
        donor_last_name="" if is_anonymous else form.cleaned_data.get("last_name", ""),
        donor_phone="" if is_anonymous else form.cleaned_data.get("phone", ""),
        donor_address="" if is_anonymous else form.cleaned_data.get("address", ""),
        donor_email=form.cleaned_data["email"],
        donor_message=form.cleaned_data.get("message", ""),
        amount=amount_cents,
        currency="USD",
        status=DonationStatus.PENDING,
        note=f"Donation checkout created. Processing fee included: {processing_fee_cents / 100:.2f} USD.",
        raw_payload={
            "base_amount_cents": base_amount_cents,
            "cover_processing_fee": True,
            "processing_fee_cents": processing_fee_cents,
            "checkout_amount_cents": amount_cents,
        },
    )
    try:
        checkout_session = _build_donation_checkout_session(donation)
    except stripe.error.StripeError as exc:
        error_message = (
            getattr(exc, "user_message", None)
            or str(exc)
            or "Stripe could not start the donation checkout."
        )
        donation.status = DonationStatus.FAILED
        donation.note = f"Stripe checkout creation failed: {error_message}"
        donation.raw_payload = {
            "base_amount_cents": base_amount_cents,
            "cover_processing_fee": True,
            "processing_fee_cents": processing_fee_cents,
            "checkout_amount_cents": amount_cents,
            "stripe_error": error_message,
        }
        donation.save(update_fields=["status", "note", "raw_payload", "updated_at"])
        messages.error(
            request,
            "We could not start the donation checkout. Please try again or use one-time donation for now.",
        )
        return render(request, "intensive/donate.html", _donation_context(_donation_form_values_from_post(request.POST)))

    donation.provider_ref = checkout_session.id
    donation.stripe_checkout_id = checkout_session.id
    donation.save(update_fields=["provider_ref", "stripe_checkout_id", "updated_at"])
    return redirect(checkout_session.url)


@require_GET
def resume_checkout(request: HttpRequest, registration_id: str) -> HttpResponse:
    registration = get_object_or_404(
        Registration.objects.select_related("session"),
        id=registration_id,
    )
    if registration.status == RegistrationStatus.PAID:
        messages.info(request, "This registration has already been paid.")
        return redirect(f"{reverse('success')}?ref={registration.payment_ref}")

    session = registration.session
    paid_count = session.registrations.filter(status=RegistrationStatus.PAID).count()
    if paid_count >= session.capacity:
        messages.error(request, "This session is now full, so payment cannot be completed.")
        return redirect("home")

    if not settings.STRIPE_SECRET_KEY:
        messages.error(request, "Stripe is not configured yet. Please contact support.")
        return redirect("home")

    checkout_session = _build_checkout_session(registration, session)
    registration.payment_ref = checkout_session.id
    if registration.status != RegistrationStatus.PENDING:
        registration.status = RegistrationStatus.PENDING
    registration.save(update_fields=["payment_ref", "status", "updated_at"])
    _create_payment_transaction_once(
        registration=registration,
        session=session,
        transaction_type=TransactionType.CHECKOUT_CREATED,
        status=registration.status,
        provider=PaymentProvider.STRIPE,
        amount=registration.amount_paid,
        currency=session.currency.upper(),
        payment_ref=checkout_session.id,
        stripe_payment_intent=str(checkout_session.get("payment_intent", "")),
        note="Checkout session created from retry link.",
    )
    return redirect(checkout_session.url)


@require_GET
def donation_success(request: HttpRequest) -> HttpResponse:
    ref = request.GET.get("ref")
    donation = None
    manage_url = ""
    if ref:
        donation = Donation.objects.filter(stripe_checkout_id=ref).first()
        if not donation and settings.STRIPE_SECRET_KEY:
            try:
                checkout = stripe.checkout.Session.retrieve(ref)
            except stripe.error.StripeError:
                checkout = None
            if checkout:
                donation_id = checkout.get("metadata", {}).get("donation_id")
                if donation_id:
                    donation = Donation.objects.filter(id=donation_id).first()
                    if donation and donation.stripe_checkout_id != ref:
                        donation.stripe_checkout_id = ref
                        donation.provider_ref = ref
                        donation.save(update_fields=["stripe_checkout_id", "provider_ref", "updated_at"])
        if donation:
            if donation.stripe_checkout_id != ref:
                donation.stripe_checkout_id = ref
                donation.provider_ref = ref
                donation.save(update_fields=["stripe_checkout_id", "provider_ref", "updated_at"])
            _sync_pending_donation_from_stripe(
                donation, note="Donation completed from success page verification."
            )
        if donation:
            manage_url = build_donation_manage_url(donation)
    return render(request, "intensive/donation_success.html", {"donation": donation, "manage_url": manage_url})


@require_GET
def donation_cancel(request: HttpRequest) -> HttpResponse:
    donation = None
    donation_id = request.GET.get("donation_id")
    if donation_id:
        donation = Donation.objects.filter(id=donation_id).first()
        if donation and donation.status != DonationStatus.COMPLETED:
            donation.status = DonationStatus.CANCELED
            donation.note = "Donation checkout canceled by donor."
            donation.save(update_fields=["status", "note", "updated_at"])
    return render(request, "intensive/donation_cancel.html", {"donation": donation})


@require_GET
def donation_manage(request: HttpRequest, token: str) -> HttpResponse:
    try:
        data = signing.loads(token, salt=DONATION_MANAGE_SALT, max_age=60 * 60 * 24 * 365 * 3)
    except signing.BadSignature:
        messages.error(request, "This donation management link is invalid or expired.")
        return redirect("donate")

    donation_id = data.get("donation_id")
    donation = Donation.objects.filter(id=donation_id).first()
    if not donation or donation.frequency != DonationFrequency.MONTHLY or not donation.stripe_customer_id:
        messages.error(request, "Monthly donation details were not found for this link.")
        return redirect("donate")
    if not settings.STRIPE_SECRET_KEY:
        messages.error(request, "Stripe is not configured yet. Please contact support.")
        return redirect("donate")
    try:
        portal = stripe.billing_portal.Session.create(
            customer=donation.stripe_customer_id,
            return_url=f"{settings.SITE_BASE_URL}/donate/",
        )
    except stripe.error.StripeError:
        messages.error(request, "We could not open the donation management portal right now.")
        return redirect("donate")
    return redirect(portal.url)


@csrf_exempt
@require_POST
def stripe_webhook(request: HttpRequest) -> HttpResponse:
    payload = request.body
    signature = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    endpoint_secret = settings.STRIPE_WEBHOOK_SECRET

    if not endpoint_secret:
        return HttpResponse(status=400)

    try:
        event = stripe.Webhook.construct_event(payload, signature, endpoint_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        checkout = event["data"]["object"]
        donation_id = checkout.get("metadata", {}).get("donation_id")
        if donation_id:
            donation = Donation.objects.filter(id=donation_id).first()
            if donation:
                was_completed = donation.status == DonationStatus.COMPLETED
                donation.status = DonationStatus.COMPLETED
                donation.provider = "STRIPE"
                donation.provider_ref = str(checkout.get("id", donation.provider_ref))
                donation.stripe_checkout_id = str(checkout.get("id", donation.stripe_checkout_id))
                donation.stripe_payment_intent = str(checkout.get("payment_intent", donation.stripe_payment_intent))
                donation.stripe_subscription_id = str(checkout.get("subscription", donation.stripe_subscription_id))
                donation.stripe_customer_id = str(checkout.get("customer", donation.stripe_customer_id))
                donation.amount = checkout.get("amount_total", donation.amount) or donation.amount
                donation.currency = str(checkout.get("currency", donation.currency)).upper()
                donation.note = "Donation completed via Stripe Checkout."
                donation.raw_payload = checkout
                donation.save()
                forward_donation_to_donor_elf(donation)
                if not was_completed:
                    send_admin_donation_notification(donation)
                    send_donation_thank_you(donation)
            return HttpResponse(status=200)

        registration_id = checkout.get("metadata", {}).get("registration_id")
        if registration_id:
            try:
                portal_followup_id: str | None = None
                with transaction.atomic():
                    registration = Registration.objects.select_for_update().get(id=registration_id)
                    session = registration.session
                    paid_count = session.registrations.filter(status=RegistrationStatus.PAID).count()
                    if paid_count >= session.capacity:
                        registration.status = RegistrationStatus.CANCELED
                        registration.save(update_fields=["status", "updated_at"])
                        _create_payment_transaction_once(
                            registration=registration,
                            session=session,
                            transaction_type=TransactionType.PAYMENT_CANCELED,
                            status=registration.status,
                            provider=PaymentProvider.STRIPE,
                            amount=checkout.get("amount_total", registration.amount_paid),
                            currency=str(checkout.get("currency", session.currency)).upper(),
                            payment_ref=str(checkout.get("id", registration.payment_ref)),
                            stripe_payment_intent=str(checkout.get("payment_intent", "")),
                            note="Canceled after payment because capacity was full.",
                        )
                    else:
                        registration.status = RegistrationStatus.PAID
                        registration.payment_ref = checkout.get("id", registration.payment_ref)
                        registration.amount_paid = checkout.get("amount_total", registration.amount_paid)
                        registration.currency = checkout.get("currency", session.currency).upper()
                        registration.updated_at = timezone.now()
                        registration.save()
                        _create_payment_transaction_once(
                            registration=registration,
                            session=session,
                            transaction_type=TransactionType.PAYMENT_COMPLETED,
                            status=registration.status,
                            provider=PaymentProvider.STRIPE,
                            amount=registration.amount_paid,
                            currency=registration.currency.upper(),
                            payment_ref=registration.payment_ref,
                            stripe_payment_intent=str(checkout.get("payment_intent", "")),
                            note="Payment confirmed from webhook.",
                        )
                        portal_followup_id = str(registration.id)
                if portal_followup_id:
                    paid_reg = Registration.objects.select_related("session").get(id=portal_followup_id)
                    _ensure_registration_confirmation_email(paid_reg)
                    _ensure_admin_paid_registration_notification(paid_reg)
                    provision_portal_access(paid_reg)
            except Registration.DoesNotExist:
                return HttpResponse(status=200)
    elif event["type"] == "checkout.session.expired":
        checkout = event["data"]["object"]
        donation_id = checkout.get("metadata", {}).get("donation_id")
        if donation_id:
            donation = Donation.objects.filter(id=donation_id).first()
            if donation and donation.status != DonationStatus.COMPLETED:
                donation.status = DonationStatus.CANCELED
                donation.note = "Donation checkout session expired."
                donation.raw_payload = checkout
                donation.save(update_fields=["status", "note", "raw_payload", "updated_at"])
            return HttpResponse(status=200)

        registration_id = checkout.get("metadata", {}).get("registration_id")
        if registration_id:
            try:
                registration = Registration.objects.select_related("session").get(id=registration_id)
            except Registration.DoesNotExist:
                return HttpResponse(status=200)
            if registration.status != RegistrationStatus.PAID:
                if registration.status != RegistrationStatus.CANCELED:
                    registration.status = RegistrationStatus.CANCELED
                    registration.save(update_fields=["status", "updated_at"])
                existing = PaymentTransaction.objects.filter(
                    registration=registration,
                    transaction_type=TransactionType.PAYMENT_CANCELED,
                    payment_ref=str(checkout.get("id", registration.payment_ref)),
                ).exists()
                if not existing:
                    _create_payment_transaction_once(
                        registration=registration,
                        session=registration.session,
                        transaction_type=TransactionType.PAYMENT_CANCELED,
                        status=registration.status,
                        provider=PaymentProvider.STRIPE,
                        amount=checkout.get("amount_total", registration.amount_paid),
                        currency=str(checkout.get("currency", registration.session.currency)).upper(),
                        payment_ref=str(checkout.get("id", registration.payment_ref)),
                        stripe_payment_intent=str(checkout.get("payment_intent", "")),
                        note="Checkout session expired.",
                    )
                    send_payment_retry_email(registration)
    elif event["type"] == "payment_intent.payment_failed":
        payment_intent = event["data"]["object"]
        donation_id = payment_intent.get("metadata", {}).get("donation_id")
        if donation_id:
            donation = Donation.objects.filter(id=donation_id).first()
            if donation and donation.status != DonationStatus.COMPLETED:
                donation.status = DonationStatus.FAILED
                donation.stripe_payment_intent = str(payment_intent.get("id", donation.stripe_payment_intent))
                donation.note = (
                    payment_intent.get("last_payment_error", {}).get("message")
                    or "Donation payment failed in Stripe."
                )
                donation.raw_payload = payment_intent
                donation.save(update_fields=["status", "stripe_payment_intent", "note", "raw_payload", "updated_at"])
                forward_donation_to_donor_elf(donation)
            return HttpResponse(status=200)

        registration_id = payment_intent.get("metadata", {}).get("registration_id")
        if registration_id:
            try:
                registration = Registration.objects.select_related("session").get(id=registration_id)
            except Registration.DoesNotExist:
                return HttpResponse(status=200)
            if registration.status != RegistrationStatus.PAID:
                if registration.status != RegistrationStatus.CANCELED:
                    registration.status = RegistrationStatus.CANCELED
                    registration.save(update_fields=["status", "updated_at"])
                existing = PaymentTransaction.objects.filter(
                    registration=registration,
                    transaction_type=TransactionType.PAYMENT_ERROR,
                    stripe_payment_intent=str(payment_intent.get("id", "")),
                ).exists()
                if not existing:
                    error_message = (
                        payment_intent.get("last_payment_error", {}).get("message")
                        or "Payment failed in Stripe."
                    )
                    _create_payment_transaction_once(
                        registration=registration,
                        session=registration.session,
                        transaction_type=TransactionType.PAYMENT_ERROR,
                        status=registration.status,
                        provider=PaymentProvider.STRIPE,
                        amount=payment_intent.get("amount", registration.amount_paid),
                        currency=str(payment_intent.get("currency", registration.session.currency)).upper(),
                        payment_ref=registration.payment_ref,
                        stripe_payment_intent=str(payment_intent.get("id", "")),
                        note=f"Stripe payment failed: {error_message}",
                    )
                    send_payment_retry_email(registration)
    elif event["type"] == "invoice.paid":
        invoice = event["data"]["object"]
        subscription_id = str(invoice.get("subscription", ""))
        if subscription_id:
            base_donation = Donation.objects.filter(stripe_subscription_id=subscription_id).first()
            if base_donation:
                provider_ref = str(invoice.get("id", ""))
                existing = Donation.objects.filter(provider_ref=provider_ref).exists()
                if not existing:
                    recurring = Donation.objects.create(
                        provider="STRIPE",
                        provider_ref=provider_ref,
                        frequency=DonationFrequency.MONTHLY,
                        is_anonymous=base_donation.is_anonymous,
                        donor_first_name=base_donation.donor_first_name,
                        donor_last_name=base_donation.donor_last_name,
                        donor_phone=base_donation.donor_phone,
                        donor_address=base_donation.donor_address,
                        donor_email=base_donation.donor_email,
                        donor_message=base_donation.donor_message,
                        amount=invoice.get("amount_paid", base_donation.amount) or base_donation.amount,
                        currency=str(invoice.get("currency", base_donation.currency)).upper(),
                        status=DonationStatus.COMPLETED,
                        note="Recurring monthly donation payment received.",
                        stripe_subscription_id=subscription_id,
                        stripe_customer_id=str(invoice.get("customer", "")),
                        raw_payload=invoice,
                    )
                    forward_donation_to_donor_elf(recurring)
                    send_admin_donation_notification(recurring)
                    send_donation_thank_you(recurring)
            return HttpResponse(status=200)
    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        subscription_id = str(invoice.get("subscription", ""))
        if subscription_id:
            base_donation = Donation.objects.filter(stripe_subscription_id=subscription_id).first()
            if base_donation:
                failure = Donation.objects.create(
                    provider="STRIPE",
                    provider_ref=str(invoice.get("id", "")),
                    frequency=DonationFrequency.MONTHLY,
                    is_anonymous=base_donation.is_anonymous,
                    donor_first_name=base_donation.donor_first_name,
                    donor_last_name=base_donation.donor_last_name,
                    donor_phone=base_donation.donor_phone,
                    donor_address=base_donation.donor_address,
                    donor_email=base_donation.donor_email,
                    donor_message=base_donation.donor_message,
                    amount=invoice.get("amount_due", base_donation.amount) or base_donation.amount,
                    currency=str(invoice.get("currency", base_donation.currency)).upper(),
                    status=DonationStatus.FAILED,
                    note="Recurring monthly donation payment failed.",
                    stripe_subscription_id=subscription_id,
                    stripe_customer_id=str(invoice.get("customer", "")),
                    raw_payload=invoice,
                )
                forward_donation_to_donor_elf(failure)
            return HttpResponse(status=200)
    return HttpResponse(status=200)


@require_GET
def success(request: HttpRequest) -> HttpResponse:
    ref = request.GET.get("ref")
    reg_id = request.GET.get("reg_id")
    registration = None
    # Free registration flow uses reg_id instead of Stripe ref
    if reg_id:
        try:
            registration = Registration.objects.filter(
                id=reg_id, status=RegistrationStatus.PAID
            ).select_related("session").first()
        except (ValueError, TypeError):
            registration = None
    if not registration and ref:
        registration = Registration.objects.filter(payment_ref=ref).select_related("session").first()
        if not registration and settings.STRIPE_SECRET_KEY:
            try:
                checkout = stripe.checkout.Session.retrieve(ref)
            except stripe.error.StripeError:
                checkout = None
            if checkout:
                registration_id = checkout.get("metadata", {}).get("registration_id")
                if registration_id:
                    registration = (
                        Registration.objects.filter(id=registration_id).select_related("session").first()
                    )
                    if registration and registration.payment_ref != ref:
                        registration.payment_ref = ref
                        registration.save(update_fields=["payment_ref", "updated_at"])

        if registration and registration.status != RegistrationStatus.PAID:
            _sync_pending_registration_from_stripe(
                registration, note="Payment confirmed from success-page verification."
            )

        if registration and registration.status == RegistrationStatus.PAID:
            _ensure_registration_confirmation_email(registration)
            _ensure_admin_paid_registration_notification(registration)
            provision_portal_access(registration)
    return render(
        request,
        "intensive/success.html",
        {"registration": registration},
    )


@require_GET
def cancel(request: HttpRequest) -> HttpResponse:
    registration = None
    registration_id = request.GET.get("registration_id")
    if registration_id:
        registration = Registration.objects.filter(id=registration_id).select_related("session").first()
    if registration and registration.status != RegistrationStatus.PAID:
        if registration.status != RegistrationStatus.CANCELED:
            registration.status = RegistrationStatus.CANCELED
            registration.save(update_fields=["status", "updated_at"])

        existing = PaymentTransaction.objects.filter(
            registration=registration,
            transaction_type=TransactionType.PAYMENT_CANCELED,
            payment_ref=registration.payment_ref,
        ).exists()
        if not existing:
            _create_payment_transaction_once(
                registration=registration,
                session=registration.session,
                transaction_type=TransactionType.PAYMENT_CANCELED,
                status=registration.status,
                provider=PaymentProvider.STRIPE,
                amount=registration.amount_paid or registration.session.price,
                currency=registration.session.currency.upper(),
                payment_ref=registration.payment_ref,
                note="Customer canceled payment from checkout page.",
            )
            send_payment_retry_email(registration)
    context = {"registration": registration}
    return render(request, "intensive/cancel.html", context)


@csrf_exempt
@require_POST
def donor_elf_webhook(request: HttpRequest) -> HttpResponse:
    expected_secret = (os.getenv("DONOR_ELF_WEBHOOK_SECRET", "") or "").strip()
    if expected_secret:
        incoming_secret = (
            request.META.get("HTTP_X_DONOR_ELF_SECRET", "")
            or request.GET.get("secret", "")
            or request.POST.get("secret", "")
        ).strip()
        if incoming_secret != expected_secret:
            return HttpResponse(status=403)

    payload: dict = {}
    if request.body:
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
    if not payload:
        payload = request.POST.dict()
    if not payload:
        return HttpResponse(status=400)

    provider_ref = _payload_get(payload, "donation_id", "id", "transaction_id", "reference")
    donor_first_name = _payload_get(payload, "donor_first_name", "first_name")
    donor_last_name = _payload_get(payload, "donor_last_name", "last_name")
    combined = _payload_get(payload, "name", "donor_name", "full_name")
    if not donor_first_name and not donor_last_name and combined:
        parts = combined.strip().split(None, 1)
        donor_first_name = parts[0] if parts else ""
        donor_last_name = parts[1] if len(parts) > 1 else ""
    donor_phone = _payload_get(payload, "donor_phone", "phone")
    donor_address = _payload_get(payload, "donor_address", "address")
    donor_email = _payload_get(payload, "email", "donor_email")
    amount = _to_cents(payload.get("amount") or payload.get("donation_amount") or payload.get("total"))
    currency = _payload_get(payload, "currency", default="USD").upper()
    status = _map_donation_status(_payload_get(payload, "status", "payment_status", "state"))
    event_name = _payload_get(payload, "event", "event_type")
    note = f"Donor Elf webhook{': ' + event_name if event_name else ''}"

    defaults = {
        "provider": "DONOR_ELF",
        "donor_first_name": donor_first_name,
        "donor_last_name": donor_last_name,
        "donor_phone": donor_phone,
        "donor_address": donor_address,
        "donor_email": donor_email,
        "amount": amount,
        "currency": currency or "USD",
        "status": status,
        "note": note,
        "raw_payload": payload,
    }
    if provider_ref:
        Donation.objects.update_or_create(provider_ref=provider_ref, defaults=defaults)
    else:
        Donation.objects.create(provider_ref="", **defaults)
    return HttpResponse(status=200)


@login_required
@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    session_id = request.GET.get("session")
    status = request.GET.get("status")
    if status is None:
        status = RegistrationStatus.PAID
    registrations = Registration.objects.select_related("session").all()
    sessions = Session.objects.order_by("start_date")
    if session_id:
        registrations = registrations.filter(session_id=session_id)
    if status:
        registrations = registrations.filter(status=status)

    # Catch up missed "payment confirmed" admin alerts for recent paid registrations.
    pending_admin_alerts = (
        Registration.objects.select_related("session")
        .filter(status=RegistrationStatus.PAID, admin_paid_notification_sent=False)
        .order_by("-updated_at")[:120]
    )
    for registration in pending_admin_alerts:
        _ensure_admin_paid_registration_notification(registration)

    context = {
        "registrations": registrations[:300],
        "sessions": sessions,
        "selected_session": session_id,
        "selected_status": status,
        "status_choices": RegistrationStatus.choices,
        "admin_page": "registrations",
    }
    return render(request, "intensive/dashboard.html", context)


@login_required
@require_POST
def dashboard_registrations_sync_pending(request: HttpRequest) -> HttpResponse:
    synced_count = 0
    pending = (
        Registration.objects.select_related("session")
        .filter(status=RegistrationStatus.PENDING)
        .exclude(payment_ref="")
        .order_by("-created_at")[:300]
    )
    for registration in pending:
        if _sync_pending_registration_from_stripe(
            registration, note="Payment backfilled from dashboard pending sync."
        ):
            synced_count += 1

    if synced_count:
        messages.success(request, f"Synced {synced_count} pending registration(s) from Stripe.")
    else:
        messages.info(request, "No pending registrations were ready to sync.")
    return redirect("dashboard")


@login_required
def dashboard_free_codes(request: HttpRequest) -> HttpResponse:
    """Generate and list one-time free registration codes. POST generates 10 new codes."""
    if request.method == "POST":
        if request.POST.get("action") == "fix_orphan_codes":
            fixed = FreeRegistrationCode.objects.filter(
                is_used=True,
                used_registration__isnull=True,
            ).update(is_used=False, used_at=None)
            if fixed:
                messages.success(
                    request,
                    f"Reset {fixed} stuck code(s). They show as Available again (registration had been removed).",
                )
            else:
                messages.info(request, "No stuck codes needed fixing.")
            return redirect("dashboard_free_codes")

        count = 10
        codes = []
        for _ in range(count):
            code = _generate_free_registration_code()
            obj = FreeRegistrationCode.objects.create(code=code)
            codes.append(obj.code)
        messages.success(request, f"Generated {len(codes)} new free registration codes.")
        return redirect("dashboard_free_codes")

    codes = FreeRegistrationCode.objects.select_related("used_registration").order_by("-created_at")[:200]
    context = {
        "codes": codes,
        "admin_page": "free_codes",
    }
    return render(request, "intensive/dashboard_free_codes.html", context)


@login_required
@require_GET
def dashboard_donations(request: HttpRequest) -> HttpResponse:
    # Keep dashboard trustworthy even when webhook delivery is delayed or missed.
    stale_without_checkout = (
        Donation.objects.filter(status=DonationStatus.PENDING, stripe_checkout_id="")
        .filter(created_at__lte=timezone.now() - timedelta(minutes=10))
        .order_by("-created_at")[:120]
    )
    for item in stale_without_checkout:
        item.status = DonationStatus.FAILED
        item.note = "Donation checkout was not completed."
        item.save(update_fields=["status", "note", "updated_at"])

    if settings.STRIPE_SECRET_KEY:
        pending = (
            Donation.objects.filter(status=DonationStatus.PENDING)
            .exclude(stripe_checkout_id="")
            .order_by("-created_at")[:200]
        )
        for item in pending:
            _sync_pending_donation_from_stripe(item, note="Donation completed from dashboard sync.")

    status = request.GET.get("status")
    if status is None:
        status = DonationStatus.COMPLETED
    donations = Donation.objects.all()
    if status:
        donations = donations.filter(status=status)
    context = {
        "donations": donations[:500],
        "selected_status": status,
        "status_choices": DonationStatus.choices,
        "admin_page": "donations",
    }
    return render(request, "intensive/dashboard_donations.html", context)


@login_required
@require_POST
def dashboard_registration_delete(request: HttpRequest, item_id: str) -> HttpResponse:
    """Remove a non-PAID registration so the person can sign up again (e.g. retry with a free code)."""
    registration = get_object_or_404(Registration.objects.select_related("session"), id=item_id)
    if registration.status == RegistrationStatus.PAID:
        messages.error(
            request,
            "Paid registrations cannot be deleted. If something is wrong, handle it outside this tool or contact support.",
        )
        return redirect("dashboard_registration_detail", item_id=item_id)

    full_name = registration.full_name
    email = registration.email
    with transaction.atomic():
        StudentDiscountCode.objects.filter(used_registration_id=registration.id).update(
            is_used=False,
            used_at=None,
            used_registration=None,
        )
        registration.delete()
    messages.success(
        request,
        f"Deleted incomplete registration for {full_name} ({email}). They can register again from the public form.",
    )
    return redirect("dashboard")


@login_required
@require_GET
def dashboard_registration_detail(request: HttpRequest, item_id: str) -> HttpResponse:
    registration = get_object_or_404(
        Registration.objects.select_related("session"), id=item_id
    )
    transactions = registration.transactions.order_by("-created_at")
    context = {
        "registration": registration,
        "transactions": transactions,
        "admin_page": "registrations",
    }
    return render(request, "intensive/dashboard_registration_detail.html", context)


@login_required
@require_POST
def dashboard_registration_portal_email(request: HttpRequest, item_id: str) -> HttpResponse:
    """Send portal login email (new password) or reset password and email."""
    registration = get_object_or_404(Registration.objects.select_related("session"), id=item_id)
    if request.POST.get("action") == "reset":
        ok, msg = admin_reset_portal_password_email(registration)
    else:
        ok, msg = admin_send_portal_invite_email(registration)
    if ok:
        messages.success(request, msg)
    else:
        messages.error(request, msg)
    return redirect("dashboard_registration_detail", item_id=item_id)


@login_required
@require_POST
def dashboard_send_portal_invites(request: HttpRequest) -> HttpResponse:
    """Bulk: email portal invites to paid registrants who do not have a portal password yet."""
    session_id = (request.POST.get("session") or "").strip()
    qs = Registration.objects.filter(
        status=RegistrationStatus.PAID,
    ).filter(portal_password_hash="")
    if session_id:
        try:
            uuid.UUID(session_id)
        except ValueError:
            messages.error(request, "Invalid session filter.")
            return redirect("dashboard")
        qs = qs.filter(session_id=session_id)

    sent = 0
    for reg in qs.iterator(chunk_size=100):
        provision_portal_access(reg, send_email=True)
        sent += 1
    if sent:
        messages.success(
            request,
            f"Portal invitation emails sent to {sent} registrant(s) without a password yet.",
        )
    else:
        messages.info(
            request,
            "No matching paid registrants need an initial portal password (or adjust session filter).",
        )
    params: dict[str, str] = {"status": RegistrationStatus.PAID}
    if session_id:
        params["session"] = session_id
    return redirect(f"{reverse('dashboard')}?{urlencode(params)}")


def _hub_days_span_label(until_list: list, now) -> str:
    if not until_list:
        return "—"
    days_vals = []
    any_not_started = False
    for u in until_list:
        if u is None:
            any_not_started = True
        elif u > now:
            days_vals.append(max(0, math.ceil((u - now).total_seconds() / 86400)))
    if not days_vals:
        return "1st login" if any_not_started else "—"
    lo, hi = min(days_vals), max(days_vals)
    out = f"{lo}–{hi} d" if lo != hi else f"{lo} d"
    if any_not_started:
        out += "*"
    return out


@login_required
def dashboard_sessions(request: HttpRequest) -> HttpResponse:
    now = timezone.now()
    items = list(
        Session.objects.annotate(
            portal_video_n=Count("portal_videos"),
            hub_open_n=Count(
                "registrations",
                filter=Q(
                    registrations__status=RegistrationStatus.PAID,
                )
                & (
                    Q(registrations__portal_access_until__isnull=True)
                    | Q(registrations__portal_access_until__gt=now)
                ),
            ),
            hub_signed_in_n=Count(
                "registrations",
                filter=Q(
                    registrations__status=RegistrationStatus.PAID,
                    registrations__portal_access_until__gt=now,
                    registrations__portal_last_login_at__isnull=False,
                ),
            ),
        ).order_by("start_date", "title")
    )
    if items:
        sid_list = [s.id for s in items]
        open_until = Registration.objects.filter(
            session_id__in=sid_list,
            status=RegistrationStatus.PAID,
        ).filter(
            Q(portal_access_until__isnull=True) | Q(portal_access_until__gt=now)
        ).values_list("session_id", "portal_access_until")
        until_by_session = defaultdict(list)
        for sess_id, until in open_until:
            until_by_session[sess_id].append(until)
        for s in items:
            s.hub_days_span = _hub_days_span_label(until_by_session.get(s.id, []), now)
    context = {"items": items, "admin_page": "sessions"}
    return render(request, "intensive/dashboard_sessions.html", context)


@login_required
def dashboard_session_hub_access(request: HttpRequest, session_id) -> HttpResponse:
    session = get_object_or_404(Session, id=session_id)
    now = timezone.now()
    paid = (
        Registration.objects.filter(session=session, status=RegistrationStatus.PAID)
        .order_by("full_name", "email")
    )
    rows = []
    for reg in paid:
        u = reg.portal_access_until
        hub_open = u is None or u > now
        days_left = None
        days_note = ""
        if u is None and hub_open:
            days_note = "Starts at first sign-in"
        elif hub_open and u:
            days_left = max(0, math.ceil((u - now).total_seconds() / 86400))
        rows.append(
            {
                "reg": reg,
                "hub_open": hub_open,
                "days_left": days_left,
                "days_note": days_note,
            }
        )
    return render(
        request,
        "intensive/dashboard_session_hub_access.html",
        {
            "session": session,
            "rows": rows,
            "admin_page": "sessions",
        },
    )


@login_required
@require_POST
def dashboard_session_revoke_portal(request: HttpRequest, session_id, registration_id) -> HttpResponse:
    session = get_object_or_404(Session, id=session_id)
    reg = get_object_or_404(
        Registration,
        id=registration_id,
        session=session,
        status=RegistrationStatus.PAID,
    )
    now = timezone.now()
    reg.portal_access_until = now
    reg.portal_password_hash = ""
    reg.portal_last_login_at = None
    reg.save()
    messages.warning(
        request,
        f"Hub access revoked for {reg.full_name}. They can be given a new window from Registrations if needed.",
    )
    return redirect("dashboard_session_hub_access", session_id=session.id)


@login_required
def dashboard_session_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = SessionManageForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, "Session created successfully.")
            return redirect("dashboard_sessions")
        messages.error(request, "Please fix the errors in the session form.")
    else:
        form = SessionManageForm()

    context = {
        "form": form,
        "edit_obj": None,
        "admin_page": "sessions",
    }
    return render(request, "intensive/dashboard_session_form.html", context)


@login_required
def dashboard_session_edit(request: HttpRequest, item_id: str) -> HttpResponse:
    edit_obj = get_object_or_404(Session, id=item_id)

    if request.method == "POST":
        form = SessionManageForm(request.POST, request.FILES, instance=edit_obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Session updated successfully.")
            return redirect("dashboard_sessions")
        messages.error(request, "Please fix the errors in the session form.")
    else:
        form = SessionManageForm(instance=edit_obj)

    context = {
        "form": form,
        "edit_obj": edit_obj,
        "session_materials": edit_obj.registration_materials.order_by("display_order", "id"),
        "portal_video_count": edit_obj.portal_videos.count(),
        "admin_page": "sessions",
    }
    return render(request, "intensive/dashboard_session_form.html", context)


@login_required
@require_POST
def dashboard_session_delete(request: HttpRequest, item_id: str) -> HttpResponse:
    obj = get_object_or_404(Session, id=item_id)
    obj.delete()
    messages.success(request, "Session deleted.")
    return redirect("dashboard_sessions")


@login_required
def dashboard_schedule(request: HttpRequest) -> HttpResponse:
    context = {
        "items": TrainingScheduleItem.objects.order_by("display_order", "id"),
        "admin_page": "schedule",
    }
    return render(request, "intensive/dashboard_schedule.html", context)


@login_required
def dashboard_schedule_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = TrainingScheduleItemForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Schedule item created successfully.")
            return redirect("dashboard_schedule")
        messages.error(request, "Please fix the errors in the schedule form.")
    else:
        form = TrainingScheduleItemForm()

    context = {
        "form": form,
        "edit_obj": None,
        "admin_page": "schedule",
    }
    return render(request, "intensive/dashboard_schedule_form.html", context)


@login_required
def dashboard_schedule_edit(request: HttpRequest, item_id: int) -> HttpResponse:
    edit_obj = get_object_or_404(TrainingScheduleItem, id=item_id)

    if request.method == "POST":
        form = TrainingScheduleItemForm(request.POST, instance=edit_obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Schedule item updated successfully.")
            return redirect("dashboard_schedule")
        messages.error(request, "Please fix the errors in the schedule form.")
    else:
        form = TrainingScheduleItemForm(instance=edit_obj)

    context = {
        "form": form,
        "edit_obj": edit_obj,
        "admin_page": "schedule",
    }
    return render(request, "intensive/dashboard_schedule_form.html", context)


@login_required
@require_POST
def dashboard_schedule_delete(request: HttpRequest, item_id: int) -> HttpResponse:
    obj = get_object_or_404(TrainingScheduleItem, id=item_id)
    obj.delete()
    messages.success(request, "Schedule item deleted.")
    return redirect("dashboard_schedule")


@login_required
def dashboard_site_settings(request: HttpRequest) -> HttpResponse:
    instance = SiteSetting.objects.first()
    if instance is None:
        instance = SiteSetting.objects.create(site_name="Set Free In Christ")

    if request.method == "POST":
        form = SiteSettingForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, "Site settings updated successfully.")
            return redirect("dashboard_site_settings")
        detailed_error = ""
        for errors in form.errors.values():
            if errors:
                detailed_error = errors[0]
                break
        if detailed_error:
            messages.error(request, f"Please fix the errors in the settings form. {detailed_error}")
        else:
            messages.error(request, "Please fix the errors in the settings form.")
    else:
        form = SiteSettingForm(instance=instance)

    context = {
        "form": form,
        "admin_page": "settings",
    }
    return render(request, "intensive/dashboard_site_settings.html", context)


@login_required
@require_POST
def dashboard_session_material_add(request: HttpRequest, session_id) -> HttpResponse:
    """Add a PDF/doc for this session (confirmation email + learning hub when unlocked)."""
    session = get_object_or_404(Session, id=session_id)
    ALLOWED = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
    file = request.FILES.get("file")
    if not file:
        messages.error(request, "Please select a file to upload.")
        return redirect("dashboard_session_edit", item_id=session.id)
    ext = (file.name or "").lower().split(".")[-1] if "." in (file.name or "") else ""
    if f".{ext}" not in ALLOWED:
        messages.error(request, "File must be PDF, DOC, DOCX, PPT, or PPTX.")
        return redirect("dashboard_session_edit", item_id=session.id)
    max_order = (
        RegistrationMaterial.objects.filter(session=session).aggregate(m=Max("display_order"))["m"] or 0
    )
    RegistrationMaterial.objects.create(session=session, file=file, display_order=max_order + 1)
    messages.success(request, "Material added for this session.")
    return redirect("dashboard_session_edit", item_id=session.id)


@login_required
@require_POST
def dashboard_session_material_delete(request: HttpRequest, session_id, material_id: int) -> HttpResponse:
    session = get_object_or_404(Session, id=session_id)
    material = get_object_or_404(RegistrationMaterial, id=material_id, session=session)
    material.delete()
    messages.success(request, "Material removed.")
    return redirect("dashboard_session_edit", item_id=session.id)


@login_required
def dashboard_speakers(request: HttpRequest) -> HttpResponse:
    context = {
        "items": Speaker.objects.order_by("display_order", "full_name"),
        "admin_page": "speakers",
    }
    return render(request, "intensive/dashboard_speakers.html", context)


@login_required
@require_GET
def dashboard_transactions(request: HttpRequest) -> HttpResponse:
    status = request.GET.get("status")
    if status is None:
        status = RegistrationStatus.PAID
    tx_type = request.GET.get("tx_type")
    if tx_type is None:
        tx_type = TransactionType.PAYMENT_COMPLETED
    transactions = PaymentTransaction.objects.select_related("registration", "session").all()
    if status:
        transactions = transactions.filter(status=status)
    if tx_type:
        transactions = transactions.filter(transaction_type=tx_type)

    # Hide historical duplicate rows created by repeated payment callbacks.
    unique_rows = []
    seen_keys = set()
    for tx in transactions.order_by("-created_at")[:2000]:
        key = (
            tx.registration_id,
            tx.transaction_type,
            tx.provider,
            tx.payment_ref or "",
            tx.stripe_payment_intent or "",
            tx.amount,
            (tx.currency or "").upper(),
            tx.status or "",
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_rows.append(tx)
        if len(unique_rows) >= 500:
            break

    context = {
        "transactions": unique_rows,
        "selected_status": status,
        "selected_tx_type": tx_type,
        "status_choices": RegistrationStatus.choices,
        "tx_type_choices": TransactionType.choices,
        "admin_page": "transactions",
    }
    return render(request, "intensive/dashboard_transactions.html", context)


@login_required
@require_POST
def dashboard_transactions_backfill(request: HttpRequest) -> HttpResponse:
    created_count = 0
    registrations = Registration.objects.select_related("session").all()
    for registration in registrations:
        if registration.transactions.exists():
            continue

        if registration.status == RegistrationStatus.PAID:
            tx_type = TransactionType.PAYMENT_COMPLETED
        elif registration.status == RegistrationStatus.CANCELED:
            tx_type = TransactionType.PAYMENT_CANCELED
        else:
            tx_type = TransactionType.CHECKOUT_CREATED

        PaymentTransaction.objects.create(
            registration=registration,
            session=registration.session,
            transaction_type=tx_type,
            status=registration.status,
            provider=PaymentProvider.STRIPE,
            amount=registration.amount_paid or registration.session.price,
            currency=(registration.currency or registration.session.currency).upper(),
            payment_ref=registration.payment_ref,
            note="Backfilled from existing registration record.",
        )
        created_count += 1

    if created_count:
        messages.success(request, f"Backfill complete. Added {created_count} transaction record(s).")
    else:
        messages.info(request, "Backfill complete. No missing transaction records were found.")
    return redirect("dashboard_transactions")


@login_required
def dashboard_speaker_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = SpeakerForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, "Speaker created successfully.")
            return redirect("dashboard_speakers")
        messages.error(request, "Please fix the errors in the speaker form.")
    else:
        form = SpeakerForm()

    context = {
        "form": form,
        "edit_obj": None,
        "admin_page": "speakers",
    }
    return render(request, "intensive/dashboard_speaker_form.html", context)


@login_required
def dashboard_speaker_edit(request: HttpRequest, item_id: int) -> HttpResponse:
    edit_obj = get_object_or_404(Speaker, id=item_id)

    if request.method == "POST":
        form = SpeakerForm(request.POST, request.FILES, instance=edit_obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Speaker updated successfully.")
            return redirect("dashboard_speakers")
        messages.error(request, "Please fix the errors in the speaker form.")
    else:
        form = SpeakerForm(instance=edit_obj)

    context = {
        "form": form,
        "edit_obj": edit_obj,
        "admin_page": "speakers",
    }
    return render(request, "intensive/dashboard_speaker_form.html", context)


@login_required
@require_POST
def dashboard_speaker_delete(request: HttpRequest, item_id: int) -> HttpResponse:
    obj = get_object_or_404(Speaker, id=item_id)
    obj.delete()
    messages.success(request, "Speaker deleted.")
    return redirect("dashboard_speakers")


@login_required
def dashboard_portal_videos_legacy_redirect(request: HttpRequest) -> HttpResponse:
    """Old global URL — videos are managed per training session."""
    messages.info(
        request,
        "Hub videos are set per training session. Open a session, then use “Hub videos” on that session’s page.",
    )
    return redirect("dashboard_sessions")


@login_required
def dashboard_session_portal_videos(request: HttpRequest, session_id) -> HttpResponse:
    session = get_object_or_404(Session, id=session_id)
    items = session.portal_videos.order_by("display_order", "id")
    return render(
        request,
        "intensive/dashboard_session_portal_videos.html",
        {"session": session, "items": items, "admin_page": "sessions"},
    )


@login_required
def dashboard_session_portal_video_create(request: HttpRequest, session_id) -> HttpResponse:
    session = get_object_or_404(Session, id=session_id)
    if request.method == "POST":
        form = PortalVideoForm(request.POST, request.FILES)
        if form.is_valid():
            video = form.save(commit=False)
            video.session = session
            video.save()
            messages.success(request, "Video added for this session.")
            return redirect("dashboard_session_portal_videos", session_id=session.id)
        messages.error(request, "Please fix the errors below.")
    else:
        form = PortalVideoForm()
    return render(
        request,
        "intensive/dashboard_portal_video_form.html",
        {
            "form": form,
            "edit_obj": None,
            "session": session,
            "admin_page": "sessions",
        },
    )


@login_required
def dashboard_session_portal_video_edit(request: HttpRequest, session_id, item_id: int) -> HttpResponse:
    session = get_object_or_404(Session, id=session_id)
    edit_obj = get_object_or_404(PortalVideo, id=item_id, session=session)
    if request.method == "POST":
        form = PortalVideoForm(request.POST, request.FILES, instance=edit_obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Video updated.")
            return redirect("dashboard_session_portal_videos", session_id=session.id)
        messages.error(request, "Please fix the errors below.")
    else:
        form = PortalVideoForm(instance=edit_obj)
    return render(
        request,
        "intensive/dashboard_portal_video_form.html",
        {
            "form": form,
            "edit_obj": edit_obj,
            "session": session,
            "admin_page": "sessions",
        },
    )


@login_required
@require_POST
def dashboard_session_portal_video_delete(request: HttpRequest, session_id, item_id: int) -> HttpResponse:
    session = get_object_or_404(Session, id=session_id)
    obj = get_object_or_404(PortalVideo, id=item_id, session=session)
    obj.delete()
    messages.success(request, "Video removed.")
    return redirect("dashboard_session_portal_videos", session_id=session.id)


def _export_registrations_filtered_qs(request: HttpRequest):
    """
    Match dashboard filters: ?status= missing => PAID only; ?status= (empty) => all statuses.
    """
    session_id = (request.GET.get("session") or "").strip()
    raw_status = request.GET.get("status")
    if raw_status is None:
        status = RegistrationStatus.PAID
    elif raw_status.strip() == "":
        status = None
    else:
        status = raw_status.strip()

    qs = Registration.objects.select_related("session", "free_registration_code").order_by("created_at")
    if session_id:
        try:
            uuid.UUID(session_id)
        except ValueError:
            return None, "Invalid session id in export link."
        qs = qs.filter(session_id=session_id)
    if status:
        qs = qs.filter(status=status)
    return qs, None


def _export_row_cells(row: Registration) -> list:
    free_code = ""
    if getattr(row, "free_registration_code", None):
        free_code = row.free_registration_code.code or ""
    portal_until = row.portal_access_until.isoformat() if row.portal_access_until else ""
    return [
        row.full_name or "",
        row.email or "",
        row.phone or "",
        row.city or "",
        row.country or "",
        row.church or "",
        "Yes" if row.is_student else "No",
        row.student_id or "",
        row.student_discount_code or "",
        f"{row.discount_amount / 100:.2f}",
        free_code,
        row.session.title if row.session_id else "",
        row.status or "",
        row.payment_ref or "",
        f"{row.amount_paid / 100:.2f}",
        (row.currency or "").upper(),
        portal_until,
        row.created_at.isoformat() if row.created_at else "",
    ]


def _export_registrations_xlsx(qs) -> HttpResponse:
    try:
        from openpyxl import Workbook
    except ImportError:
        return HttpResponseBadRequest("Excel export requires the openpyxl package. Use CSV or run pip install openpyxl.")

    wb = Workbook()
    ws = wb.active
    ws.title = "Registrations"
    headers = [
        "Full Name",
        "Email",
        "Phone",
        "City",
        "Country",
        "Church",
        "Is Student",
        "Student ID",
        "Discount Code",
        "Discount Amount",
        "Free Reg Code",
        "Session",
        "Status",
        "Payment Ref",
        "Amount Paid",
        "Currency",
        "Portal Access Until",
        "Created At",
    ]
    ws.append(headers)
    for row in qs.iterator(chunk_size=500):
        ws.append(_export_row_cells(row))
    buffer = BytesIO()
    wb.save(buffer)
    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="registrations.xlsx"'
    return response


@login_required
@require_GET
def export_csv(request: HttpRequest) -> HttpResponse:
    qs, err = _export_registrations_filtered_qs(request)
    if err:
        return HttpResponseBadRequest(err)
    fmt = (request.GET.get("format") or "csv").lower().strip()
    if fmt in ("xlsx", "excel"):
        return _export_registrations_xlsx(qs)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="registrations.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(
        [
            "Full Name",
            "Email",
            "Phone",
            "City",
            "Country",
            "Church",
            "Is Student",
            "Student ID",
            "Discount Code",
            "Discount Amount",
            "Free Reg Code",
            "Session",
            "Status",
            "Payment Ref",
            "Amount Paid",
            "Currency",
            "Portal Access Until",
            "Created At",
        ]
    )
    for row in qs.iterator(chunk_size=500):
        writer.writerow(_export_row_cells(row))
    return response
