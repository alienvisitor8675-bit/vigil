"""Replan with an owner note (owner spec, 2026-10-25).

`POST /api/inbox/{id}/replan` takes {"note": "..."} (8 KB at most), stored verbatim
as a human-provenance artifact. The coord is handed the previously approved spec
and the note, as an owner section of its packet, and returns an amended spec and a
changelog; it does not redraft from the request. The spec item shows a diff against
the previous spec. The hidden-test author gets the amended spec and the note's
author-contract lines. The coord and the author are scripted: no model runs."""

# ruff: noqa: F811  (fixtures imported from the modules that define them)

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.jobs.test_coord_loop import (  # noqa: F401
    SPEC,
    World,
    _approve,
    _failure_item,
    _flaky,
    _named,
    _request,
    _runner_world,
    _spec_item,
    auth,
    plan,
    world,
)
from vigild.jobs.model import Role
from vigild.jobs.ownernote import (
    NOTE_MAX_BYTES,
    author_contract,
    spec_diff,
)
from vigild.packets.fence import strip_fenced

NOTE_FILE = Path(__file__).parent / "fixtures" / "owner-note-pa0002-w2.md"
ITEM5 = (
    "5. Hidden-test author contract for this task:\n"
    " • AC3 and AC6 are tested through the broker as a subprocess: argv, a fixture dir "
    "readable by uid 10002, and exit code plus stdout and stderr returned.\n"
    " • AC1, AC2, AC4 and AC5 go through the call driver with an explicit root.\n"
    " • Before replanning, confirm the broker supports argv and a 10002-readable fixture "
    "dir. If it doesn't, tell me the size of that change before doing anything else."
)
NOTE = "Add a non-goal: the CLI is untouched. Keep every other criterion as it is."
CHANGELOG = "- Added a non-goal: the CLI is untouched.\n- Everything else is as approved.\n"
AMENDED = SPEC.replace("- Changing the limit.\n", "- Changing the limit.\n- Touching the CLI.\n")


def _arm(
    w: World,
    spec: str = AMENDED,
    changelog: str | None = CHANGELOG,
    named: bool = False,
) -> None:
    """The next coord session returns an amended spec and a changelog."""

    def draft(j: str) -> tuple[str, str]:
        if named:
            sp, pl = _named(j)
            return sp.replace(
                "- Changing the limit.\n", "- Changing the limit.\n- Touching the CLI.\n"
            ), pl
        return spec, plan(j)

    def extra(clone: Path, j: str) -> None:
        if changelog is not None:
            (clone / ".vigil" / j / "spec_changelog.md").write_text(changelog)

    w.standin.coord_drafts = [draft]
    w.standin.coord_extra = extra


def _capture(w: World, role: Role = Role.COORD) -> list[str]:
    got: list[str] = []
    w.standin.on_session = lambda spec: got.append(spec.packet) if spec.role is role else None
    return got


def _replan(w: World, item: dict[str, Any], note: Any = NOTE) -> Any:
    return w.client.post(
        f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.admin), json={"note": note}
    )


def _held(w: World) -> tuple[str, dict[str, Any]]:
    """A job whose worker failed: its page offers replan."""
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    return job, _failure_item(w, job)


def _art(w: World, job: str, name: str) -> Any:
    return w.store.get_artifact(job, name)


# --- the pure parts ---------------------------------------------------------------------------


def test_the_author_contract_is_exactly_the_lines_under_the_author_item() -> None:
    assert author_contract(NOTE_FILE.read_text()) == ITEM5


def test_the_author_contract_follows_a_heading_to_the_next_heading_of_its_level() -> None:
    note = (
        "# Replan\n\nintro line\n\n## Interface\n\n- keep the flag\n\n"
        "## Hidden-test author contract\n\n- test AC3 through the broker\n\n"
        "### Detail\n\n- pass the root\n\n## Rollback\n\n- revert\n"
    )
    got = author_contract(note)
    assert got == (
        "## Hidden-test author contract\n\n- test AC3 through the broker\n\n"
        "### Detail\n\n- pass the root"
    )


