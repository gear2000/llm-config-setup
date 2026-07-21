#!/usr/bin/env python3
"""Canonical machine-local UpAgent Hub.

One process owns the discoverable Unix socket and authorizes every lifecycle command.  Commands
run only from this Hub's canonical engine directory; callers never import or execute a nearby
worktree Recruiter.  The Hub's exclusive lock is held for its entire lifetime.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import io
import json
import os
import signal
import socket
import sys
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, NamedTuple

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "upagent_hub_transport", HERE / "hub_transport.py"
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("could not load UpAgent Hub transport")
transport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(transport)

_runtime_spec = importlib.util.spec_from_file_location(
    "upagent_command_runtime", HERE / "command_runtime.py"
)
if _runtime_spec is None or _runtime_spec.loader is None:
    raise RuntimeError("could not load UpAgent command runtime")
command_runtime = importlib.util.module_from_spec(_runtime_spec)
sys.modules[_runtime_spec.name] = command_runtime
_runtime_spec.loader.exec_module(command_runtime)

AUTHORITY_ENV = "UPAGENT_HUB_INSTANCE_ID"
ENGINE_ENV = "UPAGENT_HUB_ENGINE_PATH"
HUB_PATH_ENV = "UPAGENT_HUB_PATH"
LOCK_FD_ENV = "UPAGENT_HUB_LOCK_FD"
PID_ENV = "UPAGENT_HUB_PID"
TARGETS = {
    "public": HERE / "public_api.py",
    "recruiter": HERE / "recruiter.py",
    "phase-controller": HERE / "phase_controller.py",
    "phase-await": HERE / "phase_await.py",
    "direct-controller": HERE / "direct_controller.py",
    "plan-controller": HERE.parent / "herdr" / "plan_controller.py",
    "run-lifecycle": HERE.parent / "herdr" / "run_lifecycle.py",
}
RECRUITER_TARGETS = frozenset(
    ("public", "recruiter", "phase-controller", "direct-controller")
)


class HubError(RuntimeError):
    """A Hub startup, identity, or dispatch fault."""


class CommandResult(NamedTuple):
    """Structured result returned by one request-local target invocation."""

    exit_code: int
    stdout: str
    stderr: str


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _strict_keys(value: dict[str, Any], expected: set[str], kind: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown or missing:
        raise transport.ProtocolError(
            f"{kind} frame has "
            + (f"unknown keys {', '.join(unknown)}" if unknown else "")
            + ("; " if unknown and missing else "")
            + (f"missing keys {', '.join(missing)}" if missing else "")
        )


class HubRuntime:
    """One locked socket server and its immutable canonical engine identity."""

    def __init__(self, socket_path: Path, engine_path: Path):
        self.socket_path = socket_path.resolve()
        self.engine_path = engine_path.resolve()
        self.hub_path = Path(__file__).resolve()
        if not self.engine_path.is_file() or self.engine_path.name != "recruiter.py":
            raise HubError(f"canonical engine path is invalid: {self.engine_path}")
        if self.engine_path.parent != self.hub_path.parent:
            raise HubError(
                "Hub and canonical Recruiter must come from the same engine directory"
            )
        self.instance_id = str(uuid.uuid4())
        self.pid = os.getpid()
        self.started_at_ns = time.time_ns()
        self.root = self.socket_path.parent
        # Derive ownership artifacts from the full override path. Independent sockets in the
        # same directory must not share a startup lock, service state, or ledger.
        self.lock_path = self.socket_path.with_name(f"{self.socket_path.name}.lock")
        self.identity_path = self.socket_path.with_name(
            f"{self.socket_path.name}.identity.json"
        )
        self.state_path = (
            Path(
                os.environ.get(
                    "UPAGENT_STATE",
                    str(
                        self.socket_path.with_name(
                            f"{self.socket_path.name}.recruiter.json"
                        )
                    ),
                )
            )
            .expanduser()
            .resolve()
        )
        self.ledger_path = (
            Path(
                os.environ.get(
                    "UPAGENT_HUB_DIR",
                    str(self.socket_path.with_name(f"{self.socket_path.name}.ledger")),
                )
            )
            .expanduser()
            .resolve()
        )
        self._lock_stream: Any = None
        self._server: socket.socket | None = None
        self._stopping = threading.Event()
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()
        self._module_lock = threading.RLock()
        self._target_modules: dict[str, Any] = {}
        self._recruiter_module: Any = None

    def _canonical_recruiter(self) -> Any:
        with self._module_lock:
            if self._recruiter_module is not None:
                return self._recruiter_module
            spec = importlib.util.spec_from_file_location(
                f"upagent_hub_recruiter_{self.instance_id.replace('-', '_')}",
                self.engine_path,
            )
            if spec is None or spec.loader is None:
                raise HubError(
                    f"could not load canonical Recruiter: {self.engine_path}"
                )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            module.__dict__["print"] = command_runtime.command_print
            module._bind_hub_runtime(
                self.ledger_path,
                self.state_path,
                self.identity(),
                self._lock_stream.fileno() if self._lock_stream is not None else None,
            )
            self._recruiter_module = module
            self._target_modules["recruiter"] = module
            return module

    def _load_target(self, target: str, script: Path) -> Any:
        with self._module_lock:
            module = self._target_modules.get(target)
            if module is not None:
                return module
            if target == "recruiter":
                return self._canonical_recruiter()
            spec = importlib.util.spec_from_file_location(
                f"upagent_hub_target_{target.replace('-', '_')}", script
            )
            if spec is None or spec.loader is None:
                raise HubError(f"could not load canonical Hub target: {script}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            module.__dict__["print"] = command_runtime.command_print
            if target in RECRUITER_TARGETS:
                binder = getattr(module, "_bind_recruiter_runtime", None)
                if binder is None:
                    raise HubError(
                        f"Hub target {target} cannot accept the canonical Recruiter"
                    )
                binder(self._canonical_recruiter())
            self._target_modules[target] = module
            return module

    def identity(self) -> dict[str, object]:
        herdr_session: object = "unbound"
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            state = None
        if isinstance(state, dict) and isinstance(state.get("herdr_session"), str):
            herdr_session = state["herdr_session"]
        return {
            "canonical_engine_path": str(self.engine_path),
            "herdr_session": herdr_session,
            "hub_instance_id": self.instance_id,
            "hub_path": str(self.hub_path),
            "ledger_path": str(self.ledger_path),
            "pid": self.pid,
            "process_start_time": transport.process_start_time(self.pid),
            "protocol_fingerprint": transport.PROTOCOL_FINGERPRINT,
            "protocol_version": transport.PROTOCOL_VERSION,
            "socket_path": str(self.socket_path),
            "started_at_ns": self.started_at_ns,
        }

    def _publish_identity(self) -> None:
        _write_json_atomic(self.identity_path, self.identity())
        os.chmod(self.identity_path, 0o644)

    def acquire(self) -> None:
        public_base = Path("/tmp/.upagent/hubs")
        if self.socket_path.is_relative_to(public_base):
            # The default transport is deliberately open to all machine-local users. Apply the
            # mode explicitly so a restrictive service umask cannot make the advertised 0666
            # socket unreachable through one of its parent directories.
            public_base.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(public_base.parent, 0o711)
            for directory in (public_base, self.root):
                directory.mkdir(parents=True, exist_ok=True)
                os.chmod(directory, 0o1777)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
        self._lock_stream = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._lock_stream.close()
            self._lock_stream = None
            raise HubError(f"another UpAgent Hub holds {self.lock_path}") from error
        self.socket_path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o666)
            server.listen(64)
            server.settimeout(0.25)
        except BaseException:
            server.close()
            self.socket_path.unlink(missing_ok=True)
            self.release()
            raise
        self._server = server
        self._publish_identity()

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        self.socket_path.unlink(missing_ok=True)
        self.identity_path.unlink(missing_ok=True)
        if self._lock_stream is not None:
            fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
            self._lock_stream.close()
            self._lock_stream = None

    def stop(self, *_unused: object) -> None:
        self._stopping.set()

    def _command_environment(self, caller_context: dict[str, str]) -> dict[str, str]:
        if self._lock_stream is None:
            raise HubError("Hub command attempted without the lifetime startup lock")
        separate_workspaces = "0"
        try:
            services_state = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            services_state = None
        if (
            isinstance(services_state, dict)
            and services_state.get("separate_workspaces") is True
        ):
            separate_workspaces = "1"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in transport.CALLER_CONTEXT_KEYS
            and key != transport.RAW_OWNER_TOKEN_ENV
        }
        return {
            **environment,
            **caller_context,
            AUTHORITY_ENV: self.instance_id,
            ENGINE_ENV: str(self.engine_path),
            HUB_PATH_ENV: str(self.hub_path),
            LOCK_FD_ENV: str(self._lock_stream.fileno()),
            PID_ENV: str(self.pid),
            "UPAGENT_HUB_DIR": str(self.ledger_path),
            "UPAGENT_STATE": str(self.state_path),
            "UPAGENT_SERVICES_SEPARATE_WORKSPACES": separate_workspaces,
            transport.SOCKET_ENV: str(self.socket_path),
        }

    def _invoke_target(
        self,
        module: Any,
        argv: list[str],
        cwd: Path,
        caller_context: dict[str, str],
        request_stdin: str | None,
    ) -> CommandResult:
        stdin = io.StringIO(request_stdin or "")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with command_runtime.activate(
            cwd,
            self._command_environment(caller_context),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        ):
            try:
                returned = module.main(argv)
            except SystemExit as error:
                if error.code is None:
                    returned = 0
                elif isinstance(error.code, int):
                    returned = error.code
                else:
                    command_runtime.write_stderr(f"{error.code}\n")
                    returned = 1
        return CommandResult(int(returned or 0), stdout.getvalue(), stderr.getvalue())

    def _dispatch(self, request: dict[str, Any]) -> dict[str, object]:
        _strict_keys(
            request,
            {
                "argv",
                "caller_context",
                "cwd",
                "protocol_fingerprint",
                "protocol_version",
                "stdin",
                "target",
                "type",
            },
            "request",
        )
        if request["type"] != "request":
            raise transport.ProtocolError("second frame must have type=request")
        if request["protocol_version"] != transport.PROTOCOL_VERSION:
            raise transport.ProtocolError(
                f"request protocol {request['protocol_version']!r} is incompatible with Hub protocol {transport.PROTOCOL_VERSION}"
            )
        if request["protocol_fingerprint"] != transport.PROTOCOL_FINGERPRINT:
            raise transport.ProtocolError(
                "request protocol fingerprint is incompatible"
            )
        target = request["target"]
        argv = request["argv"]
        request_stdin = transport.validate_request_stdin(target, argv, request["stdin"])
        caller_context = transport.validate_caller_context(request["caller_context"])
        cwd = request["cwd"]
        if not isinstance(target, str) or target not in {*TARGETS, "hub"}:
            raise transport.ProtocolError(f"unknown Hub target: {target!r}")
        if not isinstance(argv, list) or not all(
            isinstance(item, str) for item in argv
        ):
            raise transport.ProtocolError("request argv must be a list of strings")
        if (
            not isinstance(cwd, str)
            or not Path(cwd).is_absolute()
            or not Path(cwd).is_dir()
        ):
            raise transport.ProtocolError(
                "request cwd must be an existing absolute directory"
            )
        if target == "hub":
            if argv != ["status"]:
                raise transport.ProtocolError("Hub target supports only status")
            payload = self.identity()
            return {
                "exit_code": 0,
                "identity": payload,
                "stderr": "",
                "stdout": json.dumps(payload, indent=2, sort_keys=True) + "\n",
                "type": "response",
            }
        script = TARGETS[target].resolve()
        if not script.is_file():
            raise HubError(f"canonical Hub target is missing: {script}")
        if self._lock_stream is None:
            raise HubError("Hub lost its lifetime startup lock before dispatch")
        # Import registration is the only process-shared mutation and is guarded separately.
        # Command execution itself is concurrent: cwd, environment, and output are request-local.
        module = self._load_target(target, script)
        result = self._invoke_target(
            module, argv, Path(cwd), caller_context, request_stdin
        )
        self._publish_identity()
        return {
            "exit_code": result.exit_code,
            "identity": self.identity(),
            "stderr": result.stderr,
            "stdout": result.stdout,
            "type": "response",
        }

    def _handle(self, connection: socket.socket) -> None:
        stream: Any = None
        try:
            connection.settimeout(None)
            stream = connection.makefile("rwb", buffering=0)
            hello = transport.read_frame(stream)
            _strict_keys(
                hello,
                {"protocol_fingerprint", "protocol_version", "type"},
                "hello",
            )
            if hello["type"] != "hello":
                raise transport.ProtocolError("first frame must have type=hello")
            if hello["protocol_version"] != transport.PROTOCOL_VERSION:
                raise transport.ProtocolError(
                    f"client protocol {hello['protocol_version']!r} is incompatible with Hub protocol {transport.PROTOCOL_VERSION}"
                )
            if hello["protocol_fingerprint"] != transport.PROTOCOL_FINGERPRINT:
                raise transport.ProtocolError(
                    "client protocol fingerprint is incompatible"
                )
            transport.write_frame(
                stream,
                {
                    "identity": self.identity(),
                    "protocol_fingerprint": transport.PROTOCOL_FINGERPRINT,
                    "protocol_version": transport.PROTOCOL_VERSION,
                    "type": "hello",
                },
            )
            response = self._dispatch(transport.read_frame(stream))
            transport.write_frame(stream, response)
        except (HubError, transport.ProtocolError, OSError) as error:
            if stream is not None:
                with suppress(OSError):
                    transport.write_frame(
                        stream,
                        {
                            "error": str(error),
                            "protocol_fingerprint": transport.PROTOCOL_FINGERPRINT,
                            "protocol_version": transport.PROTOCOL_VERSION,
                            "type": "error",
                        },
                    )
        finally:
            if stream is not None:
                stream.close()
            connection.close()
            current = threading.current_thread()
            with self._threads_lock:
                self._threads.discard(current)

    def serve(self) -> None:
        self.acquire()
        try:
            while not self._stopping.is_set():
                assert self._server is not None
                try:
                    connection, _ = self._server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stopping.is_set():
                        break
                    raise
                thread = threading.Thread(
                    target=self._handle, args=(connection,), daemon=True
                )
                with self._threads_lock:
                    self._threads.add(thread)
                thread.start()
        finally:
            self.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="upagent-hub")
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    args = parser.parse_args(argv)
    runtime = HubRuntime(args.socket, args.engine)
    signal.signal(signal.SIGTERM, runtime.stop)
    signal.signal(signal.SIGINT, runtime.stop)
    runtime.serve()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HubError, transport.ProtocolError) as error:
        raise SystemExit(f"upagent-hub: {error}") from error
