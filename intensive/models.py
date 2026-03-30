import uuid

from django.db import models
from django.utils import timezone


class RegistrationStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PAID = "PAID", "Paid"
    CANCELED = "CANCELED", "Canceled"


class PaymentProvider(models.TextChoices):
    STRIPE = "STRIPE", "Stripe"
    FREE_CODE = "FREE_CODE", "Free Registration Code"


class TransactionType(models.TextChoices):
    CHECKOUT_CREATED = "CHECKOUT_CREATED", "Checkout Created"
    PAYMENT_COMPLETED = "PAYMENT_COMPLETED", "Payment Completed"
    PAYMENT_CANCELED = "PAYMENT_CANCELED", "Payment Canceled"
    PAYMENT_ERROR = "PAYMENT_ERROR", "Payment Error"


class Session(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=120)
    location = models.CharField(max_length=180, default="Dallas, TX, USA")
    start_date = models.DateField()
    end_date = models.DateField()
    capacity = models.PositiveIntegerField()
    price = models.PositiveIntegerField(help_text="Stored in the smallest currency unit.")
    currency = models.CharField(max_length=8, default="USD")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_date", "title"]

    def __str__(self) -> str:
        return f"{self.title} ({self.start_date} to {self.end_date})"

    @property
    def paid_count(self) -> int:
        return self.registrations.filter(status=RegistrationStatus.PAID).count()

    @property
    def seats_left(self) -> int:
        return max(self.capacity - self.paid_count, 0)

    @property
    def display_price(self) -> str:
        return f"{self.price / 100:.2f} {self.currency.upper()}"


class TrainingScheduleItem(models.Model):
    day_name = models.CharField(max_length=32)
    start_time = models.TimeField()
    end_time = models.TimeField()
    lunch_start = models.TimeField()
    lunch_end = models.TimeField()
    display_order = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "id"]

    def __str__(self) -> str:
        return f"{self.day_name}: {self.start_time} - {self.end_time}"


class SiteSetting(models.Model):
    site_name = models.CharField(max_length=120, default="Set Free In Christ")
    venue_address = models.CharField(
        max_length=255,
        default="Freedom Revival Center, 1200 Main St, Dallas, TX 75202, USA",
    )
    donation_url = models.URLField(blank=True, help_text="Public donation page URL (for example Donor Elf).")
    student_discount_percent = models.PositiveSmallIntegerField(
        default=0,
        help_text="Student discount percent for registration (0-95).",
    )
    event_program_pdf = models.FileField(
        upload_to="registration_materials/",
        blank=True,
        help_text="Event program PDF: shown as download link on homepage and attached to confirmation emails.",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site Setting"
        verbose_name_plural = "Site Settings"

    def __str__(self) -> str:
        return self.site_name


class RegistrationMaterial(models.Model):
    """Additional PDF/document attached to confirmation emails (not the event program)."""

    file = models.FileField(upload_to="registration_materials/")
    display_order = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["display_order", "id"]

    def __str__(self) -> str:
        return self.file.name.split("/")[-1] if self.file else "Material"


class PortalVideo(models.Model):
    """Training videos for paid registrants (stream on site only when using uploaded file)."""

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    video_file = models.FileField(
        upload_to="portal_videos/",
        blank=True,
        help_text="Uploaded video (MP4 recommended). For very large files, use External URL instead.",
    )
    external_url = models.URLField(
        blank=True,
        help_text="Optional: YouTube/Vimeo or other link. If set, viewers watch here without file upload.",
    )
    display_order = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "id"]

    def __str__(self) -> str:
        return self.title

    def clean(self) -> None:
        from django.core.exceptions import ValidationError

        if not self.video_file and not (self.external_url or "").strip():
            raise ValidationError("Add a video file or an external URL.")


class Speaker(models.Model):
    full_name = models.CharField(max_length=140)
    role_title = models.CharField(max_length=180)
    role_subtitle = models.TextField(blank=True)
    country_code = models.CharField(max_length=2, blank=True, help_text="ISO alpha-2 code, e.g. us, gb")
    country_label = models.CharField(max_length=80, blank=True)
    photo_image = models.ImageField(upload_to="speakers/", blank=True)
    photo_url = models.URLField(blank=True, help_text="Use an image URL for speaker headshot.")
    read_more_url = models.URLField(blank=True)
    sessions = models.ManyToManyField(Session, blank=True, related_name="speakers")
    display_order = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "full_name"]

    def __str__(self) -> str:
        return self.full_name


