"""Private, replayable SKY TV EPG matching laboratory.

The package's CLI deliberately exposes no Google Sheets write operation.  It
produces content-addressed proposals which a separately guarded production
writer may validate in a later phase.
"""

from .models import (
    AIReviewEvidence,
    CandidateEvidence,
    DecisionState,
    ProgrammeState,
    ProposalRecord,
    ProtectedSemantics,
    RunManifest,
)

LAB_VERSION = "2.1.0-shadow"
PROPOSAL_SCHEMA = "skytv.smart-match-proposal.v2"
RUN_MANIFEST_SCHEMA = "skytv.smart-match-manifest.v1"

__all__ = (
    "AIReviewEvidence",
    "CandidateEvidence",
    "DecisionState",
    "LAB_VERSION",
    "PROPOSAL_SCHEMA",
    "ProgrammeState",
    "ProposalRecord",
    "ProtectedSemantics",
    "RUN_MANIFEST_SCHEMA",
    "RunManifest",
)