def test_the_author_contract_takes_every_matching_section_and_ignores_case() -> None:
    note = "1. First: one\n2. AUTHOR notes:\n   - a\n3. Other\n   - b\n4. For the Author\n   - c\n"
    assert author_contract(note) == "2. AUTHOR notes:\n   - a\n\n4. For the Author\n   - c"


def test_without_an_author_section_the_author_gets_the_whole_note() -> None:
    note = "1. Interface: add --root\n 2 is not an item\n2. AC3: rewrite\n"
    assert author_contract(note) == note.strip()


def test_a_body_line_that_only_mentions_the_author_is_not_a_title() -> None:
    note = "1. Interface\n - the author reads this? no, it is a bullet\n2. Other\n"
    assert author_contract(note) == note.strip()  # no title has it: the fallback


def test_the_diff_is_unified_and_ignores_the_provenance_header() -> None:
    old = "<!-- provenance: human -->\n# Spec\n\n## Goal\nA\n\n## Rollback\nRevert.\n"
    new = "<!-- provenance: claude -->\n# Spec\n\n## Goal\nA\nB\n\n## Rollback\nRevert.\n"
    d = spec_diff(old, new)
    assert d.startswith("--- spec.md (previously approved)\n+++ spec.md (amended)\n")
    assert "\n+B\n" in d and "provenance" not in d
    assert spec_diff(old, old.replace("human", "claude")) == ""


# --- the endpoint -----------------------------------------------------------------------------


def test_the_note_is_stored_verbatim_with_human_provenance_and_an_event(world: World) -> None:
    w = world
    job, item = _held(w)
    _arm(w)
    note = "Keep it exact:\n  - spaces and a trailing newline stay\n"
    r = _replan(w, item, note)
    assert r.status_code == 200, r.text
    art = _art(w, job, "owner_note.md")
    assert art.content == note and art.provenance == "human"
    ev = [json.loads(e["data"]) for e in w.store.q(
        "SELECT data FROM events WHERE job_id=? AND type='replan'", (job,)
    )]  # fmt: skip
    assert len(ev) == 1 and ev[0]["note_bytes"] == len(note.encode())
    assert ev[0]["note_sha256"] == art.sha256 and note not in json.dumps(ev[0])


def test_a_note_over_8kb_is_refused_with_413_and_nothing_changes(world: World) -> None:
    w = world
    job, item = _held(w)
    calls = len(w.standin.calls)
    for big in ("x" * (NOTE_MAX_BYTES + 1), "é" * (NOTE_MAX_BYTES // 2 + 1)):
        r = _replan(w, item, big)
        assert r.status_code == 413, r.text
    assert _art(w, job, "owner_note.md") is None
    assert w.store.get_job(job).state.value == "blocked"
    assert len(w.standin.calls) == calls
    assert _failure_item(w, job)["id"] == item["id"]  # still open
    _arm(w)
    assert _replan(w, item, "y" * NOTE_MAX_BYTES).status_code == 200  # exactly 8 KB is fine


def test_a_note_that_is_not_text_is_refused_with_422(world: World) -> None:
    w = world
    job, item = _held(w)
    for bad in (5, "   ", ["a"], {"x": 1}):
        r = w.client.post(
            f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.admin), json={"note": bad}
        )
        assert r.status_code == 422, (bad, r.text)
    r = w.client.post(
        f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.admin), json=["a note"]
    )
    assert r.status_code == 422
    assert _art(w, job, "owner_note.md") is None and w.store.get_job(job).state.value == "blocked"


def test_the_note_needs_the_admin_token(world: World) -> None:
    w = world
    job, item = _held(w)
    r = w.client.post(
        f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.read), json={"note": NOTE}
    )
    assert r.status_code in (401, 403)
    assert _art(w, job, "owner_note.md") is None


