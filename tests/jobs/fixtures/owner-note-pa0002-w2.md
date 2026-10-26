# Owner note for the pa-0002 w2 replan (verbatim, 2026-10-25)

Replan pa-0002 item 6 (w2) with this owner note. Amend the spec before the hidden-test author runs.
1. Interface (add): node scripts/check-feature-map.mjs [--root <dir>].
 • With --root, it checks <dir>.
 • With no arguments, behaviour is unchanged: it checks the script's parent, which is what CI uses.
 • An unknown argument exits 2 with a usage line.
 • checkFeatureMap(root) keeps its signature.
 • Scope is unchanged: the script, server/test/checkFeatureMap.test.ts and docs/feature-map.md. ci.yml is not touched.
2. AC3 (rewrite): run as node scripts/check-feature-map.mjs --root <fixture> against a fixture repo containing one stale reference, it exits non-zero and its output names that reference. Run with --root pointing at a clean fixture, it exits 0. test: pnpm -C server test
3. AC4 (clarify): checkFeatureMap(<repo root>), with the root passed explicitly, or the CLI with --root <repo root>, returns no problems on the repo after the doc fixes. Hidden tests must always pass the root; never rely on the default.
4. AC6 (add): with no arguments, the script still checks its own repo and exits 0 on main. This protects CI's call.
5. Hidden-test author contract for this task:
 • AC3 and AC6 are tested through the broker as a subprocess: argv, a fixture dir readable by uid 10002, and exit code plus stdout and stderr returned.
 • AC1, AC2, AC4 and AC5 go through the call driver with an explicit root.
 • Before replanning, confirm the broker supports argv and a 10002-readable fixture dir. If it doesn't, tell me the size of that change before doing anything else.
Base: ParkAgent main at 287fb86. The spec's comment and name rules show zero violations there, so w1's 14 fixes hold.
At spec approval I'll check that:
 • there is at least one hidden test for each of AC1–AC6;
 • every AC4 test passes the root explicitly;
 • AC3 has both a stale-fixture case (non-zero exit, reference named) and a clean-fixture case (exit 0).
If any of those is missing, I'll reply "replan with author note" instead of approving.
