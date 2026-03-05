import csv

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .forms import (
    RegistrationForm,
    SessionManageForm,
    SiteSettingForm,
    SpeakerForm,
    TrainingScheduleItemForm,
)
from .models import (
    PaymentTransaction,
    PaymentProvider,
    Registration,
    RegistrationStatus,
    Session,
    SiteSetting,
    Speaker,
    TransactionType,
    TrainingScheduleItem,
)
from .services import (
    send_admin_new_registration_notification,
    send_payment_retry_email,
    send_registration_confirmation,
)

stripe.api_key = settings.STRIPE_SECRET_KEY
DEFAULT_VENUE_ADDRESS = "Freedom Revival Center, 1200 Main St, Dallas, TX 75202, USA"


def _build_checkout_session(registration: Registration, session: Session) -> stripe.checkout.Session:
    return stripe.checkout.Session.create(
        mode="payment",
        customer_email=registration.email,
        line_items=[
            {
                "price_data": {
                    "currency": session.currency.lower(),
                    "unit_amount": session.price,
                    "product_data": {"name": f"3 Day Freedom Intensive - {session.title}"},
                },
                "quantity": 1,
            }
        ],
        metadata={
            "registration_id": str(registration.id),
            "session_id": str(session.id),
        },
        success_url=f"{settings.SITE_BASE_URL}/success?ref={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.SITE_BASE_URL}/cancel?registration_id={registration.id}",
    )


