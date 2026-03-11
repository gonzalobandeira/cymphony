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

    # Load initial workflow
    try:
        workflow = load_workflow(workflow_path)
    except Exception as exc:
        log.error(f"action=startup_failed error={exc}")
        sys.exit(1)

    # Build initial config
    config = build_config(workflow, server_port_override=server_port)

    # Startup validation (spec §6.3)
    validation = validate_dispatch_config(config)
    if not validation.ok:
        for err in validation.errors:
            log.error(f"action=startup_validation_failed error={err!r}")
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
