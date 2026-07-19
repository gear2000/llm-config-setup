"""Contract tests for the merged specialist roster — who a worker can consult, and the block
that tells it so. Both must outlive whichever module owns the roster.

MIGRATION GATE. The roster lives in the Specialist Hub's `agents.yaml` today and moves into the
Recruiter's `upagent.yaml` when the hub folds in. That move crosses two loaders with DIFFERENT
semantics — the hub merges base under overlay, the Recruiter resolves its launch templates
first-match-wins — so the tests below pin the REQUIRED post-migration behavior rather than the
current address. The move is the SEAM block below and nothing else; every test is written
against behavior and should pass unchanged. Do not delete this file with the hub.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import subprocess
import sys

import pytest


MODULES = Path(__file__).resolve().parent.parent
# .../<repo>/.shared-llm/public/extensions/common -> <repo>. Identical in the kit and in every
# destination, so the phone book's commands are resolved against the justfile that owns them.
REPO_ROOT = MODULES.parents[3]

# ═══ THE SEAM ════════════════════════════════════════════════════════════════════════════
# Everything this file knows about WHERE the roster lives is in this block. When the roster
# moves under the Recruiter, change these values — here, only here. The arrow on each line is
# what it becomes; confirm each against the implementation rather than pasting it blind.
#
IMPLEMENTATION = "upagent/recruiter.py"       # the Recruiter owns the roster since Phase 3
ENGINE_DIR_ATTR = "HERE"                      # the module attribute holding the engine dir
LOAD_ROSTER = "load_specialist_roster"        # the Recruiter's SPECIALIST loader (not load_roster:
#                                             # that one resolves launch templates first-match-wins)
ENTRY_POINT = "main"                          # the module's argv entry point
ROSTER_KEY = "specialists"                    # the merged-list key
KIT_BASE_FILE = "specialists.yaml"            # beside the engine. NOT upagent.yaml: that file is
#                                             # destination-owned and deliberately unshipped, which
#                                             # is what makes its "roster not found" fail loud, and
#                                             # it is keyed by HARNESS and never decides the agent.
OVERLAY_PATH = ".shared-llm/this_repo/extensions/common/upagent/specialists.yaml"
SINGLE_FILE_ENV = "UPAGENT_CONFIG"            # cleared, not used: the specialist loader reads no
#                                             # env var, so nothing can drop the kit base
PHONE_BOOK_ARGV = ["specialists"]             # the subcommand that prints the phone book
OVERLAY_EXTRA = ""                            # runtime_dir was Specialist Hub state; there is none
# ═════════════════════════════════════════════════════════════════════════════════════════


_MOVED = """
SPECIALIST ROSTER CONTRACT — the implementation moved and this seam did not follow it.

    IMPLEMENTATION = {implementation!r}
    resolves to {path}
    which does not exist.

This file is a MIGRATION GATE, not a test of the Specialist Hub. It pins two things nothing
else in the repository checks: that a destination's roster is the kit base UNION its own
overlay (the Recruiter's first-match-wins loader would silently shrink it instead), and that
the phone book a leader pastes into every stage brief actually lists what the merge produced.

TO FIX: work down the SEAM block above — every line has an arrow saying what it becomes — then
run this file again. All tests below are written against behavior and should pass untouched.

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
            "    harness: claude\n"
            "    model: sonnet\n"
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
        OVERLAY_EXTRA.format(runtime=runtime_dir)
        + f"{ROSTER_KEY}:\n"
        "  - name: reviewer\n"
        "    description: repo reviewer with private routing\n"
        "    harness: claude\n"
        "    model: haiku\n"
        "    agent: reviewer\n"
        "  - name: payments\n"
        "    description: repo-only payments specialist\n"
        "    harness: claude\n"
        "    model: sonnet\n"
        "    agent: payments\n"
    )


def _merged_specialists() -> dict[str, dict]:
    """The effective roster a destination sees, keyed by specialist name."""
    return {a["name"]: a for a in getattr(_owner, LOAD_ROSTER)()[ROSTER_KEY]}


def _roster_metadata() -> dict:
    """The loader's own account of the merge: what it overrode, and the root it anchored on."""
    return getattr(_owner, LOAD_ROSTER)()


def _render_phone_book(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    """Run the command a phase leader pastes into every stage brief, and return its output."""
    monkeypatch.setattr(sys, "argv", [IMPLEMENTATION, *PHONE_BOOK_ARGV])
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

    assert merged["reviewer"]["model"] == "haiku"
    assert merged["reviewer"]["description"] == "repo reviewer with private routing"


def test_a_repo_only_specialist_is_added_to_the_kit_roster(
    kit_base_plus_repo_overlay: None,
) -> None:
    merged = _merged_specialists()

    assert merged["payments"]["agent"] == "payments"
    assert merged["docs"]["model"] == "sonnet"


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
        OVERLAY_EXTRA.format(runtime=tmp_path / "runtime")
        + f"{ROSTER_KEY}:\n"
        "  - name: essayist\n"
        "    description: |\n"
        "      " + "long first line " * 30 + "\n"
        "      second line that must never appear\n"
        "    harness: claude\n"
        "    model: sonnet\n"
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

    assert named <= set(summary.stdout.split()), f"phone book names undefined: {sorted(named)}"
