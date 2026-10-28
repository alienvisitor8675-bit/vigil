"""Who produced an artifact: a Claude model, a local model, or a human.

Every stored artifact and DB row carries one. The training exporter takes
`local` only, so no Claude output can enter a training set (design,
"Training-data firewall").

`eval-only` marks every row of a bench job (owner, 2026-10-27): whatever
produced it, it exists to measure vigil and never to train a model. The
store applies it (`Store.row_provenance`), and the exporter refuses it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from enum import StrEnum
from typing import Any


class Provenance(StrEnum):
    CLAUDE = "claude"
    LOCAL = "local"
    HUMAN = "human"
    EVAL_ONLY = "eval-only"


class ProvenanceError(ValueError):
    pass


def require(value: object) -> Provenance:
    """Parse a provenance tag; anything missing or unknown is an error."""
    try:
        return Provenance(str(value))
    except ValueError as e:
        raise ProvenanceError(f"missing or unknown provenance: {value!r}") from e


def check_exportable(row: Mapping[str, Any]) -> None:
    """Raise unless the row may enter a training export."""
    prov = require(row.get("provenance"))
    if prov is not Provenance.LOCAL:
        raise ProvenanceError(f"refusing to export a {prov.value}-tagged artifact")


def export_training_rows(rows: Iterable[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    """The training exporter stub: yields only `local` rows, and refuses the
    whole export if any row is Claude's, so a filter bug can't leak one."""
    rows = list(rows)
    for row in rows:
        prov = require(row.get("provenance"))
        if prov is Provenance.CLAUDE:
            raise ProvenanceError("export contains a claude-tagged artifact; refused")
        if prov is Provenance.EVAL_ONLY:
            raise ProvenanceError("export contains an eval-only (bench) row; refused")
    for row in rows:
        if row["provenance"] == Provenance.LOCAL:
            yield row
