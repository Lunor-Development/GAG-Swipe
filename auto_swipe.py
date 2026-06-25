#!/usr/bin/env py -3
"""
gag.gg vote auto-swiper — cap-reset loop for carrots + seed packs.

Run:  py -3 auto_swipe.py
Build exe:  build.bat
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

from config_util import (
    credentials_from_args,
    gag_cookie_header,
    has_roblox_auth,
    prompt_credentials,
    update_gag_session,
)
from gag_client import (
    GagClient,
    SessionStats,
    claim_vote_rewards,
    log,
    run_contest,
    run_swipe_session,
    sleep_until_reset,
)

try:
    from reauth import reauth_with_playwright, reauth_with_roblox_cookie
except ImportError:
    reauth_with_playwright = None  # type: ignore[misc, assignment]
    reauth_with_roblox_cookie = None  # type: ignore[misc, assignment]

# ---------------------------------------------------------------------------
# App settings (edit here — no config.json needed)
# ---------------------------------------------------------------------------
APP_SETTINGS = {
    "vote": "like",
    "turbo": True,
    "quiet": True,
    "loop_forever": True,
    "reset_on_cap": True,
    "rewards": False,
    "claim_rewards": True,
    "skip_claim": False,
    "swipe_delay_ms": [0, 0],
    "decision_ms": [1, 20],
    "auto_contest": False,
    "contest_carrots": "all",
    "rate_limit_retries": 8,
    "rate_limit_max_wait_s": 30,
    "use_proxies_on_rate_limit": True,
    "proxy_fetch_limit": 30,
    "proxy_timeout": 8,
    "chrome_user_data_dir": "",
    "reauth_headless": False,
    "reauth_timeout_s": 120,
}

TURBO_OVERRIDES = {
    "swipe_delay_ms": [0, 0],
    "decision_ms": [1, 20],
    "quiet": True,
}

REWARDS_OVERRIDES = {
    "reset_on_cap": False,
    "turbo": False,
    "quiet": False,
    "swipe_delay_ms": [800, 1500],
    "decision_ms": [50, 300],
}


def build_runtime_config(args: argparse.Namespace) -> dict:
    cfg = dict(APP_SETTINGS)
    if args.rewards or cfg.get("rewards"):
        cfg.update(REWARDS_OVERRIDES)
        cfg["rewards"] = True
    if (args.turbo or cfg.get("turbo")) and not cfg.get("rewards"):
        cfg.update(TURBO_OVERRIDES)
        cfg["turbo"] = True
    if args.wait_on_cap:
        cfg["reset_on_cap"] = False
    elif args.reset_on_cap:
        cfg["reset_on_cap"] = True
    if args.once:
        cfg["loop_forever"] = False
    return cfg


def reset_cap_bypass(
    client: GagClient,
    credentials: dict,
    cfg: dict,
    *,
    fast: bool = False,
) -> bool:
    """Delete gag.gg account data and re-login to refresh the 20-swipe quota."""
    t0 = time.perf_counter()
    if cfg.get("claim_rewards", True):
        claim_vote_rewards(client, quiet=cfg.get("quiet", False))
    client.delete_account()

    roblox = credentials.get("roblox_cookie", "").strip()
    if roblox and reauth_with_roblox_cookie:
        try:
            cookie = reauth_with_roblox_cookie(
                roblox,
                gag_session=credentials.get("gag_session"),
                verify_session=not fast,
            )
        except Exception as exc:
            log(f"Roblox cookie re-auth failed: {exc}")
            return False
    elif cfg.get("chrome_user_data_dir") and reauth_with_playwright:
        cookie = reauth_with_playwright(
            user_data_dir=cfg["chrome_user_data_dir"],
            headless=cfg.get("reauth_headless", False),
            timeout_s=float(cfg.get("reauth_timeout_s", 120)),
        )
    else:
        log("Cap-reset needs .ROBLOSECURITY — re-run and paste it at launch.")
        return False

    client.update_cookie(cookie)
    update_gag_session(credentials, cookie)
    client.clear_proxy()
    deck = client.vote_deck()
    elapsed = time.perf_counter() - t0
    log(
        f"Reset in {elapsed:.1f}s — {deck.get('remaining')}/{deck.get('limit')} swipes, "
        f"capped={deck.get('capped')}"
    )
    return not deck.get("capped") and int(deck.get("remaining", 0)) > 0


def resolve_credentials(args: argparse.Namespace, cfg: dict) -> dict:
    from_cli = credentials_from_args(args)
    if from_cli:
        return from_cli
    if args.no_prompt:
        print("Use --gag-session and --roblox-cookie, or run without --no-prompt.")
        sys.exit(1)
    require_roblox = bool(cfg.get("reset_on_cap")) and not cfg.get("rewards")
    return prompt_credentials(require_roblox=require_roblox)


def main() -> None:
    parser = argparse.ArgumentParser(description="gag.gg vote auto-swiper")
    parser.add_argument("--gag-session", help="__Host-gag_session cookie value")
    parser.add_argument("--roblox-cookie", help=".ROBLOSECURITY value (for cap-reset loop)")
    parser.add_argument("--no-prompt", action="store_true", help="Require cookies via CLI flags")
    parser.add_argument("--once", action="store_true", help="Run one swipe session then exit")
    parser.add_argument("--contest", action="store_true", help="Enter carrot contest after swiping")
    parser.add_argument("--dry-run", action="store_true", help="Only check auth, no swipes")
    parser.add_argument("--reset-on-cap", action="store_true", help="Delete + re-login when capped")
    parser.add_argument("--wait-on-cap", action="store_true", help="Sleep until UTC hour reset")
    parser.add_argument("--turbo", action="store_true", help="Max speed mode")
    parser.add_argument("--rewards", action="store_true", help="Stable profile, hourly wait (no delete loop)")
    args = parser.parse_args()

    cfg = build_runtime_config(args)
    credentials = resolve_credentials(args, cfg)

    try:
        cookie = gag_cookie_header(credentials)
    except ValueError as exc:
        print(exc)
        sys.exit(1)

    rewards_mode = bool(cfg.get("rewards"))
    reset_on_cap = bool(cfg.get("reset_on_cap")) and not rewards_mode

    if reset_on_cap and not has_roblox_auth(credentials) and not cfg.get("chrome_user_data_dir"):
        print("Cap-reset loop needs .ROBLOSECURITY at launch (or use --wait-on-cap).")
        sys.exit(1)

    vote_mode = cfg.get("vote", "like")
    swipe_delay = tuple(cfg.get("swipe_delay_ms", [80, 350]))
    decision_ms = tuple(cfg.get("decision_ms", [50, 400]))
    skip_claim = bool(cfg.get("skip_claim", False))
    claim_rewards = bool(cfg.get("claim_rewards", True))
    quiet = bool(cfg.get("quiet", False))
    turbo = bool(cfg.get("turbo", False))
    loop_forever = bool(cfg.get("loop_forever", True))
    auto_contest = args.contest or cfg.get("auto_contest", False)
    contest_carrots = cfg.get("contest_carrots", "all")

    totals = SessionStats()

    with GagClient(
        cookie,
        rate_limit_retries=int(cfg.get("rate_limit_retries", 8)),
        rate_limit_max_wait_s=float(cfg.get("rate_limit_max_wait_s", 30)),
        use_proxies_on_rate_limit=bool(cfg.get("use_proxies_on_rate_limit", True)),
        proxy_fetch_limit=int(cfg.get("proxy_fetch_limit", 30)),
        proxy_timeout=float(cfg.get("proxy_timeout", 8)),
    ) as client:
        try:
            me = client.auth_me()
        except httpx.HTTPError as exc:
            print(f"Auth failed ({exc}). Check your gag_session cookie.")
            sys.exit(1)

        if not me.get("signedIn"):
            print("Not signed in — log in on gag.gg and paste a fresh gag_session.")
            sys.exit(1)

        log(f"Signed in as {me.get('username')} ({me.get('sub')}) — carrots: {me.get('carrots', 0)}")
        if rewards_mode:
            log("Rewards mode: 20 swipes/hour, wait for UTC reset (no delete loop)")
        elif reset_on_cap:
            mode = "turbo" if turbo else "normal"
            log(f"Cap-reset loop ({mode}): swipe -> delete -> re-login -> repeat")

        if args.dry_run:
            deck = client.vote_deck()
            log(f"Dry run OK — {deck.get('remaining')}/{deck.get('limit')} swipes, capped={deck.get('capped')}")
            return

        cycle = 0
        while True:
            cycle += 1
            if reset_on_cap and client.is_vote_capped():
                if not quiet:
                    log(f"--- cycle {cycle}: capped — reset ---")
                if not reset_cap_bypass(client, credentials, cfg, fast=turbo):
                    log("Cap reset failed; exiting.")
                    sys.exit(1)
                continue

            if not quiet:
                log(f"--- cycle {cycle}: swiping ---")
            t0 = time.perf_counter()
            session = run_swipe_session(
                client,
                vote_mode=vote_mode,
                swipe_delay_ms=swipe_delay,  # type: ignore[arg-type]
                decision_ms_range=decision_ms,  # type: ignore[arg-type]
                skip_claim=skip_claim,
                claim_rewards=claim_rewards,
                quiet=quiet,
            )
            totals.swipes += session.swipes
            totals.carrots_awarded += session.carrots_awarded
            totals.jackpots.extend(session.jackpots)
            totals.claimed.extend(session.claimed)
            swipe_s = time.perf_counter() - t0

            rewards = len(session.jackpots) + len(session.claimed)
            if not quiet:
                log(
                    f"Session done in {swipe_s:.1f}s — swipes={session.swipes}, "
                    f"jackpots={len(session.jackpots)}, claimed={len(session.claimed)}, "
                    f"total={totals.swipes}"
                )
            elif session.swipes:
                extra = ""
                if session.jackpots:
                    extra = f", JACKPOT: {session.jackpots[0].get('item', 'reward')}"
                elif rewards:
                    extra = f", {rewards} rewards"
                log(f"cycle {cycle}: {session.swipes} swipes in {swipe_s:.1f}s{extra}")

            if auto_contest:
                run_contest(client, contest_carrots)

            if not loop_forever:
                break

            if session.finished or session.swipes >= 20:
                if reset_on_cap:
                    if not quiet:
                        log(f"--- cycle {cycle}: reset ---")
                    if reset_cap_bypass(client, credentials, cfg, fast=turbo):
                        continue
                    log("Cap reset failed; exiting.")
                    sys.exit(1)
                sleep_until_reset()
            elif not quiet:
                log("Swipes still available — continuing…")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
