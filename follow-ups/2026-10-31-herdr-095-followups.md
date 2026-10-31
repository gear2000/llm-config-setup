# Herdr 0.9.5 go-live follow-ups (batch)

Date: 2026-10-31. Source: VALIDATE.md run 2, `/var/tmp/upagent-validate-20261030-2308/`.
Status: fixed on branch `upagent/herdr-095-followups-20261031`; see Results at the end.

## Rule for every item: reproduce first, then fix

1. Reproduce the bug before touching the code. Write a test that fails on today's `main` for
   the reason described. Where the item says so, also reproduce it live.
2. Record the red run (test name and failure output) in the item's report.
3. Fix. The same test must now pass, and `just test` must stay green.
4. Rerun the live repro, if there is one, and show that the bug is gone.
5. If an item cannot be reproduced, stop on that item and say so. Do not fix it blind.

## 1. The hub's note to the Sentinel never gets through

- **Where:** `recruiter.py`, `_prompt_sentinel_after_nudge` (around line 7614).
- **What happens:** after the hub sends "continue" to a quiet worker, it tells the Sentinel
  `SENTINEL_STALL_NUDGED`. `_submit_agent_prompt` first waits for the Sentinel to be idle, for
  `SENTINEL_PROMPT_IDLE_TIMEOUT_MS` (120 s). The Sentinel is always busy watching, so the wait
  times out every time, and the ledger records `sentinel-stall-nudge-resume-unavailable`.
- **Evidence:** D9 ledger, at 60 s nudge then 180 s unavailable; 361 s then 481 s; 1263 s then
  1383 s.
- **Cost:** the Sentinel never gets the note. The hub also blocks for 120 s on each nudge and
  does not check that worker in the meantime.
- **Fix:** do not block on the Sentinel being idle. Send the note without the idle wait (or
  with a short wait), or drop the note, since the Sentinel can read the nudge in the ledger.
  Pick whichever keeps the existing tests' intent.
- **Reproduced?** Yes, live: D9 run 2, three times.
- **Repro test (must fail today):** a fake Sentinel that never goes idle. After one nudge,
  assert that `sentinel-stall-nudge-resume-unavailable` is not recorded and that the hub
  returns from the nudge step in well under 120 s (use a fake clock, not a real wait).
- **Live repro:** rerun VALIDATE.md D9 (25 min).
- **Done when:** the repro test passes, and a D9 rerun shows no
  `sentinel-stall-nudge-resume-unavailable`, with each nudge followed by the next hub event
  within seconds, not 120 s.

## 2. Stopping a resident specialist leaves its leftover processes running

- **Where:** `specialist_lifecycle.py`, `_stop` (line 385), which runs on
  `upagent-specialist-down`, on restart, and when a resident expires.
- **What happens:** `_stop` runs a plain `herdr pane close` and checks that the pane is gone.
  It does not record and stop the pane's processes the way `recruiter._close_worker_pane`
  does since 8039bae. A background job the resident left running (a dev server, a watcher,
  `sleep &`) moves to init and keeps running.
- **Reproduced?** Yes, live on main 09b69bb, 2026-10-31: a `qa` resident (claude-sonnet-5-5)
  started `sleep 917` with Claude's background Bash; after `upagent-specialist-down` returned
  `stopped`, pid 3970527 was still running, re-parented away from the pane
  (`/var/tmp/resident-repro-20261031/run-c.log`). Script: `/var/tmp/resident-repro-20261031/repro.sh`.
- **Repro test (must fail today):** a unit test where the resident pane's process tree holds a
  live child. After `_stop`, assert that the child got SIGTERM (or SIGKILL) and is gone.
- **Live repro:** `upagent-specialist-up` one cheap interactive resident in the scratch repo,
  have it run `sleep 600 &` and return to idle, note the sleep's pid, run
  `upagent-specialist-down`, and check that the pid is still alive (bug) / gone (fixed).
- **Fix:** reuse the recruiter's process-tree snapshot and teardown in `_stop` (or call
  `_close_worker_pane`). Keep the existing idle and identity checks.
