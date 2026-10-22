"""Shared, deadline-aware TMDB transport for the isolated verification runner.

The production client's normal network/5xx retry policy remains in charge of
those retries. This wrapper paces every wire attempt and handles HTTP 429 itself,
so the production client's shorter Retry-After cap cannot shorten a server pause.
Snapshots contain only counters, route classes and an absolute cooldown time.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import requests


DEFAULT_REQUESTS_PER_SECOND = 20.0
_COUNTERS = ("wireAttempts", "networkErrors", "rateLimitResponses",
             "rateLimitRetries", "retryableServerResponses", "deferredCalls")
_DURATIONS = ("waitSeconds", "cooldownSeconds")


class VerificationDeferred(requests.RequestException):
    """A resumable pause, with no request URL, headers, parameters or body."""

    def __init__(self, reason: str, retry_at: float):
        self.reason = self.deferred_reason = reason
        self.retry_at = self.retryAt = retry_at
        super().__init__(f"TMDB verification deferred: {reason}.")


def _finite(value: Any, *, minimum: float = 0) -> float:
    if isinstance(value, bool):
        raise ValueError("Invalid verification rate-limit state.")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Invalid verification rate-limit state.") from None
    if not math.isfinite(result) or result < minimum:
        raise ValueError("Invalid verification rate-limit state.")
    return result


def retry_after_seconds(value: object, now: float, attempt: int = 0) -> float:
    """Parse seconds or an HTTP date, preserving the full server-specified wait."""
    try:
        delay = float(value)
        if not math.isfinite(delay):
            raise ValueError
        return max(0.0, delay)
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = parsed.timestamp() - now
            if not math.isfinite(delay):
                raise ValueError
            return max(0.0, delay)
        except (ValueError, TypeError, OverflowError, IndexError):
            # Only an absent/invalid header uses bounded fallback backoff.
            return float(min(60, 2 ** min(max(0, attempt), 6)))


def _route_class(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or parts[0] != "3":
        return "other"
    if parts[1] == "search" and len(parts) == 3 and parts[2] in {"movie", "tv"}:
        return "search_" + parts[2]
    if parts[1] == "find":
        return "find_external_id"
    if parts[1] in {"movie", "tv"} and len(parts) >= 3:
        kind = parts[1]
        suffix = parts[3] if len(parts) > 3 else "details"
        return kind + "_" + (suffix if suffix in {"details", "images", "alternative_titles", "external_ids", "credits", "release_dates"} else "other")
    if parts[1] in {"trending", "configuration", "discover"}:
        return parts[1]
    return "other"


class SharedRateLimiter:
    """One evenly spaced request gate shared by all workers in one batch.

    ``deadline`` and durable ``cooldownUntil`` are UNIX seconds. Scheduling uses
    monotonic time after construction, including when the wall clock changes.
    Pass the prior ``snapshot()`` as ``resume_state`` to retain a long cooldown
    and cumulative counters across processes/batches. ``persist``, when given,
    receives an immediate snapshot after request/cooldown changes. It must
    atomically save that state; failures propagate so testing cannot continue
    while silently losing its durable cooldown. Concurrent saves are ordered,
    and the callback runs outside the scheduling lock.
    """

    def __init__(self, rate: float = DEFAULT_REQUESTS_PER_SECOND,
                 deadline: float | None = None, *, now: Callable[[], float] | None = None,
                 monotonic: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 resume_state: Mapping[str, Any] | None = None,
                 persist: Callable[[dict[str, Any]], None] | None = None):
        self.rate = _finite(rate)
        if self.rate <= 0:
            raise ValueError("Verification request rate must be positive.")
        self._now = now or time.time
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.RLock()
        self._persist_lock = threading.RLock()
        if persist is not None and not callable(persist):
            raise ValueError("Verification state persistence must be callable.")
        self._persist_callback = persist
        wall, mono = self._now(), self._monotonic()
        self.deadline = None if deadline is None else _finite(deadline)
        self._deadline_mono = None if deadline is None else mono + max(0.0, self.deadline - wall)
        self._next_request = mono
        self._cooldown_mono = mono
        self.last_deferred: VerificationDeferred | None = None
        self._stats: dict[str, Any] = {key: 0 for key in _COUNTERS}
        self._stats.update({key: 0.0 for key in _DURATIONS})
        self._stats.update({"statusCounts": {}, "routeCounts": {}})
        self._stats.update({"firstRequestAt": None, "lastRequestAt": None})
        if resume_state is not None:
            if not isinstance(resume_state, Mapping) or resume_state.get("schemaVersion", 1) != 1:
                raise ValueError("Invalid verification rate-limit state.")
            cooldown = resume_state.get("cooldownUntil")
            if cooldown is not None:
                self._cooldown_mono = mono + max(0.0, _finite(cooldown) - wall)
            for key in (*_COUNTERS, *_DURATIONS):
                value = _finite(resume_state.get(key, 0))
                if key in _COUNTERS and not value.is_integer():
                    raise ValueError("Invalid verification rate-limit state.")
                self._stats[key] = int(value) if key in _COUNTERS else value
            for key in ("firstRequestAt", "lastRequestAt"):
                value = resume_state.get(key)
                self._stats[key] = None if value is None else _finite(value)
            for key in ("statusCounts", "routeCounts"):
                mapping = resume_state.get(key, {})
                if not isinstance(mapping, Mapping):
                    raise ValueError("Invalid verification rate-limit state.")
                allowed = ({str(value) for value in range(100, 600)} | {"other"}) if key == "statusCounts" else {
                    "other", "search_movie", "search_tv", "find_external_id", "trending", "configuration", "discover",
                    *(kind + "_" + suffix for kind in ("movie", "tv") for suffix in ("details", "images", "alternative_titles", "external_ids", "credits", "release_dates", "other")),
                }
                for name, count in mapping.items():
                    # Never propagate arbitrary persisted strings into reports.
                    if name not in allowed:
                        raise ValueError("Invalid verification rate-limit state.")
                    number = _finite(count)
                    if not number.is_integer():
                        raise ValueError("Invalid verification rate-limit state.")
                    self._stats[key][name] = int(number)

    @property
    def remaining_seconds(self) -> float | None:
        with self._lock:
            return None if self._deadline_mono is None else max(0.0, self._deadline_mono - self._monotonic())

    def _persist(self) -> None:
        if self._persist_callback is not None:
            # Take the snapshot after obtaining the save lock. A worker that
            # was descheduled before entering this method cannot overwrite a
            # later save with an older precomputed snapshot.
            with self._persist_lock:
                self._persist_callback(self.snapshot())

    def _defer(self, reason: str, ready: float, mono: float) -> VerificationDeferred:
        self._stats["deferredCalls"] += 1
        self.last_deferred = VerificationDeferred(reason, self._now() + max(0.0, ready - mono))
        return self.last_deferred

    def acquire(self) -> None:
        """Wait for a permit, or defer without sleeping past the batch deadline."""
        while True:
            deferred = None
            with self._lock:
                mono = self._monotonic()
                ready = max(self._next_request, self._cooldown_mono)
                if self._deadline_mono is not None and (mono >= self._deadline_mono or ready >= self._deadline_mono):
                    reason = "rate_limit_cooldown" if self._cooldown_mono >= self._deadline_mono else "batch_deadline"
                    deferred = self._defer(reason, ready, mono)
                delay = max(0.0, ready - mono)
                if deferred is None and delay <= 0:
                    self._next_request = mono + 1.0 / self.rate
                    return
            if deferred is not None:
                self._persist()
                raise deferred
            # Do not hold the lock while waiting: another worker's 429 must be
            # able to extend the global cooldown immediately.
            started = self._monotonic()
            try:
                self._sleep(min(60.0, delay))
            finally:
                with self._lock:
                    self._stats["waitSeconds"] += max(0.0, self._monotonic() - started)

    def _apply_cooldown(self, seconds: float) -> None:
        seconds = _finite(seconds)
        mono = self._monotonic()
        previous = max(mono, self._cooldown_mono)
        self._cooldown_mono = max(previous, mono + seconds)
        self._stats["cooldownSeconds"] += self._cooldown_mono - previous

    def cooldown(self, seconds: float) -> None:
        with self._lock:
            self._apply_cooldown(seconds)
        self._persist()

    def record_attempt(self, route: str, *, rate_limit_retry: bool = False) -> None:
        with self._lock:
            timestamp = self._now()
            if self._stats["firstRequestAt"] is None:
                self._stats["firstRequestAt"] = timestamp
            self._stats["lastRequestAt"] = timestamp
            self._stats["wireAttempts"] += 1
            self._stats["routeCounts"][route] = self._stats["routeCounts"].get(route, 0) + 1
            if rate_limit_retry:
                self._stats["rateLimitRetries"] += 1
        self._persist()

    def record_response(self, status: int, *, cooldown_seconds: float | None = None) -> None:
        with self._lock:
            key = str(status) if type(status) is int and 100 <= status < 600 else "other"
            self._stats["statusCounts"][key] = self._stats["statusCounts"].get(key, 0) + 1
            if status == 429:
                self._stats["rateLimitResponses"] += 1
            elif type(status) is int and 500 <= status < 600:
                self._stats["retryableServerResponses"] += 1
            if cooldown_seconds is not None:
                self._apply_cooldown(cooldown_seconds)
        self._persist()

    def record_network_error(self) -> None:
        with self._lock:
            self._stats["networkErrors"] += 1
        self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            remaining = max(0.0, self._cooldown_mono - self._monotonic())
            return {"schemaVersion": 1, "requestsPerSecond": self.rate,
                    "cooldownUntil": self._now() + remaining if remaining else None,
                    **{key: value.copy() if isinstance(value, dict) else value for key, value in self._stats.items()}}


class PacedSession:
    """A requests-compatible ``get`` wrapper; share its limiter, not Sessions.

    ``rateLimitRetries`` counts retries made here. Add the existing TMDB client's
    retry count for retries of network errors and 5xx replies. ``wireAttempts``
    is the authoritative API usage count, including every kind of retry.
    """

    def __init__(self, limiter: SharedRateLimiter, session: Any = None):
        self.limiter = limiter
        self.session = session if session is not None else requests.Session()
        self._owns_session = session is None
        if self._owns_session:
            self.session.trust_env = False
        self.last_deferred: VerificationDeferred | None = None

    def get(self, url: str, **kwargs: Any) -> Any:
        try:
            parsed = urlsplit(url)
        except (TypeError, ValueError):
            raise requests.RequestException("TMDB verification destination is not allowed.") from None
        if (parsed.scheme != "https" or parsed.netloc != "api.themoviedb.org"
                or not parsed.path.startswith("/3/") or parsed.fragment):
            raise requests.RequestException("TMDB verification destination is not allowed.")
        route = _route_class(parsed.path)
        # The API hostname is the only permitted destination. Never follow a
        # redirect carrying authorization or query-string API credentials.
        options = {**kwargs, "allow_redirects": False}
        rate_retries = 0
        while True:
            try:
                self.limiter.acquire()
            except VerificationDeferred as error:
                self.last_deferred = error
                raise
            self.limiter.record_attempt(route, rate_limit_retry=rate_retries > 0)
            try:
                response = self.session.get(url, **options)
            except requests.RequestException:
                self.limiter.record_network_error()
                raise requests.RequestException("TMDB verification request failed.") from None
            if response.status_code != 429:
                try:
                    self.limiter.record_response(response.status_code)
                except BaseException:
                    response.close()
                    raise
                return response
            try:
                delay = retry_after_seconds(response.headers.get("Retry-After"), self.limiter._now(), rate_retries)
                # Persist the 429 counter and its full cooldown together. A
                # checkpoint must never advertise a 429 without its pause.
                self.limiter.record_response(429, cooldown_seconds=delay)
            finally:
                response.close()
            rate_retries += 1

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> "PacedSession":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
