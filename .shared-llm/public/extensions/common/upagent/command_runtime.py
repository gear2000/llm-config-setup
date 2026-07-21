#!/usr/bin/env python3
"""Request-local command context for concurrent in-process Hub dispatch."""

from __future__ import annotations

import argparse
import builtins
import io
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, TextIO


@dataclass(frozen=True)
class CommandContext:
    """Immutable caller context; output sinks are local to the dispatching context."""

    cwd: Path
    environ: Mapping[str, str]
    stdin: TextIO | None = None
    stdout: TextIO | None = None
    stderr: TextIO | None = None


_CONTEXT: ContextVar[CommandContext | None] = ContextVar(
    "upagent_command_context", default=None
)


def current_cwd() -> Path:
    context = _CONTEXT.get()
    return context.cwd if context is not None else Path.cwd().resolve()


def current_environ() -> Mapping[str, str]:
    context = _CONTEXT.get()
    return (
        context.environ if context is not None else MappingProxyType(dict(os.environ))
    )


def getenv(name: str, default: str | None = None) -> str | None:
    return current_environ().get(name, default)


def stdin_stream() -> TextIO:
    """Return request-local input; Hub targets never consume process stdin."""

    context = _CONTEXT.get()
    return context.stdin if context is not None and context.stdin else sys.stdin


def stdout_stream() -> TextIO:
    """Return the current request's stdout without mutating ``sys.stdout``."""

    context = _CONTEXT.get()
    return context.stdout if context is not None and context.stdout else sys.stdout


def stderr_stream() -> TextIO:
    """Return the current request's stderr without mutating ``sys.stderr``."""

    context = _CONTEXT.get()
    return context.stderr if context is not None and context.stderr else sys.stderr


class ArgumentParser(argparse.ArgumentParser):
    """Argument parser whose help, usage, and errors use request-local sinks."""

    def _print_message(self, message: str, file: Any = None) -> None:
        if not message:
            return
        if file is None or file is sys.stderr:
            destination = stderr_stream()
        elif file is sys.stdout:
            destination = stdout_stream()
        else:
            destination = file
        destination.write(message)


def command_print(*args: object, **kwargs: Any) -> None:
    """``print`` replacement whose default streams are scoped to one Hub request.

    Hub-owned background entry points explicitly install a context with no output sinks, so
    runner diagnostics use process streams rather than the response that started them.
    """

    context = _CONTEXT.get()
    requested = kwargs.get("file")
    if context is not None:
        if requested is None or requested is sys.stdout:
            kwargs["file"] = context.stdout or sys.stdout
        elif requested is sys.stderr:
            kwargs["file"] = context.stderr or sys.stderr
    builtins.print(*args, **kwargs)


def run_detached(call: Any, *args: object) -> Any:
    """Run a background entry point in an empty context on every supported Python build."""
    return Context().run(call, *args)


def write_stdout(value: str) -> None:
    stdout_stream().write(value)


def write_stderr(value: str) -> None:
    stderr_stream().write(value)


@contextmanager
def activate(
    cwd: Path,
    environ: Mapping[str, str],
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> Iterator[CommandContext]:
    context = CommandContext(
        cwd=cwd.resolve(),
        environ=MappingProxyType(dict(environ)),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)


@contextmanager
def capture_output() -> Iterator[tuple[io.StringIO, io.StringIO]]:
    """Nest structured output capture without touching process-global streams."""

    parent = _CONTEXT.get()
    cwd = parent.cwd if parent is not None else Path.cwd().resolve()
    environ = (
        parent.environ if parent is not None else MappingProxyType(dict(os.environ))
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    with activate(cwd, environ, stdout=stdout, stderr=stderr):
        yield stdout, stderr
