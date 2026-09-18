"""Advisory-only AI review for Matching Lab shortlists.

This module deliberately has no Sheet client and exposes no apply operation.
Gemini receives two to eight locally retrieved candidates under opaque ``cNNN``
keys.  Real EPG IDs remain in the local :class:`~matching_lab.models.ProposalRecord`.
The model can support the locally selected key, disagree, or abstain; it cannot
introduce a candidate and it never changes the selected key.

The network boundary is delegated to the repository's existing hardened Gemini
review client.  That client supplies a fixed JSON response schema, treats all
model output as untrusted, redacts secret-like input, disables redirects, and
converts transport or validation failures into closed error results.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

from .compat import gemini_review
from .models import (
    AIReviewEvidence,
    ContractError,
    DecisionState,
    ProposalRecord,
    canonical_json_bytes,
    safe_text,
    sha256_bytes,
)


AI_REQUEST_SCHEMA = "skytv.matching-lab.ai-request.v1"
PROMPT_VERSION = "matching-lab-advisory-v1"
DEFAULT_MODEL = gemini_review.DEFAULT_MODEL
MAX_ADVISORY_REQUESTS = 1_000
MAX_CALL_SIZE = gemini_review.MAX_ROWS_PER_CALL

_OPAQUE_KEY_RE = re.compile(r"\Ac[0-9]{3}\Z")
_MODEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_CACHE_SCHEMA_VERSION = 2
_CACHE_CHECKPOINT_SCHEMA = "skytv.matching-lab.ai-cache-checkpoint.v1"


class AdvisoryDisposition(str, Enum):
    """Closed local interpretation of one model result."""

    SUPPORTS_LOCAL = "AI_SUPPORTS_LOCAL"
    DISAGREES = "AI_DISAGREES"
    LOW_CONFIDENCE = "AI_LOW_CONFIDENCE"
    ABSTAINED = "AI_ABSTAINED"
    ERROR = "AI_REVIEW_ERROR"


@dataclass(frozen=True, slots=True)
class AdvisoryCandidate:
    """Candidate context safe to send under an opaque local key."""

    candidate_key: str
    display_name: str
    region: str = ""
    feed: str = ""

    def __post_init__(self) -> None:
        key = str(self.candidate_key or "")
        if _OPAQUE_KEY_RE.fullmatch(key) is None:
            raise ContractError("AI candidate keys must use the opaque cNNN format.")
        object.__setattr__(self, "candidate_key", key)
        display_name = _bounded_text(self.display_name, maximum=180)
        if not display_name:
            raise ContractError("An AI advisory candidate requires a display name.")
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "region", _bounded_text(self.region, maximum=64).upper())
        object.__setattr__(self, "feed", _bounded_text(self.feed, maximum=64).upper())


@dataclass(frozen=True, slots=True)
class AdvisoryRequest:
    """One local decision and the only candidates Gemini may discuss."""

    review_id: str
    channel_name: str
    category_name: str
    market: str
    candidates: tuple[AdvisoryCandidate, ...]
    locally_selected_candidate_key: str = ""

    def __post_init__(self) -> None:
        review_id = safe_text(self.review_id, maximum=300)
        if not review_id:
            raise ContractError("An AI advisory request requires a local review_id.")
        object.__setattr__(self, "review_id", review_id)
        channel_name = _bounded_text(self.channel_name, maximum=180)
        if not channel_name:
            raise ContractError("An AI advisory request requires a channel name.")
        object.__setattr__(self, "channel_name", channel_name)
        object.__setattr__(
            self, "category_name", _bounded_text(self.category_name, maximum=120)
        )
        object.__setattr__(self, "market", _bounded_text(self.market, maximum=64).upper())

        candidates = tuple(self.candidates)
        if not 2 <= len(candidates) <= 8:
            raise ContractError("AI advisory requests require two to eight candidates.")
        if any(not isinstance(candidate, AdvisoryCandidate) for candidate in candidates):
            raise ContractError("AI advisory candidates have an invalid type.")
        keys = tuple(candidate.candidate_key for candidate in candidates)
        if len(keys) != len(set(keys)):
            raise ContractError("AI advisory candidate keys must be unique.")
        object.__setattr__(
            self,
            "candidates",
            tuple(sorted(candidates, key=lambda candidate: candidate.candidate_key)),
        )
        selected = str(self.locally_selected_candidate_key or "")
        if selected and selected not in set(keys):
            raise ContractError("The local selection is absent from the AI shortlist.")
        object.__setattr__(self, "locally_selected_candidate_key", selected)


@dataclass(frozen=True, slots=True)
class AdvisoryOutcome:
    """Structured evidence only; this object carries no write authorization."""

    review_id: str
    disposition: AdvisoryDisposition
    evidence: AIReviewEvidence
    suggested_candidate_key: str = ""
    requires_review: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, AdvisoryDisposition):
            object.__setattr__(
                self, "disposition", AdvisoryDisposition(str(self.disposition))
            )
        if self.suggested_candidate_key and _OPAQUE_KEY_RE.fullmatch(
            self.suggested_candidate_key
        ) is None:
            raise ContractError("An AI outcome contains a non-opaque candidate key.")
        if type(self.requires_review) is not bool:
            raise ContractError("requires_review must be exactly boolean.")

    @property
    def reason_code(self) -> str:
        return self.disposition.value


@dataclass(frozen=True, slots=True)
class AdvisoryBatchResult:
    """Ordered advisory outcomes and aggregate, non-content usage counters."""

    outcomes: tuple[AdvisoryOutcome, ...]
    cache_hits: int = 0
    prompt_tokens: int = 0
    candidate_tokens: int = 0
    total_tokens: int = 0
    batches_attempted: int = 0
    batches_succeeded: int = 0


class AdvisoryCacheError(RuntimeError):
    """The optional replay cache could not be used safely."""


def _cache_entries_checkpoint(
    connection: sqlite3.Connection,
) -> tuple[int, str]:
    """Hash the complete ordered cache so missing or changed rows fail closed."""

    rows = connection.execute(
        """
        SELECT request_sha256, model, prompt_version, decision,
               candidate_key, confidence, error_code
        FROM advisory_cache
        ORDER BY request_sha256
        """
    ).fetchall()
    document = {
        "schema": _CACHE_CHECKPOINT_SCHEMA,
        "rows": [[str(value or "") for value in row] for row in rows],
    }
    return len(rows), sha256_bytes(canonical_json_bytes(document))


def _write_cache_checkpoint(connection: sqlite3.Connection) -> None:
    count, digest = _cache_entries_checkpoint(connection)
    connection.executemany(
        """
        INSERT INTO cache_meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (("entry_count", str(count)), ("entries_sha256", digest)),
    )


