import csv
import json
import os
import secrets
import string
import re
from datetime import timedelta
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
from django.db.models import Max
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .forms import (
    DonationForm,
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
    build_donation_manage_url,
    forward_donation_to_donor_elf,
    DONATION_MANAGE_SALT,
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


def _home_context(reg_form: dict | None = None, focus_register: bool = False) -> dict:
    sessions = Session.objects.filter(is_active=True).order_by("start_date")
    schedule_items = TrainingScheduleItem.objects.filter(is_active=True).order_by("display_order")
    today = timezone.localdate()
    next_session = sessions.filter(start_date__gte=today).first()
    upcoming_session = next_session or sessions.first()
    speakers = Speaker.objects.filter(is_active=True).prefetch_related("sessions")
    if upcoming_session:
        session_speakers = speakers.filter(sessions=upcoming_session)
        if session_speakers.exists():
            speakers = session_speakers
    site_setting = SiteSetting.objects.first()
    program_pdf = site_setting.event_program_pdf if site_setting else None
    return {
        "sessions": sessions,
        "schedule_items": schedule_items,
        "countdown_session": next_session,
        "speakers": speakers,
        "speakers_session": upcoming_session,
        "venue_address": site_setting.venue_address if site_setting else DEFAULT_VENUE_ADDRESS,
        "donation_url": site_setting.donation_url if site_setting and site_setting.donation_url else "",
        "student_discount_percent": site_setting.student_discount_percent if site_setting else 0,
        "program_pdf_url": program_pdf.url if program_pdf else "",
        "stripe_publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
        "reg_form": reg_form or {},
        "focus_register": focus_register,
        "country_choices": RegistrationForm.COUNTRY_CHOICES,
        "country_options": RegistrationForm.COUNTRY_OPTIONS,
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
                send_admin_new_registration_notification(registration)
                _ensure_registration_confirmation_email(registration)
                _ensure_admin_paid_registration_notification(registration)
                return redirect(f"{reverse('success')}?reg_id={registration.id}")

        messages.error(request, "Invalid or already-used discount code.")
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
        values = {
            "amount": request.POST.get("amount", ""),
            "frequency": request.POST.get("frequency", DonationFrequency.ONE_TIME),
            "is_anonymous": request.POST.get("is_anonymous", ""),
            "full_name": request.POST.get("full_name", ""),
            "email": request.POST.get("email", ""),
            "message": request.POST.get("message", ""),
        }
        return render(request, "intensive/donate.html", _donation_context(values))

    if not settings.STRIPE_SECRET_KEY:
        messages.error(request, "Stripe is not configured yet. Please contact support.")
        values = {
            "amount": request.POST.get("amount", ""),
            "frequency": request.POST.get("frequency", DonationFrequency.ONE_TIME),
            "is_anonymous": request.POST.get("is_anonymous", ""),
            "full_name": request.POST.get("full_name", ""),
            "email": request.POST.get("email", ""),
            "message": request.POST.get("message", ""),
        }
        return render(request, "intensive/donate.html", _donation_context(values))

    base_amount_cents = int(form.cleaned_data["amount"] * 100)
    amount_cents, processing_fee_cents = _gross_up_amount_for_processing_fee(base_amount_cents)
    is_anonymous = form.cleaned_data.get("is_anonymous", False)
    donation = Donation.objects.create(
        provider="STRIPE",
        frequency=form.cleaned_data["frequency"],
        is_anonymous=is_anonymous,
        donor_name="" if is_anonymous else form.cleaned_data.get("full_name", ""),
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
        values = {
            "amount": request.POST.get("amount", ""),
            "frequency": request.POST.get("frequency", DonationFrequency.ONE_TIME),
            "is_anonymous": request.POST.get("is_anonymous", ""),
            "full_name": request.POST.get("full_name", ""),
            "email": request.POST.get("email", ""),
            "message": request.POST.get("message", ""),
        }
        messages.error(
            request,
            "We could not start the donation checkout. Please try again or use one-time donation for now.",
        )
        return render(request, "intensive/donate.html", _donation_context(values))

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
                        _ensure_registration_confirmation_email(registration)
                        _ensure_admin_paid_registration_notification(registration)
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
                        donor_name=base_donation.donor_name,
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
                    donor_name=base_donation.donor_name,
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
    donor_name = _payload_get(payload, "name", "donor_name", "full_name")
    donor_email = _payload_get(payload, "email", "donor_email")
    amount = _to_cents(payload.get("amount") or payload.get("donation_amount") or payload.get("total"))
    currency = _payload_get(payload, "currency", default="USD").upper()
    status = _map_donation_status(_payload_get(payload, "status", "payment_status", "state"))
    event_name = _payload_get(payload, "event", "event_type")
    note = f"Donor Elf webhook{': ' + event_name if event_name else ''}"

    defaults = {
        "provider": "DONOR_ELF",
        "donor_name": donor_name,
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
def dashboard_sessions(request: HttpRequest) -> HttpResponse:
    context = {
        "items": Session.objects.order_by("start_date", "title"),
        "admin_page": "sessions",
    }
    return render(request, "intensive/dashboard_sessions.html", context)


@login_required
def dashboard_session_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = SessionManageForm(request.POST)
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
        form = SessionManageForm(request.POST, instance=edit_obj)
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
        "registration_materials": RegistrationMaterial.objects.order_by("display_order", "id"),
        "admin_page": "settings",
    }
    return render(request, "intensive/dashboard_site_settings.html", context)


@login_required
@require_POST
def dashboard_settings_material_add(request: HttpRequest) -> HttpResponse:
    """Add a registration material (PDF/doc attached to confirmation emails)."""
    ALLOWED = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
    file = request.FILES.get("file")
    if not file:
        messages.error(request, "Please select a file to upload.")
        return redirect("dashboard_site_settings")
    ext = (file.name or "").lower().split(".")[-1] if "." in (file.name or "") else ""
    if f".{ext}" not in ALLOWED:
        messages.error(request, "File must be PDF, DOC, DOCX, PPT, or PPTX.")
        return redirect("dashboard_site_settings")
    max_order = RegistrationMaterial.objects.aggregate(m=Max("display_order"))["m"] or 0
    RegistrationMaterial.objects.create(file=file, display_order=max_order + 1)
    messages.success(request, "Material added. It will be attached to confirmation emails.")
    return redirect("dashboard_site_settings")


@login_required
@require_POST
def dashboard_settings_material_delete(request: HttpRequest, material_id: int) -> HttpResponse:
    material = get_object_or_404(RegistrationMaterial, id=material_id)
    material.delete()
    messages.success(request, "Material removed.")
    return redirect("dashboard_site_settings")


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
@require_GET
def export_csv(request: HttpRequest) -> HttpResponse:
    session_id = request.GET.get("session")
    status = request.GET.get("status")
    registrations = Registration.objects.select_related("session").all()
    if session_id:
        registrations = registrations.filter(session_id=session_id)
    if status:
        registrations = registrations.filter(status=status)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = "attachment; filename=registrations.csv"
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
            "Session",
            "Status",
            "Payment Ref",
            "Amount Paid",
            "Currency",
            "Created At",
        ]
    )
    for row in registrations:
        writer.writerow(
            [
                row.full_name,
                row.email,
                row.phone,
                row.city,
                row.country,
                row.church,
                "Yes" if row.is_student else "No",
                row.student_id,
                row.student_discount_code,
                f"{row.discount_amount / 100:.2f}",
                row.session.title,
                row.status,
                row.payment_ref,
                row.amount_paid,
                row.currency,
                row.created_at.isoformat(),
            ]
        )
    return response
