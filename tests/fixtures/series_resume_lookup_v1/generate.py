"""Regenerate synthetic shared wire fixtures; writes only beside this file.

Standard library only. This is fixture preparation, not a production publisher
or an alternative implementation to import into application code.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE / "artifacts"
SERVER = "server_1"
NOW = "2026-11-13T12:00:00Z"
CHECKED = "2026-11-13T00:00:00Z"
ALGORITHM = "sha256-domain-nul-first-byte-v1"
SLOTS = [f"{number:02x}" for number in range(256)]
PARENT_FIELDS = (
    "id", "providerSeriesId", "contentId", "title", "category", "adult",
    "genre", "genres", "language", "languages", "originalLanguage",
    "isMultilingual", "languageListComplete", "languageSource", "episodesStatus",
    "availableEpisodeCount", "serverEpisodeCount", "episodeInventoryComplete",
    "episodeInventoryCheckedAt",
)
EPISODE_FIELDS = (
    "seasonNumber", "episodeNumber", "containerExtension", "title", "durationSeconds",
)
LANGUAGES = [
    "ar", "bn", "cs", "da", "de", "el", "en", "es", "fa", "fi", "fr", "gu",
    "he", "hi", "hu", "id", "it", "ja", "km", "kn", "ko", "lo", "ml", "mr",
    "ms", "mul", "ne", "nl", "no", "pa", "pl", "pt", "ro", "ru", "si", "sk",
    "so", "sv", "sw", "ta", "te", "th", "tl", "tr", "uk", "und", "ur", "vi", "zh",
]
PAYLOADS: dict[str, dict] = {}


def raw_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def gzip_bytes(raw):
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0,
                       compresslevel=9) as handle:
        handle.write(raw)
    return buffer.getvalue()


def document(name, value):
    (HERE / name).write_bytes(raw_json(value) + b"\n")


def artifact(prefix, payload, count, *, compressed=True):
    raw = raw_json(payload)
    encoded = gzip_bytes(raw) if compressed else raw
    digest = hashlib.sha256(encoded).hexdigest()
    name = f"{prefix}.{digest[:16]}.json" + (".gz" if compressed else "")
    (ARTIFACTS / name).write_bytes(encoded)
    PAYLOADS[name] = copy.deepcopy(payload)
    return {"file": name, "sha256": digest, "compressedBytes": len(encoded),
            "uncompressedBytes": len(raw), "recordCount": count}


def encoded_artifact(prefix, raw, count, *, declared_expanded=None):
    encoded = gzip_bytes(raw)
    digest = hashlib.sha256(encoded).hexdigest()
    name = f"{prefix}.{digest[:16]}.json.gz"
    (ARTIFACTS / name).write_bytes(encoded)
    return {"file": name, "sha256": digest, "compressedBytes": len(encoded),
            "uncompressedBytes": len(raw) if declared_expanded is None else declared_expanded,
            "recordCount": count}


def hash_vector(kind, server, series, episode=None):
    parts = [f"skytv-resume-{kind}-v1", server, series]
    if kind == "episode":
        parts.append(episode)
    data = "\0".join(parts).encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    result = {"kind": kind, "serverId": server, "providerSeriesId": series,
              "utf8Hex": data.hex(), "sha256": digest, "bucket": digest[:2]}
    if kind == "episode":
        result["providerEpisodeId"] = episode
    return result


def bucket(kind, series, episode=None):
    return hash_vector(kind, SERVER, series, episode)["bucket"]


def episode(identifier, number=1, *, extension="mkv", duration=1800, title=None):
    return {"id": identifier, "seasonNumber": 1, "episodeNumber": number,
            "title": title or f"Episode {number}", "containerExtension": extension,
            "addedAt": CHECKED, "airDate": None, "overview": None,
            "durationSeconds": duration, "stillUrl": None}


def parent(identifier, episodes=(), *, status="available", complete=True,
           checked=CHECKED, adult=False, languages=None, category=None):
    languages = languages or ["en"]
    language = "mul" if len(languages) > 1 else languages[0]
    unknown = language == "und"
    genre = "adult" if adult is True else "Drama"
    return {
        "id": identifier, "providerSeriesId": identifier,
        "contentId": f"{SERVER}:series:{identifier}", "title": f"Synthetic {identifier}",
        "category": category or {"id": "50", "name": "English Series"},
        "adult": adult, "genre": genre, "genres": [genre], "language": language,
        "languages": languages, "originalLanguage": None if unknown else "en",
        "isMultilingual": len(languages) > 1, "languageListComplete": not unknown,
        "languageSource": "unknown" if unknown else "provider_audio",
        "episodesStatus": status, "availableEpisodeCount": len(episodes),
        "serverEpisodeCount": len(episodes) if checked is not None else None,
        "episodeInventoryComplete": complete, "episodeInventoryCheckedAt": checked,
        "episodes": list(episodes), "episodeFiles": [], "originalTitle": None,
        "overview": None, "releaseDate": None, "tmdbId": None,
        "artwork": {"posterUrl": None, "backdropUrl": None}, "rating": None,
        "voteCount": None, "popularity": None, "cast": [], "directors": [],
        "trailers": [], "seasonCount": 1, "episodeCount": None,
        "status": None, "nextEpisode": None, "firstSeenAt": CHECKED,
        "addedAt": None, "providerLastModified": None, "lastAirDate": None,
        "latestEpisodeAddedAt": CHECKED if episodes else None,
        "metadataMatch": "unavailable", "originalPlatforms": [],
        "airedEpisodeCount": None,
    }


def parent_projection(item):
    return {"section": "series", **{key: copy.deepcopy(item[key]) for key in PARENT_FIELDS}}


def episode_projection(series_id, item):
    return {"providerSeriesId": series_id, "providerEpisodeId": item["id"],
            **{key: copy.deepcopy(item[key]) for key in EPISODE_FIELDS}}


def make_buckets(kind, rows):
    table = {}
    for slot in SLOTS:
        selected = [row for row in rows if bucket(
            "series" if kind == "series" else "episode", row["providerSeriesId"],
            row.get("providerEpisodeId")) == slot]
        if not selected:
            table[slot] = {"empty": True, "recordCount": 0}
            continue
        selected.sort(key=(lambda row: row["providerSeriesId"]) if kind == "series"
                      else (lambda row: (row["providerSeriesId"], row["providerEpisodeId"])))
        payload = {"schemaVersion": 1, "dataset": f"vod-resume-{kind}",
                   "serverId": SERVER, "bucket": slot,
                   "recordCount": len(selected), "items": selected}
        table[slot] = artifact(f"{SERVER}_resume_{kind}_{slot}", payload, len(selected))
    return table


def schema():
    identifier = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$"}
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    timestamp = {"type": ["string", "null"],
                 "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$",
                 "format": "date-time"}
    count = {"type": "integer", "minimum": 0, "maximum": 50000}
    nullable_count = {**count, "type": ["integer", "null"]}
    category = {"type": "object", "additionalProperties": False,
                "required": ["id", "name"], "properties": {
                    "id": {**identifier, "type": ["string", "null"]},
                    "name": {"type": ["string", "null"], "minLength": 1, "maxLength": 160}}}
    parent_properties = {
        "section": {"const": "series"}, "id": identifier, "providerSeriesId": identifier,
        "contentId": {"type": "string", "minLength": 1, "maxLength": 141},
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
        "category": category, "adult": {"type": ["boolean", "null"]},
        "genre": {"type": "string", "minLength": 1},
        "genres": {"type": "array", "maxItems": 12, "uniqueItems": True,
                   "items": {"type": "string", "minLength": 1}},
        "language": {"enum": LANGUAGES},
        "languages": {"type": "array", "minItems": 1, "maxItems": len(LANGUAGES),
                      "uniqueItems": True, "items": {"enum": LANGUAGES}},
        "originalLanguage": {"type": ["string", "null"], "pattern": "^[a-z]{2,3}$"},
        "isMultilingual": {"type": "boolean"}, "languageListComplete": {"type": "boolean"},
        "languageSource": {"enum": ["provider_audio", "title_suffix", "provider_multilingual",
                                    "verified_original", "unknown"]},
        "episodesStatus": {"enum": ["available", "stale", "pending", "unavailable"]},
        "availableEpisodeCount": count, "serverEpisodeCount": nullable_count,
        "episodeInventoryComplete": {"type": "boolean"}, "episodeInventoryCheckedAt": timestamp,
    }
    episode_properties = {
        "providerSeriesId": identifier, "providerEpisodeId": identifier,
        "seasonNumber": {"type": "integer", "minimum": 0, "maximum": 100000},
        "episodeNumber": {"type": "integer", "minimum": 0, "maximum": 100000},
        "containerExtension": {"type": ["string", "null"], "pattern": "^[a-z0-9]{1,8}$"},
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
        "durationSeconds": {"type": ["integer", "null"], "minimum": 0, "maximum": 604800},
    }
    def exact(properties):
        return {"type": "object", "additionalProperties": False,
                "required": list(properties), "properties": properties}
    def reference(maximum):
        return exact({"file": {"type": "string", "pattern": "^[a-z0-9_.-]+\\.[0-9a-f]{16}\\.json\\.gz$"},
                      "sha256": sha, "compressedBytes": {"type": "integer", "minimum": 1, "maximum": 65536},
                      "uncompressedBytes": {"type": "integer", "minimum": 1, "maximum": 262144},
                      "recordCount": {"type": "integer", "minimum": 1, "maximum": maximum}})
    empty = exact({"empty": {"const": True}, "recordCount": {"const": 0}})
    def table(reference_name):
        return {"type": "object", "minProperties": 256, "maxProperties": 256,
                "patternProperties": {"^[0-9a-f]{2}$": {
                    "oneOf": [{"$ref": "#/$defs/empty"}, {"$ref": f"#/$defs/{reference_name}"}]}},
                "additionalProperties": False}
    def child(kind, maximum, row_name):
        return exact({"schemaVersion": {"const": 1}, "dataset": {"const": f"vod-resume-{kind}"},
                      "serverId": {"type": "string", "pattern": "^server_([1-9][0-9]?)$"},
                      "bucket": {"type": "string", "pattern": "^[0-9a-f]{2}$"},
                      "recordCount": {"type": "integer", "minimum": 1, "maximum": maximum},
                      "items": {"type": "array", "minItems": 1, "maxItems": maximum,
                                "items": {"$ref": f"#/$defs/{row_name}"}}})
    root = exact({"schemaVersion": {"const": 1}, "dataset": {"const": "vod-resume-index"},
                  "serverId": {"type": "string", "pattern": "^server_([1-9][0-9]?)$"},
                  "seriesManifestSha256": sha, "hashAlgorithm": {"const": ALGORITHM},
                  "seriesRecords": {"type": "integer", "minimum": 0, "maximum": 524288},
                  "episodeRecords": {"type": "integer", "minimum": 0, "maximum": 2097152},
                  "seriesBuckets": table("seriesReference"),
                  "episodeBuckets": table("episodeReference")})
    result = {"$schema": "https://json-schema.org/draft/2020-12/schema",
              "$id": "https://fixture.invalid/series-resume-lookup-v1/schema.json",
              "title": "SkyTV series resume lookup v1",
              "description": "Structure only; graph, identity, ordering, coverage and parity checks are additionally mandatory.",
              "oneOf": [{"$ref": "#/$defs/root"}, {"$ref": "#/$defs/seriesBucket"},
                        {"$ref": "#/$defs/episodeBucket"}],
              "$defs": {"root": root, "empty": empty,
                        "seriesReference": reference(2048), "episodeReference": reference(8192),
                        "rootReference": reference(512), "parent": exact(parent_properties),
                        "episode": exact(episode_properties),
                        "seriesBucket": child("series", 2048, "parent"),
                        "episodeBucket": child("episodes", 8192, "episode")}}
    result["$defs"]["rootReference"]["properties"]["recordCount"] = {"const": 512}
    return result


def main():
    ARTIFACTS.mkdir(exist_ok=True)
    parents = [
        parent("00123", [episode("9001")]), parent("123", [episode("9001")]),
        parent("ShowA", [episode("Ep-01"), episode("E.p:02", 2, duration=None)],
               languages=["hi", "pa"]),
        parent("showa", [episode("Ep-01", extension="mp4")]),
        parent("1", [episode("23")]), parent("12", [episode("3")]),
        parent("zero"), parent("pending", status="pending", complete=False, checked=None),
        parent("stale", [episode("cache-1")], status="stale", checked="2026-11-01T00:00:00Z"),
        parent("unknown-adult", [episode("safe-unknown")], adult=None,
               languages=["und"], category={"id": None, "name": None}),
        parent("partial", [episode("present")], complete=False),
        parent("partial-no-time", [episode("present")], complete=False, checked=None),
        parent("unavailable", status="unavailable", complete=False, checked=None),
        parent("complete-empty-unavailable", status="unavailable"),
        parent("no-route", [episode("no-extension", extension=None)]),
        parent("adult-true", [episode("blocked")], adult=True),
    ]
    # For a partial source, serverEpisodeCount is unknown; retained observed
    # count is separately and truthfully availableEpisodeCount.
    next(row for row in parents if row["id"] == "partial")["serverEpisodeCount"] = None
    source_rows = copy.deepcopy(parents)
    external_parent = next(row for row in source_rows if row["id"] == "1")
    external = {"schemaVersion": 1, "dataset": "vod-episodes", "serverId": SERVER,
                "providerSeriesId": "1", "generatedAt": NOW, "recordCount": 1,
                "items": copy.deepcopy(external_parent["episodes"])}
    external_ref = artifact(f"{SERVER}_episodes_external_000000", external, 1)
    external_parent["episodes"] = []
    external_parent["episodeFiles"] = [external_ref]
    source_rows.sort(key=lambda row: (row["category"]["id"] or "", row["id"]))
    source_shards = []
    for start in range(0, len(source_rows), 8):
        selected = source_rows[start:start + 8]
        ref = artifact(f"{SERVER}_series_{start:06d}", {
            "schemaVersion": 1, "dataset": "vod-series", "serverId": SERVER,
            "generatedAt": NOW, "recordCount": len(selected), "items": selected}, len(selected))
        ref["providerCategoryIds"] = sorted({row["category"]["id"] for row in selected if row["category"]["id"]})
        ref["languages"] = sorted({language for row in selected for language in row["languages"]})
        source_shards.append(ref)
    manifest = {"schemaVersion": 1, "seriesMetadataVersion": 2, "serverId": SERVER,
                "dataset": "vod-series", "generatedAt": NOW, "totalRecords": len(parents),
                "categories": [{"id": "50", "name": "English Series"}], "shards": source_shards}
    manifest_ref = artifact(f"{SERVER}_series_manifest", manifest, len(source_shards), compressed=False)
    parent_rows = [parent_projection(row) for row in parents]
    episode_rows = [episode_projection(row["id"], ep) for row in parents for ep in row["episodes"]]
    root = {"schemaVersion": 1, "dataset": "vod-resume-index", "serverId": SERVER,
            "seriesManifestSha256": manifest_ref["sha256"], "hashAlgorithm": ALGORITHM,
            "seriesRecords": len(parent_rows), "episodeRecords": len(episode_rows),
            "seriesBuckets": make_buckets("series", parent_rows),
            "episodeBuckets": make_buckets("episodes", episode_rows)}
    root_ref = artifact(f"{SERVER}_resume_index", root, 512)
    descriptor = {"acceptedSnapshotGeneratedAt": NOW,
                  "artifactDirectory": "artifacts", "serverId": SERVER,
                  "series": manifest_ref, "seriesResumeLookup": root_ref}
    document("accepted_descriptor.json", descriptor)
    document("source_records.json", {"serverId": SERVER, "generatedAt": NOW, "items": parents})
    document("schema.json", schema())
    for name, definition in (("root", "root"), ("parent", "parent"),
                             ("episode", "episode")):
        document(f"{name}.schema.json", {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$ref": f"schema.json#/$defs/{definition}"})
    vectors = [hash_vector(kind, server, sid, eid)
               for kind, server, sid, eid in [
                   ("series", SERVER, "00123", None), ("series", SERVER, "123", None),
                   ("series", SERVER, "ShowA", None), ("series", SERVER, "showa", None),
                   ("series", "server_2", "00123", None),
                   ("series", SERVER, "A._:-0", None),
                   ("episode", SERVER, "00123", "9001"),
                   ("episode", SERVER, "123", "9001"),
                   ("episode", SERVER, "1", "23"), ("episode", SERVER, "12", "3"),
                   ("episode", SERVER, "ShowA", "Ep-01"),
                   ("episode", SERVER, "showa", "Ep-01"),
                   ("episode", "server_2", "00123", "9001"),
                   ("episode", SERVER, "A._:-0", "E._:-01")]]
    document("hash_vectors.json", {"hashAlgorithm": ALGORITHM, "vectors": vectors,
        "invalidInputs": [
            {"providerSeriesId": "", "expected": "RejectInvalidIdentity"},
            {"providerSeriesId": " 123", "expected": "RejectInvalidIdentity"},
            {"providerSeriesId": "123 ", "expected": "RejectInvalidIdentity"},
            {"providerSeriesId": "A/1", "expected": "RejectInvalidIdentity"},
            {"providerSeriesId": "A\u0000B", "expected": "RejectInvalidIdentity"},
            {"providerSeriesId": "ਪੰਜਾਬੀ", "expected": "RejectInvalidIdentity"},
            {"providerSeriesId": "A" * 121, "expected": "RejectInvalidIdentity"},
        ]})

    def changed_publication(name, final_parents):
        """An additional complete source graph: no production code is used."""
        source = copy.deepcopy(final_parents)
        externalized = next(row for row in source if row["id"] == "1")
        externalized["episodes"] = []
        externalized["episodeFiles"] = [external_ref]
        source.sort(key=lambda row: (row["category"]["id"] or "", row["id"]))
        refs = []
        for start in range(0, len(source), 8):
            selected = source[start:start + 8]
            ref = artifact(f"{SERVER}_series_{name}_{start:06d}", {
                "schemaVersion": 1, "dataset": "vod-series", "serverId": SERVER,
                "generatedAt": NOW, "recordCount": len(selected), "items": selected}, len(selected))
            ref["providerCategoryIds"] = sorted({row["category"]["id"] for row in selected if row["category"]["id"]})
            ref["languages"] = sorted({language for row in selected for language in row["languages"]})
            refs.append(ref)
        categories = {row["category"]["id"]: row["category"] for row in source if row["category"]["id"]}
        changed_manifest = {"schemaVersion": 1, "seriesMetadataVersion": 2, "serverId": SERVER,
            "dataset": "vod-series", "generatedAt": NOW, "totalRecords": len(source),
            "categories": [categories[key] for key in sorted(categories)], "shards": refs}
        changed_series_ref = artifact(f"{SERVER}_series_manifest_{name}", changed_manifest,
                                      len(refs), compressed=False)
        prows = [parent_projection(row) for row in final_parents]
        erows = [episode_projection(row["id"], ep) for row in final_parents for ep in row["episodes"]]
        changed_root = {**copy.deepcopy(root), "seriesManifestSha256": changed_series_ref["sha256"],
            "seriesRecords": len(prows), "episodeRecords": len(erows),
            "seriesBuckets": make_buckets("series", prows),
            "episodeBuckets": make_buckets("episodes", erows)}
        changed_ref = artifact(f"{SERVER}_resume_index", changed_root, 512)
        return {**descriptor, "series": changed_series_ref, "seriesResumeLookup": changed_ref}

    moved_parents = copy.deepcopy(parents)
    next(row for row in moved_parents if row["id"] == "00123")["category"] = {
        "id": "99", "name": "Current Moved Category"}
    moved_descriptor = changed_publication("moved", moved_parents)
    removed_descriptor = changed_publication("removed", [row for row in parents if row["id"] != "00123"])
    document("move_removal_cases.json", {"before": descriptor, "cases": [
        {"name": "category_and_ordinal_move", "after": moved_descriptor,
         "providerSeriesId": "00123", "providerEpisodeId": "9001",
         "expectedInventoryResult": "PositiveCurrentMembership", "currentCategoryId": "99",
         "note": "Resolve by exact current identity; no old-category or old-shard shortcut"},
        {"name": "exact_parent_removed", "after": removed_descriptor,
         "providerSeriesId": "00123", "providerEpisodeId": "9001",
         "expectedInventoryResult": "ParentAbsent", "distinctSurvivingSeriesId": "123",
         "note": "Leading-zero sibling remains a different identity; preserve durable progress"},
    ]})

    # Valid graph change with identical ordinary series manifest: filename
    # aliases change the lookup root SHA while retaining child bytes/checksums.
    alias_root = copy.deepcopy(root)
    chosen_slot = bucket("series", "00123")
    original_ref = alias_root["seriesBuckets"][chosen_slot]
    alias_root["seriesBuckets"][chosen_slot] = artifact(
        f"{SERVER}_resume_series_alias_{chosen_slot}", PAYLOADS[original_ref["file"]],
        original_ref["recordCount"])
    alias_ref = artifact(f"{SERVER}_resume_index_alias", alias_root, 512)
    document("root_only_change.json", {"seriesManifestSha256": manifest_ref["sha256"],
             "before": root_ref, "after": alias_ref,
             "expected": "Retire old evidence even though ordinary series SHA and child facts are unchanged"})
    document("unsupported_descriptors.json", [
        {"name": "absent", "descriptor": {key: value for key, value in descriptor.items()
                                          if key != "seriesResumeLookup"},
         "expected": "UnsupportedContract"},
        {"name": "removed", "before": descriptor,
         "after": {key: value for key, value in descriptor.items() if key != "seriesResumeLookup"},
         "expected": "UnsupportedContract; retire former evidence"},
    ])

    negatives = []
    def negative(name, change, layer):
        candidate = copy.deepcopy(root)
        change(candidate)
        ref = artifact(f"{SERVER}_resume_index_bad_{name}", candidate, 512)
        negatives.append({"name": name, "seriesResumeLookup": ref,
                          "expected": "Unknown", "rejectionLayer": layer})
    def change_child(candidate, kind, slot, name, change):
        table = candidate["seriesBuckets" if kind == "series" else "episodeBuckets"]
        old = table[slot]
        child = copy.deepcopy(PAYLOADS[old["file"]])
        change(child)
        table[slot] = artifact(f"{SERVER}_resume_{kind}_bad_{name}_{slot}", child,
                               child["recordCount"])

    ps = bucket("series", "00123")
    es = bucket("episode", "00123", "9001")
    negative("missing_slot", lambda r: r["episodeBuckets"].pop("ff"), "root schema")
    negative("extra_slot", lambda r: r["episodeBuckets"].update({"100": {"empty": True, "recordCount": 0}}), "root schema")
    negative("wrong_manifest", lambda r: r.update(seriesManifestSha256="b" * 64), "accepted manifest binding")
    negative("wrong_server", lambda r: r.update(serverId="server_2"), "accepted server binding")
    negative("extra_root_field", lambda r: r.update(recordCount=512), "root schema")
    negative("null_slot", lambda r: r["seriesBuckets"].update({"ff": None}), "root schema")
    negative("bad_child_sha", lambda r: r["seriesBuckets"][ps].update(sha256="0" * 64), "artifact checksum")
    negative("bad_child_length", lambda r: r["seriesBuckets"][ps].update(
        compressedBytes=r["seriesBuckets"][ps]["compressedBytes"] + 1), "artifact length")
    negative("declared_compressed_overflow", lambda r: r["seriesBuckets"][ps].update(
        compressedBytes=65537), "declared artifact cap before allocation")
    negative("declared_expanded_overflow", lambda r: r["seriesBuckets"][ps].update(
        uncompressedBytes=262145), "declared artifact cap before allocation")
    negative("declared_series_row_overflow", lambda r: r["seriesBuckets"][ps].update(
        recordCount=2049), "declared series row cap")
    negative("declared_episode_row_overflow", lambda r: r["episodeBuckets"][es].update(
        recordCount=8193), "declared episode row cap")
    oversized_raw = raw_json(PAYLOADS[root["seriesBuckets"][ps]["file"]])
    oversized_raw += b" " * (262145 - len(oversized_raw))
    oversized_ref = encoded_artifact(f"{SERVER}_resume_series_bad_expansion_{ps}",
                                    oversized_raw, 1, declared_expanded=262144)
    negative("actual_expanded_overflow", lambda r: r["seriesBuckets"].update({ps: oversized_ref}),
             "streaming expansion cap; compressed hash and length are valid")
    original_raw = raw_json(PAYLOADS[root["seriesBuckets"][ps]["file"]])
    duplicate_key_raw = original_raw[:-1] + b',"schemaVersion":1}'
    duplicate_key_ref = encoded_artifact(f"{SERVER}_resume_series_bad_duplicate_key_{ps}",
                                         duplicate_key_raw, 1)
    negative("duplicate_json_key", lambda r: r["seriesBuckets"].update({ps: duplicate_key_ref}),
             "strict JSON duplicate-key rejection")
    negative("missing_child", lambda r: r["seriesBuckets"][ps].update(
        file="missing." + r["seriesBuckets"][ps]["sha256"][:16] + ".json.gz"), "artifact unavailable")
    negative("wrong_bucket", lambda r: change_child(r, "series", ps, "wrong_bucket",
        lambda c: c.update(bucket="ff" if ps != "ff" else "00")), "child identity")
    negative("duplicate_parent", lambda r: change_child(r, "series", ps, "duplicate_parent",
        lambda c: (c["items"].append(copy.deepcopy(c["items"][0])), c.update(recordCount=len(c["items"])))), "uniqueness")
    negative("missing_parent_field", lambda r: change_child(r, "series", ps, "missing_parent_field",
        lambda c: c["items"][0].pop("adult")), "parent schema")
    negative("coerced_id", lambda r: change_child(r, "series", ps, "coerced_id",
        lambda c: c["items"][0].update(providerSeriesId=123)), "exact string identity")
    negative("wrong_content_id", lambda r: change_child(r, "series", ps, "wrong_content_id",
        lambda c: c["items"][0].update(contentId=f"{SERVER}:series:123")), "identity parity")
    negative("projection_mismatch", lambda r: change_child(r, "series", ps, "projection_mismatch",
        lambda c: c["items"][0].update(adult=True)), "full source projection parity")
    negative("future_checked_at", lambda r: change_child(r, "series", ps, "future_checked_at",
        lambda c: c["items"][0].update(episodeInventoryCheckedAt="2026-11-14T00:00:00Z")), "accepted cutoff")
    negative("duplicate_episode", lambda r: change_child(r, "episodes", es, "duplicate_episode",
        lambda c: (c["items"].append(copy.deepcopy(c["items"][0])), c.update(recordCount=len(c["items"])))), "pair uniqueness")
    negative("unsafe_extension", lambda r: change_child(r, "episodes", es, "unsafe_extension",
        lambda c: c["items"][0].update(containerExtension="../mkv")), "episode schema")
    negative("boolean_duration", lambda r: change_child(r, "episodes", es, "boolean_duration",
        lambda c: c["items"][0].update(durationSeconds=True)), "episode integer type")
    negative("wrong_episode_placement", lambda r: change_child(r, "episodes", es, "wrong_episode_placement",
        lambda c: c["items"][0].update(providerSeriesId="123")), "hash placement and coverage")
    negative("lost_parent_coverage", lambda r: r["seriesBuckets"].update({ps: {"empty": True, "recordCount": 0}}), "full source coverage")
    negative("lost_episode_coverage", lambda r: r["episodeBuckets"].update({es: {"empty": True, "recordCount": 0}}), "full source coverage")
    document("negative_cases.json", {"acceptedDescriptor": descriptor, "cases": negatives})
    document("source_failure_cases.json", {"acceptedDescriptor": descriptor, "cases": [
        {"name": "missing_referenced_external_episode", "omitFiles": [external_ref["file"]],
         "expected": "RejectCandidate", "note": "Never manufacture verified-empty membership for missing source input"},
        {"name": "source_checksum_mismatch", "corruptFiles": [manifest_ref["file"]],
         "expected": "RejectCandidate", "note": "Reject before generating any destination"},
    ]})

    matrix = []
    def resolution(name, sid, eid, expected, note):
        matrix.append({"name": name, "providerSeriesId": sid, "providerEpisodeId": eid,
                       "seriesBucket": bucket("series", sid),
                       "episodeBucket": bucket("episode", sid, eid),
                       "expectedInventoryResult": expected, "note": note})
    resolution("available_exact", "00123", "9001", "PositiveCurrentMembership", "Local current guards and playback DTO still required")
    resolution("leading_zero_distinct", "123", "9001", "PositiveCurrentMembership", "Distinct parent from00123")
    resolution("case_preserved_upper", "ShowA", "Ep-01", "PositiveCurrentMembership", "Distinct parent fromshowa; languages hi+pa")
    resolution("case_preserved_lower", "showa", "Ep-01", "PositiveCurrentMembership", "Distinct exact parent")
    resolution("pair_tuple_one", "1", "23", "PositiveCurrentMembership", "External episode file in source graph")
    resolution("pair_tuple_two", "12", "3", "PositiveCurrentMembership", "Same concatenated text123 but different tuple")
    resolution("complete_zero", "zero", "absent", "EpisodeAbsent", "Available, complete, checked empty inventory")
    resolution("complete_member_miss", "00123", "absent", "EpisodeAbsent", "Only from complete current available inventory")
    resolution("partial_positive", "partial", "present", "PositiveCurrentMembership", "Completenessfalse does not invalidate positive checked presence")
    resolution("partial_missing", "partial", "absent", "Unknown", "Partial inventory cannot prove episode absence")
    resolution("partial_null_time", "partial-no-time", "present", "Unknown", "Positive pair without checked time is insufficient")
    resolution("pending", "pending", "absent", "Unknown", "No pending absence proof")
    resolution("stale_positive", "stale", "cache-1", "Unknown", "Retained pair remains indexed but stale cannot authorize new recovery")
    resolution("unavailable", "unavailable", "absent", "Unknown", "Unavailable is not snapshot episodeabsence")
    resolution("complete_empty_unavailable", "complete-empty-unavailable", "absent", "Unknown", "Completeemptyflag does not override unavailable status")
    resolution("unknown_adult", "unknown-adult", "safe-unknown", "PositiveCurrentMembership", "adultnull/languageund/categorynull preserved; this is NOT policy authorization")
    resolution("explicit_adult", "adult-true", "blocked", "PositiveCurrentMembership", "Inventorypositive does not bypass current adult/parental guards")
    resolution("unusable_extension", "no-route", "no-extension", "Unknown", "No usable current playback DTO from null extension")
    resolution("parent_removed", "removed", "9001", "ParentAbsent", "Verified complete parent coverage only; progress retained")
    document("resolution_cases.json", {"acceptedSnapshotGeneratedAt": NOW,
             "seriesManifestSha256": manifest_ref["sha256"], "lookupSha256": root_ref["sha256"],
             "cases": matrix})
    document("generation_summary.json", {"synthetic": True, "seriesRecords": len(parent_rows),
             "episodeRecords": len(episode_rows), "nonemptySeriesBuckets": sum("file" in v for v in root["seriesBuckets"].values()),
             "nonemptyEpisodeBuckets": sum("file" in v for v in root["episodeBuckets"].values()),
             "root": root_ref, "rootOnlyChanged": root_ref["sha256"] != alias_ref["sha256"],
             "negativeCases": len(negatives), "resolutionCases": len(matrix),
             "proposalSha256": "3C3DAC2FFDCBBE2DB63DBD40903941BC18C0C704BC1362219BB67A808D54B13A".lower()})
    # List every checked-in byte artifact except the checksum list itself.
    checksums = []
    for path in sorted(HERE.rglob("*")):
        if path.is_file() and path.name != "checksums.json" and "__pycache__" not in path.parts:
            encoded = path.read_bytes()
            checksums.append({"file": path.relative_to(HERE).as_posix(),
                              "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()})
    document("checksums.json", {"algorithm": "sha256", "files": checksums})
    print(json.dumps({"parents": len(parents), "episodes": len(episode_rows),
                      "root": root_ref, "vectors": len(vectors), "negatives": len(negatives)}, sort_keys=True))


if __name__ == "__main__":
    main()
