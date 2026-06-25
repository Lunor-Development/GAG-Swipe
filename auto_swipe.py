#!/usr/bin/env py -3
"""
gag.gg vote auto-swiper — carrots + seed pack jackpots.

Run:  py -3 auto_swipe.py
Build exe:  build.bat
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

from config_util import credentials_from_args, gag_cookie_header, prompt_credentials
from gag_client import (
    GagClient,
    SessionStats,
    log,
    run_contest,
    run_swipe_session,
    sleep_until_reset,
)

# ---------------------------------------------------------------------------
# App settings (edit here — no config.json needed)
# ---------------------------------------------------------------------------
APP_SETTINGS = {
    "vote": "like",
    "turbo": True,
    "quiet": True,
    "loop_forever": True,
    "reset_on_cap": True,
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
}

TURBO_OVERRIDES = {
    "swipe_delay_ms": [0, 0],
    "decision_ms": [1, 20],
    "quiet": True,
    "skip_claim": True,
}

RELAXED_OVERRIDES = {
    "turbo": False,
    "quiet": False,
    "swipe_delay_ms": [800, 1500],
    "decision_ms": [50, 300],
}


def die(msg: str, code: int = 1) -> None:
    """Print a fatal error and pause when running as a frozen EXE."""
    print(msg, flush=True)
    if getattr(sys, "frozen", False):
        input("\nPress Enter to exit...")
    sys.exit(code)


def build_runtime_config(args: argparse.Namespace) -> dict:
    cfg = dict(APP_SETTINGS)
    if args.relaxed or cfg.get("relaxed"):
        cfg.update(RELAXED_OVERRIDES)
    elif args.turbo or cfg.get("turbo"):
        cfg.update(TURBO_OVERRIDES)
        cfg["turbo"] = True
    if args.wait_on_cap:
        cfg["reset_on_cap"] = False
    elif args.reset_on_cap:
        cfg["reset_on_cap"] = True
    if args.once:
        cfg["loop_forever"] = False
    return cfg


def reset_cap_bypass(client: GagClient) -> bool:
    """Delete gag.gg profile data — poll deck instead of waiting on slow delete HTTP."""
    t0 = time.perf_counter()
    try:
        deck = client.reset_vote_quota()
    except (TimeoutError, httpx.HTTPError) as exc:
        log(f"Cap reset failed: {exc}")
        return False
    elapsed = time.perf_counter() - t0
    log(
        f"Reset in {elapsed:.1f}s — {deck.get('remaining')}/{deck.get('limit')} swipes, "
        f"capped={deck.get('capped')}"
    )
    return not deck.get("capped") and int(deck.get("remaining", 0)) > 0


def resolve_credentials(args: argparse.Namespace) -> dict:
    from_cli = credentials_from_args(args)
    if from_cli:
        return from_cli
    if args.no_prompt:
        die("Use --gag-session or run without --no-prompt.")
    return prompt_credentials()


def main() -> None:
    parser = argparse.ArgumentParser(description="gag.gg vote auto-swiper")
    parser.add_argument("--gag-session", help="__Host-gag_session cookie value")
    parser.add_argument("--no-prompt", action="store_true", help="Require --gag-session")
    parser.add_argument("--once", action="store_true", help="Run one swipe session then exit")
    parser.add_argument("--contest", action="store_true", help="Enter carrot contest after swiping")
    parser.add_argument("--dry-run", action="store_true", help="Only check auth, no swipes")
    parser.add_argument(
        "--reset-on-cap",
        action="store_true",
        help="Delete profile when capped to refresh quota (default)",
    )
    parser.add_argument(
        "--wait-on-cap",
        action="store_true",
        help="Sleep until UTC hour reset instead of delete loop",
    )
    parser.add_argument("--turbo", action="store_true", help="Max speed mode (default)")
    parser.add_argument(
        "--relaxed",
        action="store_true",
        help="Slower, human-like swipe timing",
    )
    args = parser.parse_args()

    cfg = build_runtime_config(args)
    credentials = resolve_credentials(args)

    try:
        cookie = gag_cookie_header(credentials)
    except ValueError as exc:
        die(str(exc))

    vote_mode = cfg.get("vote", "like")
    swipe_delay = tuple(cfg.get("swipe_delay_ms", [80, 350]))
    decision_ms = tuple(cfg.get("decision_ms", [50, 400]))
    skip_claim = bool(cfg.get("skip_claim", False))
    claim_rewards = bool(cfg.get("claim_rewards", True))
    quiet = bool(cfg.get("quiet", False))
    turbo = bool(cfg.get("turbo", False))
    reset_on_cap = bool(cfg.get("reset_on_cap", True))
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
            die(f"Auth failed ({exc}). Check your gag_session cookie.")

        if not me.get("signedIn"):
            die("Not signed in — log in on gag.gg and paste a fresh gag_session.")

        mode = "turbo" if turbo else "relaxed"
        cap_mode = "delete-reset loop" if reset_on_cap else "hourly wait"
        log(
            f"Signed in as {me.get('username')} ({me.get('sub')}) — "
            f"carrots: {me.get('carrots', 0)} — {mode} mode, {cap_mode}"
        )

        if args.dry_run:
            deck = client.vote_deck()
            log(f"Dry run OK — {deck.get('remaining')}/{deck.get('limit')} swipes, capped={deck.get('capped')}")
            return

        cycle = 0
        while True:
            cycle += 1
            if client.is_vote_capped():
                if reset_on_cap:
                    if not quiet:
                        log(f"--- cycle {cycle}: capped — reset ---")
                    if not reset_cap_bypass(client):
                        die("Cap reset failed; exiting.")
                else:
                    if not quiet:
                        log(f"--- cycle {cycle}: capped — waiting for UTC reset ---")
                    sleep_until_reset()
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
                skip_warmup=turbo and cycle > 1,
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
                    if reset_cap_bypass(client):
                        continue
                    die("Cap reset failed; exiting.")
                sleep_until_reset()
            elif not quiet:
                log("Swipes still available — continuing…")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
