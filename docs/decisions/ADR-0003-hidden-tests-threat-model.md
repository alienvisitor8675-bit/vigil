# ADR-0003: Hidden tests and the verifier threat model

- **Status:** Proposed, 2026-10-26
- **Deciders:** the owner; drafted by a docs worker
- **Layer:** 4 of the documentation standard (`docs/design.md`).
- **Supersedes:** nothing. It replaces the claims B40 makes about
  isolation once its rollout is done (see "Consequences").

This record describes issues as classes. The repo is public.

## Context

Hidden tests (B40, learning rulings 10, 13, 14) decide a task whose scope
names a runner file. A fresh `hidden_test_author` session writes them; vigild
keeps them; only the verifier runs them, as their own base/head pair. A pass
means the owner merges a draft PR with less review than a normal one, so the
verdict has to hold up against the code it judges.

Two earlier fixes (#18, #19) and the in-flight #22 (branch
`feat/fixtures-and-replan-note`, which adds the host switch
`hidden_tests.enabled`, off by default) each closed a class and each review
found the next. Round 3 of #22 left findings that no further patch in the
current design closes cleanly, because they come from one fact: **the test
process and the subject run in one container, on one kernel namespace set,
and the isolation between them is made of uids, modes and cleanup.**

### Threat model

**Adversary.** Two parties:
- **The subject:** the worker's head code, which runs during a hidden run
  because the tests have to exercise it. It is the output of a model with a
  goal (accepted work) and it may be wrong, careless or hostile.
- **The spec and issue text** that the hidden-test author reads. It is
  untrusted input to a model. The adversary here is whoever wrote it.

**Protected:**
- the hidden-test verdict (pass, fail, unknown);
- the hidden tests' content (a worker that reads them can write to them);
- the per-task HMAC tag (it makes a failure count as an assertion);
- the report the verifier reads back.

**Trust boundaries:**

| Party | Trust |
|---|---|
| vigild (host) | Trusted. Holds the host key, the vault and the store. |
| The verifier, host side | Trusted. Starts containers, reads one trailer, decides. |
| The broker | vigild code; trusted to enforce its protocol. |
| The test process | Vigild's helper plus the author's tests. Trusted to run, not to be hostile (B33 says so). |
| The subject | Untrusted. Reaches the tests only through the broker. |

The goal: nothing the subject does changes the verdict, reads the tests or
reads the tag, whatever the subject's code does.

## History, by class

Each row is a class and what closed it (details in B33 and B40).

| Where | Class | Fix |
|---|---|---|
| #18 | In-process subversion: subject code imported into the test process changed what the test saw or reported. | Black-box tests. The test process never loads subject code. It calls the subject through a helper, the subject runs as another uid (10002) behind a broker. A nonce-framed result protocol. |
| #19 | Config-file planting: a file in scope that the runner reads as configuration. | Runner files are pinned to the base; an in-scope runner-config file is tampering and nothing runs. |
| #19 | A shared writable temp directory from which code was loaded into the test process. | The test process's directories belong to the test uid and are mode 0700; the subject gets its own fresh temp directory. |
| #22 r3 | A service the test uid started that the subject could reach, so the subject could act as the test uid through it. | No services in a hidden run (the image's prepare step's are stopped), plus a guard that fails the run closed at start and refuses calls while a test-uid listener exists. |
| #22 r3 | Hidden-test readability: the tests were readable from the subject's uid. | `/work` is mode 0700 for the test uid. |

## Round-3 findings still open

These are the findings of the review of #22 round 3 that the current design
does not close.

1. **A second mount of the verify checkout still exposes the hidden tests.**
   The tests are protected on one path and readable on another that reaches
   the same files.
2. **The reachability guard is a point-in-time check.** It looks when each
   call starts. A listener the test uid opens during a call stays reachable
   for the rest of that call.
3. **Coverage of IPC is partial.** Non-listening datagram sockets, FIFOs and
   System V and POSIX IPC objects are outside what the guard looks at.
4. **Fail-closed paths report as ordinary failures.** The guard and
   preparation refusals end a run as a failure of the task. They should be
   an escalation to the owner, because the task did nothing wrong.
5. **The documentation claimed more than the code delivers.** B40 reads as
   if the isolation were complete.

Items 1 to 3 are one class: **two principals share a kernel object
namespace** (a filesystem view, a network namespace, an IPC namespace), and
each new object type needs its own check. Patching it check by check does
not end.

## Decision

### 1. A separate subject container

Each hidden run gets **its own subject container beside the test
container**. The subject runs only there.

- **No network** at all, in its own network namespace.
- **Its own** mount, PID, IPC and user namespaces and its own `/tmp`. Nothing
  is shared by name: no `/tmp`, no IPC, no PID namespace, no user namespace.
- A **read-only copy of the subject** (base plus the worker's in-scope
  changes, never `.vigil/`), and **read-only fixtures**. It has no mount of
  the verify checkout and no path to the hidden tests.
- **One broker channel** is the only shared object: a socket or FIFO in a
  volume that holds nothing else. The test container's helper writes
  requests to it and reads results from it. The subject container's
  entrypoint reads requests and runs them.
