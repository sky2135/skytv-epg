from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Any


_OPTIMIZATION_VERSION = "1.0"


def _catalog_fingerprint(
    real_candidates: list[dict[str, str]],
    dummy_ids: dict[str, str],
) -> str:
    """Return a deterministic fingerprint without changing candidate order."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(len(real_candidates)).encode("ascii"))
    digest.update(b"\0")
    for item in real_candidates:
        for key in ("feed", "region", "epg_id", "display_name", "normalized"):
            digest.update(str(item.get(key, "")).encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")
        digest.update(b"\xff")
    digest.update(str(len(dummy_ids)).encode("ascii"))
    digest.update(b"\0")
    for key, value in sorted(dummy_ids.items()):
        digest.update(str(key).encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(str(value).encode("utf-8", errors="surrogatepass"))
        digest.update(b"\xff")
    return digest.hexdigest()


def install_performance_optimizations(engine: Any, *, max_catalog_workers: int = 6) -> dict[str, Any]:
    """Install deterministic runtime optimizations around the frozen v7 engine.

    The matcher functions, thresholds, aliases, tie-breaking, and builder rules are
    not edited. The wrappers only (1) reuse already-built indexes when the exact
    same source catalog is used twice and (2) fetch independent EPGShare text
    catalogs concurrently, then process them in the original configured order.
    """
    existing = getattr(engine, "_SKYTV_PERFORMANCE_PATCH", None)
    if isinstance(existing, dict):
        return existing

    original_prepare = engine._smart_v7_prepare_matcher
    prepare_state: dict[str, Any] = {
        "fingerprint": None,
        "real_candidates": None,
        "dummy_ids": None,
        "builds": 0,
        "reuses": 0,
    }

    @wraps(original_prepare)
    def cached_prepare(
        real_candidates: list[dict[str, str]],
        dummy_ids: dict[str, str],
    ) -> None:
        fingerprint = _catalog_fingerprint(real_candidates, dummy_ids)
        if fingerprint == prepare_state["fingerprint"]:
            prepare_state["reuses"] += 1
            return
        original_prepare(real_candidates, dummy_ids)
        prepare_state.update({
            "fingerprint": fingerprint,
            # Keep references alive so Python cannot recycle their identities while
            # the prepared global indexes still refer to their candidate dictionaries.
            "real_candidates": real_candidates,
            "dummy_ids": dummy_ids,
            "builds": prepare_state["builds"] + 1,
        })

    original_download_catalog = engine.download_epgshare_catalog

    def _fetch_catalog(feed_name: str, feed: dict[str, Any]) -> dict[str, Any]:
        if not bool(feed.get("use_for_matching", True)):
            return {"name": feed_name, "feed": feed, "status": "skipped"}
        url = str(feed.get("txt_url", "")).strip()
        if not url:
            return {"name": feed_name, "feed": feed, "status": "missing_url"}
        worker_session = engine.make_session()
        try:
            response = worker_session.get(url, timeout=(20, 180))
            if response.status_code != 200:
                return {
                    "name": feed_name,
                    "feed": feed,
                    "status": "http_error",
                    "status_code": int(response.status_code),
                }
            catalog_text = response.content.decode("utf-8-sig", errors="replace")
            ids = engine.parse_epgshare_ids(catalog_text)
            return {"name": feed_name, "feed": feed, "status": "ok", "ids": ids}
        except (engine.requests.RequestException, UnicodeError) as exc:
            return {
                "name": feed_name,
                "feed": feed,
                "status": "download_error",
                "error": str(exc),
            }
        finally:
            worker_session.close()

    def parallel_download_catalog(
        session: Any,  # Kept for API compatibility with the original function.
    ) -> tuple[list[dict[str, str]], dict[str, str]]:
        del session
        configured = [(name, dict(feed)) for name, feed in engine.EPGSHARE_FEEDS.items()]
        workers = max(1, min(int(max_catalog_workers), len(configured) or 1))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="epg-catalog") as pool:
            futures = [pool.submit(_fetch_catalog, name, feed) for name, feed in configured]
            # Calling result() in submission order preserves the exact registry order
            # used by the old sequential implementation and therefore preserves all
            # candidate ordering and tie-breaking behavior.
            fetched = [future.result() for future in futures]

        real_candidates: list[dict[str, str]] = []
        dummy_ids: dict[str, str] = {}
        seen_pairs: set[tuple[str, str]] = set()

        for result in fetched:
            feed_name = result["name"]
            feed = result["feed"]
            status = result["status"]
            if status == "skipped":
                print(f"{feed_name}: available for builds, skipped during matching")
                continue
            if status == "missing_url":
                print(f"Warning: {feed_name} has no text catalog; skipped during matching.")
                continue
            if status == "http_error":
                print(
                    f"Warning: {feed_name} returned HTTP {result['status_code']}; continuing."
                )
                continue
            if status == "download_error":
                print(f"Warning: could not download {feed_name}; continuing.")
                continue

            ids = result["ids"]
            print(f"{feed_name}: {len(ids):,} EPG IDs")
            if feed["kind"] == "dummy":
                for epg_id in ids:
                    dummy_ids[epg_id.casefold()] = epg_id
                continue

            for epg_id in ids:
                pair = (feed_name, epg_id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                display_name = engine.epg_id_to_name(epg_id)
                real_candidates.append({
                    "epg_id": epg_id,
                    "feed": feed_name,
                    "region": feed["region"],
                    "display_name": display_name,
                    "normalized": engine.normalize_name(display_name),
                })

        if not real_candidates:
            raise RuntimeError("No real EPGShare IDs could be downloaded.")
        return real_candidates, dummy_ids

    engine._smart_v7_prepare_matcher = cached_prepare
    engine.download_epgshare_catalog = parallel_download_catalog

    patch = {
        "version": _OPTIMIZATION_VERSION,
        "original_prepare": original_prepare,
        "original_download_catalog": original_download_catalog,
        "prepare_state": prepare_state,
        "max_catalog_workers": max_catalog_workers,
    }
    engine._SKYTV_PERFORMANCE_PATCH = patch
    return patch


def optimization_status(engine: Any) -> dict[str, Any]:
    patch = getattr(engine, "_SKYTV_PERFORMANCE_PATCH", {})
    state = patch.get("prepare_state", {}) if isinstance(patch, dict) else {}
    return {
        "installed": bool(patch),
        "version": patch.get("version", "") if isinstance(patch, dict) else "",
        "matcher_index_builds": int(state.get("builds", 0) or 0),
        "matcher_index_reuses": int(state.get("reuses", 0) or 0),
        "max_catalog_workers": int(patch.get("max_catalog_workers", 0) or 0)
        if isinstance(patch, dict)
        else 0,
    }