- **Done when:** both repros show the background job is gone after `down`.

## 3. When the process cleanup fails, the retry reports success

- **Where:** `recruiter.py`, `_terminate_process_snapshot` (line 5166) raises when a process
  survives SIGKILL; the launch reconciliation loop (around line 4568) then retries.
- **What happens:** attempt 1 snapshots the pane's processes, closes the pane, and raises
  "pane processes survived SIGKILL". The retry finds the pane already absent, so it records
  `already-absent` / `verified_absent: true` and marks the launch closed. The survivor's pid
  is lost, and a still-running process is recorded as clean.
- **Reproduced?** Not yet. Found by reading the code. Live repro is not practical: it needs a
  process stuck in the kernel (for example a hung FUSE or network mount), which could also
  hang the shell. **Test-only item.**
- **Repro test (must fail today):** fake one snapshot pid that stays alive after SIGKILL on
  attempt 1. Run the reconciliation loop. Assert the final result is not `verified_absent:
  true` and the survivor's pid is reported. On today's code the assert fails.
- **Fix:** carry the survivors' (pid, start time) out of the failed attempt (in the error or
  in the launch journal). Every retry re-checks those processes before it can report clean; if
  any is still alive, the result is `cleanup-failed`, naming the pids.
- **Done when:** the repro test passes, and a second test proves a survivor that dies before
  the retry still ends `verified_absent: true`.

## 4. A dead control token was left in run 1's cockpit log — DONE 2026-10-31

- **Cause:** before df3417f the `timeout-warning` message held the raw token; the run-1
  cockpit printed it, so its Claude log kept 8 copies (3 lines).
- **Reproduced?** Yes, live: run 1 leaked it (old code). Run 2 (fixed code) did not: its D8
  token is in no message, no Claude log, and nothing under `/var/tmp`. Test:
  `recruiter_test.py:2900`.
- **Token dead:** a cancel with it returned the request's finished state and changed nothing.
- **Done:** redacted all copies in place to `<redacted-control-token>`; file still 0600 and
  valid JSONL. No code change needed.

## 5. Lab leftovers — NOT a collision risk; disk only — DONE 2026-10-31

- The lab ownership record is inside its own lab dir (`/var/tmp/hu95-fix1b/ownership`), not in
  the shared `/tmp/llm-herdr-lab-<uid>`, which does not exist. No collision is possible.
- The labs hold 69 GB of copied harness homes (`/var/tmp/hu95-fix1b/home`) plus about 280 MB
  in `/var/tmp/hl95-*`. The disk is 86% full. Deleting them is Gary's call (irreversible).
- **Done:** Gary approved; deleted `/var/tmp/hu95-fix1b/home` (69 GB, incl. copied credential
  files). Lab evidence and drill dirs kept (57 MB). Disk 86% -> 78%.

## 6. Ledger files get the writer's umask, and the expiry index never shrinks

- **Where:** `recruiter.py`, `_write_json` (line 535); `reap_expired` (line 1029); index
  writes at lines 1168, 2154, 2282.
- **What happens:** (a) `_write_json` creates files with the process umask, so one lease index
  holding `requester_control_token` is 0664 (17 files under `active/by-expiry` are group or
  world readable). The ledger dir is 0700, so nobody else can read them today; the files should
  still be 0600. (b) Index entries are never removed: 265 entries with no active request.
  `reap_expired` reads every one on every call, and each holds a (dead) token.
- **Reproduced?** Yes, on this machine's live ledger (counts above).
- **Repro tests (must fail today):** (a) with umask 0o002, `_write_json` makes a file whose
  mode is not 0600. (b) claim, finish, then `reap_expired` after the expiry: the index entry is
  still on disk.
- **Fix:** (a) create the temporary file with `os.open(..., O_CREAT | O_EXCL, 0o600)`. (b) in
  `reap_expired`, under the claim lock, delete an expired index entry whose claim is gone or
  no longer matches its token and expiry; remove the expiry dir once empty.
