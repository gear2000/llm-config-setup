"""Contract tests for the merged specialist roster.

The roster defines who a worker can consult and the phone-book block that tells it so. Both must
outlive whichever module owns the roster. The Recruiter owns the current seam; these tests pin the
required merged-roster behavior rather than the implementation address.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest


MODULES = Path(__file__).resolve().parent.parent
# .../<repo>/.shared-llm/public/extensions/common -> <repo>. Identical in the kit and in every
# destination, so the phone book's commands are resolved against the justfile that owns them.
REPO_ROOT = MODULES.parents[3]

# ═══ THE SEAM ════════════════════════════════════════════════════════════════════════════
# Everything this file knows about WHERE the roster lives is in this block. If the roster moves
# again, change these values — here, only here — and confirm each against the implementation.
#
IMPLEMENTATION = "upagent/recruiter.py"  # the Recruiter owns the roster since Phase 3
ENGINE_DIR_ATTR = "HERE"  # the module attribute holding the engine dir
LOAD_ROSTER = (
    "load_specialist_roster"  # the Recruiter's SPECIALIST loader (not load_roster:
)
#                                             # that one resolves launch templates first-match-wins)
ENTRY_POINT = "main"  # the module's argv entry point
ROSTER_KEY = "specialists"  # the merged-list key
KIT_BASE_FILE = "specialists.yaml"  # beside the engine. NOT upagent.yaml: that file is
#                                             # destination-owned and deliberately unshipped, which
#                                             # is what makes its "roster not found" fail loud, and
#                                             # it is keyed by HARNESS and never decides the agent.
OVERLAY_PATH = ".shared-llm/this_repo/extensions/common/upagent/specialists.yaml"
SINGLE_FILE_ENV = "UPAGENT_CONFIG"  # cleared, not used: the specialist loader reads no
#                                             # env var, so nothing can drop the kit base
PHONE_BOOK_ARGV = ["specialists"]  # the subcommand that prints the phone book
OVERLAY_EXTRA = ""  # legacy runtime_dir overlay state is gone
# ═════════════════════════════════════════════════════════════════════════════════════════


_MOVED = """
SPECIALIST ROSTER CONTRACT — the implementation moved and this seam did not follow it.

    IMPLEMENTATION = {implementation!r}
    resolves to {path}
    which does not exist.

This file pins two things nothing else in the repository checks: that a destination's roster is
the kit base UNION its own overlay, and that the phone book a leader pastes into every stage
brief actually lists what the merge produced.

TO FIX: work down the SEAM block above, then run this file again. All tests below are written
against behavior and should pass untouched.

DO NOT delete this file to clear the error. The silent-roster-loss failure looks like a worker
simply never having a specialist to ask, and `tools/test_phase4_acceptance.py` fails its
`migrated-capability-enforced` criterion on the missing file.
"""


def _load_seam():
    """Import IMPLEMENTATION, or explain the repoint. Never skip: a silently disabled gate is
    the failure mode this whole file exists to prevent."""
    path = MODULES / IMPLEMENTATION
    if not path.is_file():
        raise RuntimeError(_MOVED.format(implementation=IMPLEMENTATION, path=path))
    spec = importlib.util.spec_from_file_location("specialist_roster_owner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for attribute in (ENGINE_DIR_ATTR, LOAD_ROSTER, ENTRY_POINT):
        if not hasattr(module, attribute):
            raise RuntimeError(
                f"{IMPLEMENTATION} has no {attribute!r} — update the SEAM block in {__file__}"
            )
    return module


_owner = _load_seam()


def _write_kit_base_roster(engine_dir: Path) -> None:
    """The roster the kit ships to every destination. The `.git` marker is not decoration: with
    no overlay the kit base becomes the primary roster, and the loader anchors `repo_root` by
    walking up from it — a real engine directory always sits inside a repository."""
    engine_dir.mkdir(parents=True, exist_ok=True)
    (engine_dir / ".git").mkdir(exist_ok=True)
    (engine_dir / KIT_BASE_FILE).write_text(
        f"{ROSTER_KEY}:\n"
        + "".join(
            f"  - name: {name}\n"
            f"    description: kit {name} specialist\n"
            "    offering: claude-sonnet-5\n"
            "    effort: medium\n"
            f"    agent: {name}\n"
            for name in ("docs", "reviewer", "database", "security")
        )
    )


def _write_repo_overlay_roster(repo_root: Path, runtime_dir: Path) -> None:
    """A destination's own roster: it redefines ONE kit specialist and adds ONE of its own."""
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    overlay = repo_root / OVERLAY_PATH
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        OVERLAY_EXTRA.format(runtime=runtime_dir) + f"{ROSTER_KEY}:\n"
        "  - name: reviewer\n"
        "    description: repo reviewer with private routing\n"
        "    offering: claude-opus-4-8\n"
        "    effort: high\n"
        "    agent: reviewer\n"
        "  - name: payments\n"
        "    description: repo-only payments specialist\n"
        "    offering: claude-sonnet-5\n"
        "    effort: medium\n"
        "    agent: payments\n"
    )