def _verify_cache_checkpoint(connection: sqlite3.Connection) -> None:
    rows = dict(
        connection.execute(
            """
            SELECT key, value FROM cache_meta
            WHERE key IN ('entry_count', 'entries_sha256')
            """
        ).fetchall()
    )
    if set(rows) != {"entry_count", "entries_sha256"}:
        raise AdvisoryCacheError("The AI replay cache checkpoint is missing.")
    try:
        expected_count = int(rows["entry_count"])
    except (TypeError, ValueError) as exc:
        raise AdvisoryCacheError("The AI replay cache checkpoint is invalid.") from exc
    expected_digest = str(rows["entries_sha256"] or "")
    actual_count, actual_digest = _cache_entries_checkpoint(connection)
    if (
        expected_count < 0
        or _SHA256_RE.fullmatch(expected_digest) is None
        or expected_count != actual_count
        or expected_digest != actual_digest
    ):
        raise AdvisoryCacheError("The AI replay cache checkpoint does not match.")


def _bounded_text(value: object, *, maximum: int) -> str:
    """Normalize bounded context before both hashing and transport."""

    text = safe_text(value, maximum=maximum * 8)
    normalized: list[str] = []
    for character in unicodedata.normalize("NFKC", text):
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            normalized.append(" ")
        else:
            normalized.append(character)
    return " ".join("".join(normalized).split())[:maximum]


