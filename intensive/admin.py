from django.contrib import admin
from django.db.models import Count, Q

from .models import (
    Donation,
    PaymentTransaction,
    Registration,
    RegistrationMaterial,
    Session,
    SiteSetting,
    Speaker,
    StudentDiscountCode,
    TrainingScheduleItem,
)


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "location",
        "start_date",
        "end_date",
        "capacity",
        "paid_registrations",
        "places_left",
        "price",
        "currency",
        "is_active",
    )
    list_filter = ("is_active", "currency", "start_date", "location")
    search_fields = ("title", "location")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            paid_registrations_count=Count(
                "registrations",
                filter=Q(registrations__status="PAID"),
            )
        )

    @admin.display(description="Paid")
    def paid_registrations(self, obj):
        return getattr(obj, "paid_registrations_count", obj.paid_count)

    @admin.display(description="Places Left")
    def places_left(self, obj):
        paid = getattr(obj, "paid_registrations_count", obj.paid_count)
        return max(obj.capacity - paid, 0)


@admin.register(Registration)
class RegistrationAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "email",
        "phone",
        "city",
        "country",
        "is_student",
        "student_id",
        "session",
        "status",
        "amount_paid",
        "currency",
        "created_at",
    )
    list_filter = ("status", "payment_provider", "currency", "session")
    search_fields = ("full_name", "email", "phone", "city", "country", "payment_ref", "student_id")


@admin.register(TrainingScheduleItem)
class TrainingScheduleItemAdmin(admin.ModelAdmin):
    list_display = (
        "display_order",
        "day_name",
        "start_time",
        "end_time",
        "lunch_start",
        "lunch_end",
        "is_active",
    )
    list_filter = ("is_active",)
    search_fields = ("day_name",)
    ordering = ("display_order", "id")


@admin.register(SiteSetting)
class SiteSettingAdmin(admin.ModelAdmin):
    list_display = (
        "site_name",
        "venue_address",
        "donation_url",
        "student_discount_percent",
        "has_event_program_pdf",
        "updated_at",
    )

    @admin.display(description="Event Program")
    def has_event_program_pdf(self, obj):
        return bool(obj.event_program_pdf)


@admin.register(RegistrationMaterial)
class RegistrationMaterialAdmin(admin.ModelAdmin):
    list_display = ("id", "file", "display_order", "created_at")
    list_editable = ("display_order",)
    ordering = ("display_order", "id")


@admin.register(Speaker)
class SpeakerAdmin(admin.ModelAdmin):
    list_display = ("full_name", "role_title", "country_label", "display_order", "is_active")
    list_filter = ("is_active", "country_label")
    search_fields = ("full_name", "role_title", "country_label")
    filter_horizontal = ("sessions",)


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "transaction_type",
        "status",
        "provider",
        "amount",
        "currency",
        "payment_ref",
        "registration",
    )
    list_filter = ("transaction_type", "status", "provider", "currency")
    search_fields = ("payment_ref", "stripe_payment_intent", "note", "registration__email", "registration__full_name")


@admin.register(Donation)
class DonationAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "provider",
        "provider_ref",
        "donor_name",
        "frequency",
        "is_anonymous",
        "donor_email",
        "amount",
        "currency",
        "status",
    )
    list_filter = ("provider", "status", "currency")
    search_fields = ("provider_ref", "donor_name", "donor_email", "note")


@admin.register(StudentDiscountCode)
class StudentDiscountCodeAdmin(admin.ModelAdmin):
    list_display = ("created_at", "student_id", "email", "code", "is_used", "used_at", "used_registration")
    list_filter = ("is_used", "created_at")
    search_fields = ("student_id", "email", "code")
