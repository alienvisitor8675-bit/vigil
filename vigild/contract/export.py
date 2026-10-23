"""Write schemas/*.json from the contract models.

Usage: uv run python -m vigild.contract.export
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from vigild.contract.models import Audit, Event, Plan, Result, Review, Task, WorkerReport

MODELS: dict[str, type[BaseModel]] = {
    "task.schema.json": Task,
    "result.schema.json": Result,
    "event.schema.json": Event,
    "worker_report.schema.json": WorkerReport,
    "plan.schema.json": Plan,
    "review.schema.json": Review,
    "audit.schema.json": Audit,
}


def schemas() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, model in MODELS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        out[name] = schema
    return out


def main() -> None:
    root = Path(__file__).resolve().parents[2] / "schemas"
    root.mkdir(exist_ok=True)
    for name, schema in schemas().items():
        (root / name).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        print(f"wrote schemas/{name}")


if __name__ == "__main__":
    main()
