# Design: Setup-First Config Ownership Model

**Issue:** BAP-187
**Status:** Approved
**Date:** 2026-03-30

## Problem

Cymphony's configuration lives in a committed `WORKFLOW.md` that mixes
repo-specific template content (hooks, prompt templates) with
operator-specific values (API keys, assignee, project slug). This makes
onboarding brittle: every new operator must manually edit a tracked file,
and the repository feels preconfigured for one person instead of
setup-driven for any operator.

## Decision

Split configuration into three layers with clear ownership:

| Layer | Location | Tracked in git? | Who owns it |
|-------|----------|-----------------|-------------|
| **Example template** | `WORKFLOW.example.md` | Yes | Repo maintainer |
| **Local config** | `.cymphony/workflow.md` | No (gitignored) | Each operator |
| **CLI overrides** | `--workflow-path`, `--port`, `--log-level` | N/A | Per-invocation |

## Precedence rules

Config resolution follows highest-to-lowest priority:

```
1. CLI --workflow-path <path>     (explicit override, wins unconditionally)
2. .cymphony/workflow.md          (local generated config)
3. WORKFLOW.md                    (legacy committed file — deprecated)
4. → setup mode                   (no config found, launch setup screen)
```

When source (3) is used, a deprecation warning is logged on every
startup, guiding the operator to migrate.

### Server port override

The `--port` CLI flag overrides `server.port` from any config source.
This is unchanged from the existing behavior.

## File layout

```
repo/
├── WORKFLOW.example.md           # committed template (sanitized, no secrets)
├── .cymphony/
│   └── workflow.md               # local config (gitignored, created by setup or migration)
├── .gitignore                    # includes .cymphony/
└── .env                          # env vars (already gitignored)
```

## First-run experience

When a new operator clones the repo and runs `cymphony --port 8080`:

1. `resolve_config_source()` finds no `--workflow-path`, no
   `.cymphony/workflow.md`, no `WORKFLOW.md`.
2. Returns `(path=.cymphony/workflow.md, source=SETUP_REQUIRED)`.
3. `load_workflow()` raises `WorkflowError` (file not found).
4. Startup enters **setup mode** → serves the setup screen on the
   configured port.
5. The setup form is pre-populated from `_DEFAULT_SETUP_FORM` defaults.
6. On save, the form writes to `.cymphony/workflow.md` (the parent
   directory is created automatically).
7. Operator restarts Cymphony → config is found at layer 2 → normal
   operation.

If `WORKFLOW.example.md` exists in the repo, the setup screen could
optionally seed its defaults from that file. This is a follow-on
enhancement (not implemented in the initial PR).

## Steady-state experience

- The setup screen and `/settings` page read and write
  `.cymphony/workflow.md`.
- The `WorkflowWatcher` watches the resolved config path (layer 1, 2,
  or 3) for live-reload.
- Operators can still use `--workflow-path` to point at any file for
  testing or multi-environment scenarios.

## Migration path for existing users

### Automatic migration

On startup, before config resolution, `migrate_legacy_workflow()` runs:

1. If `.cymphony/workflow.md` already exists → **no-op** (already
   migrated or created by setup).
2. If `WORKFLOW.md` exists but `.cymphony/workflow.md` does not →
   **copy** `WORKFLOW.md` to `.cymphony/workflow.md` and log a warning.
3. If neither exists → **no-op** (setup mode will handle it).

The copy preserves the original `WORKFLOW.md` unchanged. The operator
can then:
- Add `.cymphony/` to `.gitignore` (already done in this PR for new repos).
- Rename `WORKFLOW.md` → `WORKFLOW.example.md` (strip operator-specific
  values).
- Or simply leave `WORKFLOW.md` in place — it will be ignored once
  `.cymphony/workflow.md` exists.

### Deprecation timeline

