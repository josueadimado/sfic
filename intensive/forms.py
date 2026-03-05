import re

from django import forms
import pycountry

from .models import Session, SiteSetting, Speaker, TrainingScheduleItem


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

class SessionManageForm(forms.ModelForm):
    class Meta:
        model = Session
        fields = ["title", "location", "start_date", "end_date", "capacity", "price", "currency", "is_active"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
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
        fields = ["site_name", "venue_address"]


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
        }