- The subject container is created for the run and removed at its end (and
  on a crash, by the same cleanup path the verifier already has). It holds
  no tag, no key and no report.

The pair counts as one verifier slot, and its memory and limits are fixed by
the owner (see "Owner decisions (2026-10-26)").

**Why this closes items 1 to 3 by construction.**
- Item 1: the subject's container has no mount of the checkout, so there is
  no second path. The only mounts it has are the copy and the fixtures.
- Item 2: there is no shared loopback. A listener the test uid opens is in
  another network namespace, so no call can reach it, at any moment. The
  guard's timing question goes away.
- Item 3: datagram sockets, FIFOs and IPC objects live in namespaces the
  subject does not share. The channel is the one object they share, and it
  is the one the protocol below governs. No new object type needs a check,
  because there is nothing shared to find.

The static guards stay (they are cheap), but they are no longer the boundary.

### 2. What the broker channel's protocol guarantees

The channel is the one surface the subject can write to that the test side
reads, so the test side treats it as hostile input.

- **Framing.** Every message is length-prefixed and carries the run's nonce
  (generated by the test process, passed over stdin, as today). A message
  with a bad length, a wrong nonce, a duplicate or a missing frame is a
  protocol error, and a protocol error fails the call.
- **Size limits.** A maximum size per frame, per call and per run. Exceeding
  one ends the call with a refusal. The reader never allocates from a
  length it hasn't checked.
- **Timeouts.** A timeout on each call, counted from when the subject
  container starts it, and an overall run deadline. A hung subject is
  reported as timed out. The host kills the subject container at the
  deadline, never relying on the subject to stop.
- **A refusal result that is distinct from any exit code.** A request the
  broker refuses (malformed, over a limit, timed out, a channel error) is a
  result of its own kind in the protocol, not an exit status. A subject
  cannot produce one by exiting with a chosen code, and a test cannot read
  one as a subject's failure. The subject's exit code is data, carried in a
  field of a result frame and never used as a verdict.
- **One request at a time**, as today, and no request outlives the call that
  made it.
- **Fail closed.** Anything unparsed ends the run as an escalation (decision 3), not as a failed test.

### 3. Fail-closed paths escalate

A refusal that comes from the container setup or the broker (guard, prepare,
protocol error, a missing tool) is recorded as an **escalation to the owner**
with the reason, not as a task failure. A task failure is only a verdict from
tests that ran.

### 4. Hash pinning of the test command and the runner files

- **At the spec gate** vigild records one hash over `test_cmd` and every
  runner file as the base has it (the files `runner_config` globs match, and
  the scripts `test_cmd` runs). The hash is stored in `spec_gate.json` and
  the spec is approved with it.
- **At verify** vigild recomputes the hash from the base before any run. A
  **mismatch fails closed with no PR**: the task escalates to the owner with
  the reason. A missing hash on a spec approved before this change is the
  same.
- This folds in the "harden the verifier" item. The clean-container re-run it
  asked for already exists (B33: fresh vault checkout, new container every
  run), so this adds the pinning and its documentation only. No open issue
  for it was found; this record is its place.

Pinning closes the class where the thing that runs differs from the thing
the owner approved. It does not protect against a hostile subject, which is
the container split's job.

### 5. Issue #20 as a second layer

#20 lists gaps in the static check (`vigild/blackbox.py`). They are not
exploitable today, because the test process cannot read the subject. They
matter if that layer regresses. As classes:

1. a computed module argument built by joining literals;
2. loaders reached through an alias or a destructured name;
3. a bare specifier that the runner's own alias config maps to in-scope code;
4. reflective import through a language builtin;
5. a function constructor called without `new`.

Each gets a failing test and a fix in rollout step (c), after the container
split, and B40 says the static layer is a second layer and the container
split is the boundary. (The structural boundary today is the unreadable
subject copy; after (b) it is the separate container.)

## Residual risks

Accepted, and stated in B40 once the series is done.

- **Kernel and container-runtime escapes.** A subject that escapes its
  container has the host's rights. The mitigations are no capabilities, no
  network, a read-only root and an unprivileged user; there is no stronger
  boundary on offer on this host.
- **Docker Desktop's VM.** Both containers share the Linux VM. A VM-level
  or file-sharing issue is outside what vigil controls. Keep Docker Desktop
  updated; vigil does not claim to defend against it.
- **A hostile hidden-test author.** The author is a model reading untrusted
  spec text. It can write tests that pass a wrong implementation or fail a
  right one. The owner sees test names and categories, not code, and the
  draft PR and its review are the backstop. `vigil hidden show` exists for
  disputes.
- **Timing side channels.** A subject can measure how long calls take and
  infer something about the tests. Time limits and the framed protocol do
  not prevent it. The information is low value without a way to change the
  tests.
- **The owner pasting hidden tests into a session.** A person can move the
  tests into a worker's context, which ends their secrecy for that task. The
  files stay out of every packet by construction; a person is not covered.
