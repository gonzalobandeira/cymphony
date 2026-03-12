"""CLI entry point for Cymphony (spec §6.3 startup validation)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import build_config, validate_dispatch_config
from .workflow import load_workflow, resolve_workflow_path


def _configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cymphony",
        description="Orchestration service for Claude Code agents driven by Linear issues.",
    )
    parser.add_argument(
        "--workflow-path",
        metavar="PATH",
        default=None,
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="Enable HTTP server on this port (overrides server.port in WORKFLOW.md)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        metavar="LEVEL",
        help="Logging level (default: INFO)",
    )
    return parser.parse_args(argv)


async def _run(workflow_path: Path, server_port: int | None) -> None:
    from .logging_ import log
    from .orchestrator import Orchestrator
    from .server import start_server

    # Load initial workflow (may not exist on first run)
    try:
        workflow = load_workflow(workflow_path)
    except Exception as exc:
        workflow = None
        log.warning(f"action=workflow_load_failed error={exc}")

    # Build initial config (use a dummy workflow if loading failed)
    if workflow is not None:
        config = build_config(workflow, server_port_override=server_port)
        validation = validate_dispatch_config(config)
    else:
        validation = None

    config_valid = validation is not None and validation.ok

    if not config_valid:
        # If a port is available, start setup-only server instead of exiting
        effective_port = server_port or (
            build_config(workflow, server_port_override=None).server.port
            if workflow is not None
            else None
        )
        if effective_port is not None:
            if validation is not None:
                for err in validation.errors:
                    log.warning(f"action=startup_validation_failed error={err!r}")
            log.info(
                f"action=setup_mode "
                f"workflow_path={workflow_path} "
                f"port={effective_port} "
                "reason=config_invalid_or_missing"
            )
            _runner = await start_server(None, workflow_path, effective_port)
            try:
                await asyncio.Event().wait()
            finally:
                await _runner.cleanup()
            return
        # No port configured — cannot start setup UI, exit with error
        if validation is not None:
            for err in validation.errors:
                log.error(f"action=startup_validation_failed error={err!r}")
        else:
            log.error("action=startup_failed error='workflow missing and no port configured'")
        sys.exit(1)

    log.info(
        f"action=startup "
        f"workflow_path={workflow_path} "
        f"tracker_kind={config.tracker.kind} "
        f"project_slug={config.tracker.project_slug} "
        f"workspace_root={config.workspace.root} "
        f"poll_interval_ms={config.polling.interval_ms} "
        f"max_concurrent_agents={config.agent.max_concurrent_agents}"
    )

    orchestrator = Orchestrator(workflow_path=workflow_path, config=config, workflow=workflow)
    await orchestrator.run()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    _configure_logging(args.log_level)

    workflow_path = resolve_workflow_path(args.workflow_path)

    try:
        asyncio.run(_run(workflow_path, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
