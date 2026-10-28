"""ADR-0004 rule 1: the SSH restriction is checked for real. The probe uses the
Docker context's own target and port, and the restricted key must pass three
checks: (a) a plain command doesn't run and the session behaves like
`docker system dial-stdio` with stdin closed, (b) a pty is refused, (c) a port
forward fails. A broken key or an unreachable host is a failure. The ssh
process is faked, one result per case."""

from __future__ import annotations

from tests.executors.hostfakes import FakeSsh  # noqa: F401  (the high-level fake)
from vigild.executors.hostcheck import MARKER, SshRun, SubprocessSsh

ENDPOINT = "ssh://sandbox@box-a.example:2222"
PTY_REFUSED = "PTY allocation request failed on channel 0\n"
FWD_REFUSED = "channel 2: open failed: administratively prohibited: open failed\n"


class FakeTransport:
    """Scripted ssh results keyed by the check (plain / pty / forward)."""

    def __init__(self, plain: SshRun, pty: SshRun, forward: SshRun) -> None:
        self.results = {"plain": plain, "pty": pty, "forward": forward}
        self.argv: dict[str, list[str]] = {}

    def run(self, argv: list[str]) -> SshRun:
        kind = "pty" if "-tt" in argv else "plain"
        self.argv[kind] = argv
        return self.results[kind]

    def forward(self, argv: list[str]) -> SshRun:
        self.argv["forward"] = argv
        return self.results["forward"]


GOOD_PLAIN = SshRun(0, "", "")  # dial-stdio with stdin closed: quiet
GOOD_PTY = SshRun(0, "", PTY_REFUSED)
GOOD_FWD = SshRun(None, "", FWD_REFUSED)


def probe(plain: SshRun = GOOD_PLAIN, pty: SshRun = GOOD_PTY, fwd: SshRun = GOOD_FWD):  # type: ignore[no-untyped-def]
    t = FakeTransport(plain, pty, fwd)
    return SubprocessSsh(t).shell_refused(ENDPOINT), t


def test_a_restricted_key_passes_all_three_checks() -> None:
    (ok, note), _ = probe()
    assert ok is True, note


def test_it_uses_the_contexts_own_target_and_port() -> None:
    _, t = probe()
    for kind in ("plain", "pty", "forward"):
        argv = t.argv[kind]
        assert "sandbox@box-a.example" in argv and argv[argv.index("-p") + 1] == "2222"
        assert "BatchMode=yes" in " ".join(argv)
    assert MARKER in " ".join(t.argv["plain"])
    assert "-tt" in t.argv["pty"]
    f = t.argv["forward"]
    assert "-N" in f and "-L" in f and "ExitOnForwardFailure=yes" in " ".join(f)


def test_a_command_that_runs_fails_check_a() -> None:
    (ok, note), _ = probe(plain=SshRun(0, MARKER + "\n", ""))
    assert ok is False and "plain" in note


def test_a_session_that_is_not_dial_stdio_fails_check_a() -> None:
    # a forced command that prints something is not `docker system dial-stdio` on closed stdin
    (ok, note), _ = probe(plain=SshRun(0, "hello\n", ""))
    assert ok is False and "dial-stdio" in note


def test_a_pty_that_is_granted_fails_check_b() -> None:
    (ok, note), _ = probe(pty=SshRun(0, "", ""))
    assert ok is False and "pty" in note


def test_a_port_forward_that_works_fails_check_c() -> None:
    (ok, note), _ = probe(fwd=SshRun(None, "", ""))  # nothing refused it
    assert ok is False and "forward" in note


def test_a_broken_key_fails() -> None:
    denied = SshRun(255, "", "sandbox@box-a.example: Permission denied (publickey).\n")
    for kw in ({"plain": denied}, {"pty": denied}, {"fwd": denied}):
        (ok, note), _ = probe(**kw)  # type: ignore[arg-type]
        assert ok is False, kw
        assert "could not connect" in note or "forward" in note or "pty" in note


def test_an_unreachable_host_is_a_failure_not_a_pass() -> None:
    down = SshRun(255, "", "ssh: connect to host box-a.example port 2222: Connection refused\n")
    (ok, note), _ = probe(plain=down, pty=down, fwd=down)
    assert ok is False and "could not connect" in note


def test_a_ssh_that_cannot_run_is_a_failure() -> None:
    class Boom:
        def run(self, argv: list[str]) -> SshRun:
            raise OSError("no ssh")

        def forward(self, argv: list[str]) -> SshRun:
            raise OSError("no ssh")

    ok, note = SubprocessSsh(Boom()).shell_refused(ENDPOINT)
    assert ok is False and "OSError" in note


def test_a_forward_that_is_allowed_but_finds_no_listener_fails_check_c() -> None:
    """Opus review, round 2: OpenSSH prints "open failed: connect failed" when
    forwarding is allowed and the target merely refuses the connection. Only
    "administratively prohibited" shows the key forbids forwarding."""
    allowed = "channel 2: open failed: connect failed: Connection refused\n"
    (ok, note), _ = probe(fwd=SshRun(None, "", allowed))
    assert ok is False and "forward" in note