class FreeRegistrationCode(models.Model):
    """One-time codes for 100% free registration. Once used, the code is invalid."""

    code = models.CharField(max_length=32, unique=True, db_index=True)
    is_used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)
    used_registration = models.ForeignKey(
        "Registration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="free_codes_used",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["is_used"])]

    def __str__(self) -> str:
        status = "used" if self.is_used else "available"
        return f"{self.code} ({status})"


class Registration(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    full_name = models.CharField(max_length=160)
    email = models.EmailField()
    phone = models.CharField(max_length=40)
    city = models.CharField(max_length=120, blank=True)
    country = models.CharField(max_length=120, blank=True)
    church = models.CharField(max_length=160, blank=True)
    is_student = models.BooleanField(default=False)
    student_id = models.CharField(max_length=6, blank=True)
    student_discount_code = models.CharField(max_length=32, blank=True)
    discount_amount = models.PositiveIntegerField(
        default=0,
        help_text="Discount in the smallest currency unit.",
    )
    session = models.ForeignKey(
        Session, on_delete=models.PROTECT, related_name="registrations"
    )
    free_registration_code = models.ForeignKey(
        "FreeRegistrationCode",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="registrations",
        help_text="One-time free registration code used, if any.",
    )
    status = models.CharField(
        max_length=16,
        choices=RegistrationStatus.choices,
        default=RegistrationStatus.PENDING,
    )
    payment_provider = models.CharField(
        max_length=16, choices=PaymentProvider.choices, default=PaymentProvider.STRIPE
    )
    payment_ref = models.CharField(max_length=200, blank=True)
    amount_paid = models.PositiveIntegerField(default=0)
    confirmation_email_sent = models.BooleanField(default=False)
    admin_paid_notification_sent = models.BooleanField(default=False)
    currency = models.CharField(max_length=8, default="USD")
    portal_password_hash = models.CharField(
        max_length=128,
        blank=True,
        help_text="Hashed password for the participant resource portal.",
    )
    portal_access_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="After this time, portal login and downloads stop for this registration.",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["status"]),
            models.Index(fields=["portal_access_until"], name="intensive_reg_portal_until"),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} - {self.session.title} ({self.status})"

    @property
    def display_amount_paid(self) -> str:
        return f"{self.amount_paid / 100:.2f} {self.currency.upper()}"

    @property
    def display_discount_amount(self) -> str:
        return f"{self.discount_amount / 100:.2f} {self.currency.upper()}"


class PaymentTransaction(models.Model):
    registration = models.ForeignKey(
        Registration, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions"
    )
    session = models.ForeignKey(
        Session, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions"
    )
    transaction_type = models.CharField(max_length=32, choices=TransactionType.choices)
    status = models.CharField(max_length=16, choices=RegistrationStatus.choices, blank=True)
    provider = models.CharField(max_length=16, choices=PaymentProvider.choices, default=PaymentProvider.STRIPE)
    amount = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=8, default="USD")
    payment_ref = models.CharField(max_length=200, blank=True)
    stripe_payment_intent = models.CharField(max_length=200, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.transaction_type} - {self.payment_ref or 'N/A'}"

    @property
    def display_amount(self) -> str:
        return f"{self.amount / 100:.2f} {self.currency.upper()}"


class DonationStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"
    CANCELED = "CANCELED", "Canceled"


class DonationFrequency(models.TextChoices):
    ONE_TIME = "ONE_TIME", "One-time"
    MONTHLY = "MONTHLY", "Monthly"


class Donation(models.Model):
    provider = models.CharField(max_length=32, default="STRIPE")
    provider_ref = models.CharField(max_length=200, blank=True)
    frequency = models.CharField(max_length=16, choices=DonationFrequency.choices, default=DonationFrequency.ONE_TIME)
    is_anonymous = models.BooleanField(default=False)
    donor_name = models.CharField(max_length=160, blank=True)
    donor_email = models.EmailField(blank=True)
    donor_message = models.CharField(max_length=255, blank=True)
    amount = models.PositiveIntegerField(default=0, help_text="Stored in the smallest currency unit.")
    currency = models.CharField(max_length=8, default="USD")
    status = models.CharField(max_length=16, choices=DonationStatus.choices, default=DonationStatus.PENDING)
    note = models.CharField(max_length=255, blank=True)
    stripe_checkout_id = models.CharField(max_length=200, blank=True)
    stripe_payment_intent = models.CharField(max_length=200, blank=True)
    stripe_subscription_id = models.CharField(max_length=200, blank=True)
    stripe_customer_id = models.CharField(max_length=200, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["provider_ref"]),
            models.Index(fields=["donor_email"]),
        ]

    def __str__(self) -> str:
        return f"{self.provider} {self.provider_ref or self.id}"

    @property
    def display_amount(self) -> str:
        return f"{self.amount / 100:.2f} {self.currency.upper()}"


class StudentDiscountCode(models.Model):
    student_id = models.CharField(max_length=6)
    email = models.EmailField()
    code = models.CharField(max_length=32, unique=True)
    is_used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)
    used_registration = models.ForeignKey(
        Registration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="student_codes_used",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["student_id", "email"]),
            models.Index(fields=["code"]),
            models.Index(fields=["is_used"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} ({self.student_id})"
