"""Executable contracts for deterministic work-log output placement."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import multiprocessing
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".shared-llm/public/llm/common/common/planish_resolve.py"
PI_EXTENSION = ROOT / ".shared-llm/public/llm/pi/common/extensions/do-planish.ts"
SPEC = importlib.util.spec_from_file_location("planish_resolve_tested", SCRIPT)
assert SPEC and SPEC.loader
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)

ENV_VARS = ("WORK_LOG_DIR", "PLANISH_DIR", "WORK_LOG_HOST", "PLANISH_HOST")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every case states its own environment — inherit nothing."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ─── directory precedence ────────────────────────────────────────────────────


def test_explicit_dir_wins_and_expands_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORK_LOG_DIR", "ignored")
    monkeypatch.setenv("PLANISH_DIR", "ignored")
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: ignored\n")
    result = resolver.resolve(tmp_path, "New API", "plans/{date}/{slug}/{type}/v{n}")
    assert (
        result
        == tmp_path / "plans" / date.today().isoformat() / "new-api" / "plan" / "v1"
    )
    assert result.is_dir()


def test_work_log_dir_env_beats_legacy_env_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORK_LOG_DIR", "from-env/{slug}")
    monkeypatch.setenv("PLANISH_DIR", "legacy-env/{slug}")
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: from-config/{slug}\n")
    assert resolver.resolve(tmp_path, "Topic") == tmp_path / "from-env/topic"


def test_legacy_env_still_works_but_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLANISH_DIR", "legacy-env/{slug}")
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: from-config/{slug}\n")
    assert resolver.resolve(tmp_path, "Topic") == tmp_path / "legacy-env/topic"
    captured = capsys.readouterr()
    assert "$PLANISH_DIR is deprecated" in captured.err
    assert captured.out == ""


def test_config_without_work_log_key_is_skipped_and_walk_continues(
    tmp_path: Path,
) -> None:
    """A destination roster (~/.shared-llm.yaml) has no work_log: — it must not
    shadow the repo config nor stop the walk."""
    roster = "destinations:\n  - path: /somewhere/repo\n    harnesses: [cc, pi]\n"
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: docs/plans/{slug}\n")
    _write(tmp_path / "repo/.shared-llm.yaml", roster)
    nested = tmp_path / "repo/src/deep"
    nested.mkdir(parents=True)
    _write(nested / ".shared-llm.yaml", roster)
    assert resolver.resolve(nested, "Topic") == tmp_path / "docs/plans/topic"


def test_nearest_work_log_config_wins_over_an_outer_one(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: outer/{slug}\n")
    inner = tmp_path / "repo"
    _write(inner / ".shared-llm.yaml", "work_log:\n  dir: inner/{slug}\n")
    assert resolver.resolve(inner, "Topic") == inner / "inner/topic"


def test_config_is_relative_to_config_and_version_increments(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "src/deep"
    nested.mkdir(parents=True)
    _write(repo / ".shared-llm.yaml", "work_log:\n  dir: docs/plans/{slug}/v{n}\n")
    (repo / "docs/plans/topic/v1").mkdir(parents=True)
    assert resolver.resolve(nested, "Topic") == repo / "docs/plans/topic/v2"


def test_absolute_configured_dir_is_used_as_is(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    _write(
        tmp_path / "repo/.shared-llm.yaml",
        f"work_log:\n  dir: {target}/{{slug}}\n",
    )
    assert resolver.resolve(tmp_path / "repo", "Topic") == target / "topic"


def test_malformed_work_log_dir_fails_loud(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: []\n")
    with pytest.raises(ValueError, match="work_log.dir.*non-empty string"):
        resolver.resolve(tmp_path, "Topic")


def test_empty_work_log_dir_fails_loud(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", 'work_log:\n  dir: "   "\n')
    with pytest.raises(ValueError, match="work_log.dir.*non-empty string"):
        resolver.resolve(tmp_path, "Topic")


def test_work_log_that_is_not_a_mapping_fails_loud(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log: /var/tmp/plans\n")
    with pytest.raises(ValueError, match="work_log.*must be a mapping"):
        resolver.resolve(tmp_path, "Topic")


@pytest.mark.parametrize("body", ["work_log:\n", "work_log: {}\n"])
def test_empty_work_log_fails_loud(tmp_path: Path, body: str) -> None:
    """An empty block carries no dir and no host — taking the default silently
    is the fallback this contract exists to prevent."""
    _write(tmp_path / ".shared-llm.yaml", body)
    with pytest.raises(ValueError, match="work_log"):
        resolver.resolve(tmp_path, "Topic")


def test_work_log_flow_mapping_is_honored(tmp_path: Path) -> None:
    """Flow style is valid YAML — it must behave like the block form."""
    _write(tmp_path / ".shared-llm.yaml", 'work_log: {dir: "plans/{slug}"}\n')
    result = resolver.resolve(tmp_path, "Redesign Auth")
    assert result == tmp_path / "plans/redesign-auth"


def test_work_log_without_dir_takes_the_default(tmp_path: Path) -> None:
    """host-only config is legitimate — the dir falls back to the default."""
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  host: example-host\n")
    result = resolver.resolve(tmp_path, "Topic")
    default_root = str(Path("/var/tmp/work-log").resolve())
    assert str(result).startswith(default_root + "/")


# ─── legacy .planish.yaml fallback ───────────────────────────────────────────


def test_legacy_config_still_resolves_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    nested = repo / "src"
    nested.mkdir(parents=True)
    _write(repo / ".planish.yaml", "dir: docs/plans/{slug}\n")
    assert resolver.resolve(nested, "Topic") == repo / "docs/plans/topic"
    captured = capsys.readouterr()
    assert ".planish.yaml is deprecated" in captured.err
    assert "work_log.dir" in captured.err
    assert captured.out == ""


def test_work_log_config_beats_a_nearer_legacy_config(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: modern/{slug}\n")
    nested = tmp_path / "repo"
    _write(nested / ".planish.yaml", "dir: legacy/{slug}\n")
    assert resolver.resolve(nested, "Topic") == tmp_path / "modern/topic"


def test_malformed_legacy_dir_fails_loud(tmp_path: Path) -> None:
    _write(tmp_path / ".planish.yaml", "dir: []\n")
    with pytest.raises(ValueError, match='"dir" must be a non-empty string'):
        resolver.resolve(tmp_path, "Topic")


def test_default_is_outside_repo(tmp_path: Path) -> None:
    result = resolver.resolve(tmp_path, "Topic")
    # /var/tmp may itself be a symlink, so compare against the resolved root
    # rather than the literal string.
    default_root = str(Path("/var/tmp/work-log").resolve())
    assert str(result).startswith(default_root + "/")
    assert result.name == "topic"


def test_empty_topic_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="topic must be non-empty"):
        resolver.resolve(tmp_path, "   ")


# ─── review host ─────────────────────────────────────────────────────────────


def test_host_env_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        tmp_path / ".shared-llm.yaml",
        "work_log:\n  dir: plans/{slug}\n  host: config-host\n",
    )
    assert resolver.resolve_host(tmp_path) == "config-host"

    monkeypatch.setenv("PLANISH_HOST", "legacy-host")
    assert resolver.resolve_host(tmp_path) == "legacy-host"
    assert "$PLANISH_HOST is deprecated" in capsys.readouterr().err

    monkeypatch.setenv("WORK_LOG_HOST", "env-host")
    assert resolver.resolve_host(tmp_path) == "env-host"


def test_host_falls_back_to_legacy_config_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / ".planish.yaml", "dir: plans/{slug}\nhost: legacy-host\n")
    assert resolver.resolve_host(tmp_path) == "legacy-host"
    captured = capsys.readouterr()
    assert "work_log.host" in captured.err


def test_host_is_none_when_unconfigured(tmp_path: Path) -> None:
    assert resolver.resolve_host(tmp_path) is None


def test_work_log_config_without_host_does_not_reach_legacy(tmp_path: Path) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: plans/{slug}\n")
    _write(tmp_path / "repo/.planish.yaml", "host: legacy-host\n")
    assert resolver.resolve_host(tmp_path / "repo") is None


# ─── review URL ──────────────────────────────────────────────────────────────


def test_review_url_needs_both_url_base_and_serve_root(tmp_path: Path) -> None:
    """One key alone cannot name a URL — never guess one that 404s."""
    for body in (
        "work_log:\n  dir: docs/plans/{slug}\n  url_base: http://example-host:8089\n",
        "work_log:\n  dir: docs/plans/{slug}\n  serve_root: docs\n",
    ):
        _write(tmp_path / ".shared-llm.yaml", body)
        plan_dir = resolver.resolve(tmp_path, "Topic")
        assert resolver.resolve_review_url(tmp_path, plan_dir) is None


def test_review_url_maps_the_plan_dir_onto_the_served_root(tmp_path: Path) -> None:
    _write(
        tmp_path / ".shared-llm.yaml",
        "work_log:\n"
        "  dir: docs/plans/{slug}\n"
        "  url_base: http://example-host:8089/\n"
        "  serve_root: docs\n",
    )
    plan_dir = resolver.resolve(tmp_path, "Redesign Auth")
    # No artifact named: the URL is the DIRECTORY, and says so with a trailing
    # slash rather than looking like a page that will not load.
    assert (
        resolver.resolve_review_url(tmp_path, plan_dir)
        == "http://example-host:8089/plans/redesign-auth/"
    )
    # Named artifact: the URL is the page a reviewer actually opens.
    assert (
        resolver.resolve_review_url(tmp_path, plan_dir, "plan.html")
        == "http://example-host:8089/plans/redesign-auth/plan.html"
    )


def test_review_url_percent_encodes_every_path_component(tmp_path: Path) -> None:
    """A `#` in a configured directory is PATH, not a fragment; likewise a space
    and a literal `%`. Handing back the raw characters gives a URL the server
    never sees the whole of."""
    _write(
        tmp_path / ".shared-llm.yaml",
        "work_log:\n"
        '  dir: "docs/plans/#ticket 7/100%/{slug}"\n'
        "  url_base: http://example-host:8089\n"
        "  serve_root: docs\n",
    )
    plan_dir = resolver.resolve(tmp_path, "Redesign Auth")
    url = resolver.resolve_review_url(tmp_path, plan_dir, "plan.html")
    assert url == (
        "http://example-host:8089/plans/%23ticket%207/100%25/redesign-auth/plan.html"
    )
    split = urlsplit(url)
    assert split.fragment == "" and split.query == ""


def test_review_url_artifact_cannot_leave_the_plan_dir(tmp_path: Path) -> None:
    _write(
        tmp_path / ".shared-llm.yaml",
        "work_log:\n"
        "  dir: docs/plans/{slug}\n"
        "  url_base: http://example-host:8089\n"
        "  serve_root: docs\n",
    )
    plan_dir = resolver.resolve(tmp_path, "Topic")
    with pytest.raises(ValueError, match="must stay inside the plan directory"):
        resolver.resolve_review_url(tmp_path, plan_dir, "../other/plan.html")
    with pytest.raises(ValueError, match="non-empty file name"):
        resolver.resolve_review_url(tmp_path, plan_dir, "  ")


def test_review_url_is_none_when_the_plan_dir_escapes_the_served_root(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / ".shared-llm.yaml",
        "work_log:\n"
        "  dir: elsewhere/{slug}\n"
        "  url_base: http://example-host:8089\n"
        "  serve_root: docs\n",
    )
    plan_dir = resolver.resolve(tmp_path, "Topic")
    assert resolver.resolve_review_url(tmp_path, plan_dir) is None


def test_cli_reports_the_review_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        tmp_path / ".shared-llm.yaml",
        "work_log:\n"
        "  dir: docs/plans/{slug}\n"
        "  host: example-host\n"
        "  url_base: http://example-host:8089\n"
        "  serve_root: docs\n",
    )
    assert resolver.main(["--topic", "Topic", "--cwd", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["review_url"] == "http://example-host:8089/plans/topic/"

    assert (
        resolver.main(
            ["--topic", "Topic", "--cwd", str(tmp_path), "--artifact", "plan.html"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["review_url"] == "http://example-host:8089/plans/topic/plan.html"


def test_handing_an_existing_plan_dir_back_reuses_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asking for the URL of a page written later must not cost a new directory.
    A run that already holds its plan dir passes it back as `--dir`: the same
    directory comes out (no second `{n}` claim) with the artifact's URL."""
    _write(
        tmp_path / ".shared-llm.yaml",
        "work_log:\n"
        "  dir: docs/plans/{slug}/v{n}\n"
        "  url_base: http://example-host:8089\n"
        "  serve_root: docs\n",
    )
    run_dir = resolver.resolve(tmp_path, "Topic")
    assert run_dir.name == "v1"

    assert (
        resolver.main(
            [
                "--topic",
                "Topic",
                "--cwd",
                str(tmp_path),
                "--dir",
                str(run_dir),
                "--artifact",
                "plan/v2/plan.html",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan_dir"] == str(run_dir)
    assert (
        payload["review_url"]
        == "http://example-host:8089/plans/topic/v1/plan/v2/plan.html"
    )
    assert not (run_dir.parent / "v2").exists()


def test_cli_artifact_must_be_usable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        tmp_path / ".shared-llm.yaml",
        "work_log:\n"
        "  dir: docs/plans/{slug}\n"
        "  url_base: http://example-host:8089\n"
        "  serve_root: docs\n",
    )
    with pytest.raises(SystemExit) as exit_info:
        resolver.main(
            ["--topic", "Topic", "--cwd", str(tmp_path), "--artifact", "../escape.html"]
        )
    assert "must stay inside the plan directory" in str(exit_info.value)
    assert capsys.readouterr().out == ""


# ─── review URL, fetched over HTTP ───────────────────────────────────────────
#
# A review URL that does not load is worse than none: the reviewer follows it,
# gets a 404 or a directory listing, and the turn is wasted. These cases run a
# real static server over the resolved tree and fetch what the resolver hands
# back — the only check that proves the URL and the file agree.


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _static_server(root: Path) -> Iterator[str]:
    """`python3 -m http.server` over `root`; yields its base URL."""
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(root),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                urllib.request.urlopen(f"{base}/", timeout=1).close()
                break
            except urllib.error.URLError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        yield base
    finally:
        process.terminate()
        process.wait(timeout=15)


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode()


@pytest.mark.parametrize(
    "segment", ["plain", "#ticket", "with space", "100%done", "a&b=c"]
)
def test_review_url_fetches_the_named_artifact(tmp_path: Path, segment: str) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    with _static_server(root) as base:
        _write(
            tmp_path / ".shared-llm.yaml",
            "work_log:\n"
            f'  dir: {json.dumps(f"docs/plans/{segment}/{{slug}}")}\n'
            f"  url_base: {base}\n"
            "  serve_root: docs\n",
        )
        plan_dir = resolver.resolve(tmp_path, "Redesign Auth")
        body = f"<h1>plan for {segment}</h1>"
        (plan_dir / "plan.html").write_text(body)

        url = resolver.resolve_review_url(tmp_path, plan_dir, "plan.html")
        assert url is not None
        assert _fetch(url) == body

        # Without an artifact the same directory still answers — as a listing
        # that names the page, never as a URL that 404s.
        directory_url = resolver.resolve_review_url(tmp_path, plan_dir)
        assert directory_url is not None and directory_url.endswith("/")
        assert "plan.html" in _fetch(directory_url)


# ─── concurrent {n} allocation ───────────────────────────────────────────────


def _claim_worker(barrier, queue, cwd: str) -> None:  # pragma: no cover - subprocess
    barrier.wait()
    try:
        queue.put(str(resolver.resolve(Path(cwd), "Topic")))
    except Exception as error:  # noqa: BLE001 - reported to the parent verbatim
        queue.put(f"ERROR: {error}")


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="needs fork so children share the already-loaded resolver module",
)
def test_concurrent_callers_each_claim_their_own_version(tmp_path: Path) -> None:
    """Scan-then-create hands the same vN to everyone who scans first."""
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: plans/{slug}/v{n}\n")
    ctx = multiprocessing.get_context("fork")
    callers = 8
    barrier = ctx.Barrier(callers)
    queue = ctx.Queue()
    workers = [
        ctx.Process(target=_claim_worker, args=(barrier, queue, str(tmp_path)))
        for _ in range(callers)
    ]
    for worker in workers:
        worker.start()
    claimed = [queue.get(timeout=30) for _ in range(callers)]
    for worker in workers:
        worker.join(timeout=30)

    assert all(not entry.startswith("ERROR") for entry in claimed), claimed
    assert len(set(claimed)) == callers, claimed


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_stdout_stays_pure_json_while_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLANISH_DIR", str(tmp_path / "plans/{slug}"))
    monkeypatch.setenv("WORK_LOG_HOST", "env-host")
    assert resolver.main(["--topic", "Topic", "--cwd", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "host": "env-host",
        "plan_dir": str(tmp_path / "plans/topic"),
        "review_url": None,
    }
    assert "deprecated" in captured.err


def test_cli_fails_loudly_on_a_malformed_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / ".shared-llm.yaml", "work_log:\n  dir: []\n")
    with pytest.raises(SystemExit) as exit_info:
        resolver.main(["--topic", "Topic", "--cwd", str(tmp_path)])
    assert "work_log.dir" in str(exit_info.value)
    assert capsys.readouterr().out == ""


def test_pi_submit_requires_the_canonical_absolute_path() -> None:
    source = PI_EXTENSION.read_text()
    assert 'name: "planish_resolve_dir"' not in source
    assert "filePath must be the absolute path returned by planish_resolve.py" in source
    assert "const base = path.dirname(filePath)" in source
    assert "ctx?.cwd ?? process.cwd()" not in source
