#!/usr/bin/env python3
"""Pure command grammar for the public ``just upagent ...`` façade."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

HERE = Path(__file__).resolve().parent
_runtime_name = "upagent_command_runtime"
if _runtime_name in sys.modules:
    command_runtime = sys.modules[_runtime_name]
else:
    _runtime_spec = importlib.util.spec_from_file_location(
        _runtime_name, HERE / "command_runtime.py"
    )
    if _runtime_spec is None or _runtime_spec.loader is None:
        raise RuntimeError("could not load UpAgent command runtime")
    command_runtime = importlib.util.module_from_spec(_runtime_spec)
    sys.modules[_runtime_name] = command_runtime
    _runtime_spec.loader.exec_module(command_runtime)


class PublicCommandError(ValueError):
    """The public command grammar is invalid."""


class _Parser(command_runtime.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise PublicCommandError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="just upagent",
        description="Submit and observe work through the canonical machine-local UpAgent Hub.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  just upagent lists --type offerings
  just upagent request --type worker --offering pi-gpt-5-6-sol --effort high \\
    --agent backend --prompt-file /abs/brief.md
  just upagent request --file /abs/request.json --wait --json
  just upagent await --request 01957f4e-7f7f-7f8b-9c42-6e7f52f9321a
  just upagent await-any --request ID --cursor '{"ID": 12}'
""",
        add_help=True,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("help", help="print this complete grammar; launches nothing")
    up = sub.add_parser("up", help="discover or start the canonical Hub and services")
    up.add_argument("--separate-workspaces", action="store_true")

    status = sub.add_parser("status", help="show Hub or request state")
    status.add_argument("--request")
    status.add_argument("--json", action="store_true")

    lists = sub.add_parser("lists", help="list offerings, specialists, or workers")
    lists.add_argument(
        "--type", required=True, choices=("offerings", "specialists", "workers")
    )
    lists.add_argument(
        "--status", choices=("active", "terminal", "all"), default="active"
    )
    lists.add_argument("--json", action="store_true")

    request = sub.add_parser(
        "request",
        help=(
            "submit one closed-schema request; return after worker health while "
            "advisory manager failure degrades"
        ),
    )
    request.add_argument("--file")
    request.add_argument("--type", choices=("worker", "specialist"))
    request.add_argument("--request-id")
    request.add_argument("--offering")
    request.add_argument("--effort")
    request.add_argument("--agent")
    request.add_argument("--specialist")
    request.add_argument("--prompt-file")
    request.add_argument("--cwd")
    request.add_argument("--wait", action="store_true")
    request.add_argument("--json", action="store_true")

    await_parser = sub.add_parser(
        "await", help="block for one request decision or terminal result"
    )
    await_parser.add_argument("--request", required=True)
    await_parser.add_argument("--notify-after-ms", type=int, default=600_000)
    await_parser.add_argument("--json", action="store_true")

    await_any = sub.add_parser(
        "await-any", help="wait until any watched request changes"
    )
    await_any.add_argument("--request", required=True, action="append")
    await_any.add_argument(
        "--cursor",
        default="{}",
        help="JSON object mapping each request id to its last integer sequence",
    )
    await_any.add_argument("--timeout-ms", type=int, default=600_000)
    await_any.add_argument("--json", action="store_true")

    verify = sub.add_parser(
        "verify", help="launch an independent review of a terminal request"
    )
    verify.add_argument("--request", required=True)
    verify.add_argument("--offering", required=True)
    verify.add_argument("--effort", required=True)
    verify.add_argument("--agent", required=True)
    verify.add_argument("--wait", action="store_true")
    verify.add_argument("--json", action="store_true")

    respond = sub.add_parser(
        "respond", help="answer the current fenced timeout decision"
    )
    respond.add_argument("--request", required=True)
    respond.add_argument("--control-token", required=True)
    respond.add_argument("--nonce", required=True)
    respond.add_argument("--action", required=True, choices=("extend", "cancel"))
    respond.add_argument("--extension-ms", required=True, type=int)
    respond.add_argument("--json", action="store_true")

    reconcile = sub.add_parser(
        "reconcile", help="reconcile dead or expired owned workers"
    )
    reconcile.add_argument("--json", action="store_true")
    return parser


def parse_argv(argv: Sequence[str]) -> argparse.Namespace:
    parser = build_parser()
    if not argv:
        return argparse.Namespace(command="help")
    args = parser.parse_args(list(argv))
    if args.command is None:
        args.command = "help"
    if args.command == "lists" and args.type != "workers" and args.status != "active":
        raise PublicCommandError("--status is valid only with lists --type workers")
    if args.command == "request":
        defining = (
            "type",
            "request_id",
            "offering",
            "effort",
            "agent",
            "specialist",
            "prompt_file",
            "cwd",
        )
        if args.file is not None and any(
            getattr(args, field) is not None for field in defining
        ):
            raise PublicCommandError(
                "--file is mutually exclusive with every request-defining flag; --wait and --json remain allowed"
            )
        if args.file is None and args.type is None:
            raise PublicCommandError(
                "request requires either --file or --type with named request flags"
            )
        if args.file is None and args.type == "worker":
            missing = [
                field
                for field in ("offering", "effort", "agent", "prompt_file")
                if getattr(args, field) is None
            ]
            if missing or args.specialist is not None:
                raise PublicCommandError(
                    "worker request requires --offering, --effort, --agent, and --prompt-file "
                    "and is incompatible with --specialist"
                )
        if (
            args.file is None
            and args.type == "specialist"
            and (
                args.specialist is None
                or args.prompt_file is None
                or any(
                    getattr(args, field) is not None
                    for field in ("offering", "effort", "agent")
                )
            )
        ):
            raise PublicCommandError(
                "specialist request requires --specialist and --prompt-file and is "
                "incompatible with --offering, --effort, and --agent"
            )
    if args.command == "await" and args.notify_after_ms < 0:
        raise PublicCommandError("--notify-after-ms must be zero or greater")
    if args.command == "await-any" and args.timeout_ms <= 0:
        raise PublicCommandError("--timeout-ms must be a positive integer")
    if args.command == "respond":
        if args.action == "cancel" and args.extension_ms != 0:
            raise PublicCommandError("cancel requires --extension-ms 0")
        if args.action == "extend" and args.extension_ms <= 0:
            raise PublicCommandError("extend requires a positive --extension-ms")
    return args


def help_text() -> str:
    return build_parser().format_help()