| Phase | Behavior |
|-------|----------|
| **Phase 1 (this PR)** | Auto-migrate on first run. Log deprecation warning when legacy path is used directly (no migration). |
| **Phase 2 (follow-on)** | Seed setup form from `WORKFLOW.example.md` when available. |
| **Phase 3 (future)** | Remove legacy `WORKFLOW.md` fallback. Require `.cymphony/workflow.md` or `--workflow-path`. |

## Setting classification

| Setting | Owner layer | Rationale |
|---------|-------------|-----------|
| `tracker.api_key` | Local config / env var | Operator-specific secret |
| `tracker.project_slug` | Local config | Operator-specific project binding |
| `tracker.assignee` | Local config | Operator-specific filter |
| `tracker.active_states` | Example template | Shared across operators |
| `tracker.terminal_states` | Example template | Shared across operators |
| `polling.interval_ms` | Example template | Repo default, operator can override |
| `workspace.root` | Local config | Machine-specific path |
| `agent.*` | Example template | Repo defaults |
| `codex.*` | Example template | Repo defaults |
| `hooks.*` | Example template | Repo-specific git operations |
| `server.port` | Local config / CLI | Machine-specific binding |
| `transitions.*` | Example template | Shared workflow definition |
| `prompt_template` | Example template | Repo-specific prompt |

The setup screen exposes all settings. The example template provides
sensible defaults. Operator-specific values are left blank in the
template (e.g., `project_slug: ""`, `assignee: ""`).

## Startup path pseudocode

```python
def main():
    args = parse_args()
    configure_logging(args.log_level)

    # Step 1: Migrate legacy config if needed
    if not args.workflow_path:
        migrate_legacy_workflow()

    # Step 2: Resolve config source with precedence
    workflow_path, source = resolve_config_source(args.workflow_path)

    # Step 3: Log source and warnings
    if source == LEGACY_COMMITTED:
        log.warning("Using deprecated WORKFLOW.md; migrate to .cymphony/workflow.md")
    else:
        log.info(f"Config source: {source}, path: {workflow_path}")

    # Step 4: Load .env from config directory
    load_dotenv(workflow_path)

    # Step 5: Load and validate
    try:
        workflow = load_workflow(workflow_path)
        config = build_config(workflow, server_port_override=args.port)
        validation = validate_dispatch_config(config)
        if not validation.ok:
            enter_setup_mode(workflow_path, validation.errors)
            return
    except WorkflowError:
        enter_setup_mode(workflow_path)
        return

    # Step 6: Run orchestrator
    orchestrator = Orchestrator(workflow_path, config, workflow)
    orchestrator.run()
```

## Validation and error behavior

| Scenario | Behavior |
|----------|----------|
| No config file found | Enter setup mode |
| Config file exists but has YAML errors | Enter setup mode with parse error |
| Config file exists but fails validation | Enter setup mode with validation errors |
| `.cymphony/workflow.md` and `WORKFLOW.md` both exist | `.cymphony/workflow.md` wins (precedence rule 2) |
| `--workflow-path` points to nonexistent file | Enter setup mode with file-not-found error |
| Legacy `WORKFLOW.md` found, no local config | Auto-migrate, then use local config |

## Follow-on implementation tasks

1. **Seed setup form from example template** — When entering setup mode,
   if `WORKFLOW.example.md` exists, use it to populate form defaults
   instead of hardcoded `_DEFAULT_SETUP_FORM`.

2. **Add `cymphony init` CLI command** — Copies `WORKFLOW.example.md` →
   `.cymphony/workflow.md` interactively, prompting for
   operator-specific values.

3. **Remove legacy fallback (Phase 3)** — After sufficient adoption,
   remove `WORKFLOW.md` from the precedence chain. Log an error
   with migration instructions instead of silently falling back.

4. **Per-operator overlay support** — Allow `.cymphony/local.yml` to
   override specific keys from the main config without replacing
   the entire file. (Stretch goal, not required for initial milestone.)
