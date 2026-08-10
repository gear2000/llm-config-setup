#!/usr/bin/env python3
"""Pure command grammar for the public ``just upagent ...`` façade."""

from __future__ import annotations

import argparse
import importlib.util
import re
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
        description="Submit and observe work through canonical per-command UpAgent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  just upagent lists --type offerings
  just upagent request --type worker --offering pi-gpt-5-6-sol --effort high \\
    --agent backend --prompt-file /abs/brief.md
  just upagent request --file /abs/request.json --cockpit-pane LIVE_PANE --wait --json
  just upagent get --request 01957f4e-7f7f-7f8b-9c42-6e7f52f9321a --json
  just upagent cancel --request ID --control-token-file /private/token
  just upagent cleanup --all-terminal --older-than-seconds 86400
  just upagent await-any --request ID --cursor '{"ID": 12}'
""",
        add_help=True,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("help", help="print this complete grammar; launches nothing")
    up = sub.add_parser("up", help="ensure the optional services status pane")
    up.add_argument("--separate-workspaces", action="store_true")

    status = sub.add_parser("status", help="show runtime or request state")
    status.add_argument("--request")
    status.add_argument("--json", action="store_true")

    get = sub.add_parser(
        "get", help="read one request state and its retained artifact pointers"
    )
    get.add_argument("--request", required=True)
    get.add_argument("--json", action="store_true")

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
    request.add_argument("--duration-minutes", type=int)
    request.add_argument("--keep-open", action="store_true", default=None)
    request.add_argument(
        "--cockpit-pane",
        metavar="LIVE_PANE",
        help="invocation-only live caller-pane override; excluded from the request payload",
    )
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
    verify.add_argument(
        "--effort",
        help="required for effortful offerings; omit for default-only offerings",
    )
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

    review_await = sub.add_parser(
        "review-await",
        help="block until a retained worker publishes its next checkpoint",
    )
    review_await.add_argument("--request", required=True)
    review_await.add_argument("--after", type=int, default=0)
    review_await.add_argument("--timeout-ms", type=int, default=600_000)
    review_await.add_argument("--json", action="store_true")

    review_continue = sub.add_parser(
        "review-continue", help="send feedback to one retained worker checkpoint"
    )
    review_continue.add_argument("--request", required=True)
    review_continue.add_argument("--checkpoint", required=True, type=int)
    review_continue.add_argument("--checkpoint-sha256", required=True)
    review_continue.add_argument("--prompt-file", required=True)
    review_continue.add_argument("--control-token-file", required=True)
    review_continue.add_argument("--json", action="store_true")

    review_release = sub.add_parser(
        "review-release",
        help="release one retained worker to publish its terminal result",
    )
    review_release.add_argument("--request", required=True)
    review_release.add_argument("--checkpoint", required=True, type=int)
    review_release.add_argument("--checkpoint-sha256", required=True)
    review_release.add_argument("--control-token-file", required=True)
    review_release.add_argument("--json", action="store_true")

    cancel = sub.add_parser(
        "cancel", help="cancel one owned request without a timeout nonce"
    )
    cancel.add_argument("--request", required=True)
    cancel.add_argument("--control-token-file", required=True)
    cancel.add_argument("--json", action="store_true")

    cleanup = sub.add_parser(
        "cleanup", help="dry-run or prune successfully terminal request history"
    )
    selection = cleanup.add_mutually_exclusive_group(required=True)
    selection.add_argument("--request")
    selection.add_argument("--all-terminal", action="store_true")
    cleanup.add_argument("--older-than-seconds", type=int, default=0)
    cleanup.add_argument("--apply", action="store_true")
    cleanup.add_argument("--json", action="store_true")

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
            "duration_minutes",
            "keep_open",
        )
        if args.file is not None and any(
            getattr(args, field) is not None for field in defining
        ):
            raise PublicCommandError(
                "--file is mutually exclusive with every request-defining flag; --cockpit-pane, --wait, and --json remain allowed"
            )
        if args.file is None and args.type is None:
            raise PublicCommandError(
                "request requires either --file or --type with named request flags"
            )
        if args.duration_minutes is not None and not 1 <= args.duration_minutes <= 120:
            raise PublicCommandError("--duration-minutes must be between 1 and 120")
        if args.file is None and args.type == "worker":
            # --effort is resolved per-offering downstream: effortful offerings
            # reject an omitted effort before launch; default-only offerings
            # normalize the omission to the canonical "default" selection.
            missing = [
                field
                for field in ("offering", "agent", "prompt_file")
                if getattr(args, field) is None
            ]
            if missing or args.specialist is not None:
                raise PublicCommandError(
                    "worker request requires --offering, --agent, and --prompt-file "
                    "(--effort when the offering is effortful) "
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
    if args.command == "review-await" and (args.after < 0 or args.timeout_ms <= 0):
        raise PublicCommandError(
            "review-await requires --after >= 0 and --timeout-ms > 0"
        )
    if args.command in ("review-continue", "review-release"):
        if args.checkpoint <= 0:
            raise PublicCommandError("--checkpoint must be a positive integer")
        if re.fullmatch(r"[0-9a-f]{64}", args.checkpoint_sha256) is None:
            raise PublicCommandError("--checkpoint-sha256 must be lowercase SHA-256")
    if args.command == "await" and args.notify_after_ms < 0:
        raise PublicCommandError("--notify-after-ms must be zero or greater")
    if args.command == "await-any" and args.timeout_ms <= 0:
        raise PublicCommandError("--timeout-ms must be a positive integer")
    if args.command == "respond":
        if args.action == "cancel" and args.extension_ms != 0:
            raise PublicCommandError("cancel requires --extension-ms 0")
        if args.action == "extend" and args.extension_ms <= 0:
            raise PublicCommandError("extend requires a positive --extension-ms")
    if args.command == "cleanup" and args.older_than_seconds < 0:
        raise PublicCommandError("--older-than-seconds must be zero or greater")
    return args


def help_text() -> str:
    return build_parser().format_help()
