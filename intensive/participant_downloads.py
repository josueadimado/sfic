"""Group and label learning-hub download links for participant UI."""

from __future__ import annotations

import os.path
import re
from collections import defaultdict
from typing import Any


def prettify_stored_filename(file_name: str) -> tuple[str, str]:
    """
    Turn stored upload names into a readable title and a short extension label.

    Returns (display_title, ext_lower) e.g. ("Finding freedom in Jesus", "pdf").
    """
    base = os.path.basename(file_name or "")
    if not base:
        return "Document", "file"
    stem, ext = os.path.splitext(base)
    # Strip trailing _xxxxxxxx chunk often added by storage back-ends (7+ random alnum).
    stem = re.sub(r"_[a-zA-Z0-9]{7,}$", "", stem)
    stem = stem.replace("_", " ").replace("-", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    if not stem:
        stem = "Document"
    # Light touch: don't title() everything (acronyms); capitalize first char
    if stem:
        stem = stem[0].upper() + stem[1:]
    ext_clean = (ext.lstrip(".") or "file").lower()
    return stem, ext_clean


def build_participant_download_sections(program_download_regs, materials) -> list[dict[str, Any]]:
    """One block per intensive: session heading + list of program + material rows."""
    buckets: dict[Any, dict[str, Any]] = defaultdict(lambda: {"session": None, "items": []})

    for preg in program_download_regs:
        s = preg.session
        buckets[s.id]["session"] = s
        buckets[s.id]["items"].append(
            {
                "type": "program",
                "preg": preg,
                "label": "Event program",
                "ext_badge": "pdf",
            }
        )

    for mat in materials:
        s = mat.session
        buckets[s.id]["session"] = s
        label, ext = prettify_stored_filename(mat.file.name if mat.file else "")
        buckets[s.id]["items"].append(
            {
                "type": "material",
                "mat": mat,
                "label": label,
                "ext_badge": ext,
            }
        )

    out: list[dict[str, Any]] = []
    for _sid, data in buckets.items():
        sess = data["session"]
        if sess is not None:
            out.append({"session": sess, "items": data["items"]})

    out.sort(key=lambda row: row["session"].start_date, reverse=True)
    return out
