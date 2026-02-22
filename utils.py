from __future__ import annotations

import re


def is_digits(s: str) -> bool:
    return bool(re.fullmatch(r"\d+", s.strip()))


def fmt_bytes(n: int) -> str:
    # Human readable, base-2-ish but user-friendly.
    if n < 0:
        return str(n)
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    i = 0
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    if i == 0:
        return f"{int(size)} {units[i]}"
    return f"{size:.2f} {units[i]}"


def safe_filename(name: str, *, fallback: str = "manifest") -> str:
    """
    Make a filesystem/Discord attachment friendly filename.
    Keeps it simple: ASCII-ish, replace bad chars with underscore.
    """
    name = (name or "").strip()
    if not name:
        name = fallback

    # Replace common filesystem-hostile characters.
    name = re.sub(r'[<>:"/\\\\|?*\\n\\r\\t]', "_", name)
    name = re.sub(r"\\s+", " ", name).strip()
    return name[:150]

