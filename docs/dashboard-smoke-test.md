# Dashboard Smoke Test

This workflow validates the dashboard against a real running Cymphony orchestrator while using a safe fake coding runner.

## Why this exists

The dashboard now includes:

- operator controls
- waiting and blocked reasons
- recent problems
- skipped issue visibility
- per-issue drill-down

Unit tests cover rendering and handler logic, but they do not replace a live operator pass against an actual orchestrator process.

## Safe runner

The smoke test uses [`scripts/fake_claude_stream.py`](../scripts/fake_claude_stream.py), which emits Claude-style `stream-json` events without touching a real repo or model provider.

Modes:

- `success`: produces a normal running -> success flow
- `fail`: produces a failed turn so retrying/problem surfaces can be checked
- `input_required`: produces a hard-fail input-required result

## Prerequisites

- `LINEAR_API_KEY` exported in your shell
- the `cymphony` package installed in your environment
- a Cymphony issue assigned to you and in `In Progress`

Recommended issue:

- `BAP-177` — the smoke-test task itself

## Run

```bash
CYMPHONY_FAKE_CLAUDE_MODE=success ./scripts/run_dashboard_smoke_test.sh
```

Optional overrides:

```bash
PORT=8090 ASSIGNEE=gonzalobandeira PROJECT_SLUG=cymphony-b2a8d0064141 ./scripts/run_dashboard_smoke_test.sh
```

## What to verify

### Success pass

1. Open the dashboard.
2. Confirm the issue appears in `Running`.
3. Expand the issue drill-down and verify:
   - latest plan section renders
   - recent events render
   - runtime/session fields render
4. Use `Pause Dispatching` and `Resume Dispatching`.
5. Use `Cancel Worker` while the fake runner is sleeping.
6. Use `Skip` and `Requeue`.

### Failure pass

Run again with:

```bash
CYMPHONY_FAKE_CLAUDE_MODE=fail ./scripts/run_dashboard_smoke_test.sh
```

Verify:

- the retry queue updates
- waiting reasons and recent problems remain intelligible
- the issue detail endpoint `/api/v1/<IDENTIFIER>` reflects the retry state

## Notes

- The script writes a temporary workflow to `.tmp/WORKFLOW.dashboard-smoke.md`.
- The smoke test intentionally constrains polling to `In Progress` issues assigned to the current operator.
- This is meant for dashboard/operator validation, not agent correctness.
