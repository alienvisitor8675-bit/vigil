"""The verifier's container, for real (pytest -m docker): no network, a
read-only checkout copied into the container's tmpfs, the agent uid with no
capabilities, output capped, a timeout that kills."""

from __future__ import annotations

from pathlib import Path

import pytest

from vigild.config import Config
from vigild.executors.runtime import DockerTestRunner
from vigild.verifier import outcome

pytestmark = pytest.mark.docker


@pytest.fixture
def runner(cfg: Config, runtime: object) -> DockerTestRunner:  # runtime: images are built
    return DockerTestRunner(cfg, runtime.client)  # type: ignore[attr-defined]


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "verify" / "fx-0001-w1-after" / "src"
    src.mkdir(parents=True)
    (src / "ok.txt").write_text("ok\n")
    return src


def _run(runner: DockerTestRunner, cfg: Config, source: Path, cmd: str, **kw: int):  # type: ignore[no-untyped-def]
    return runner.run(
        cfg.vigild.images.claude_executor,
        source,
        cmd,
        timeout_s=kw.get("timeout_s", 60),
        max_output=kw.get("max_output", 64 << 10),
    )


def test_no_network_read_only_source_agent_uid(
    runner: DockerTestRunner, cfg: Config, source: Path
) -> None:
    r = _run(
        runner,
        cfg,
        source,
        "id -u; grep CapEff /proc/self/status; pwd; cat ok.txt; "
        # errexit is on: a probe that may fail goes in an `if`
        "if touch /src/x 2>/dev/null; then echo SRC=0; else echo SRC=1; fi; "
        "if touch /work/x; then echo WORK=0; else echo WORK=1; fi; "
        "if touch /tmp/x; then echo TMP=0; else echo TMP=1; fi; "
        "if touch /etc/x 2>/dev/null; then echo ROOTFS=0; else echo ROOTFS=1; fi; "
        'python -c "import urllib.request as u\n'
        "try:\n u.urlopen('https://pypi.org/simple/', timeout=5); print('REACHED')\n"
        "except Exception as e:\n print('BLOCKED', type(e).__name__)\"; exit 3",
    )
    assert r.exit == 3 and outcome(r.exit, r.timed_out) == "fail"
    lines = r.output.splitlines()
    assert lines[0] == "10001", r.output
    assert "CapEff:\t0000000000000000" in r.output
    # the checkout is read-only; the suite runs on a copy in the container's tmpfs
    assert "/work" in lines and "ok" in lines
    assert "SRC=1" in r.output and "WORK=0" in r.output and "TMP=0" in r.output
    assert "ROOTFS=1" in r.output
    assert not (source / "x").exists()
    assert "BLOCKED" in r.output and "REACHED" not in r.output


def test_output_is_capped_to_its_tail(runner: DockerTestRunner, cfg: Config, source: Path) -> None:
    r = _run(
        runner, cfg, source, "head -c 300000 /dev/zero | tr '\\0' a; echo END", max_output=4096
    )
    assert r.exit == 0 and r.truncated
    assert len(r.output) <= 4096 and r.output.rstrip().endswith("END")


def test_a_hung_test_is_killed(runner: DockerTestRunner, cfg: Config, source: Path) -> None:
    r = _run(runner, cfg, source, "sleep 60", timeout_s=3)
    assert r.timed_out and outcome(r.exit, r.timed_out) == "timeout"


def test_pipefail_and_errexit_are_on(runner: DockerTestRunner, cfg: Config, source: Path) -> None:
    """Round 3, F2: a failure anywhere in a pipe or a list fails the run."""
    r = _run(runner, cfg, source, "false | cat; echo AFTER")
    assert r.exit == 1 and "AFTER" not in r.output
    r = _run(runner, cfg, source, "ls /nope 2>&1 | tail -n 1; echo AFTER")
    assert r.exit != 0 and "AFTER" not in r.output


def test_a_pytest_report_comes_back_over_the_stream(
    runner: DockerTestRunner, cfg: Config, source: Path
) -> None:
    """The generic image has Python but no pytest; vigil's own dev image has
    both, and the dev-image validation runs pytest there. Here a stand-in
    writes the report where pytest would."""
    xml = "<testsuite><testcase classname='tests.test_a' name='test_a'/></testsuite>"
    r = _run(runner, cfg, source, f'printf %s "{xml}" > /tmp/vigil-report/pytest.xml')
    assert r.exit == 0 and r.reports == {"pytest.xml": xml.encode()}
    assert "VIGIL-REPORT" not in r.output


def test_forged_reports_count_for_nothing(
    runner: DockerTestRunner, cfg: Config, source: Path
) -> None:
    """Round 4, item 3: a stray report name, a fake trailer printed mid-run,
    and the marker isn't anywhere a test can read it."""
    xml = "<testsuite><testcase classname='t' name='a'/></testsuite>"
    cmd = (
        f'printf %s "{xml}" > /tmp/vigil-report/zz.xml; '
        "echo 'VIGIL-REPORT-0 BEGIN'; echo 'VIGIL-REPORT-0 pytest.xml'; echo AAAA; "
        "echo 'VIGIL-REPORT-0 END 0'; "
        "cat /proc/1/cmdline /proc/1/environ 2>/dev/null | tr '\\0' '\\n' | grep -cE 'VIGIL-REPORT-[0-9a-f]{32}' "
        "> /tmp/seen || true; echo SEEN=$(cat /tmp/seen)"
    )
    r = _run(runner, cfg, source, cmd)
    assert r.exit == 0 and r.reports == {}, r
    assert "SEEN=0" in r.output


def test_the_test_cannot_read_the_scripts_memory(
    runner: DockerTestRunner, cfg: Config, source: Path
) -> None:
    """Phase 2a, item 0 (architecture B33): the script holding the marker is
    root; the test runs as the agent uid and can't open PID 1's memory."""
    cmd = (
        "id -u; stat -c %u /proc/1; "
        "python3 -c \"\ntry:\n open('/proc/1/mem','rb'); print('MEM=open')\n"
        "except OSError as e:\n print('MEM=refused', type(e).__name__)\""
    )
    r = _run(runner, cfg, source, cmd)
    assert r.exit == 0, r.output
    lines = r.output.splitlines()
    assert lines[:2] == ["10001", "0"], r.output
    assert "MEM=refused" in r.output and "MEM=open" not in r.output


def test_the_report_dir_is_made_by_the_agent_uid_not_root(
    runner: DockerTestRunner, cfg: Config, source: Path
) -> None:
    """2a round 2 (review A, untested): the script's `mkdir` of the report dir
    runs as the agent uid; made by root, the test couldn't write its report."""
    from vigild.verifier import REPORT_DIR

    r = _run(runner, cfg, source, f"stat -c %u {REPORT_DIR}")
    assert r.exit == 0, r.output
    assert r.output.splitlines()[0] == "10001", r.output
