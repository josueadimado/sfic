"""Participant (registrant) portal: login with email + password, downloads, streaming videos."""

import mimetypes

from django.contrib import messages
from django.contrib.auth.hashers import check_password
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from django.db.models import Max

from .models import (
    PortalVideo,
    Registration,
    RegistrationMaterial,
    RegistrationStatus,
    Session,
    SiteSetting,
)
from .participant_downloads import build_participant_download_sections
from .services import reset_and_email_portal_password
from .video_urls import iframe_src_for_external_video


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


def _registrations_allow_file_downloads(reg_qs):
    """PDFs / materials only for completed intensives (past last day), not while session is still upcoming."""
    today = timezone.localdate()
    return reg_qs.filter(session__end_date__lt=today).exists()


def _portal_downloads_context(regs):
    today = timezone.localdate()
    allow = _registrations_allow_file_downloads(regs)
    future_end = None
    if regs.exists() and not allow:
        future_end = regs.aggregate(m=Max("session__end_date"))["m"]
    return allow, future_end


def _participant_program_download_regs(regs):
    """Paid portal regs whose intensive has ended and session has an event program file."""
    today = timezone.localdate()
    return [reg for reg in regs if reg.session.end_date < today and reg.session.event_program_pdf]


def _participant_materials_for_downloads(regs):
    """Additional materials for past sessions this learner is registered for (hub + same rules as program)."""
    today = timezone.localdate()
    session_ids = [r.session_id for r in regs if r.session.end_date < today]
    if not session_ids:
        return RegistrationMaterial.objects.none()
    return (
        RegistrationMaterial.objects.filter(session_id__in=session_ids)
        .select_related("session")
        .order_by("-session__start_date", "display_order", "id")
    )


def _participant_videos_for_regs(regs):
    """Hub videos for every session this learner has a paid, active portal registration for."""
    session_ids = list({r.session_id for r in regs})
    if not session_ids:
        return PortalVideo.objects.none()
    return (
        PortalVideo.objects.filter(is_active=True, session_id__in=session_ids)
        .select_related("session")
        .order_by("session__start_date", "display_order", "id")
    )


def _participant_may_view_video(regs, video) -> bool:
    return regs.filter(session_id=video.session_id).exists()


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

        allowed_ids = list(
            Registration.objects.filter(
                email__iexact=email,
                status=RegistrationStatus.PAID,
                portal_access_until__gt=timezone.now(),
            ).values_list("id", flat=True),
        )
        request.session["participant_registration_ids"] = [str(pk) for pk in allowed_ids]
        request.session["participant_email"] = email
        if allowed_ids:
            Registration.objects.filter(id__in=allowed_ids).update(
                portal_last_login_at=timezone.now(),
            )
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
    materials = _participant_materials_for_downloads(regs)
    videos = _participant_videos_for_regs(regs)
    hub_session_count = regs.values_list("session_id", flat=True).distinct().count()
    latest_until = regs.order_by("-portal_access_until").values_list("portal_access_until", flat=True).first()
    first_reg = regs.first()
    welcome_name = first_reg.full_name if first_reg else "Participant"
    allow_file_downloads, downloads_unlock_after = _portal_downloads_context(regs)
    program_download_regs = _participant_program_download_regs(regs)
    download_sections = build_participant_download_sections(program_download_regs, materials)

    return render(
        request,
        "intensive/participant/home.html",
        {
            "registrations": regs,
            "site": site,
            "materials": materials,
            "videos": videos,
            "latest_access_until": latest_until,
            "program_download_regs": program_download_regs,
            "download_sections": download_sections,
            "allow_file_downloads": allow_file_downloads,
            "downloads_unlock_after": downloads_unlock_after,
            "dashboard_section": "home",
            "welcome_name": welcome_name,
            "registration_count": regs.count(),
            "hub_session_count": hub_session_count,
        },
    )


def _require_participant(request):
    regs = _participant_registrations(request)
    if not regs.exists():
        return None
    return regs


@require_GET
def participant_download_program(request, session_id):
    regs = _require_participant(request)
    if not regs:
        return redirect("participant_portal_login")

    session = get_object_or_404(Session, pk=session_id)
    reg = next((r for r in regs if r.session_id == session.id), None)
    if not reg:
        raise Http404("Program not available for this session.")

    if not _registrations_allow_file_downloads(
        Registration.objects.filter(pk=reg.pk)
    ):
        messages.info(
            request,
            "PDF downloads unlock after the last day of your intensive, while your hub access is still active. "
            "You can still watch training videos anytime during your 30-day window.",
        )
        return redirect("participant_portal_home")

    f = session.event_program_pdf
    if not f:
        raise Http404("Program not available.")
    name = f.name.split("/")[-1] if f.name else "event-program.pdf"
    mime, _ = mimetypes.guess_type(name)
    resp = FileResponse(f.open("rb"), as_attachment=True, filename=name, content_type=mime or "application/pdf")
    return resp


@require_GET
def participant_download_material(request, material_id: int):
    regs = _require_participant(request)
    if not regs:
        return redirect("participant_portal_login")

    if not _registrations_allow_file_downloads(regs):
        messages.info(
            request,
            "Document downloads unlock after the last day of your intensive, while your hub access is still active.",
        )
        return redirect("participant_portal_home")

    material = get_object_or_404(RegistrationMaterial, pk=material_id)
    today = timezone.localdate()
    allowed = regs.filter(session_id=material.session_id, session__end_date__lt=today).exists()
    if not allowed:
        raise Http404("This file is not available for your registration.")

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
    if not _participant_may_view_video(regs, video):
        raise Http404("Video not available.")
    if not video.video_file and not (video.external_url or "").strip():
        raise Http404()
    video_iframe_src = ""
    if (video.external_url or "").strip():
        video_iframe_src = iframe_src_for_external_video(video.external_url)
    return render(
        request,
        "intensive/participant/video.html",
        {
            "video": video,
            "video_iframe_src": video_iframe_src,
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
    if not _participant_may_view_video(regs, video):
        raise Http404("Video not available.")
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
