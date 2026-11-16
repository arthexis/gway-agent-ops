# Scheduler #87 — disposable runtime harness

This is **test-only tooling**, not a release service. No production timer or installed AgentOps executable is called by the harness.

## Preconditions and trust boundaries

- Source under test must be a **clean, separate checkout** of exact commit `4e7507240c3386270a466be7115926996be1835b` (original Draft #81 candidate). The harness refuses any other SHA or dirty checkout. The harness script itself may be taken from the subsequent Draft branch, **outside the frozen checkout**.
- Test ledger lives under a **new private mode-0700 scratch directory**, outside the checkout and outside the normal AgentOps ledger. Only synthetic approval records and the fake submitter are used.
- The pinned source must have its Python dependencies available in the test environment. Missing prerequisites are a **blocker**, not permission to install or change production.
- There is no runtime GitHub lookup, OAuth or direct AgentOps dispatch: the harness explicitly injects a trusted test resolver and a fake submitter. This is an intentional isolation boundary, so it cannot certify real GitHub/IAM integration.
- Never use the packaged `gway-agent-ops-scheduler.service` or `gway-agent-ops-scheduler.timer` for this test. Those target the installed executable, not the frozen source.

## Commands in the isolated checkout context

Use `python tests/scheduler_runtime_harness.py` from the Draft branch file location, with explicit `--checkout /path/to/frozen-source` and `--ledger /private/scratch/tasks.sqlite3` arguments. The private scratch directory must be created with mode 0700 beforehand. Do **not** change `GWAY_AGENT_LEDGER_PATH` in the production service environment.

1. `prepare`: create a single synthetic approved schedule due in one second, without dispatch.
2. `tick`: invoke the scheduler with a fake submitter; repeat in fresh processes and inspect `*.fake_calls` and ledger state to check one-shot behavior.
3. `--crash-after-fake tick`: deliberately exit the subprocess with code 71 after writing a fake marker. Restart `tick` in a distinct process and verify no second marker. Never inject this into a real service.
4. `inspect`: read the synthetic ledger.
5. `timeout-probe`: create a synthetic process group, force a short timeout, terminate its process group, verify quiescence. This test does not execute AgentOps and can be run locally.

For user-systemd validation, invoke **only** the frozen-source harness `tick` through a distinctly named, temporary `systemd-run --user` unit in a disposable sandbox, with `NoNewPrivileges=yes`, bounded timeout, and explicit post-run evidence of no active units, processes or timers. A transient timer must also use a unique name and be fully removed. Verify the exact `ExecStart` before starting anything. **Do not** activate, reload, install, or enable canonical AgentOps units. Systemd execution is opt-in and must be separately validated on gway-001; this file does not auto-start it.

## Acceptance

Report PASS/FAIL/NOT TESTED independently for: (a) real transient user-systemd invocation/timer, (b) subprocess termination and crash recovery on the same synthetic SQLite ledger, (c) process-group timeout cleanup. Preserve command exit codes, source SHA, unit names, ledger state and fake marker counts; sanitize paths and credentials. A 0 exit code or green hermetic CI does **not** establish runtime acceptance. No AgentOps submit, permanent deployment, production timers or PR merge are authorized here.
