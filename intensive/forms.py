import re
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

from django import forms
import pycountry

from .models import DonationFrequency, PortalVideo, Session, SiteSetting, Speaker, TrainingScheduleItem

# Shared rules for session event program uploads (PDF / common doc types).
_EVENT_PROGRAM_ALLOWED_EXT = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
_EVENT_PROGRAM_ALLOWED_CT = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def validate_event_program_upload(file):
    """Return file if valid; raise ValidationError otherwise."""
    if not file:
        return file
    file_name = str(getattr(file, "name", ""))
    content_type = str(getattr(file, "content_type", "")).lower()
    file_ext = Path(file_name).suffix.lower()
    ext_ok = file_ext in _EVENT_PROGRAM_ALLOWED_EXT
    ct_ok = content_type in _EVENT_PROGRAM_ALLOWED_CT or content_type in ("application/x-pdf",)
    if content_type == "application/octet-stream" and ext_ok:
        ct_ok = True
    if not ext_ok and not ct_ok:
        raise forms.ValidationError("Event program must be a PDF, DOC, DOCX, PPT, or PPTX file.")
    return file


def _flag_emoji(alpha_2: str) -> str:
    return "".join(chr(127397 + ord(char)) for char in alpha_2.upper())


def _country_choices() -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = [("", "Select country...")]
    countries = sorted(pycountry.countries, key=lambda item: item.name)
    for item in countries:
        label = f"{_flag_emoji(item.alpha_2)} {item.name}"
        choices.append((item.name, label))
    return choices


def _country_options() -> list[tuple[str, str, str]]:
    countries = sorted(pycountry.countries, key=lambda item: item.name)
    return [(item.name, f"{_flag_emoji(item.alpha_2)} {item.name}", item.alpha_2.lower()) for item in countries]


class RegistrationForm(forms.Form):
    COUNTRY_CHOICES = _country_choices()
    COUNTRY_OPTIONS = _country_options()

    full_name = forms.CharField(max_length=160)
    email = forms.EmailField(max_length=254)
    phone = forms.CharField(max_length=40)
    city = forms.CharField(max_length=120)
    country = forms.ChoiceField(choices=COUNTRY_CHOICES)
    church = forms.CharField(max_length=160, required=False)
    is_student = forms.BooleanField(required=False)
    student_id = forms.CharField(max_length=6, required=False)
    student_discount_code = forms.CharField(max_length=32, required=False)
    discount_code = forms.CharField(max_length=32, required=False, label="Free registration code")
    session_id = forms.UUIDField()

    def clean_session_id(self):
        session_id = self.cleaned_data["session_id"]
        try:
            session = Session.objects.get(id=session_id, is_active=True)
        except Session.DoesNotExist as exc:
            raise forms.ValidationError("The selected session is not available.") from exc
        return session.id

    def clean_full_name(self):
        full_name = self.cleaned_data["full_name"].strip()
        if len(full_name) < 2:
            raise forms.ValidationError("Please enter your full name.")
        return full_name

    def clean_phone(self):
        phone = self.cleaned_data["phone"].strip()
        if phone.count("+") > 1 or ("+" in phone and not phone.startswith("+")):
            raise forms.ValidationError("Use one country code prefix, like +1 or +234.")
        normalized = re.sub(r"[^\d+]", "", phone)
        digit_count = len(re.sub(r"\D", "", normalized))
        if digit_count < 7 or digit_count > 15:
            raise forms.ValidationError("Please enter a valid international phone number.")
        return phone

    def clean_city(self):
        city = self.cleaned_data["city"].strip()
        if len(city) < 2:
            raise forms.ValidationError("Please enter your city.")
        return city

    def clean_student_id(self):
        student_id = (self.cleaned_data.get("student_id") or "").strip()
        is_student = self.cleaned_data.get("is_student", False)
        if not is_student:
            return ""
        if not re.fullmatch(r"\d{6}", student_id):
            raise forms.ValidationError("Andrews University student ID must be exactly 6 digits.")
        return student_id

    def clean_student_discount_code(self):
        return (self.cleaned_data.get("student_discount_code") or "").strip().upper()

    def clean_discount_code(self):
        # Allow pasting with spaces or dashes; stored codes are alphanumeric only.
        raw = (self.cleaned_data.get("discount_code") or "").strip().upper()
        return re.sub(r"[\s\-]+", "", raw)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_student", False) and not cleaned.get("student_id"):
            self.add_error("student_id", "Please enter your 6-digit student ID.")
        if not cleaned.get("is_student", False):
            cleaned["student_discount_code"] = ""
        return cleaned


