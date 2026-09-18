"""Load the repository's existing top-level script modules exactly once.

The production repository predates Python packages and imports modules such as
``build_epg_streaming`` by their top-level names.  Adding both ``scripts.foo``
and ``foo`` to one process can create two copies of the same dataclasses, so the
lab follows the production import convention instead of changing sealed code.
"""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPOSITORY_ROOT / "scripts"
SRC_DIR = REPOSITORY_ROOT / "src"

for _path in (SCRIPT_DIR, SRC_DIR):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

import ai_review_gemini as gemini_review  # noqa: E402
import auto_match_inventory as automatch  # noqa: E402
import build_epg_streaming as streaming  # noqa: E402
import epg_catalog_stream as catalog_stream  # noqa: E402
import sync_channel_inventory as sync  # noqa: E402
from skytv_epg_contextual_v8 import (  # noqa: E402
    parse_candidate_context_v8,
    parse_channel_context_v8,
)


__all__ = (
    "REPOSITORY_ROOT",
    "SCRIPT_DIR",
    "SRC_DIR",
    "automatch",
    "catalog_stream",
    "gemini_review",
    "parse_candidate_context_v8",
    "parse_channel_context_v8",
    "streaming",
    "sync",
)