def _redact_known_epg_ids(
    value: object,
    epg_ids: Iterable[str],
    *,
    maximum: int,
    fallback: str,
) -> str:
    """Remove every literal shortlisted EPG ID before hashing or transport."""

    text = _bounded_text(value, maximum=maximum)
    for epg_id in sorted(
        {str(item or "") for item in epg_ids if str(item or "")},
        key=lambda item: (-len(item), item.casefold(), item),
    ):
        text = re.sub(re.escape(epg_id), "[epg-id]", text, flags=re.IGNORECASE)
    return _bounded_text(text, maximum=maximum) or fallback


def _validate_model(model: object) -> str:
    value = str(model or "")
    if _MODEL_RE.fullmatch(value) is None:
        raise ContractError("The Gemini model name is invalid.")
    return value


def validate_model_name(model: object) -> str:
    """Validate and return a model identifier without making a network call."""

    return _validate_model(model)


def canonical_request_payload(
    request: AdvisoryRequest, *, model: str = DEFAULT_MODEL
) -> dict[str, object]:
    """Return the stable private request document used only for local hashing."""

    selected_model = _validate_model(model)
    return {
        "schema": AI_REQUEST_SCHEMA,
        "prompt_version": PROMPT_VERSION,
        "model": selected_model,
        # Bind cache replay to the local row/proposal without placing that
        # identity in the Gemini payload or the SQLite cache.
        "review_binding_sha256": sha256_bytes(request.review_id.encode("utf-8")),
        "channel_name": request.channel_name,
        "category_name": request.category_name,
        "market": request.market,
        "locally_selected_candidate_key": request.locally_selected_candidate_key,
        "candidates": [
            {
                "candidate_key": candidate.candidate_key,
                "display_name": candidate.display_name,
                "region": candidate.region,
                "feed": candidate.feed,
            }
            for candidate in request.candidates
        ],
    }


def request_sha256(request: AdvisoryRequest, *, model: str = DEFAULT_MODEL) -> str:
    """Hash the complete normalized request, model, and prompt contract."""

    return sha256_bytes(canonical_json_bytes(canonical_request_payload(request, model=model)))


def request_from_proposal(proposal: ProposalRecord) -> AdvisoryRequest:
    """Create an advisory request without copying any candidate EPG ID."""

    if not isinstance(proposal, ProposalRecord):
        raise TypeError("proposal must be a ProposalRecord.")
    compatible = tuple(candidate for candidate in proposal.candidates if not candidate.conflicts)
    epg_ids = tuple(candidate.epg_id for candidate in compatible)
    return AdvisoryRequest(
        review_id=proposal.proposal_id,
        channel_name=_redact_known_epg_ids(
            proposal.channel_name,
            epg_ids,
            maximum=180,
            fallback="Provider channel",
        ),
        category_name=_redact_known_epg_ids(
            proposal.category_name,
            epg_ids,
            maximum=120,
            fallback="",
        ),
        market=proposal.market,
        candidates=tuple(
            AdvisoryCandidate(
                candidate_key=candidate.candidate_key,
                display_name=_redact_known_epg_ids(
                    candidate.display_name,
                    epg_ids,
                    maximum=180,
                    fallback=f"Candidate {candidate.candidate_key}",
                ),
                region=candidate.region,
                feed=candidate.feed,
            )
            for candidate in compatible
        ),
        locally_selected_candidate_key=proposal.selected_candidate_key,
    )