class DonationForm(forms.Form):
    amount = forms.DecimalField(
        min_value=1,
        max_digits=10,
        decimal_places=2,
        help_text="Enter amount in USD.",
    )
    frequency = forms.ChoiceField(choices=DonationFrequency.choices, initial=DonationFrequency.ONE_TIME)
    is_anonymous = forms.BooleanField(required=False)
    full_name = forms.CharField(max_length=160, required=False)
    email = forms.EmailField(max_length=254, required=False)
    message = forms.CharField(max_length=255, required=False)

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount < 5:
            raise forms.ValidationError("Minimum donation is 5.00 USD.")
        return amount

    def clean_full_name(self):
        full_name = self.cleaned_data.get("full_name", "").strip()
        is_anonymous = self.cleaned_data.get("is_anonymous", False)
        if not is_anonymous and len(full_name) < 2:
            raise forms.ValidationError("Please enter your full name or choose anonymous.")
        return full_name

    def clean(self):
        cleaned = super().clean()
        is_anonymous = cleaned.get("is_anonymous", False)
        full_name = (cleaned.get("full_name") or "").strip()
        email = (cleaned.get("email") or "").strip()

        if is_anonymous:
            cleaned["full_name"] = ""
            cleaned["email"] = ""
            cleaned["message"] = ""
            return cleaned

        if not email:
            self.add_error("email", "Please enter your email or choose anonymous.")
        if len(full_name) < 2:
            self.add_error("full_name", "Please enter your full name or choose anonymous.")
        return cleaned

class SessionManageForm(forms.ModelForm):
    price = forms.DecimalField(
        min_value=0,
        max_digits=10,
        decimal_places=2,
        help_text="Enter amount in USD (for example 120.00).",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.price is not None:
            self.initial["price"] = (Decimal(self.instance.price) / Decimal("100")).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        self.fields["event_program_pdf"].label = "Event program PDF"
        self.fields["event_program_pdf"].required = False
        self.fields["event_program_pdf"].help_text = (
            "For this session only: public homepage download appears only before this session’s first day (when it is "
            "the next scheduled intensive); confirmation email attachment; hub download for paid registrants after the "
            "live dates (when downloads unlock)."
        )

    def clean_price(self):
        price_value = self.cleaned_data["price"]
        cents = (price_value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(cents)

    def clean_event_program_pdf(self):
        return validate_event_program_upload(self.cleaned_data.get("event_program_pdf"))

    class Meta:
        model = Session
        fields = [
            "title",
            "location",
            "start_date",
            "end_date",
            "capacity",
            "price",
            "currency",
            "is_active",
            "event_program_pdf",
        ]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "event_program_pdf": forms.ClearableFileInput(
                attrs={"accept": ".pdf,.doc,.docx,.ppt,.pptx,application/pdf"}
            ),
        }


class TrainingScheduleItemForm(forms.ModelForm):
    class Meta:
        model = TrainingScheduleItem
        fields = [
            "day_name",
            "start_time",
            "end_time",
            "lunch_start",
            "lunch_end",
            "display_order",
            "is_active",
        ]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
            "lunch_start": forms.TimeInput(attrs={"type": "time"}),
            "lunch_end": forms.TimeInput(attrs={"type": "time"}),
        }


class SiteSettingForm(forms.ModelForm):
    class Meta:
        model = SiteSetting
        fields = [
            "site_name",
            "venue_address",
            "donation_url",
            "student_discount_percent",
        ]

    def clean_student_discount_percent(self):
        value = self.cleaned_data.get("student_discount_percent", 0)
        if value < 0 or value > 95:
            raise forms.ValidationError("Student discount percent must be between 0 and 95.")
        return value


class PortalVideoForm(forms.ModelForm):
    """Training videos shown to logged-in participants in the learning hub."""

    class Meta:
        model = PortalVideo
        fields = [
            "title",
            "description",
            "external_url",
            "video_file",
            "display_order",
            "is_active",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "e.g. Session 1 – Opening teaching"}),
            "description": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "Short description (optional)",
                }
            ),
            "external_url": forms.URLInput(
                attrs={
                    "placeholder": "https://www.youtube.com/embed/xxxxxxxxxxx",
                }
            ),
            "video_file": forms.ClearableFileInput(),
        }

    def clean(self):
        cleaned = super().clean()
        file = cleaned.get("video_file")
        url = (cleaned.get("external_url") or "").strip()
        if file is False:
            has_file = False
        elif file:
            has_file = True
        else:
            has_file = bool(self.instance.pk and self.instance.video_file)
        if not has_file and not url:
            raise forms.ValidationError(
                "Add either an external URL (YouTube/Vimeo embed link) or upload a video file."
            )
        return cleaned


class SpeakerForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["photo_image"].help_text = "Uploading a new image will replace the current one."

    class Meta:
        model = Speaker
        fields = [
            "full_name",
            "role_title",
            "role_subtitle",
            "country_code",
            "country_label",
            "photo_image",
            "photo_url",
            "read_more_url",
            "sessions",
            "display_order",
            "is_active",
        ]
        widgets = {
            "sessions": forms.SelectMultiple(attrs={"size": 6}),
            "role_title": forms.Textarea(attrs={"rows": 2, "placeholder": "Use Enter for a line break (e.g. Set Free in Christ\\nMission)"}),
            "role_subtitle": forms.Textarea(attrs={"rows": 3}),
        }