def _merged_specialists() -> dict[str, dict]:
    """The effective roster a destination sees, keyed by specialist name."""
    return {a["name"]: a for a in getattr(_owner, LOAD_ROSTER)()[ROSTER_KEY]}


def _roster_metadata() -> dict:
    """The loader's own account of the merge: what it overrode, and the root it anchored on."""
    return getattr(_owner, LOAD_ROSTER)()


def _render_phone_book(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    """Run the command a phase leader pastes into every stage brief, and return its output."""
    monkeypatch.setattr(sys, "argv", [IMPLEMENTATION, *PHONE_BOOK_ARGV])
    monkeypatch.setattr(_owner, "_require_hub_authority", lambda: None)
    getattr(_owner, ENTRY_POINT)()
    return capsys.readouterr().out


@pytest.fixture
def kit_base_plus_repo_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A destination that ships four kit specialists and overrides one of them."""
    engine_dir = tmp_path / "kit"
    repo_root = tmp_path / "repo"
    _write_kit_base_roster(engine_dir)
    _write_repo_overlay_roster(repo_root, tmp_path / "runtime")
    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(repo_root)


# --- the merge ----------------------------------------------------------------


def test_kit_specialists_survive_a_repo_overlay_that_names_only_some_of_them(
    kit_base_plus_repo_overlay: None,
) -> None:
    """THE ROSTER MERGE GATE — the effective roster is base UNION overlay, never first-match-wins.

    A destination that overrides ONE specialist must keep every other specialist the kit ships.
    If this is ever implemented as first-match-wins — the overlay file replacing the base file
    wholesale, which is how the Recruiter resolves its LAUNCH TEMPLATES and therefore the shape
    a migration is most likely to reach for — this destination silently drops `docs`, `database`
    and `security`. Its phone book shrinks from five specialists to two, with no error and no
    warning: the failure looks like a worker simply never having a specialist to ask.

    The explicit count is the tripwire. Do not relax it to a subset or membership check.
    """
    merged = _merged_specialists()

    assert len(merged) == 5
    assert set(merged) == {"docs", "reviewer", "database", "security", "payments"}


def test_an_overlay_entry_clobbers_the_same_named_kit_entry(
    kit_base_plus_repo_overlay: None,
) -> None:
    """Union, not concatenation: overriding a specialist replaces it rather than duplicating it."""
    merged = _merged_specialists()

    assert merged["reviewer"]["offering"] == "claude-opus-4-8"
    assert merged["reviewer"]["description"] == "repo reviewer with private routing"


def test_a_repo_only_specialist_is_added_to_the_kit_roster(
    kit_base_plus_repo_overlay: None,
) -> None:
    merged = _merged_specialists()

    assert merged["payments"]["agent"] == "payments"
    assert merged["docs"]["offering"] == "claude-sonnet-5"


def test_every_specialist_records_which_roster_produced_it(
    kit_base_plus_repo_overlay: None,
) -> None:
    """Provenance survives the merge. It is not decoration: it is what the phone book renders
    as the `(this repo)` marker, and it is the only way to answer "where did this entry come
    from" once base and overlay have been flattened into one list."""
    merged = _merged_specialists()

    assert merged["docs"]["origin"] == "kit-base"
    assert merged["database"]["origin"] == "kit-base"
    assert merged["reviewer"]["origin"] == "this-repo"
    assert merged["payments"]["origin"] == "this-repo"


def test_the_merge_reports_which_kit_entries_the_overlay_replaced(
    kit_base_plus_repo_overlay: None,
) -> None:
    """The merge's audit trail. Overriding a kit specialist is legitimate; doing it by accident
    — a name collision nobody intended — is how a destination loses the kit's behavior with no
    error. The loader has to say which names it replaced, or nothing can."""
    assert _roster_metadata()["overridden"] == ["reviewer"]


def test_the_merged_roster_anchors_on_the_repository_it_describes(
    kit_base_plus_repo_overlay: None, tmp_path: Path
) -> None:
    """`repo_root` is where a consult actually runs. A specialist answering questions about a
    repository from somewhere else reads a different tree than the one it is describing —
    the bug fixed in `fe96fba` (consults run in the roster's repo, not a frozen worktree path).
    The roster is what carries that anchor, so it has to survive the move with the roster."""
    assert _roster_metadata()["repo_root"] == tmp_path / "repo"


def test_a_destination_with_no_overlay_of_its_own_gets_the_whole_kit_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case, and the one a merge bug hides in: with nothing to merge, every kit
    specialist must still be present and attributed to the kit."""
    engine_dir = tmp_path / "kit"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write_kit_base_roster(engine_dir)
    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(elsewhere)

    merged = _merged_specialists()

    assert set(merged) == {"docs", "reviewer", "database", "security"}
    assert all(entry["origin"] == "kit-base" for entry in merged.values())


# --- the phone book -----------------------------------------------------------


def test_the_phone_book_lists_every_merged_specialist(
    kit_base_plus_repo_overlay: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The block a leader pastes into every brief is rendered and read, not grepped for in the
    prose that describes it. A worker can only consult a specialist it was told exists, so a
    merge that quietly shrinks the roster shrinks this block too."""
    book = _render_phone_book(monkeypatch, capsys)

    for name in _merged_specialists():
        assert f"**{name}**" in book


def test_the_phone_book_states_the_rule_and_the_receipt_it_promises(
    kit_base_plus_repo_overlay: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The protocol's claim is that a worker never has to discover consulting on its own —
    which holds only if the pasted block itself carries the rule, the receipt field and the
    evidence standard. Rendered here so the claim is checked against the renderer."""
    book = _render_phone_book(monkeypatch, capsys)

    assert "MANDATORY" in book
    assert "consults" in book
    assert "file:line" in book


def test_the_phone_book_carries_each_specialists_description(
    kit_base_plus_repo_overlay: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare list of names is not a phone book. The worker picks a specialist by what it
    covers, so the description is the entry — and it must be the MERGED one, not the kit's."""
    book = _render_phone_book(monkeypatch, capsys)

    assert "kit docs specialist" in book
    assert "repo-only payments specialist" in book
    # The overlay's description wins for a name it redefines.
    assert "repo reviewer with private routing" in book
    assert "kit reviewer specialist" not in book


def test_the_phone_book_marks_which_specialists_are_this_repos_own(
    kit_base_plus_repo_overlay: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Provenance reaches the worker, not just the loader. A repo-owned specialist is marked so
    a worker can tell local convention from what the kit ships everywhere."""
    book = _render_phone_book(monkeypatch, capsys)

    payments = next(line for line in book.splitlines() if "**payments**" in line)
    docs = next(line for line in book.splitlines() if "**docs**" in line)

    assert "(this repo)" in payments
    assert "(this repo)" not in docs


def test_the_phone_book_caps_an_essay_description_to_one_bounded_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """This block rides inside EVERY stage brief of every phase. An unbounded roster
    description is therefore not a cosmetic problem — it is context spent in every worker the
    run ever hires. One line per specialist, hard-capped, whatever the roster says."""
    engine_dir = tmp_path / "kit"
    repo_root = tmp_path / "repo"
    _write_kit_base_roster(engine_dir)
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    overlay = repo_root / OVERLAY_PATH
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        OVERLAY_EXTRA.format(runtime=tmp_path / "runtime") + f"{ROSTER_KEY}:\n"
        "  - name: essayist\n"
        "    description: |\n"
        "      " + "long first line " * 30 + "\n"
        "      second line that must never appear\n"
        "    offering: claude-sonnet-5\n"
        "    effort: medium\n"
        "    agent: essayist\n"
    )
    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(repo_root)

    book = _render_phone_book(monkeypatch, capsys)

    essayist = [line for line in book.splitlines() if "essayist" in line]
    assert len(essayist) == 1
    assert len(essayist[0]) < 260
    assert "second line that must never appear" not in book


# --- the include chain ---------------------------------------------------------


def _write_roster(
    path: Path, specialists: list[dict], *, include: list[str] | None = None
) -> None:
    """One roster document, written directly (not via the fixture's fixed shape) so include
    tests can compose arbitrary chains."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict = {"specialists": specialists}
    if include is not None:
        doc["include"] = include
    path.write_text(_owner.yaml.safe_dump(doc))


def _entry(name: str, **overrides: object) -> dict:
    entry = {
        "name": name,
        "description": f"{name} specialist",
        "offering": "claude-sonnet-5",
        "effort": "medium",
        "agent": name,
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def include_chain_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """kit base (docs, reviewer) -> included repo A (reviewer, payments) -> included repo B
    (payments) -> the primary overlay's own entries (payments). Every name but `docs` is
    redefined at least once, so the merge order is only provable if clobber precedence holds."""
    engine_dir = tmp_path / "kit"
    _write_kit_base_roster(engine_dir)  # docs, reviewer, database, security

    repo_a = tmp_path / "repo-a"
    (repo_a / ".git").mkdir(parents=True)
    roster_a = repo_a / OVERLAY_PATH
    _write_roster(
        roster_a,
        [
            _entry("reviewer", description="repo-a reviewer"),
            _entry("payments", description="repo-a payments"),
        ],
    )

    repo_b = tmp_path / "repo-b"
    (repo_b / ".git").mkdir(parents=True)
    roster_b = repo_b / OVERLAY_PATH
    _write_roster(roster_b, [_entry("payments", description="repo-b payments")])

    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    overlay = repo_root / OVERLAY_PATH
    _write_roster(
        overlay,
        [_entry("payments", description="primary payments")],
        include=[str(roster_a), str(roster_b)],
    )

    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(repo_root)
    return {"repo_a": repo_a, "repo_b": repo_b, "repo_root": repo_root}


def test_include_chain_merges_base_then_includes_in_order_then_primary(
    include_chain_world: dict,
) -> None:
    """Merge order: kit base -> each include in list order -> the overlay's own entries. The
    primary overlay's own `payments` must win over both included repos' `payments`, and repo-b's
    `reviewer`-free chain must still leave repo-a's `reviewer` clobbering the kit base one."""
    merged = _merged_specialists()

    assert set(merged) == {"docs", "reviewer", "database", "security", "payments"}
    assert merged["reviewer"]["description"] == "repo-a reviewer"
    assert merged["payments"]["description"] == "primary payments"


def test_include_chain_reports_every_clobbered_name(include_chain_world: dict) -> None:
    assert set(_roster_metadata()["overridden"]) == {"reviewer", "payments"}


def test_an_included_rosters_specialist_is_anchored_to_its_own_repo(
    include_chain_world: dict,
) -> None:
    """`reviewer` came from repo-a's included roster and must resolve `location`/launch cwd
    against repo-a, never the primary overlay's repo — the per-file anchoring this chain must
    preserve."""
    index = _owner._specialist_index(_roster_metadata())

    assert index["reviewer"]["repo_root"] == include_chain_world["repo_a"]
    assert index["payments"]["repo_root"] == include_chain_world["repo_root"]
    assert index["docs"]["repo_root"] == include_chain_world["repo_root"]


def test_a_relative_include_path_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_dir = tmp_path / "kit"
    _write_kit_base_roster(engine_dir)
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    overlay = repo_root / OVERLAY_PATH
    _write_roster(overlay, [_entry("payments")], include=["../elsewhere/specialists.yaml"])
    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(repo_root)

    with pytest.raises(_owner.RecruiterError, match="must be absolute"):
        getattr(_owner, LOAD_ROSTER)()


def test_a_missing_include_path_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_dir = tmp_path / "kit"
    _write_kit_base_roster(engine_dir)
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    overlay = repo_root / OVERLAY_PATH
    missing = tmp_path / "nowhere" / "specialists.yaml"
    _write_roster(overlay, [_entry("payments")], include=[str(missing)])
    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(repo_root)

    with pytest.raises(_owner.RecruiterError, match="does not exist"):
        getattr(_owner, LOAD_ROSTER)()


def test_a_nested_include_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Includes are one level deep: an included file that itself carries `include:` fails loud
    rather than being silently flattened or silently ignored."""
    engine_dir = tmp_path / "kit"
    _write_kit_base_roster(engine_dir)

    repo_a = tmp_path / "repo-a"
    (repo_a / ".git").mkdir(parents=True)
    roster_a = repo_a / OVERLAY_PATH
    nested_target = repo_a / "nested-specialists.yaml"
    _write_roster(nested_target, [_entry("nested")])
    _write_roster(roster_a, [_entry("payments")], include=[str(nested_target)])

    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    overlay = repo_root / OVERLAY_PATH
    _write_roster(overlay, [_entry("local")], include=[str(roster_a)])
    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(repo_root)

    with pytest.raises(_owner.RecruiterError, match="include"):
        getattr(_owner, LOAD_ROSTER)()


def test_an_include_naming_itself_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_dir = tmp_path / "kit"
    _write_kit_base_roster(engine_dir)
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    overlay = repo_root / OVERLAY_PATH
    _write_roster(overlay, [_entry("payments")], include=[str(overlay)])
    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(repo_root)

    with pytest.raises(_owner.RecruiterError, match="itself"):
        getattr(_owner, LOAD_ROSTER)()


def test_a_duplicate_include_path_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_dir = tmp_path / "kit"
    _write_kit_base_roster(engine_dir)

    repo_a = tmp_path / "repo-a"
    (repo_a / ".git").mkdir(parents=True)
    roster_a = repo_a / OVERLAY_PATH
    _write_roster(roster_a, [_entry("payments")])

    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    overlay = repo_root / OVERLAY_PATH
    _write_roster(overlay, [_entry("local")], include=[str(roster_a), str(roster_a)])
    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(repo_root)

    with pytest.raises(_owner.RecruiterError, match="more than once"):
        getattr(_owner, LOAD_ROSTER)()


def test_an_aggregator_only_overlay_with_no_local_specialists_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`include:` with entries but no local `specialists:` list is legitimate — a repo that only
    aggregates other repos' rosters."""
    engine_dir = tmp_path / "kit"
    _write_kit_base_roster(engine_dir)

    repo_a = tmp_path / "repo-a"
    (repo_a / ".git").mkdir(parents=True)
    roster_a = repo_a / OVERLAY_PATH
    _write_roster(roster_a, [_entry("payments")])

    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    overlay = repo_root / OVERLAY_PATH
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(_owner.yaml.safe_dump({"include": [str(roster_a)]}))
    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(repo_root)

    merged = _merged_specialists()

    assert "payments" in merged
    assert "docs" in merged


def test_an_included_roster_with_no_local_specialists_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The aggregator relaxation applies only to the overlay that carries `include:`; every file
    it includes still needs a non-empty `specialists:` list."""
    engine_dir = tmp_path / "kit"
    _write_kit_base_roster(engine_dir)

    repo_a = tmp_path / "repo-a"
    (repo_a / ".git").mkdir(parents=True)
    roster_a = repo_a / OVERLAY_PATH
    roster_a.parent.mkdir(parents=True, exist_ok=True)
    roster_a.write_text(_owner.yaml.safe_dump({"specialists": []}))

    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    overlay = repo_root / OVERLAY_PATH
    _write_roster(overlay, [_entry("local")], include=[str(roster_a)])
    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(repo_root)

    with pytest.raises(_owner.RecruiterError, match="non-empty"):
        getattr(_owner, LOAD_ROSTER)()


def test_the_kit_base_roster_may_not_define_include(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the discovered this_repo overlay may carry `include:`. If the kit base itself names
    one it is rejected, whether or not any overlay is present."""
    engine_dir = tmp_path / "kit"
    (engine_dir / ".git").mkdir(parents=True)
    other = tmp_path / "other-repo-specialists.yaml"
    _write_roster(other, [_entry("payments")])
    _write_roster(
        engine_dir / KIT_BASE_FILE, [_entry("docs")], include=[str(other)]
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv(SINGLE_FILE_ENV, raising=False)
    monkeypatch.setattr(_owner, ENGINE_DIR_ATTR, engine_dir)
    monkeypatch.chdir(elsewhere)

    with pytest.raises(_owner.RecruiterError, match="include"):
        getattr(_owner, LOAD_ROSTER)()


def test_every_command_the_phone_book_names_resolves_to_a_real_recipe(
    kit_base_plus_repo_overlay: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The loop the protocol depends on, closed: the block tells a worker to run a command, and
    that command must exist. Pinning the literal `just specialist-hub consult` would only pin
    today's address — this pins the property, so the door may be renamed but never dangle.

    A worker cannot recover from a phone book that names a command `just` does not have: the
    consult simply fails, and the mandatory-consult rule silently stops being enforceable.
    """
    book = _render_phone_book(monkeypatch, capsys)

    named = set(re.findall(r"`just ([a-z0-9][a-z0-9-]*)", book))
    assert named, "the phone book names no runnable command — a worker cannot act on it"

    summary = subprocess.run(
        ["just", "--justfile", str(REPO_ROOT / "justfile"), "--summary"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert named <= set(summary.stdout.split()), (
        f"phone book names undefined: {sorted(named)}"
    )