def test_a_replan_without_a_note_is_unchanged(world: World) -> None:
    w = world
    job, item = _held(w)
    packets = _capture(w)
    r = w.client.post(f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert _art(w, job, "owner_note.md") is None and _art(w, job, "spec_previous.md") is None
    assert "# 3a." not in packets[0] and "This is an amendment" not in packets[0]
    spec_item = _spec_item(w)
    assert "amendment" not in spec_item["body"]
    assert "outside scope" in packets[0]  # the failures still reach the coord


# --- the coord amends --------------------------------------------------------------------------


def test_the_coord_gets_the_approved_spec_and_the_note_as_an_owner_section(world: World) -> None:
    w = world
    job, item = _held(w)
    approved = _art(w, job, "spec.md").content
    packets = _capture(w)
    _arm(w)
    assert _replan(w, item).status_code == 200
    (packet,) = packets
    # an owner section, trusted: it survives the removal of every fenced block
    acted_on = strip_fenced(packet)
    assert "# 3a. Owner's amendment" in acted_on
    assert NOTE in acted_on and "<<<UNTRUSTED" not in packet.split("# 3a.")[1].split("# 4.")[0]
    # the previously approved spec, whole, in that section
    section = acted_on.split("# 3a.")[1].split("# 4.")[0]
    assert approved.split("\n", 1)[1].strip() in section
    # amend, not redraft
    assert "amend" in section.lower() and "do not redraft" in section.lower()
    assert "spec_changelog.md" in packet.split("# 5.")[1]
    assert (Path(__file__).parents[2] / "prompts" / "coord.md").read_text().count("amend") >= 2


def test_the_amended_spec_changelog_and_diff_are_stored_and_the_item_shows_them(
    world: World,
) -> None:
    w = world
    job, item = _held(w)
    approved = _art(w, job, "spec.md")
    _arm(w)
    assert _replan(w, item).status_code == 200
    assert w.store.get_job(job).state.value == "spec_pending"
    spec = _art(w, job, "spec.md")
    assert "- Touching the CLI." in spec.content and spec.provenance == "claude"
    prev = _art(w, job, "spec_previous.md")
    assert prev.content == approved.content and prev.provenance == approved.provenance
    assert _art(w, job, "spec_changelog.md").content == CHANGELOG
    assert _art(w, job, "spec_changelog.md").provenance == "claude"
    diff = _art(w, job, "spec_diff.md").content
    assert "\n+- Touching the CLI.\n" in diff and "\n-" not in diff.split("@@", 1)[1]
    body = _spec_item(w)["body"]
    assert body["amendment"] == {
        "changelog": CHANGELOG, "diff": diff, "note_bytes": len(NOTE.encode()),
    }  # fmt: skip
    # plan.md is re-planned from the amended spec, and the gate ran on the amended spec
    assert _art(w, job, "plan.md").provenance == "claude"
    assert json.loads(_art(w, job, "spec_gate.json").content)["ok"] is True


def test_the_spec_gate_runs_on_the_amended_spec_and_a_changelog_is_required(world: World) -> None:
    w = world
    _, item = _held(w)
    packets = _capture(w)
    broken = AMENDED.replace("## Rollback\nRevert the PRs.\n", "")
    seq = iter([(broken, CHANGELOG), (AMENDED, None), (AMENDED, CHANGELOG)])
    pending: list[tuple[str, str | None]] = []

    def draft(j: str) -> tuple[str, str]:
        pending.append(next(seq))
        return pending[-1][0], plan(j)

    def extra(clone: Path, j: str) -> None:
        if pending[-1][1] is not None:
            (clone / ".vigil" / j / "spec_changelog.md").write_text(pending[-1][1])  # type: ignore[arg-type]

    w.standin.coord_drafts = [draft, draft, draft]
    w.standin.coord_extra = extra
    assert _replan(w, item).status_code == 200
    assert len(packets) == 3  # a refused amendment is revised, up to three drafts
    assert "Rollback" in packets[1] and "spec_changelog.md" in packets[2]
    assert "spec_changelog.md" in packets[2].split("refused your last draft")[1]
    assert _spec_item(w)["body"]["amendment"]["changelog"] == CHANGELOG


# --- the spec approval item offers it too ------------------------------------------------------


def test_the_spec_item_offers_replan_with_a_note(world: World) -> None:
    w = world
    job = _request(w)
    assert _spec_item(w)["body"]["actions"] == ["approve", "replan"]
    assert job


def test_replan_with_a_note_from_the_spec_item_amends_the_draft_and_is_no_approval(
    world: World,
) -> None:
    w = world
    job = _request(w)
    item = _spec_item(w)
    drafted = _art(w, job, "spec.md")
    packets = _capture(w)
    _arm(w)
    r = _replan(w, item)
    assert r.status_code == 200, r.text
    assert r.json() == {"job_id": job, "state": "spec_pending", "run": "started"}
    # the old item is closed and a new one is filed; it is not an approval
    assert w.store.get_inbox_item(item["id"]).status == "resolved"
    new = _spec_item(w)
    assert new["id"] != item["id"] and "- Touching the CLI." in _art(w, job, "spec.md").content
    assert w.store.q("SELECT id FROM approvals WHERE job_id=?", (job,)) == []
    assert not w.store.q("SELECT id FROM events WHERE job_id=? AND type='approval'", (job,))
    assert w.store.get_job(job).state.value == "spec_pending"
    assert _art(w, job, "spec_previous.md").content == drafted.content
    assert new["body"]["amendment"]["changelog"] == CHANGELOG
    assert drafted.content.split("\n", 1)[1].strip() in packets[0]
    (ev,) = w.store.q("SELECT data FROM events WHERE job_id=? AND type='replan'", (job,))
    assert json.loads(ev["data"])["inbox_item_id"] == item["id"]


def test_the_spec_item_replan_needs_a_note(world: World) -> None:
    w = world
    job = _request(w)
    item = _spec_item(w)
    r = w.client.post(f"/api/inbox/{item['id']}/replan", headers=auth(w.tokens.admin))
    assert r.status_code == 422, r.text
    assert w.store.get_inbox_item(item["id"]).status == "open"
    assert _art(w, job, "owner_note.md") is None


def test_a_later_plain_replan_does_not_reuse_the_note(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    job = _request(w)
    _arm(w)
    assert _replan(w, _spec_item(w)).status_code == 200
    w.client.post(
        f"/api/inbox/{_spec_item(w)['id']}/approve", headers=auth(w.tokens.admin), json={}
    )
    page = _failure_item(w, job)
    packets = _capture(w)
    w.standin.coord_drafts = []
    r = w.client.post(f"/api/inbox/{page['id']}/replan", headers=auth(w.tokens.admin))
    assert r.status_code == 200, r.text
    assert "# 3a." not in packets[0]
    assert "amendment" not in _spec_item(w)["body"]  # the old diff is not this spec's


# --- the author ---------------------------------------------------------------------------------


def _author_packets(w: World) -> list[str]:
    return [p for role, p in w.standin.packets if role == "hidden_test_author"]


def test_the_author_gets_the_amended_spec_and_only_the_notes_author_lines(world: World) -> None:
    w = world
    _runner_world(w)
    job = _request(w)
    assert len(_author_packets(w)) == 1 and "Touching the CLI" not in _author_packets(w)[0]
    _arm(w, named=True)
    note = NOTE_FILE.read_text()
    assert _replan(w, _spec_item(w), note).status_code == 200
    first, second = _author_packets(w)
    assert "Owner's contract" not in first
    # the amended spec, not the old one
    assert "- Touching the CLI." in second.split("# 3.")[0]
    # exactly item 5's lines, under the author-contract heading, in the contract part
    contract = second.split("# 3.")[1].split("# 4.")[0]
    assert "The owner's contract for these tests" in contract and ITEM5 in contract
    # and nothing else of the note
    for other in (
        "AC3 (rewrite)",
        "Replan pa-0002 item 6",
        "Base: ParkAgent main",
        "At spec approval",
    ):
        assert other not in second, other
    assert _spec_item(w)["body"]["amendment"]["note_bytes"] == len(note.encode())
    assert job


def test_without_an_author_section_the_author_gets_the_whole_note(world: World) -> None:
    w = world
    _runner_world(w)
    _request(w)
    _arm(w, named=True)
    assert _replan(w, _spec_item(w), NOTE).status_code == 200
    second = _author_packets(w)[1]
    assert NOTE in second.split("# 3.")[1].split("# 4.")[0]


def test_the_author_prompt_says_to_use_make_fixture_for_every_fixture() -> None:
    text = (Path(__file__).parents[2] / "prompts" / "hidden_test_author.md").read_text()
    assert "make_fixture" in text and "makeFixture" in text
    assert "every fixture" in text and "mkdtemp" in text  # and why not a temp dir of your own


def test_an_author_run_without_a_note_has_no_owner_contract(world: World) -> None:
    w = world
    _runner_world(w)
    _request(w)
    (only,) = _author_packets(w)
    assert "Owner's contract" not in only and "owner's contract" not in only


# --- round 2 -----------------------------------------------------------------------------------


def test_notes_accumulate_and_the_baseline_is_the_approved_spec(world: World) -> None:
    w = world
    _runner_world(w)
    w.standin.worker_edit = _flaky({"w1"})
    job = _approve(w)
    approved = _art(w, job, "spec.md").content
    n1 = "## Author contract\n- first author line\n"
    n2 = "## Author contract\n- second author line\n"
    _arm(w, named=True)
    assert _replan(w, _failure_item(w, job), n1).status_code == 200
    assert _art(w, job, "spec.md").content != approved  # draft 1 is not approved
    packets = _capture(w)
    _arm(w, named=True)
    assert _replan(w, _spec_item(w), n2).status_code == 200  # from draft 1's spec item
    # the baseline is still the approved spec, not draft 1
    assert _art(w, job, "spec_previous.md").content == approved
    diff = _spec_item(w)["body"]["amendment"]["diff"]
    assert diff.startswith("--- spec.md (previously approved)\n")
    # both notes, in order, reach the coord
    (packet,) = packets
    assert packet.index(n1.strip()) < packet.index(n2.strip())
    # and the author
    last = _author_packets(w)[-1]
    assert last.index("first author line") < last.index("second author line")


def test_with_no_approval_the_baseline_is_labelled_the_current_draft(world: World) -> None:
    w = world
    job = _request(w)
    packets = _capture(w)
    _arm(w)
    assert _replan(w, _spec_item(w)).status_code == 200
    (packet,) = packets
    assert "## The current draft (never approved)" in packet
    assert "## The previously approved spec" not in packet
    diff = _spec_item(w)["body"]["amendment"]["diff"]
    assert diff.startswith("--- spec.md (current draft, never approved)\n")
    assert job


def test_an_approval_after_a_noted_replan_closes_its_notes(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    job = _request(w)
    _arm(w)
    assert _replan(w, _spec_item(w), "old note: only this once").status_code == 200
    w.client.post(
        f"/api/inbox/{_spec_item(w)['id']}/approve", headers=auth(w.tokens.admin), json={}
    )
    packets = _capture(w)
    r = _replan(w, _failure_item(w, job), "new note")
    assert r.status_code == 200
    assert "new note" in packets[0] and "old note" not in packets[0]


def test_author_contract_does_not_match_authorization_or_authority() -> None:
    note = "1. OAuth authorization\n - secret\n2. Authority and authored text\n - more\n"
    assert author_contract(note) == note.strip()  # no title matches: the whole note
    both = note + "3. Author contract\n - y\n"
    assert author_contract(both) == "3. Author contract\n - y"


def test_plain_unindented_lines_under_an_author_item_are_its_body() -> None:
    note = "1. Interface\n - x\n5. Hidden-test author contract:\nuse the helper\nread the root\n"
    assert author_contract(note) == "5. Hidden-test author contract:\nuse the helper\nread the root"


def test_a_blank_line_does_not_end_an_author_items_body() -> None:
    note = "5. Author contract:\nline a\n\nline b\n6. Next\n - z\n"
    assert author_contract(note) == "5. Author contract:\nline a\n\nline b"
    heading = "## Author contract\n\nline a\n\nline b\n\n## Next\nz\n"
    assert author_contract(heading) == "## Author contract\n\nline a\n\nline b"


def test_an_approval_with_no_earlier_spec_is_labelled_pre_versioning(world: World) -> None:
    w = world
    job, item = _held(w)
    approval = w.store.get_artifact(job, "approval.json")
    w.store.x(
        "DELETE FROM artifacts WHERE job_id=? AND name='spec.md' AND id < ?", (job, approval.id)
    )
    w.store.add_artifact(
        job, "spec.md", SPEC, "claude"
    )  # the only spec on file, after the approval
    packets = _capture(w)
    _arm(w)
    assert _replan(w, item).status_code == 200
    (ev,) = w.store.q("SELECT data FROM events WHERE job_id=? AND type='replan'", (job,))
    assert json.loads(ev["data"])["baseline"] == "approved-pre-versioning"
    assert "## The approved spec (pre-versioning" in packets[0]
    diff = _spec_item(w)["body"]["amendment"]["diff"]
    assert diff.startswith("--- spec.md (approved, pre-versioning)\n")


def test_an_empty_author_section_falls_back_to_the_whole_note() -> None:
    note = "1. Interface\n - x\n2. Author contract:\n3. Other\n - y\n"
    assert author_contract(note) == note.strip()


def test_a_replan_note_amends_a_human_written_spec(world: World) -> None:
    w = world
    w.standin.worker_edit = _flaky({"w1"})
    r = w.client.post(
        "/api/tasks",
        headers=auth(w.tokens.admin),
        json={
            "repo": "o/fixture", "why": "w", "value": 3, "risk": 1,
            "spec": {"goal": "document the rate limit", "test_cmd": "pytest -q tests/test_feature_map.py",
                     "done_when": "d",
                     "files_in_scope": ["docs/feature-map.md", "tests/test_feature_map.py"]},
        },
    )  # fmt: skip
    assert r.status_code in (200, 202), r.text
    job = r.json()["job_id"]
    human = _art(w, job, "spec.md")
    assert human.provenance == "human"
    w.client.post(
        f"/api/inbox/{_spec_item(w)['id']}/approve", headers=auth(w.tokens.admin), json={}
    )
    _arm(w)
    assert _replan(w, _failure_item(w, job)).status_code == 200
    spec = _art(w, job, "spec.md")
    assert "- Touching the CLI." in spec.content and spec.provenance == "claude"
    prev = _art(w, job, "spec_previous.md")
    assert prev.content == human.content and prev.provenance == "human"
    assert _art(w, job, "spec_diff.md").content.strip()
    assert _spec_item(w)["body"]["amendment"]["diff"]


def test_a_note_with_a_lone_surrogate_is_422_not_500(world: World) -> None:
    w = world
    _, item = _held(w)
    r = w.client.post(
        f"/api/inbox/{item['id']}/replan",
        headers={**auth(w.tokens.admin), "Content-Type": "application/json"},
        content='{"note": "bad \\ud800 text"}',
    )
    assert r.status_code == 422, r.text
    assert w.store.get_job(item["job_id"]).state.value == "blocked"


def test_a_replan_while_the_runner_holds_the_job_is_refused_before_the_item_resolves(
    world: World,
) -> None:
    w = world
    job = _request(w)
    item = _spec_item(w)
    with w.runner._running_lock:
        w.runner._running.add(job)
    r = _replan(w, item)
    assert r.status_code == 409, r.text
    assert w.store.get_inbox_item(item["id"]).status == "open"
    assert _art(w, job, "owner_note.md") is None
    with w.runner._running_lock:
        w.runner._running.discard(job)
    _arm(w)
    assert _replan(w, item).status_code == 200


def test_the_coord_prompt_says_scope_paths_have_no_trailing_slash() -> None:
    # pa-0003: the first draft failed the spec gate on `server/src/` and `docs/`
    from pathlib import Path

    text = (Path(__file__).parents[2] / "prompts" / "coord.md").read_text()
    assert "no trailing slash" in text and "out_of_scope" in text
