"""Turn pasted YouTube/Vimeo snippets into iframe-safe URLs for PortalVideo.external_url."""

from __future__ import annotations

import re

# YouTube video ids are typically 11 chars ([A-Za-z0-9_-]).
_YT_ID = re.compile(r"^[\w-]{11}$")


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
        return f"https://www.youtube.com/embed/{s}"

    candidate = s if re.match(r"^https?://", s, re.I) else f"https://{s.lstrip('/')}"
    low = candidate.lower()

    m = re.search(r"youtu\.be/([\w-]{11})(?:\?|/|$)", candidate, re.I)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}"

    if "youtube.com" in low or "youtube-nocookie.com" in low:
        m = re.search(r"[?&]v=([\w-]{11})", candidate, re.I)
        if m:
            return f"https://www.youtube.com/embed/{m.group(1)}"
        m = re.search(r"youtube(?:-nocookie)?\.com/embed/([\w-]{11})", candidate, re.I)
        if m:
            return f"https://www.youtube.com/embed/{m.group(1)}"
        m = re.search(r"youtube\.com/shorts/([\w-]{11})", candidate, re.I)
        if m:
            return f"https://www.youtube.com/embed/{m.group(1)}"

    m = re.search(r"vimeo\.com/(?:video/)?(\d+)(?:\?|/|$)", candidate, re.I)
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}"
    m = re.search(r"player\.vimeo\.com/video/(\d+)", candidate, re.I)
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}"

    if re.match(r"^https?://", s, re.I):
        return s

    if re.match(r"^https?://", candidate, re.I):
        return candidate

    return s