- **Done when:** both tests pass; after one reap on the live ledger, only entries of active
  claims remain and every remaining file is 0600.

## 7. A Claude resident in a new folder hangs at Claude's folder-trust screen

- **Where:** `specialist_lifecycle.py`, `_start` (~line 397). Hub panes go through
  `recruiter._launch_managed_pane`, which calls `_ensure_claude_folder_trust(order["cwd"])`
  for claude/claudex launches (`recruiter.py:4668`). `_start` launches residents itself and
  never pre-trusts the folder.
- **Reproduced?** Yes, live, twice (2026-10-31): `upagent-specialist-up qa` (claude-sonnet-5-5)
  from a new folder `/var/tmp/resident-repro-20261031/repo` failed in 5 s with
  `agent_not_ready ... blocked during startup`; the pane showed "Quick safety check: Is this a
  project you created or one you trust?".
- **Repro test (must fail today):** `_start` for a claude (and a claudex) offering with an
  untrusted cwd must call `_ensure_claude_folder_trust(cwd)` before `agent start`.
- **Fix:** call `recruiter._ensure_claude_folder_trust(state["cwd"])` in `_start` for claude and
  claudex harnesses, before the pane launch.
- **Done when:** the test passes and a live `upagent-specialist-up qa` from a new folder
  reaches ready.

## 8. A consult given a relative consult.json path is rejected

