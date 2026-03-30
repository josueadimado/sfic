"""Turn pasted YouTube/Vimeo snippets into iframe-safe URLs for PortalVideo.external_url."""

from __future__ import annotations

import re

# YouTube video ids are typically 11 chars ([A-Za-z0-9_-]).
_YT_ID = re.compile(r"^[\w-]{11}$")


def _ensure_youtube_iframe_embed(url: str) -> str:
    """
    YouTube blocks normal watch pages inside <iframe> (X-Frame-Options / frame policies).
    Only https://www.youtube.com/embed/VIDEO_ID (or youtube-nocookie) works reliably.
    """
    u = (url or "").strip()
    if not u:
        return ""
    low = u.lower()
    if "youtube-nocookie.com" in low and "/embed/" in low:
        return u
    if "youtube.com" not in low and "youtu.be" not in low:
        return u

    if "/embed/" in low:
        # Normalize host to www for consistency (embed paths work on youtube.com too).
        if "youtube-nocookie.com" in low:
            return u
        m = re.search(r"youtube\.com/embed/([\w-]{11})", u, re.I)
        if m:
            return f"https://www.youtube.com/embed/{m.group(1)}"

    # /watch?v=, /watch/?v=, &v=
    if "youtube.com" in low and "/watch" in low:
        m = re.search(r"[?&]v=([\w-]{11})", u, re.I)
        if m:
            return f"https://www.youtube.com/embed/{m.group(1)}"

    if "youtu.be/" in low:
        m = re.search(r"youtu\.be/([\w-]{11})(?:\?|/|$)", u, re.I)
        if m:
            return f"https://www.youtube.com/embed/{m.group(1)}"

    # /shorts/VIDEO_ID
    if "youtube.com/shorts/" in low:
        m = re.search(r"youtube\.com/shorts/([\w-]{11})", u, re.I)
        if m:
            return f"https://www.youtube.com/embed/{m.group(1)}"

    return u


def normalize_portal_external_url(raw: str) -> str:
    """
    Convert common paste patterns to URLs that work as <iframe src="...">.

    Accepts: bare YouTube id, youtu.be/..., youtube.com/watch?v=..., /embed/...,
    /shorts/..., Vimeo page or player links, or any https URL (passed through).
    """
    s = (raw or "").strip()
    if not s:
        return ""

    if _YT_ID.fullmatch(s):
        out = f"https://www.youtube.com/embed/{s}"
        return _ensure_youtube_iframe_embed(out)

    candidate = s if re.match(r"^https?://", s, re.I) else f"https://{s.lstrip('/')}"
    low = candidate.lower()

    out = ""

    m = re.search(r"youtu\.be/([\w-]{11})(?:\?|/|$)", candidate, re.I)
    if m:
        out = f"https://www.youtube.com/embed/{m.group(1)}"

    if not out and ("youtube.com" in low or "youtube-nocookie.com" in low):
        m = re.search(r"[?&]v=([\w-]{11})", candidate, re.I)
        if m:
            out = f"https://www.youtube.com/embed/{m.group(1)}"
        if not out:
            m = re.search(r"youtube(?:-nocookie)?\.com/embed/([\w-]{11})", candidate, re.I)
            if m:
                out = f"https://www.youtube.com/embed/{m.group(1)}"
        if not out:
            m = re.search(r"youtube\.com/shorts/([\w-]{11})", candidate, re.I)
            if m:
                out = f"https://www.youtube.com/embed/{m.group(1)}"

    if not out:
        m = re.search(r"vimeo\.com/(?:video/)?(\d+)(?:\?|/|$)", candidate, re.I)
        if m:
            out = f"https://player.vimeo.com/video/{m.group(1)}"
    if not out:
        m = re.search(r"player\.vimeo\.com/video/(\d+)", candidate, re.I)
        if m:
            out = f"https://player.vimeo.com/video/{m.group(1)}"

    if not out and re.match(r"^https?://", s, re.I):
        out = s
    if not out and re.match(r"^https?://", candidate, re.I):
        out = candidate
    if not out:
        out = s

    return _ensure_youtube_iframe_embed(out)


def iframe_src_for_external_video(raw: str) -> str:
    """
    URL for <iframe src="..."> on the participant video page.

    Runs full normalization plus a final YouTube watch→embed pass so old database
    rows and odd URLs still play inline.
    """
    return normalize_portal_external_url(raw) if (raw or "").strip() else ""
