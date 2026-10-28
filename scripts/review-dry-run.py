"""Rerun the reviewer on a past task with no effects.

  uv run python scripts/review-dry-run.py --data-dir ~/vigil-data --job pa-0003 \
      --task pa-0003-w1 --head <sha> --out ~/review-dry/pa-0003-w1.json --repo-visibility public

The data dir is only read (database `mode=ro`, vault and mirror through
gitsafe). The run happens in a scratch data dir with its own gateway; it needs
port 8788, so stop vigild first. See the runbook, "Review dry run".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vigild.reviewdry import DryRunError, dry_run, render


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--data-dir", type=Path, required=True, help="vigild's data dir (read only)")
    p.add_argument("--job", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--head", help="commit to review (default: the latest pr_open event's head)")
    p.add_argument("--out", type=Path, required=True, help="the verdict and findings, as JSON")
    p.add_argument("--scratch", type=Path, help="scratch data dir (default: a new temp dir)")
    p.add_argument(
        "--repo-visibility", choices=("public", "private"), default="private",
        help="the repo's visibility, since GitHub isn't asked; private (the default) refuses "
        "any hosted backend the repo's policy doesn't allow",
    )  # fmt: skip
    a = p.parse_args(argv)
    try:
        result = dry_run(
            data_dir=a.data_dir, job_id=a.job, task_id=a.task, head=a.head, out=a.out,
            scratch=a.scratch, private=a.repo_visibility == "private",
        )  # fmt: skip
    except DryRunError as e:
        print(f"review-dry-run: {e}", file=sys.stderr)
        return 2
    print(render(result))
    print(f"written: {a.out}")
    return 0 if result["error"] is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
