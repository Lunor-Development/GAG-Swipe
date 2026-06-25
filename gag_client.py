"""HTTP client for gag.gg vote + contest APIs."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

from proxy_pool import ProxyPool

BASE = "https://gag.gg"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "priority": "u=1, i",
}
DEFAULT_RATE_LIMIT_RETRIES = 8
DEFAULT_RATE_LIMIT_MAX_WAIT_S = 30.0
DEFAULT_PROXY_TIMEOUT_S = 8.0
MAX_PROXY_ROTATIONS_PER_REQUEST = 8


@dataclass
class SessionStats:
    swipes: int = 0
    carrots_awarded: int = 0
    jackpots: list[dict[str, Any]] = field(default_factory=list)
    claimed: list[dict[str, Any]] = field(default_factory=list)
    capped_runs: int = 0
    finished: bool = False
    reward_due: bool = False
    reward_won_ever: bool = False
    last_swipe: dict[str, Any] | None = None


class GagClient:
    def __init__(
        self,
        cookie: str,
        timeout: float = 30.0,
        *,
        rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
        rate_limit_max_wait_s: float = DEFAULT_RATE_LIMIT_MAX_WAIT_S,
        use_proxies_on_rate_limit: bool = True,
        proxy_fetch_limit: int = 30,
        proxy_timeout: float = DEFAULT_PROXY_TIMEOUT_S,
    ) -> None:
        self._rate_limit_retries = max(0, int(rate_limit_retries))
        self._rate_limit_max_wait_s = max(1.0, float(rate_limit_max_wait_s))
        self._proxy_timeout = max(3.0, float(proxy_timeout))
        self._cookie = cookie.strip()
        self._timeout = timeout
        self._proxy_url: str | None = None
        self._proxy_pool = (
            ProxyPool(fetch_limit=proxy_fetch_limit) if use_proxies_on_rate_limit else None
        )
        self._client = self._make_client()

    def _make_client(self, proxy_url: str | None = None) -> httpx.Client:
        req_timeout = (
            httpx.Timeout(self._proxy_timeout, connect=min(5.0, self._proxy_timeout))
            if proxy_url
            else self._timeout
        )
        kwargs: dict[str, Any] = {
            "base_url": BASE,
            "timeout": req_timeout,
            "headers": {
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": BASE,
                "Referer": f"{BASE}/vote/",
                "Cookie": self._cookie,
                **BROWSER_HEADERS,
            },
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.Client(**kwargs)

    def _rebuild_client(self, proxy_url: str | None) -> None:
        self._client.close()
        self._proxy_url = proxy_url
        self._client = self._make_client(proxy_url)

    def _rotate_proxy(self) -> bool:
        if not self._proxy_pool:
            return False
        try:
            proxy = self._proxy_pool.next()
        except Exception as exc:
            log(f"ProxyScrape fetch failed: {exc}")
            return False
        if not proxy:
            log("No working proxies available from ProxyScrape")
            return False
        label = proxy.split("://", 1)[-1]
        self._rebuild_client(proxy)
        log(f"Switched to proxy {label}")
        return True

    def clear_proxy(self) -> None:
        if self._proxy_url is None:
            return
        self._rebuild_client(None)
        log("Back to direct connection")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GagClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _json(self, resp: httpx.Response) -> dict[str, Any]:
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"expected object JSON, got {type(data)}")
        return data

    def _retry_wait_s(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(self._rate_limit_max_wait_s, max(0.5, float(retry_after)))
            except ValueError:
                pass
        backoff = min(self._rate_limit_max_wait_s, (2**attempt) + random.uniform(0.3, 1.2))
        return max(0.5, backoff)

    def _recover_transport(self, label: str, exc: httpx.RequestError) -> bool:
        err = type(exc).__name__
        if self._proxy_url:
            if self._proxy_pool:
                self._proxy_pool.mark_failed(self._proxy_url)
            log(f"Proxy error on {label} ({err}) — rotating")
            if self._rotate_proxy():
                return True
            log("Proxies exhausted — switching back to direct")
            self.clear_proxy()
            return True
        return False

    def _request(
        self,
        method: str,
        path: str,
        *,
        expect_json: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        label = path.rsplit("/", 1)[-1] or path
        proxy_rotations = 0
        for attempt in range(self._rate_limit_retries + 1):
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                if self._recover_transport(label, exc):
                    proxy_rotations += 1
                    if proxy_rotations <= MAX_PROXY_ROTATIONS_PER_REQUEST:
                        continue
                if attempt >= self._rate_limit_retries:
                    raise
                wait_s = min(5.0, 1.0 + attempt * 0.5)
                log(f"Request error on {label} ({type(exc).__name__}) — retry in {wait_s:.1f}s")
                time.sleep(wait_s)
                continue

            if resp.status_code != 429:
                if expect_json:
                    return self._json(resp)
                resp.raise_for_status()
                return None

            if attempt >= self._rate_limit_retries:
                resp.raise_for_status()

            if self._rotate_proxy():
                log(f"Rate limited (429) on {label} — retrying via proxy")
                continue

            wait_s = self._retry_wait_s(resp, attempt)
            log(
                f"Rate limited (429) on {label} — waiting {wait_s:.1f}s "
                f"({attempt + 1}/{self._rate_limit_retries})"
            )
            time.sleep(wait_s)

        raise RuntimeError(f"rate limit retries exhausted for {path}")

    def auth_me(self) -> dict[str, Any]:
        return self._request("GET", "/api/auth/me")  # type: ignore[return-value]

    def profile_me(self) -> dict[str, Any]:
        return self._request("GET", "/api/profile/me")  # type: ignore[return-value]

    def vote_deck(self) -> dict[str, Any]:
        return self._request("GET", "/api/vote/deck")  # type: ignore[return-value]

    def server_time(self) -> dict[str, Any]:
        return self._request("GET", "/api/time")  # type: ignore[return-value]

    def events(self) -> dict[str, Any]:
        return self._request("GET", "/api/events")  # type: ignore[return-value]

    def vote_claim(self) -> dict[str, Any]:
        return self._request("POST", "/api/vote/claim")  # type: ignore[return-value]

    def vote_swipe(
        self,
        image_id: str,
        vote: Literal["like", "dislike"],
        decision_ms: int,
    ) -> dict[str, Any]:
        payload = {
            "image_id": image_id,
            "vote": vote,
            "decision_ms": max(0, int(decision_ms)),
        }
        return self._request(  # type: ignore[return-value]
            "POST",
            "/api/vote/swipe",
            json=payload,
            headers={"Content-Type": "application/json"},
        )

    def contest_state(self) -> dict[str, Any]:
        return self._request(  # type: ignore[return-value]
            "GET",
            "/api/contest/state",
            headers={"Referer": f"{BASE}/contest/"},
        )

    def contest_enter(self, carrots: int) -> dict[str, Any]:
        return self._request(  # type: ignore[return-value]
            "POST",
            "/api/contest/enter",
            json={"carrots": carrots},
            headers={
                "Content-Type": "application/json",
                "Referer": f"{BASE}/contest/",
            },
        )

    def delete_account(self) -> None:
        """Wipe gag.gg profile — resets swipe cap for the same Roblox account on re-login."""
        self._request(
            "POST",
            "/api/account/delete",
            expect_json=False,
            headers={"Referer": f"{BASE}/profile/"},
        )

    def update_cookie(self, cookie: str) -> None:
        self._cookie = cookie.strip()
        self._rebuild_client(self._proxy_url)

    def is_vote_capped(self) -> bool:
        deck = self.vote_deck()
        return bool(deck.get("capped")) or int(deck.get("remaining", 0)) <= 0


def log(msg: str) -> None:
    now = datetime.now()
    ts = f"{now.hour}:{now.strftime('%M:%S')}"
    print(f"[{ts}] {msg}")


def next_hourly_reset() -> datetime:
    """Match gag.gg vote timer: top of next UTC hour + ~2s buffer."""
    now = datetime.now(timezone.utc)
    reset = now.replace(minute=0, second=0, microsecond=0)
    if now.minute != 0 or now.second > 2:
        from datetime import timedelta

        reset += timedelta(hours=1)
    return reset.replace(second=2, microsecond=0)


def sleep_until_reset() -> None:
    target = next_hourly_reset()
    wait_s = max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    log(f"Hourly cap hit — sleeping {wait_s:.0f}s until {target.isoformat()}")
    time.sleep(wait_s + random.uniform(0.5, 2.0))


def pick_vote(mode: str) -> Literal["like", "dislike"]:
    if mode == "dislike":
        return "dislike"
    if mode == "random":
        return random.choice(["like", "dislike"])
    return "like"


def format_reward(reward: dict[str, Any]) -> str:
    count = int(reward.get("count") or 1)
    item = reward.get("item") or reward.get("reward") or "reward"
    category = reward.get("category") or "?"
    return f"{item}" + (f" x{count}" if count > 1 else "") + f" ({category})"


def warmup_vote_session(client: GagClient) -> None:
    """Match browser vote-page startup (auth, time sync, events, pending claim)."""
    client.auth_me()
    try:
        client.server_time()
    except httpx.HTTPError:
        pass
    try:
        client.events()
    except httpx.HTTPError:
        pass


def log_session_reward_status(stats: SessionStats) -> None:
    """Summarize batch outcome after a full swipe cap."""
    if not stats.finished:
        return
    if stats.jackpots:
        return
    if stats.reward_won_ever:
        log("  Batch done — rewardWonEver=true (check Roblox mail)")
    elif stats.reward_due:
        log("  Batch done — no seed pack rolled this cycle (RNG miss)")


def claim_vote_rewards(
    client: GagClient,
    *,
    quiet: bool = False,
    stats: SessionStats | None = None,
    retries: int = 1,
    retry_delay_s: float = 0.0,
) -> dict[str, Any] | None:
    """Claim pending seed packs / items via POST /api/vote/claim."""
    for attempt in range(max(1, retries)):
        if attempt and retry_delay_s > 0:
            time.sleep(retry_delay_s)
        try:
            resp = client.vote_claim()
        except httpx.HTTPError as exc:
            if not quiet:
                print(f"  claim failed: {exc}")
            return None

        claimed = resp.get("claimed")
        if claimed:
            if stats is not None:
                stats.claimed.append(claimed)
            log(f"  CLAIMED: {format_reward(claimed)}")
            return claimed

    return None


def finalize_vote_rewards(
    client: GagClient,
    stats: SessionStats,
    *,
    quiet: bool = False,
    claim_retries: int = 3,
) -> None:
    """Poll /api/vote/claim after a swipe batch."""
    claim_vote_rewards(
        client,
        quiet=quiet,
        stats=stats,
        retries=claim_retries,
        retry_delay_s=0.4,
    )


def run_swipe_session(
    client: GagClient,
    *,
    vote_mode: str = "like",
    swipe_delay_ms: tuple[int, int] = (80, 350),
    decision_ms_range: tuple[int, int] = (50, 400),
    stats: SessionStats | None = None,
    skip_claim: bool = False,
    claim_rewards: bool = True,
    quiet: bool = False,
) -> SessionStats:
    stats = stats or SessionStats()

    warmup_vote_session(client)
    if claim_rewards and not skip_claim:
        claim_vote_rewards(client, quiet=quiet, stats=stats)

    seen: set[str] = set()
    deck_data = client.vote_deck()
    remaining = int(deck_data.get("remaining", 0))
    limit = int(deck_data.get("limit", 20))
    if not quiet:
        log(f"Deck loaded — {remaining}/{limit} swipes remaining")

    if deck_data.get("capped"):
        stats.capped_runs += 1
        stats.finished = True
        return stats

    if deck_data.get("locked"):
        if claim_rewards:
            claim_vote_rewards(client, quiet=quiet, stats=stats)
            deck_data = client.vote_deck()
        if deck_data.get("locked"):
            if not quiet:
                log(f"Account locked pending claim: {deck_data.get('claim')}")
            stats.finished = True
            return stats

    queue = [c for c in deck_data.get("deck", []) if c.get("id") not in seen]
    delay_min, delay_max = swipe_delay_ms

    while remaining > 0 and queue:
        card = queue.pop(0)
        image_id = card.get("id")
        if not image_id or image_id in seen:
            continue
        seen.add(image_id)

        vote = pick_vote(vote_mode)
        lo, hi = decision_ms_range
        decision_ms = lo if lo == hi else random.randint(lo, hi)
        result = client.vote_swipe(image_id, vote, decision_ms)
        stats.last_swipe = result

        stats.swipes += 1
        if result.get("carrotAwarded"):
            stats.carrots_awarded += 1

        remaining = min(int(result.get("remaining", 0)), remaining)
        if not quiet:
            name = card.get("name", image_id)
            log(
                f"  [{stats.swipes}] {vote} {name!r} — "
                f"{remaining} left"
                + (" CAP" if result.get("capped") else "")
            )

        if jackpot := result.get("jackpot"):
            stats.jackpots.append(jackpot)
            log(f"  JACKPOT: {format_reward(jackpot)}")

        if result.get("claimRequired"):
            pending = result.get("claim") or result.get("jackpot")
            if pending and pending not in stats.jackpots:
                stats.jackpots.append(pending)
                log(f"  JACKPOT (claim): {format_reward(pending)}")
            claim_vote_rewards(
                client, quiet=quiet, stats=stats, retries=3, retry_delay_s=0.5
            )

        if result.get("capped") or remaining <= 0:
            stats.capped_runs += 1
            stats.finished = True
            stats.reward_due = bool(result.get("rewardDue"))
            stats.reward_won_ever = bool(result.get("rewardWonEver"))
            break

        if not queue and remaining > 0:
            refill = client.vote_deck()
            remaining = int(refill.get("remaining", remaining))
            for c in refill.get("deck", []):
                cid = c.get("id")
                if cid and cid not in seen:
                    queue.append(c)

        if delay_max > 0:
            time.sleep(random.randint(delay_min, delay_max) / 1000.0)

    if stats.finished:
        log_session_reward_status(stats)

    if claim_rewards and stats.finished:
        finalize_vote_rewards(client, stats, quiet=quiet)

    return stats


def run_contest(client: GagClient, carrots: int | Literal["all"] = "all") -> None:
    state = client.contest_state()
    balance = int(state.get("carrots", state.get("balance", 0)))
    if carrots == "all":
        amount = balance
    else:
        amount = min(int(carrots), balance)

    if amount <= 0:
        print("Contest: no carrots to enter")
        return

    result = client.contest_enter(amount)
    print(f"Contest: entered {amount} carrots — {result}")
