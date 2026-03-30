"""Participant (registrant) portal: login with email + password, downloads, streaming videos."""

import mimetypes

from django.contrib import messages
from django.contrib.auth.hashers import check_password
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import (
    PortalVideo,
    Registration,
    RegistrationMaterial,
    RegistrationStatus,
    SiteSetting,
)
from .services import reset_and_email_portal_password


def _participant_registrations(request):
    ids = request.session.get("participant_registration_ids") or []
    if not ids:
        return Registration.objects.none()
    now = timezone.now()
    return (
        Registration.objects.filter(
            id__in=ids,
            status=RegistrationStatus.PAID,
            portal_access_until__gt=now,
        )
        .select_related("session")
        .order_by("-portal_access_until")
    )


def participant_portal_login(request):
    if request.method == "POST" and request.POST.get("action") == "reset":
        email = (request.POST.get("email") or "").strip()
        reset_and_email_portal_password(email)
        messages.info(
            request,
            "If that email has an active portal, we sent a new password. Check your inbox.",
        )
        return redirect("participant_portal_login")

    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        password = request.POST.get("password") or ""
        next_url = request.POST.get("next") or reverse("participant_portal_home")

        candidates = Registration.objects.filter(
            email__iexact=email,
            status=RegistrationStatus.PAID,
            portal_access_until__gt=timezone.now(),
        )
        authenticated = False
        for reg in candidates:
            if reg.portal_password_hash and check_password(password, reg.portal_password_hash):
                authenticated = True
                break

        if not authenticated:
            return render(
                request,
                "intensive/participant/login.html",
                {
                    "error": "Invalid email or password, or your 30-day access may have expired.",
                    "email_value": request.POST.get("email") or "",
                },
            )

        allowed_ids = Registration.objects.filter(
            email__iexact=email,
            status=RegistrationStatus.PAID,
            portal_access_until__gt=timezone.now(),
        ).values_list("id", flat=True)
        request.session["participant_registration_ids"] = [str(pk) for pk in allowed_ids]
        request.session["participant_email"] = email
        return redirect(next_url)

    return render(request, "intensive/participant/login.html", {})


@require_POST
def participant_portal_logout(request):
    request.session.pop("participant_registration_ids", None)
    request.session.pop("participant_email", None)
    messages.success(request, "You have signed out of the participant portal.")
    return redirect("participant_portal_login")


@require_GET
def participant_portal_home(request):
    regs = _participant_registrations(request)
    if not regs.exists():
        request.session.pop("participant_registration_ids", None)
        request.session.pop("participant_email", None)
        messages.info(request, "Please sign in to view your resources.")
        return redirect("participant_portal_login")

    site = SiteSetting.objects.first()
    materials = RegistrationMaterial.objects.order_by("display_order", "id")
    videos = PortalVideo.objects.filter(is_active=True).order_by("display_order", "id")
    latest_until = regs.order_by("-portal_access_until").values_list("portal_access_until", flat=True).first()
    first_reg = regs.first()
    welcome_name = first_reg.full_name if first_reg else "Participant"

    return render(
        request,
        "intensive/participant/home.html",
        {
            "registrations": regs,
            "site": site,
            "materials": materials,
            "videos": videos,
            "latest_access_until": latest_until,
            "has_program_pdf": bool(site and site.event_program_pdf),
            "dashboard_section": "home",
            "welcome_name": welcome_name,
            "registration_count": regs.count(),
        },
    )


def _require_participant(request):
    regs = _participant_registrations(request)
    if not regs.exists():
        return None
    return regs


@require_GET
def participant_download_program(request):
    regs = _require_participant(request)
    if not regs:
        return redirect("participant_portal_login")

    site = SiteSetting.objects.first()
    if not site or not site.event_program_pdf:
        raise Http404("Program not available.")

    f = site.event_program_pdf
    name = f.name.split("/")[-1] if f.name else "event-program.pdf"
    mime, _ = mimetypes.guess_type(name)
    resp = FileResponse(f.open("rb"), as_attachment=True, filename=name, content_type=mime or "application/pdf")
    return resp


@require_GET
def participant_download_material(request, material_id: int):
    regs = _require_participant(request)
    if not regs:
        return redirect("participant_portal_login")

    material = get_object_or_404(RegistrationMaterial, pk=material_id)
    if not material.file:
        raise Http404()
    name = material.file.name.split("/")[-1] or "material"
    mime, _ = mimetypes.guess_type(name)
    return FileResponse(
        material.file.open("rb"),
        as_attachment=True,
        filename=name,
        content_type=mime or "application/octet-stream",
    )


@require_GET
def participant_portal_video_watch(request, video_id: int):
    regs = _require_participant(request)
    if not regs:
        return redirect("participant_portal_login")

    video = get_object_or_404(PortalVideo, pk=video_id, is_active=True)
    if not video.video_file and not (video.external_url or "").strip():
        raise Http404()
    return render(
        request,
        "intensive/participant/video.html",
        {
            "video": video,
            "dashboard_section": "video",
        },
    )


@require_GET
def participant_portal_video_stream(request, video_id: int):
    """Stream uploaded file only (inline, for in-browser playback)."""
    regs = _require_participant(request)
    if not regs:
        return HttpResponseForbidden("Sign in required.")

    video = get_object_or_404(PortalVideo, pk=video_id, is_active=True)
    if not video.video_file:
        raise Http404()

    name = video.video_file.name.split("/")[-1] if video.video_file.name else "video.mp4"
    mime, _ = mimetypes.guess_type(name)
    resp = FileResponse(
        video.video_file.open("rb"),
        as_attachment=False,
        content_type=mime or "video/mp4",
    )
    resp["Content-Disposition"] = "inline"
    return resp