def _home_context(reg_form: dict | None = None) -> dict:
    sessions = Session.objects.filter(is_active=True).order_by("start_date")
    schedule_items = TrainingScheduleItem.objects.filter(is_active=True).order_by("display_order")
    today = timezone.localdate()
    upcoming_session = sessions.filter(start_date__gte=today).first() or sessions.first()
    speakers = Speaker.objects.filter(is_active=True).prefetch_related("sessions")
    if upcoming_session:
        session_speakers = speakers.filter(sessions=upcoming_session)
        if session_speakers.exists():
            speakers = session_speakers
    site_setting = SiteSetting.objects.first()
    return {
        "sessions": sessions,
        "schedule_items": schedule_items,
        "speakers": speakers,
        "speakers_session": upcoming_session,
        "venue_address": site_setting.venue_address if site_setting else DEFAULT_VENUE_ADDRESS,
        "stripe_publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
        "reg_form": reg_form or {},
        "country_choices": RegistrationForm.COUNTRY_CHOICES,
        "country_options": RegistrationForm.COUNTRY_OPTIONS,
    }


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    return render(request, "intensive/home.html", _home_context())


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
            "session_id": raw.get("session_id", ""),
        }
        return render(request, "intensive/home.html", _home_context(form_values))

    session = get_object_or_404(Session, id=form.cleaned_data["session_id"], is_active=True)
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
            "session_id": str(form.cleaned_data["session_id"]),
        }
        return render(request, "intensive/home.html", _home_context(form_values))

    registration = Registration.objects.create(
        full_name=form.cleaned_data["full_name"],
        email=form.cleaned_data["email"],
        phone=form.cleaned_data["phone"],
        city=form.cleaned_data["city"],
        country=form.cleaned_data["country"],
        church=form.cleaned_data["church"],
        session=session,
        status=RegistrationStatus.PENDING,
        payment_provider=PaymentProvider.STRIPE,
        amount_paid=session.price,
        currency=session.currency,
    )
    send_admin_new_registration_notification(registration)

    if not settings.STRIPE_SECRET_KEY:
        messages.error(request, "Stripe is not configured yet. Please add keys in .env.")
        registration.status = RegistrationStatus.CANCELED
        registration.save(update_fields=["status", "updated_at"])
        PaymentTransaction.objects.create(
            registration=registration,
            session=session,
            transaction_type=TransactionType.PAYMENT_ERROR,
            status=registration.status,
            provider=PaymentProvider.STRIPE,
            amount=session.price,
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
            "session_id": str(form.cleaned_data["session_id"]),
        }
        return render(request, "intensive/home.html", _home_context(form_values))

    checkout_session = _build_checkout_session(registration, session)
    registration.payment_ref = checkout_session.id
    registration.save(update_fields=["payment_ref", "updated_at"])
    PaymentTransaction.objects.create(
        registration=registration,
        session=session,
        transaction_type=TransactionType.CHECKOUT_CREATED,
        status=registration.status,
        provider=PaymentProvider.STRIPE,
        amount=session.price,
        currency=session.currency.upper(),
        payment_ref=checkout_session.id,
        stripe_payment_intent=str(checkout_session.get("payment_intent", "")),
        note="Checkout session created.",
    )
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
    PaymentTransaction.objects.create(
        registration=registration,
        session=session,
        transaction_type=TransactionType.CHECKOUT_CREATED,
        status=registration.status,
        provider=PaymentProvider.STRIPE,
        amount=session.price,
        currency=session.currency.upper(),
        payment_ref=checkout_session.id,
        stripe_payment_intent=str(checkout_session.get("payment_intent", "")),
        note="Checkout session created from retry link.",
    )
    return redirect(checkout_session.url)


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
                        PaymentTransaction.objects.create(
                            registration=registration,
                            session=session,
                            transaction_type=TransactionType.PAYMENT_CANCELED,
                            status=registration.status,
                            provider=PaymentProvider.STRIPE,
                            amount=checkout.get("amount_total", session.price),
                            currency=str(checkout.get("currency", session.currency)).upper(),
                            payment_ref=str(checkout.get("id", registration.payment_ref)),
                            stripe_payment_intent=str(checkout.get("payment_intent", "")),
                            note="Canceled after payment because capacity was full.",
                        )
                    else:
                        registration.status = RegistrationStatus.PAID
                        registration.payment_ref = checkout.get("id", registration.payment_ref)
                        registration.amount_paid = checkout.get("amount_total", session.price)
                        registration.currency = checkout.get("currency", session.currency).upper()
                        registration.updated_at = timezone.now()
                        registration.save()
                        PaymentTransaction.objects.create(
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
                        send_registration_confirmation(registration)
            except Registration.DoesNotExist:
                return HttpResponse(status=200)
    elif event["type"] == "checkout.session.expired":
        checkout = event["data"]["object"]
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
                    PaymentTransaction.objects.create(
                        registration=registration,
                        session=registration.session,
                        transaction_type=TransactionType.PAYMENT_CANCELED,
                        status=registration.status,
                        provider=PaymentProvider.STRIPE,
                        amount=checkout.get("amount_total", registration.session.price),
                        currency=str(checkout.get("currency", registration.session.currency)).upper(),
                        payment_ref=str(checkout.get("id", registration.payment_ref)),
                        stripe_payment_intent=str(checkout.get("payment_intent", "")),
                        note="Checkout session expired.",
                    )
                    send_payment_retry_email(registration)
    return HttpResponse(status=200)


@require_GET
def success(request: HttpRequest) -> HttpResponse:
    ref = request.GET.get("ref")
    registration = None
    if ref:
        registration = Registration.objects.filter(payment_ref=ref).select_related("session").first()
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
            PaymentTransaction.objects.create(
                registration=registration,
                session=registration.session,
                transaction_type=TransactionType.PAYMENT_CANCELED,
                status=registration.status,
                provider=PaymentProvider.STRIPE,
                amount=registration.session.price,
                currency=registration.session.currency.upper(),
                payment_ref=registration.payment_ref,
                note="Customer canceled payment from checkout page.",
            )
            send_payment_retry_email(registration)
    context = {"registration": registration}
    return render(request, "intensive/cancel.html", context)


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

    if request.method == "POST":
        form = SiteSettingForm(request.POST, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, "Site settings updated successfully.")
            return redirect("dashboard_site_settings")
        messages.error(request, "Please fix the errors in the settings form.")
    else:
        form = SiteSettingForm(instance=instance)

    context = {
        "form": form,
        "admin_page": "settings",
    }
    return render(request, "intensive/dashboard_site_settings.html", context)


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

    context = {
        "transactions": transactions[:500],
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
                row.session.title,
                row.status,
                row.payment_ref,
                row.amount_paid,
                row.currency,
                row.created_at.isoformat(),
            ]
        )
    return response