- **The tag in the test container.** The HMAC tag is not the host key. It is
  a per-task value derived from `VIGIL_HOST_KEY`, and ruling 10's "no key in
  a container" refers to `VIGIL_HOST_KEY`, which never enters a container.
  The tag itself is in the test container's environment for the base run, so
  it is exposed there: with the subject in another container only the test
  process can read it, but a hostile test (not a subject) can read it and
  forge a failure that counts. The exposure is limited to one task's tag and
  to the base run. This is the author-is-hostile risk again.
- **The author's output is trusted for what it asserts.** A test that is
  weaker than the spec says is not detected by the verifier. Only fails-on-
  base is checked.
- **Residual DoS.** A subject can use its whole time and memory budget. That
  fails a run; it does not change a verdict.

## Rollout, as a PR series

Each PR is separate, has its own review of at most two rounds, and has a
done-when. `hidden_tests.enabled` stays off per repo until (d) is merged
with a clean review. The owner turns it on.

**(a) Hash pinning.**
- Done when: the gate records the hash; verify recomputes it; a changed
  `test_cmd` or runner file fails closed with no PR and an escalation; tests
  cover both, and an approval from before this change.

**(b) The subject container and the broker channel.**
- Done when: a hidden run starts a subject container with no network, the
  read-only subject copy and fixtures, and the channel volume and nothing
  else; the protocol's framing, limits, timeouts and refusal result have
  tests that fail when each is removed; round-3 items 1 to 3 have Docker
  tests that fail on the old design; and item 4 is an escalation.
- Done when, also: **a real hidden run passes in each repo's dev image, for
  ParkAgent and for vigil.** (The ParkAgent run is done through vigild only,
  after the owner confirms, as CLAUDE.md says.)
- New code lives in `vigild/subject/`, with changes in `vigild/verifier.py`
  and `vigild/hiddenkit/`.

**(c) The #20 static fixes.**
- Done when: each of the five classes has a failing test that is now green,
  and B40 names the static layer as the second layer.

**(d) The docs, and the switch.**
- Done when: B40 and the feature map describe what the code does, the
  runbook says how to turn the switch on, the round-3 documentation finding
  is closed, and the owner turns `hidden_tests.enabled` on for a repo after
  a clean review.

## Consequences

- **Cost.** One more container per hidden run: its start, a copy of the
  subject, a channel volume and its cleanup. The verifier already runs one
  container at a time per machine, so a hidden run takes longer by the
  subject container's start for each call batch. The two containers
  share one verifier slot and its 6 GiB budget in the 11.7 GiB Docker VM (see
  "Owner decisions (2026-10-26)").
- **Complexity.** A protocol to maintain, and a second image path (the
  subject runs in the repo's dev image, never the session image).
- **What B40 says until it's done.** It says what the code does today and
  no more: black-box tests, a uid split, a broker, a guard that checks at
  each call, and no claim that the isolation is complete. B40 points at this
  record. The switch stays off, so no task's acceptance depends on the open
  items.
- **Docs.** `docs/architecture.md` B40 and the feature map change in each
  PR that changes behaviour.

## Alternatives

1. **Keep patching the one container.** Each check closes one object type;
   the round-3 list shows the next one arrives each review. Rejected: the
   class does not end.
2. **A network namespace for the subject uid in the same container.** It
   needs a broad capability the container is otherwise without, and it does
   not give the subject its own IPC, mount or PID view. Rejected (already
   noted in #22).
3. **A VM per subject** (a microVM). The strongest option, and the cost on a
   Mac mini with Docker Desktop is out of proportion for phase 1. It is the
   next step if a container escape is ever shown.
4. **Never run subject code in a hidden run** (static checks of the diff
   only). It does not test behaviour, which is the point of the tests.
5. **Turn the feature off for good.** It is off now. The runner-file class
   has no other way to be accepted besides owner review of every task.

## Owner decisions (2026-10-26)

1. **Verifier slot.** A hidden run's test and subject containers count as one
   verifier slot. They start and stop together, and
   `container.verifier_concurrency` stays at 1, so nothing else runs in the
   verifier while a pair is up. Raising concurrency is not worth it on an
   11.7 GiB VM.
2. **Memory, within the slot's existing 6 GiB budget:**
   - test container: 4.5 GiB, tmpfs included (it runs only the hidden tests,
     not the full suite);
   - subject container: 1 GiB;
   - headroom: 0.5 GiB.

   Workers' sessions keep the rest of the VM exactly as today.
3. **Subject container limits:** 1 GiB of memory with no swap, 256
   processes, 1 CPU, a 256 MiB tmpfs, and the existing per-call timeout.
4. **Out of memory.**
   - If the subject runs out of memory, that is a subject failure, and the
     test observes it as an error.
   - If the test container runs out of memory, the result is unknown: it
     escalates and never passes.
5. **Settings and reporting.** All of these are host-policy settings. Each
   container's `memory.peak` is recorded in `tests.json`, and the peaks are
   reported to the owner after the first five hidden runs.

## When this changes

Revisit this record when any of these happens:
- a hidden run is used on a repo that is not ParkAgent or vigil;
- a container escape or a verdict change is reported against it;
- the learning epic extends hidden tests from the runner-file class to every
  task (then the cost line above is multiplied);
- Docker Desktop is replaced as the runtime.