class _ReplayCache:
    """Small validated SQLite cache containing hashes and enums, never prose."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.connection: sqlite3.Connection | None = None
        self.namespace_sha256 = ""

    def __enter__(self) -> "_ReplayCache":
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
                raise AdvisoryCacheError("The AI replay cache is not a regular file.")
            connection = sqlite3.connect(str(self.path), timeout=10.0)
            self.connection = connection
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                # The cache contains only digests, opaque keys, and enums.  An
                # unsupported chmod is not a reason to risk a second AI call.
                pass
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, _CACHE_SCHEMA_VERSION}:
                raise AdvisoryCacheError("The AI replay cache schema is unsupported.")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS advisory_cache (
                    request_sha256 TEXT PRIMARY KEY NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    candidate_key TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    error_code TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_meta (
                    key TEXT PRIMARY KEY NOT NULL,
                    value TEXT NOT NULL
                ) WITHOUT ROWID
                """
            )
            namespace_row = connection.execute(
                "SELECT value FROM cache_meta WHERE key = 'namespace_token'"
            ).fetchone()
            if namespace_row is None:
                existing_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM advisory_cache"
                    ).fetchone()[0]
                )
                if existing_count:
                    raise AdvisoryCacheError(
                        "The AI replay cache has rows but no namespace."
                    )
                connection.execute(
                    """
                    INSERT INTO cache_meta(key, value)
                    VALUES ('namespace_token', ?)
                    """,
                    (secrets.token_hex(32),),
                )
                _write_cache_checkpoint(connection)
                namespace_row = connection.execute(
                    "SELECT value FROM cache_meta WHERE key = 'namespace_token'"
                ).fetchone()
            else:
                _verify_cache_checkpoint(connection)
            namespace_token = str(namespace_row[0] if namespace_row else "")
            if _SHA256_RE.fullmatch(namespace_token) is None:
                raise AdvisoryCacheError(
                    "The AI replay cache namespace is missing or invalid."
                )
            self.namespace_sha256 = sha256_bytes(
                f"matching-lab-ai-cache-v1:{namespace_token}".encode("ascii")
            )
            connection.execute(f"PRAGMA user_version = {_CACHE_SCHEMA_VERSION}")
            connection.commit()
            return self
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise AdvisoryCacheError("The AI replay cache could not be opened.") from exc
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def get(
        self,
        digest: str,
        *,
        model: str,
        candidate_keys: frozenset[str],
    ) -> AIReviewEvidence | None:
        if self.connection is None:
            raise AdvisoryCacheError("The AI replay cache is closed.")
        try:
            row = self.connection.execute(
                """
                SELECT model, prompt_version, decision, candidate_key,
                       confidence, error_code
                FROM advisory_cache WHERE request_sha256 = ?
                """,
                (digest,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise AdvisoryCacheError("The AI replay cache could not be read.") from exc
        if row is None:
            return None
        cached_model, prompt_version, decision, candidate_key, confidence, error_code = (
            str(value or "") for value in row
        )
        valid_decisions = {
            gemini_review.ReviewDecision.SUGGEST.value,
            gemini_review.ReviewDecision.ABSTAIN.value,
            gemini_review.ReviewDecision.ERROR.value,
        }
        valid_confidence = {
            value.value for value in gemini_review.ReviewConfidence
        }
        invalid = (
            cached_model != model
            or prompt_version != PROMPT_VERSION
            or decision not in valid_decisions
            or confidence not in valid_confidence
            or (decision == "SUGGEST" and candidate_key not in candidate_keys)
            or (
                decision == "SUGGEST"
                and (not candidate_key or confidence == "NONE" or bool(error_code))
            )
            or (
                decision == "ABSTAIN"
                and (candidate_key or confidence != "NONE" or bool(error_code))
            )
            or (
                decision == "ERROR"
                and (candidate_key or confidence != "NONE" or not error_code)
            )
        )
        if invalid:
            raise AdvisoryCacheError("The AI replay cache contains an invalid record.")
        return AIReviewEvidence(
            request_sha256=digest,
            model=model,
            prompt_version=PROMPT_VERSION,
            decision=decision,
            candidate_key=candidate_key,
            confidence=confidence,
            error_code=error_code,
            cached=True,
        )

    def put(self, evidence: AIReviewEvidence) -> None:
        if self.connection is None:
            raise AdvisoryCacheError("The AI replay cache is closed.")
        if evidence.decision not in {"SUGGEST", "ABSTAIN", "ERROR"}:
            return
        try:
            self.connection.execute(
                """
                INSERT INTO advisory_cache (
                    request_sha256, model, prompt_version, decision,
                    candidate_key, confidence, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_sha256) DO NOTHING
                """,
                (
                    evidence.request_sha256,
                    evidence.model,
                    evidence.prompt_version,
                    evidence.decision,
                    evidence.candidate_key,
                    evidence.confidence,
                    evidence.error_code,
                ),
            )
            stored = self.connection.execute(
                """
                SELECT model, prompt_version, decision, candidate_key,
                       confidence, error_code
                FROM advisory_cache WHERE request_sha256 = ?
                """,
                (evidence.request_sha256,),
            ).fetchone()
            expected = (
                evidence.model,
                evidence.prompt_version,
                evidence.decision,
                evidence.candidate_key,
                evidence.confidence,
                evidence.error_code,
            )
            if stored is None or tuple(str(value or "") for value in stored) != expected:
                raise AdvisoryCacheError(
                    "The AI replay cache contains conflicting evidence for one request."
                )
            _write_cache_checkpoint(self.connection)
            self.connection.commit()
        except AdvisoryCacheError:
            self.connection.rollback()
            raise
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise AdvisoryCacheError("The AI replay cache could not be written.") from exc


def replay_cache_namespace_sha256(path: Path) -> str:
    """Return the durable cache namespace used to distinguish explicit retries."""

    with _ReplayCache(Path(path)) as cache:
        if _SHA256_RE.fullmatch(cache.namespace_sha256) is None:
            raise AdvisoryCacheError("The AI replay cache namespace is invalid.")
        return cache.namespace_sha256


def _outcome(
    request: AdvisoryRequest,
    evidence: AIReviewEvidence,
) -> AdvisoryOutcome:
    suggested = evidence.candidate_key if evidence.decision == "SUGGEST" else ""
    if evidence.decision == "ERROR":
        disposition = AdvisoryDisposition.ERROR
        requires_review = True
    elif evidence.decision == "ABSTAIN":
        disposition = AdvisoryDisposition.ABSTAINED
        requires_review = True
    elif evidence.confidence != "HIGH":
        disposition = AdvisoryDisposition.LOW_CONFIDENCE
        requires_review = True
    elif not request.locally_selected_candidate_key:
        disposition = AdvisoryDisposition.DISAGREES
        requires_review = True
    elif suggested != request.locally_selected_candidate_key:
        disposition = AdvisoryDisposition.DISAGREES
        requires_review = True
    else:
        disposition = AdvisoryDisposition.SUPPORTS_LOCAL
        requires_review = False
    return AdvisoryOutcome(
        review_id=request.review_id,
        disposition=disposition,
        evidence=evidence,
        suggested_candidate_key=suggested,
        requires_review=requires_review,
    )


def _wire_request(request: AdvisoryRequest, digest: str) -> Any:
    """Map to the hardened client, substituting opaque keys for EPG IDs."""

    return gemini_review.ReviewRequest(
        review_id=digest,
        channel_name=request.channel_name,
        category=request.category_name,
        market=request.market,
        candidates=tuple(
            gemini_review.ReviewCandidate(
                candidate_key=candidate.candidate_key,
                # The legacy transport schema requires this field.  Repeating
                # the opaque key preserves the schema without disclosing the
                # real EPG ID held in the proposal.
                epg_id=candidate.candidate_key,
                display_name=candidate.display_name,
                region=candidate.region,
                feed=candidate.feed,
            )
            for candidate in request.candidates
        ),
    )


def review_advisories(
    requests: Sequence[AdvisoryRequest],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    cache_path: Path | None = None,
    timeout_seconds: float = gemini_review.DEFAULT_TIMEOUT_SECONDS,
    transport: Any | None = None,
    sensitive_values: Sequence[str] = (),
) -> AdvisoryBatchResult:
    """Return bounded AI evidence, using validated replay records when present.

    Suggestions, abstentions, and fixed-code errors are cached.  This prevents
    an unchanged replay from spending money or producing a different bundle;
    an operator must explicitly remove the private cache to retry an errored
    request.  The caller receives no generated prose and no field that grants
    apply authority.
    """

    items = tuple(requests)
    if len(items) > MAX_ADVISORY_REQUESTS:
        raise ContractError(
            f"At most {MAX_ADVISORY_REQUESTS} AI advisory requests are allowed per run."
        )
    if any(not isinstance(item, AdvisoryRequest) for item in items):
        raise TypeError("Every AI advisory item must be an AdvisoryRequest.")
    review_ids = tuple(item.review_id for item in items)
    if len(review_ids) != len(set(review_ids)):
        raise ContractError("AI advisory review_id values must be unique per run.")
    selected_model = _validate_model(model)
    if not items:
        return AdvisoryBatchResult(outcomes=())

    digests = tuple(request_sha256(item, model=selected_model) for item in items)
    by_digest = dict(zip(digests, items, strict=True))
    if len(by_digest) != len(items):
        raise ContractError("AI advisory requests produced a duplicate request hash.")

    cache_manager: _ReplayCache | None = (
        _ReplayCache(Path(cache_path)) if cache_path is not None else None
    )
    cache = cache_manager.__enter__() if cache_manager is not None else None
    evidence_by_digest: dict[str, AIReviewEvidence] = {}
    cache_hits = 0
    prompt_tokens = 0
    candidate_tokens = 0
    total_tokens = 0
    batches_attempted = 0
    batches_succeeded = 0
    try:
        if cache is not None:
            for item, digest in zip(items, digests, strict=True):
                evidence = cache.get(
                    digest,
                    model=selected_model,
                    candidate_keys=frozenset(
                        candidate.candidate_key for candidate in item.candidates
                    ),
                )
                if evidence is not None:
                    evidence_by_digest[digest] = evidence
                    cache_hits += 1

        missing = tuple(
            (digest, by_digest[digest])
            for digest in digests
            if digest not in evidence_by_digest
        )
        for start in range(0, len(missing), MAX_CALL_SIZE):
            chunk = missing[start : start + MAX_CALL_SIZE]
            result = gemini_review.review_flagged_channels(
                tuple(_wire_request(item, digest) for digest, item in chunk),
                api_key=api_key,
                model=selected_model,
                timeout_seconds=timeout_seconds,
                transport=transport,
                sensitive_values=sensitive_values,
            )
            prompt_tokens += result.prompt_tokens
            candidate_tokens += result.candidate_tokens
            total_tokens += result.total_tokens
            batches_attempted += result.batches_attempted
            batches_succeeded += result.batches_succeeded
            if len(result.results) != len(chunk):
                raise ContractError("The hardened AI client returned an invalid result count.")
            for raw in result.results:
                digest = str(raw.review_id or "")
                item = by_digest.get(digest)
                if item is None or _SHA256_RE.fullmatch(digest) is None:
                    raise ContractError("The hardened AI client returned an unknown request.")
                candidate_key = str(raw.candidate_key or "")
                allowed_keys = {candidate.candidate_key for candidate in item.candidates}
                if candidate_key and candidate_key not in allowed_keys:
                    # The underlying client also enforces this; repeat the
                    # boundary here so future transport changes fail closed.
                    decision = "ERROR"
                    candidate_key = ""
                    confidence = "NONE"
                    error_code = "OUT_OF_SET_CANDIDATE"
                else:
                    decision = raw.decision.value
                    confidence = raw.confidence.value
                    error_code = str(raw.error_code or "")
                evidence = AIReviewEvidence(
                    request_sha256=digest,
                    model=selected_model,
                    prompt_version=PROMPT_VERSION,
                    decision=decision,
                    candidate_key=candidate_key,
                    confidence=confidence,
                    error_code=error_code,
                    cached=False,
                )
                evidence_by_digest[digest] = evidence
                if cache is not None:
                    cache.put(evidence)
    finally:
        if cache_manager is not None:
            cache_manager.__exit__(None, None, None)

    outcomes = tuple(
        _outcome(item, evidence_by_digest[digest])
        for item, digest in zip(items, digests, strict=True)
    )
    return AdvisoryBatchResult(
        outcomes=outcomes,
        cache_hits=cache_hits,
        prompt_tokens=prompt_tokens,
        candidate_tokens=candidate_tokens,
        total_tokens=total_tokens,
        batches_attempted=batches_attempted,
        batches_succeeded=batches_succeeded,
    )


def attach_advisory(
    proposal: ProposalRecord,
    outcome: AdvisoryOutcome,
) -> ProposalRecord:
    """Attach evidence without changing the selected candidate or promoting state.

    A disagreement, abstention, low-confidence answer, or error demotes an
    ``AUTO_ELIGIBLE`` proposal to ``NEEDS_REVIEW`` and clears any apply-lane
    flag.  Support leaves the deterministic state untouched.  In every case,
    the selected candidate key is preserved exactly.
    """

    if outcome.review_id != proposal.proposal_id:
        raise ContractError("AI evidence is not bound to this proposal.")
    reasons = set(proposal.reason_codes)
    reasons.add(outcome.reason_code)
    state = proposal.state
    # Advisory evidence never carries an apply capability, even if a caller
    # supplied an independently constructed AUTO_ELIGIBLE record.
    auto_apply_eligible = False
    if outcome.requires_review:
        if state is DecisionState.AUTO_ELIGIBLE:
            state = DecisionState.NEEDS_REVIEW
            reasons.difference_update(
                {
                    "LANE_CONSERVATIVE_STRICT_EXACT",
                    "LANE_STANDARD_STRONG",
                    "LANE_TRUSTED_ALIAS",
                }
            )
            reasons.add("LANE_HUMAN_REVIEW")
        auto_apply_eligible = False
    normalized_evidence = replace(outcome.evidence, cached=False)
    draft = replace(
        proposal,
        proposal_id="0" * 64,
        state=state,
        reason_codes=tuple(sorted(reasons)),
        auto_apply_eligible=auto_apply_eligible,
        ai=normalized_evidence,
    )
    result = replace(draft, proposal_id=draft.computed_id())
    result.verify_id()
    if result.selected_candidate_key != proposal.selected_candidate_key:
        raise ContractError("AI review attempted to replace the local selection.")
    return result


def requests_from_proposals(
    proposals: Iterable[ProposalRecord],
) -> tuple[AdvisoryRequest, ...]:
    """Build requests only for proposals with a valid 2-8 candidate shortlist."""

    requests: list[AdvisoryRequest] = []
    for proposal in proposals:
        compatible = sum(1 for candidate in proposal.candidates if not candidate.conflicts)
        if (
            proposal.state in {DecisionState.AUTO_ELIGIBLE, DecisionState.NEEDS_REVIEW}
            and proposal.selected_candidate_key
            and 2 <= compatible <= 8
        ):
            requests.append(request_from_proposal(proposal))
    return tuple(requests)


__all__ = (
    "AI_REQUEST_SCHEMA",
    "DEFAULT_MODEL",
    "MAX_ADVISORY_REQUESTS",
    "PROMPT_VERSION",
    "AdvisoryBatchResult",
    "AdvisoryCacheError",
    "AdvisoryCandidate",
    "AdvisoryDisposition",
    "AdvisoryOutcome",
    "AdvisoryRequest",
    "attach_advisory",
    "canonical_request_payload",
    "request_from_proposal",
    "request_sha256",
    "requests_from_proposals",
    "replay_cache_namespace_sha256",
    "review_advisories",
    "validate_model_name",
)