- **Where:** `recruiter.py`, `consult_artifact_paths` (~line 15636) keeps the path as given.
- **What happens:** `just upagent-consult consults/x.json` (relative, as the consult help text
  invites: "write <your-cwd>/consults/<id>.json ... run `just upagent-consult <that
  consult.json>`") writes an order whose artifact paths are relative, and order validation
  rejects it: "`artifact_publication.compacted_path` must be an absolute path".
- **Reproduced?** Yes, live, 2026-10-31 (`/var/tmp/resident-repro-20261031/consult-a.log`).
- **Repro test (must fail today):** `cmd_consult("consults/x.json", ...)` from a tmp cwd does
  not reject on `compacted_path`; or directly, `consult_artifact_paths("consults/x.json")`
  returns absolute paths.
- **Fix:** `Path(consult_path).absolute()` in `consult_artifact_paths` (and use the absolute
  path wherever `cmd_consult` reads the consult file).
- **Done when:** the test passes and a live relative-path consult is accepted.

## 9. The hub trusts a dead services pane recorded in services.json

- **Where:** `recruiter._recruiter_pane_from_state` (~line 11691) returns the recorded
  `recruiter_pane` without a liveness check. Callers: `cmd_consult` (~16040, uses it as the
  consult's cockpit pane), the intake clerk launch (~11159, runs `cmd_up` only when it is None),
  shared manager placement (~8940), and `public_api.py:1236` (`services_ready`).
- **What happens:** after a Herdr server restart, `services.json` (here written 2026-09-14)
  still names `w2M:p1`, which no longer exists. `upagent status` reports `services_ready:
  true`. A consult then launches from the dead pane: `pane get w2M:p1 ... pane_not_found`,
  repeated `pane-launch-cleanup-pending`, and the receipt says only "dispatch child exited but
  dead claim ... could not be reconciled".
- **Reproduced?** Yes, live, 2026-10-31 (consult `consult-c7c84183272d0a4e37a6ae5a`).
- **Repro tests (must fail today):** with a state file naming a pane that `_live_pane_ids`
  does not list: (a) `services_ready` is false; (b) `cmd_consult` fails before any launch with
  a clear error naming the dead pane and `just upagent-up`; (c) the intake path runs `cmd_up`
  as it already does for a missing pane.
- **Fix:** one helper that returns the recorded pane only when it is live in the recorded Herdr
  session, else None; use it in those callers. Keep `cmd_down`'s own handling unchanged.
- **Done when:** the tests pass; live, `upagent status` shows not ready after a restart, and a
  consult says to run `just upagent-up` instead of the opaque reconcile error.

## Noted, not fixed (low payoff)

- With no repo roster, the kit-default roster anchors consults to the kit checkout, so a
  resident started with `upagent-specialist-up` from a non-destination folder is never used by
  a consult from that folder (consult cwd = kit, resident cwd = the folder). Real destinations
  carry their own roster, so they do not hit this.

## Results (2026-10-31)

Branch `upagent/herdr-095-followups-20261031`. Every repro test below was re-run by the lead
against unmodified `main` 09b69bb and failed there for the stated reason; all pass on the branch.
Full suite on the branch head: 2166 passed.

| Item | Commit | Repro test (red on main) | Live check on the branch |
| --- | --- | --- | --- |
| 1 | 8b0da09 | `sentinel_test::test_the_stall_nudged_note_reaches_a_sentinel_that_is_never_idle_without_blocking` | PASS: D9 rerun passed (SUM 5050, 3 nudges, `worker-stall-escalation`), zero `sentinel-stall-nudge-resume-unavailable`; next hub event ~14 s after each nudge (was 120 s) |
| 1b | 0e66962 | `sentinel_test::test_a_rejected_closeout_reaches_a_never_idle_sentinel_without_blocking[...]` (recheck/landing retry had the same 120 s idle gate; red showed an uncorroborated STALLED accepted over a live worker) | covered by D9 |
| 2 | 415c397 | `specialist_lifecycle_test::test_down_ends_a_background_job_the_resident_left_running` | PASS: resident's `sleep 917` (pid 4101558) gone after `down`; on main pid 3970527 survived |
| 3 | b7b0d68 | `recruiter_test::test_launch_reconciliation_never_reports_clean_while_a_sigkill_survivor_lives` (+ guard `..._is_clean_once_the_sigkill_survivor_is_gone`) | test-only by design |
| 6 | c149102 | `recruiter_test::test_ledger_json_files_are_private_whatever_the_umask`, `::test_reap_removes_expiry_index_entries_of_finished_or_replaced_claims`, `::test_reap_skips_an_index_entry_another_reaper_pruned_after_the_listing` | (a) PASS: after D9 no token-bearing ledger file is group/world readable (remaining 0664 files are lock files and agent-written artifacts, no token). (b) checked on the main ledger after merge |
| 7 | 86f6769 | `specialist_lifecycle_test::test_claude_resident_pre_trusts_its_folder_before_agent_start[claude/claudex]`, `::test_a_failed_folder_trust_never_wedges_the_resident_launch` | PASS: `specialist-up qa` from a new untrusted folder reached `ready`; on main it hung at the trust screen |
| 8 | a522e15 | `recruiter_test::test_a_consult_given_a_relative_path_writes_an_order_with_absolute_artifacts` | PASS: relative-path consult answered `cited`; on main it was rejected |
| 9 | 2216495, 2e3e07c | `public_api_test::test_services_are_not_ready_when_the_recorded_pane_is_dead`, `recruiter_test::test_consult_fails_before_launch_when_the_services_pane_is_dead`, `::test_shared_manager_placement_refuses_a_dead_services_pane`, `::test_intake_clerk_brings_services_up_when_the_recorded_pane_is_dead`; 2e3e07c adds the public request door and the resident fallbacks | PASS: after closing the services pane, status shows `services_ready: False` and a consult refuses with "... is not live in its Herdr session (was Herdr restarted?); run `just upagent-up` first" |

Live-check scripts: `/var/tmp/resident-repro-20261031/repro.sh`, `/var/tmp/followup-live-20261031/`.
Note for live checks on a branch: build the worktree's gitignored `offerings.yaml`
(`harness.materialize_offering_roster`) and run `just upagent-up` with `UPAGENT_CANONICAL_REPO`
set; the branch hub has its own state dir but shares the labelled services pane.

Left alone (low payoff): `_stop` SIGKILL-survivor retry loses pids; item 3 survivors from an
earlier pass are overwritten, not merged; a consult blames a dead pane when Herdr is merely
unreachable.
