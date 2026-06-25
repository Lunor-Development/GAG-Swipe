"""Credential helpers for gag.gg / Roblox auth."""

from __future__ import annotations

import sys
from typing import Any


def parse_gag_session(cookie_header: str) -> str | None:
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("__Host-gag_session="):
            return part.split("=", 1)[1]
    return None


def normalize_gag_session(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("gag_session is empty")
    if raw.startswith("__Host-gag_session="):
        raw = raw.split("=", 1)[1]
    if "PASTE" in raw:
        raise ValueError("Replace the placeholder gag_session value")
    return raw


def normalize_roblox_cookie(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("roblox_cookie is empty")
    if "PASTE" in raw:
        raise ValueError("Replace the placeholder roblox_cookie value")
    return raw


def gag_cookie_header(credentials: dict[str, Any]) -> str:
    session = normalize_gag_session(str(credentials.get("gag_session") or ""))
    return f"__Host-gag_session={session}"


def roblox_cookie(credentials: dict[str, Any]) -> str:
    return normalize_roblox_cookie(str(credentials.get("roblox_cookie") or ""))


def has_gag_auth(credentials: dict[str, Any]) -> bool:
    try:
        gag_cookie_header(credentials)
        return True
    except ValueError:
        return False


def has_roblox_auth(credentials: dict[str, Any]) -> bool:
    try:
        roblox_cookie(credentials)
        return True
    except ValueError:
        return False


def update_gag_session(credentials: dict[str, Any], cookie_header: str) -> None:
    """Update in-memory credentials after OAuth re-auth."""
    session = parse_gag_session(cookie_header)
    if session:
        credentials["gag_session"] = session
    credentials["cookie"] = cookie_header


def prompt_credentials(*, require_roblox: bool = True) -> dict[str, Any]:
    print()
    print("=" * 44)
    print("  gag.gg Auto-Swiper")
    print("=" * 44)
    print()
    print("Get these from your browser (DevTools → Application → Cookies):")
    print("  • gag.gg  →  __Host-gag_session")
    if require_roblox:
        print("  • roblox.com  →  .ROBLOSECURITY  (for delete + re-login loop)")
    print()

    gag_raw = input("gag_session: ").strip()
    credentials: dict[str, Any] = {"gag_session": normalize_gag_session(gag_raw)}

    if require_roblox:
        rbx_raw = input(".ROBLOSECURITY: ").strip()
        credentials["roblox_cookie"] = normalize_roblox_cookie(rbx_raw)

    print()
    return credentials


def credentials_from_args(args: Any) -> dict[str, Any] | None:
    gag = (getattr(args, "gag_session", None) or "").strip()
    roblox = (getattr(args, "roblox_cookie", None) or "").strip()
    if not gag and not roblox:
        return None
    if not gag:
        print("Missing --gag-session")
        sys.exit(1)
    credentials: dict[str, Any] = {"gag_session": normalize_gag_session(gag)}
    if roblox:
        credentials["roblox_cookie"] = normalize_roblox_cookie(roblox)
    return credentials
