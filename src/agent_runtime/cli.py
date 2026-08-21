"""CLI for local Agent execution and API serving."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Optional, Sequence

from agent_runtime.config import AppConfig
from agent_runtime.factory import create_runtime
from agent_runtime.models import AgentKind


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-runtime")
    parser.add_argument("--config", default="config/dev.json")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="execute one Agent run and wait for completion")
    run.add_argument("input")
    run.add_argument(
        "--pattern",
        choices=[kind.value for kind in AgentKind],
        default=AgentKind.SIMPLE.value,
    )
    run.add_argument("--session-id")
    run.add_argument("--audit", action="store_true")

    serve = commands.add_parser("serve", help="serve the Agent Runtime HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    return parser


async def _run_once(config: AppConfig, args) -> int:
    runtime = create_runtime(config)
    try:
        run = await runtime.submit(
            args.input,
            AgentKind(args.pattern),
            args.session_id,
        )
        await runtime.wait(run.id, timeout=config.runtime.run_timeout_seconds + 5)
        payload = run.to_dict()
        if args.audit:
            payload["audit"] = runtime.audit_events(run.trace_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if run.state.value == "succeeded" else 1
    finally:
        await runtime.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = AppConfig.load(args.config)
    if args.command == "run":
        return asyncio.run(_run_once(config, args))
    if args.command == "serve":
        try:
            import uvicorn
        except ImportError as exc:
            raise SystemExit("uvicorn is unavailable; install the api extra") from exc
        from agent_runtime.api import create_app

        uvicorn.run(create_app(args.config), host=args.host, port=args.port)
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
