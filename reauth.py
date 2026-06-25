"""Re-sign into gag.gg via Roblox OAuth (browser or .ROBLOSECURITY cookie)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

BASE = "https://gag.gg"
LOGIN_URL = f"{BASE}/api/auth/roblox/login?return=/vote"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
_REDIRECTS = (301, 302, 303, 307, 308)


def user_id_from_gag_session(session: str) -> str | None:
    """Decode Roblox user id from gag session JWT payload (avoids an extra API call)."""
    try:
        payload_b64 = session.strip().split(".")[0]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        sub = data.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


def cookie_header_from_context(cookies: list[dict[str, Any]]) -> str:
    gag = [c for c in cookies if c.get("domain", "").endswith("gag.gg")]
    if not gag:
        raise RuntimeError("No gag.gg cookies after login — OAuth may not have finished")
    return "; ".join(f"{c['name']}={c['value']}" for c in gag)


def cookie_header_from_client(client: httpx.Client) -> str:
    parts: list[str] = []
    for cookie in client.cookies.jar:
        if cookie.domain.endswith("gag.gg"):
            parts.append(f"{cookie.name}={cookie.value}")
    if not parts:
        raise RuntimeError("No gag.gg cookies after login — OAuth may not have finished")
    return "; ".join(parts)


def _csrf_post(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    resp = client.post(url, **kwargs)
    token = resp.headers.get("x-csrf-token")
    if resp.status_code == 403 and token:
        headers = dict(kwargs.get("headers") or {})
        headers["x-csrf-token"] = token
        kwargs["headers"] = headers
        resp = client.post(url, **kwargs)
    return resp


def _follow(client: httpx.Client, url: str, limit: int = 10) -> httpx.Response:
    for _ in range(limit):
        resp = client.get(url, headers={"Accept": "text/html,*/*"})
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        location = resp.headers.get("location")
        if not location:
            return resp
        url = urljoin(str(resp.url), location)
    return resp


def _oauth_params_from_login(client: httpx.Client) -> tuple[dict[str, str], str]:
    """Follow OAuth redirects until params are in the Location URL; skip authorize HTML."""
    login = client.get(
        LOGIN_URL,
        headers={"Accept": "text/html,*/*", "Referer": f"{BASE}/vote/"},
    )
    if login.status_code not in _REDIRECTS or not login.headers.get("location"):
        login.raise_for_status()
    url = login.headers["location"]
    referer = url

    for _ in range(8):
        resp = client.get(url, headers={"Accept": "text/html,*/*"})
        if resp.status_code in _REDIRECTS:
            next_url = urljoin(str(resp.url), resp.headers["location"])
            qs = parse_qs(urlparse(next_url).query)
            if qs.get("code_challenge") and qs.get("state"):
                params = {k: v[0] for k, v in qs.items()}
                return params, next_url
            url = next_url
            referer = next_url
            continue

        qs = parse_qs(urlparse(str(resp.url)).query)
        if qs.get("code_challenge") and qs.get("state"):
            params = {k: v[0] for k, v in qs.items()}
            return params, str(resp.url)
        break

    raise RuntimeError("OAuth params missing from redirect chain")


def reauth_with_roblox_cookie(
    roblox_security: str,
    *,
    timeout: float = 45.0,
    gag_session: str | None = None,
    verify_session: bool = True,
) -> str:
    """
    Complete gag.gg Roblox OAuth using a .ROBLOSECURITY value (no browser).

    Returns a gag.gg Cookie header string suitable for GagClient.
    """
    roblox_security = roblox_security.strip()
    if not roblox_security:
        raise ValueError("roblox_security cookie is empty")

    with httpx.Client(
        follow_redirects=False,
        timeout=timeout,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
    ) as client:
        client.cookies.set(".ROBLOSECURITY", roblox_security, domain=".roblox.com")

        user_id = user_id_from_gag_session(gag_session) if gag_session else None
        if not user_id:
            user = client.get("https://users.roblox.com/v1/users/authenticated")
            user.raise_for_status()
            user_id = str(user.json()["id"])

        params, referer = _oauth_params_from_login(client)

        pr = client.get(
            "https://apis.roblox.com/oauth/v1/permission-request",
            params={
                "clientId": params["client_id"],
                "redirectUri": params["redirect_uri"],
                "scopes": params.get("scope", "openid profile"),
                "responseTypes": "code",
            },
        )
        pr.raise_for_status()
        pr_data = pr.json()

        body = {
            "userId": user_id,
            "clientId": params["client_id"],
            "resourceInfos": [{"owner": {"id": user_id, "type": "User"}, "resources": {}}],
            "responseTypes": pr_data.get("responseTypes") or ["Code"],
            "redirectUri": params["redirect_uri"],
            "scopes": [
                {"scopeType": s["scopeType"], "operations": s["operations"]}
                for s in pr_data.get("scopes", [])
            ]
            or [
                {"scopeType": "openid", "operations": ["read"]},
                {"scopeType": "profile", "operations": ["read"]},
            ],
            "state": params["state"],
            "codeChallenge": params["code_challenge"],
            "codeChallengeMethod": params["code_challenge_method"],
        }
        if params.get("nonce"):
            body["nonce"] = params["nonce"]

        grant = _csrf_post(
            client,
            "https://apis.roblox.com/oauth/v1/authorizations",
            json=body,
            headers={
                "Content-Type": "application/json-patch+json",
                "Referer": referer,
                "Origin": "https://authorize.roblox.com",
            },
        )
        grant.raise_for_status()
        location = grant.json().get("location")
        if not location:
            raise RuntimeError(f"Roblox OAuth grant returned no redirect: {grant.text[:200]}")

        callback = _follow(client, location)
        if "auth_error" in str(callback.url):
            raise RuntimeError(f"gag.gg OAuth callback failed: {callback.url}")

        cookie = cookie_header_from_client(client)
        if verify_session:
            me = client.get(
                f"{BASE}/api/auth/me",
                headers={"Cookie": cookie, "User-Agent": UA},
            )
            me.raise_for_status()
            if not me.json().get("signedIn"):
                raise RuntimeError("gag.gg session cookie set but /api/auth/me is not signed in")

        return cookie


def reauth_with_playwright(
    *,
    user_data_dir: str | None = None,
    headless: bool = False,
    timeout_s: float = 120.0,
) -> str:
    """
    Open Roblox OAuth in Chromium/Chrome, click through consent, return gag.gg cookie string.

    Use `user_data_dir` pointing at your Chrome profile so Roblox is already logged in.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright required. Run: py -3 -m pip install playwright && py -3 -m playwright install chromium"
        ) from exc

    deadline = time.time() + timeout_s

    with sync_playwright() as p:
        if user_data_dir:
            context = p.chromium.launch_persistent_context(
                user_data_dir,
                channel="chrome",
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = p.chromium.launch(headless=headless, channel="chrome")
            context = browser.new_context()
            page = context.new_page()

        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        while time.time() < deadline:
            try:
                cookie_header_from_context(context.cookies())
                if "gag.gg" in page.url:
                    break
            except RuntimeError:
                pass

            if "authorize.roblox.com" in page.url:
                for label in ("Continue", "Accept", "Authorize", "Agree"):
                    btn = page.get_by_role("button", name=label)
                    if btn.count() > 0:
                        try:
                            btn.first.click(timeout=3000)
                        except Exception:
                            pass
                        break
            page.wait_for_timeout(800)
        else:
            raise TimeoutError(f"OAuth did not finish within {timeout_s:.0f}s (last url: {page.url})")

        cookie = cookie_header_from_context(context.cookies())
        context.close()
        return cookie
