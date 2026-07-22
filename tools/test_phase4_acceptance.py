"""Executable acceptance for Phase 4 of the UpAgent consolidation — "delete the second hub".

Phase 4's stated verification is a sentence:

    repository search finds no live Specialist Hub implementation or command; one UpAgent
    service accepts all work; the previously blocked phase can pass the specialist gate
    through the unified path.

`phase_4_report()` decides that sentence mechanically, as six criteria. Run it as a report at
any time, against any tree:

    python3 tools/test_phase4_acceptance.py            # exit 0 only when Phase 4 is done

WHERE THE LINE BETWEEN "LIVE" AND "SURVIVING VOCABULARY" IS DRAWN
-----------------------------------------------------------------
`rg specialist` is the wrong instrument. The roster is twelve generic engineering personas,
consultation survives Phase 4 as a concept, and the word goes on appearing in prose that is
SUPPOSED to survive. So no criterion here greps source text for a word. Each one resolves
something instead:

  * A justfile `import` names a directory that must exist: `just --summary` exits non-zero when
    it does not. The live module set is therefore an executable fact, not a reading of a file.
  * A recipe is live iff `just --summary` lists it.
  * A recipe's engine is read from the recipe BODY with `{{_VAR}}` expansion, so a startup
    dependency is what bring-up actually runs — not what the comment above it claims.
  * `consult_token` is looked for in the Python ABSTRACT SYNTAX TREE, never in file text.
    Comments never enter the AST at all, and module/class/function docstrings are located and
    excluded explicitly. What remains — string literals inside expressions, identifiers,
    argument names, function names — is code. The parser draws the line, not a heuristic:
    `recruiter.py`'s docstring reference to "the sibling specialist hub" and the stale comment
    about pinning `dedicated` are invisible to C4, while `"consult_token": uuid.uuid4().hex`
    is not. Today that is 6 code sites in `recruiter.py` against 30+ raw text matches.
  * A documented command is resolved to the module whose justfile DEFINES it, so the question
    asked is "which engine owns this door", not "does this word appear near it".

The one place a bare name is used is C2, and it is used as a PATH in the import graph — a
module directory, which is this kit's own unit of identity ("Tool modules are self-contained
directories (script + config + own justfile), imported below" — `justfile`). A markdown file
saying "specialist" is not a directory and does not register.

WHY DELETING PROSE CANNOT SATISFY THIS
--------------------------------------
Five criteria are absence checks, and absence is cheap. C6 is not. It runs the two migration
gates as real tests and requires them green. Those gates load the roster merge, render the
phone book, and submit answers to the citation contract through whichever module owns them.
Deleting the Specialist Hub with no working replacement makes them fail at import; deleting
THEM to quiet C6 makes C6 fail on the missing file. The only way to satisfy the whole set is
to have actually moved the capability.

CURRENT STATE — THIS RUNS TODAY AND SAYS WHERE THE MIGRATION STANDS
--------------------------------------------------------------------
Option (a), not a skip. The report runs against the real repository right now and reports
FAIL with the outstanding criteria named, which is the honest pre-demolition answer.
`PHASE_4_DEMOLITION_LANDED` below is the single knob, and it is a tripwire in both directions:
while it is False, `test_the_repository_matches_the_declared_migration_state` FAILS the moment
the demolition actually lands ("flip the knob"), and once flipped the same test enforces every
criterion forever. There is no setting of it under which this file passes both before and
after. The self-tests below run against synthetic trees and are the permanent proof that each
criterion bites.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Flip to True in the commit that lands the Phase 4 demolition. See the module docstring:
# leaving it False after the hub is gone is itself a failure.
PHASE_4_DEMOLITION_LANDED = True

# The module directory that must not survive, named as a path in the import graph.
SECOND_HUB = "specialist"
# The module that must be the one request door.
REQUEST_DOOR = "upagent"
# Bring-up, health and teardown of the always-up UpAgent services.
SERVICE_RECIPES = ("upagent-up", "upagent-status", "upagent-down")
# The command Phase 4 names for removal.
SECOND_HUB_COMMAND = "specialist-hub"

_LAYERS = ".shared-llm/public/layers/slash-commands/common/common"
# The documents that tell an agent which commands to run.
PROTOCOL_DOCS = (
    f"{_LAYERS}/phase-leader/command.md",
    f"{_LAYERS}/tui-control/command.md",
    f"{_LAYERS}/meta-runner-phase-protocol.md",
)
# The gates that must still pass through whichever module owns the capability after the move.
MIGRATION_GATES = (
    ".shared-llm/public/extensions/common/upagent/specialist_roster_contract_test.py",
    ".shared-llm/public/extensions/common/upagent/consult_answer_contract_test.py",
)

_EXTENSIONS = ".shared-llm/public/extensions"

_IMPORT = re.compile(r"^import\s+'([^']+)'", re.M)
_JUST_VARIABLE = re.compile(r"^(_[A-Z_]+)\s*:=\s*(.+)$", re.M)
_VAR_USE = re.compile(r"\{\{\s*(_[A-Z_]+)\s*\}\}")
# An engine invocation inside a recipe body, after variable expansion.
_ENGINE_PATH = re.compile(r"extensions/(?:common|this_repo)/([a-z0-9_-]+)/([a-z0-9_]+\.py)")
# A runnable command in a protocol document is always in code formatting.
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_CODE_FENCE = re.compile(r"```[a-z]*\n(.*?)```", re.S)
_JUST_COMMAND = re.compile(r"^just\s+([a-zA-Z0-9][a-zA-Z0-9_-]*)")
_RECIPE_DEF = re.compile(r"^([a-z][a-z0-9-]*)(?:\s+[^\n]*?)?:", re.M)


# --- report -------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One criterion's verdict plus the evidence it was decided on."""

    criterion: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class Report:
    findings: tuple[Finding, ...]

    @property
    def passed(self) -> bool:
        return all(f.passed for f in self.findings)

    @property
    def outstanding(self) -> tuple[str, ...]:
        return tuple(f.criterion for f in self.findings if not f.passed)

    def of(self, criterion: str) -> Finding:
        """The finding for one criterion. Fail-loud when the name is not a criterion."""
        for finding in self.findings:
            if finding.criterion == criterion:
                return finding
        raise KeyError(f"no such criterion: {criterion!r} (have {self.outstanding or 'all pass'})")

    def render(self) -> str:
        width = max(len(f.criterion) for f in self.findings)
        lines = [
            "",
            f"Phase 4 acceptance — {'PASS' if self.passed else 'FAIL'} "
            f"({len(self.findings) - len(self.outstanding)}/{len(self.findings)} criteria met)",
            "",
        ]
        for f in self.findings:
            lines.append(f"  {'PASS' if f.passed else 'FAIL'}  {f.criterion.ljust(width)}  {f.evidence}")
        lines.append("")
        return "\n".join(lines)


# --- resolvable facts ---------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _just_summary(root: Path) -> tuple[frozenset[str], str]:
    """The recipe set `just` actually resolves, plus the error when it resolves nothing.

    A dangling `import` makes this exit non-zero, which is why removing a module's directory
    while its import survives is caught here rather than by reading the justfile.
    """
    try:
        proc = subprocess.run(
            ["just", "--justfile", str(root / "justfile"), "--summary"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:  # pragma: no cover - `just` is a hard prerequisite of the kit
        return frozenset(), "the `just` CLI is not on PATH"
    if proc.returncode != 0:
        return frozenset(), (proc.stderr or proc.stdout).strip().splitlines()[0]
    return frozenset(proc.stdout.split()), ""


def _live_modules(root: Path) -> dict[str, Path]:
    """Tool-module directories the root justfile imports, keyed by module name."""
    return {
        (root / rel).parent.name: (root / rel).parent
        for rel in _IMPORT.findall(_read(root / "justfile"))
    }


def _recipe_body(text: str, recipe: str) -> str | None:
    """The indented body of `recipe`, or None when this justfile does not define it."""
    header = re.compile(rf"^{re.escape(recipe)}\b[^\n]*:\s*$")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not header.match(line):
            continue
        body: list[str] = []
        for following in lines[i + 1 :]:
            if following.strip() and not following[:1].isspace():
                break
            body.append(following)
        return "\n".join(body).strip("\n")
    return None


def _expand(justfile_text: str, body: str) -> str:
    """Substitute `{{_VAR}}` from the justfile's own assignments, so a recipe body reveals the
    engine it runs rather than the variable name it runs it through."""
    variables = dict(_JUST_VARIABLE.findall(justfile_text))
    for _ in range(5):  # assignments may reference other assignments
        expanded = _VAR_USE.sub(lambda m: variables.get(m.group(1), m.group(0)), body)
        if expanded == body:
            break
        body = expanded
    return body


def _engines_in(root: Path, module: str, recipe: str) -> set[tuple[str, str]] | None:
    """`(module, engine.py)` pairs a recipe launches, or None when the recipe is not defined."""
    text = _read(root / _EXTENSIONS / "common" / module / "justfile")
    body = _recipe_body(text, recipe)
    if body is None:
        return None
    return set(_ENGINE_PATH.findall(_expand(text, body)))


def _all_justfiles(root: Path) -> list[Path]:
    return sorted((root / _EXTENSIONS).glob("*/*/justfile"))


def _defining_module(root: Path, recipe: str) -> str | None:
    """The module whose justfile defines `recipe`, or None when nothing does."""
    for justfile in _all_justfiles(root):
        if recipe in _RECIPE_DEF.findall(_read(justfile)):
            return justfile.parent.name
    return None


def _documented_commands(root: Path) -> set[str]:
    """Every `just <recipe>` the protocol documents tell an agent to run.

    Read from inline code spans and fenced blocks only — in bare prose "just run the tests" is
    English, not a recipe.
    """
    commands: set[str] = set()
    for rel in PROTOCOL_DOCS:
        text = _read(root / rel)
        snippets = _CODE_SPAN.findall(text)
        for block in _CODE_FENCE.findall(text):
            snippets.extend(block.splitlines())
        for snippet in snippets:
            match = _JUST_COMMAND.match(snippet.strip().lstrip("$ ").strip())
            if match:
                commands.add(match.group(1))
    return commands


def _live_python(root: Path) -> list[Path]:
    return [
        p
        for p in sorted((root / _EXTENSIONS).rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


def code_references(source: str, identifier: str) -> list[tuple[int, str]]:
    """`(line, kind)` for every CODE reference to `identifier` — never a prose one.

    Comments never reach the AST. Docstrings do, so they are located as the first statement of
    a module/class/function and excluded by node identity. Everything left is executable: a
    string literal in an expression, a name, an attribute, an argument, a definition.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = (getattr(node, "body", None) or [None])[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and identifier in node.value:
                hits.append((node.lineno, "string literal"))
        elif isinstance(node, ast.Name) and identifier in node.id:
            hits.append((node.lineno, "name"))
        elif isinstance(node, ast.Attribute) and identifier in node.attr:
            hits.append((node.lineno, "attribute"))
        elif isinstance(node, ast.arg) and identifier in node.arg:
            hits.append((node.lineno, "parameter"))
        # A call site passing `consult_token=...` is live code even when nothing else in the
        # file mentions the name. Missing this once let a fully "demolished" tree still hand
        # the token to a helper, so every binding position is enumerated, not just the obvious
        # ones: parameters AND call keywords, definitions AND imports.
        elif isinstance(node, ast.keyword) and node.arg and identifier in node.arg:
            hits.append((node.lineno, "call keyword"))
        elif isinstance(node, ast.alias) and identifier in (node.name, node.asname or ""):
            hits.append((node.lineno, "import"))
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and any(
            identifier in name for name in node.names
        ):
            hits.append((node.lineno, "declaration"))
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and identifier in node.name:
            hits.append((node.lineno, "definition"))
    return sorted(hits)


# --- the six criteria ---------------------------------------------------------


def _c1_single_service_bring_up(root: Path) -> Finding:
    """No second-hub startup dependency: bring-up, health and teardown run ONE engine module.

    Read from the expanded recipe bodies, so this is the service topology that actually
    executes. It is the criterion a half-demolition trips: deleting the hub while `upagent-up`
    still launches it, or leaving `upagent-status` reading a second runtime directory.
    """
    started: dict[str, set[str]] = {}
    missing: list[str] = []
    for recipe in SERVICE_RECIPES:
        engines = _engines_in(root, REQUEST_DOOR, recipe)
        if engines is None:
            missing.append(recipe)
            continue
        started[recipe] = {module for module, _ in engines}

    if missing:
        return Finding("single-service-bring-up", False, f"upagent/justfile defines no {missing}")

    detail = "; ".join(f"{r}: {sorted(started[r]) or 'no engine'}" for r in SERVICE_RECIPES)
    extra = sorted(set().union(*started.values()) - {REQUEST_DOOR})
    problems = []
    if extra:
        problems.append(f"a second service survives in {sorted(started)}: {extra}")
    # Bring-up must still bring the door UP. Without this, gutting `upagent-up` entirely would
    # satisfy "one service" by starting none — the check would be passable by deletion.
    if REQUEST_DOOR not in started["upagent-up"]:
        problems.append(f"`upagent-up` must start `{REQUEST_DOOR}`, starts {sorted(started['upagent-up'])}")

    if problems:
        return Finding("single-service-bring-up", False, f"{'; '.join(problems)} — {detail}")
    return Finding("single-service-bring-up", True, f"one service — {detail}")


def _c2_no_second_hub_module(root: Path) -> Finding:
    """No live Specialist Hub implementation: the module is neither imported nor on disk."""
    modules = _live_modules(root)
    on_disk = sorted(
        str(p.relative_to(root))
        for p in (root / _EXTENSIONS).glob(f"*/{SECOND_HUB}")
        if p.is_dir()
    )
    problems = []
    if SECOND_HUB in modules:
        problems.append(f"root justfile imports {modules[SECOND_HUB].relative_to(root)}/justfile")
    if on_disk:
        problems.append(f"directories still present: {on_disk}")
    if problems:
        return Finding("no-second-hub-module", False, "; ".join(problems))
    return Finding(
        "no-second-hub-module",
        True,
        f"live modules: {sorted(modules)}; no `{SECOND_HUB}` directory under {_EXTENSIONS}",
    )


def _c3_no_second_hub_command(root: Path) -> Finding:
    """No live `specialist-hub` command, and no recipe left pointing at a deleted engine."""
    recipes, error = _just_summary(root)
    if error:
        return Finding("no-second-hub-command", False, f"`just --summary` failed: {error}")

    problems = []
    if SECOND_HUB_COMMAND in recipes:
        problems.append(f"`just {SECOND_HUB_COMMAND}` is still a resolvable recipe")

    # A recipe that survives its engine is a command an agent can run into a crash.
    for justfile in _all_justfiles(root):
        text = _read(justfile)
        for module, engine in set(_ENGINE_PATH.findall(_expand(text, text))):
            for kind in ("common", "this_repo"):
                candidate = root / _EXTENSIONS / kind / module / engine
                if candidate.is_file():
                    break
            else:
                problems.append(
                    f"{justfile.relative_to(root)} runs {module}/{engine}, which does not exist"
                )

    if problems:
        return Finding("no-second-hub-command", False, "; ".join(sorted(set(problems))))
    return Finding(
        "no-second-hub-command",
        True,
        f"{len(recipes)} recipes resolve; none is `{SECOND_HUB_COMMAND}`; every engine path exists",
    )


def _c4_no_consult_token_mechanism(root: Path) -> Finding:
    """No live `consult_token` issuance or validation, decided on the AST rather than the text."""
    hits: list[str] = []
    for path in _live_python(root):
        try:
            references = code_references(_read(path), "consult_token")
        except SyntaxError as e:  # a source file that does not parse is its own problem
            return Finding("no-consult-token-mechanism", False, f"{path.relative_to(root)}: {e}")
        hits.extend(
            f"{path.relative_to(root)}:{line} ({kind})" for line, kind in references
        )
    if hits:
        head = "; ".join(hits[:4]) + (f"; +{len(hits) - 4} more" if len(hits) > 4 else "")
        return Finding("no-consult-token-mechanism", False, f"{len(hits)} code references — {head}")
    return Finding(
        "no-consult-token-mechanism",
        True,
        f"no code reference in {len(_live_python(root))} modules (prose and comments ignored)",
    )


def _c5_one_request_door(root: Path) -> Finding:
    """One UpAgent service accepts all work: every documented command resolves, and the engines
    that own them are live modules with the second hub absent."""
    recipes, error = _just_summary(root)
    if error:
        return Finding("one-request-door", False, f"`just --summary` failed: {error}")

    documented = _documented_commands(root)
    if not documented:
        return Finding("one-request-door", False, "the protocol documents name no runnable command")

    undefined = sorted(documented - recipes)
    owners = {c: _defining_module(root, c) for c in sorted(documented)}
    live = set(_live_modules(root))
    foreign = sorted({m for m in owners.values() if m is not None and m not in live})
    second_hub_doors = sorted(c for c, m in owners.items() if m == SECOND_HUB)

    problems = []
    if undefined:
        problems.append(f"documented but undefined: {undefined}")
    if second_hub_doors:
        problems.append(f"`{SECOND_HUB}` still owns {second_hub_doors}")
    if foreign:
        problems.append(f"owned by non-imported modules: {foreign}")
    if REQUEST_DOOR not in set(owners.values()):
        problems.append(f"no documented command is owned by `{REQUEST_DOOR}` — there is no door")

    if problems:
        return Finding("one-request-door", False, "; ".join(problems))
    return Finding(
        "one-request-door",
        True,
        f"{len(documented)} documented commands, all resolving, owned by "
        f"{sorted({m for m in owners.values() if m})}",
    )


def _c6_migrated_capability_still_enforced(root: Path) -> Finding:
    """The specialist gate passes through the unified path: the migration gates are green.

    This is the criterion deletion cannot satisfy. The gates exercise the roster merge, the
    rendered phone book and the answer citation contract against whichever module owns them.
    """
    missing = [rel for rel in MIGRATION_GATES if not (root / rel).is_file()]
    if missing:
        return Finding("migrated-capability-enforced", False, f"gate files missing: {missing}")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *MIGRATION_GATES, "-q", "-p", "no:cacheprovider"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stdout or proc.stderr).strip().splitlines()
        return Finding(
            "migrated-capability-enforced",
            False,
            f"migration gates failed (rc={proc.returncode}): {tail[-1] if tail else 'no output'}",
        )
    summary = [ln for ln in proc.stdout.splitlines() if "passed" in ln or "failed" in ln]
    return Finding("migrated-capability-enforced", True, summary[-1] if summary else "gates green")


CRITERIA = (
    _c1_single_service_bring_up,
    _c2_no_second_hub_module,
    _c3_no_second_hub_command,
    _c4_no_consult_token_mechanism,
    _c5_one_request_door,
    _c6_migrated_capability_still_enforced,
)


def phase_4_report(root: Path = ROOT) -> Report:
    """Decide every Phase 4 criterion against `root`."""
    return Report(tuple(criterion(root) for criterion in CRITERIA))


# --- the real repository ------------------------------------------------------


def test_the_repository_matches_the_declared_migration_state() -> None:
    """The knob and the repository must agree, in both directions.

    While `PHASE_4_DEMOLITION_LANDED` is False this fails the moment the demolition actually
    lands, which is what forces someone to flip it; once flipped it enforces all six criteria.
    There is no state in which it passes both before and after.
    """
    report = phase_4_report()

    if PHASE_4_DEMOLITION_LANDED:
        assert report.passed, "Phase 4 is declared landed but is not accepted:" + report.render()
    else:
        assert not report.passed, (
            "Phase 4 acceptance now PASSES against the real repository — the demolition has "
            "landed. Set PHASE_4_DEMOLITION_LANDED = True in this file so the criteria become "
            "binding instead of expected-to-fail." + report.render()
        )


def test_every_criterion_reaches_a_verdict_on_the_real_repository() -> None:
    """No criterion may error out or go silent on the tree it exists to judge — a crash inside
    a check would otherwise read the same as a clean tree once the knob is flipped."""
    report = phase_4_report()

    assert len(report.findings) == len(CRITERIA)
    assert all(f.evidence.strip() for f in report.findings)


# --- proof that each criterion bites ------------------------------------------
#
# Every criterion is exercised against a synthetic repository that is fully demolished, and
# then against one specific incomplete demolition. The fixture is the permanent version of the
# "prove it fails" demonstration: a criterion that stops biting fails its own test here.


_ENGINE_STUB = "print('engine')\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def demolished(tmp_path: Path) -> Path:
    """A synthetic repository in which Phase 4 is complete: one hub, one door, gates green."""
    root = tmp_path / "repo"
    ext = root / _EXTENSIONS

    _engine = "{{justfile_directory()}}/.shared-llm/public/extensions/common/upagent/recruiter.py"
    _write(ext / "common/upagent/recruiter.py", _ENGINE_STUB)
    _write(
        ext / "common/upagent/justfile",
        f"_UPAGENT := \"{_engine}\"\n"
        "\n"
        "upagent-up *F:\n"
        "    python3 {{_UPAGENT}} up {{F}}\n"
        "\n"
        "upagent-status:\n"
        "    python3 {{_UPAGENT}} status\n"
        "\n"
        "upagent-down:\n"
        "    python3 {{_UPAGENT}} down || true\n"
        "\n"
        "upagent-request *A:\n"
        "    python3 {{_UPAGENT}} request {{A}}\n"
        "\n"
        "upagent-consult *A:\n"
        "    python3 {{_UPAGENT}} consult {{A}}\n",
    )
    _write(ext / "common/runner/tui_controller.py", _ENGINE_STUB)
    _write(
        ext / "common/runner/justfile",
        "run-start D:\n"
        "    python3 {{justfile_directory()}}/.shared-llm/public/extensions/"
        "common/runner/tui_controller.py {{D}}\n",
    )
    _write(
        root / "justfile",
        "import '.shared-llm/public/extensions/common/upagent/justfile'\n"
        "import '.shared-llm/public/extensions/common/runner/justfile'\n"
        "\n"
        "default:\n"
        "    @just --list\n",
    )

    _write(
        root / PROTOCOL_DOCS[0],
        "Place the order with `just upagent-request <order.json>`.\n"
        "Ask a specialist with `just upagent-consult <consult.json>`.\n",
    )
    _write(root / PROTOCOL_DOCS[1], "Bring the service up with `just upagent-up`.\n")
    _write(root / PROTOCOL_DOCS[2], "The phone book names `just upagent-consult`.\n")

    for gate in MIGRATION_GATES:
        _write(root / gate, "def test_the_capability_survived() -> None:\n    assert True\n")
    return root


def test_a_complete_demolition_is_accepted(demolished: Path) -> None:
    """The control: without this, every failure below could be a check that never passes."""
    report = phase_4_report(demolished)

    assert report.passed, report.render()


def test_c1_catches_a_hub_deleted_while_bring_up_still_starts_it(demolished: Path) -> None:
    """The named scenario: the module is gone but `upagent-up` did not hear about it."""
    justfile = demolished / _EXTENSIONS / "common/upagent/justfile"
    justfile.write_text(
        justfile.read_text().replace(
            "upagent-up *F:\n    python3 {{_UPAGENT}} up {{F}}\n",
            "upagent-up *F:\n"
            "    python3 {{_UPAGENT}} up {{F}}\n"
            "    python3 {{justfile_directory()}}"
            "/.shared-llm/public/extensions/common/specialist/hub.py up || true\n",
        )
    )
    report = phase_4_report(demolished)

    assert not report.passed
    assert not report.of("single-service-bring-up").passed
    assert "specialist" in report.of("single-service-bring-up").evidence


def test_c1_catches_a_second_runtime_left_in_the_status_recipe(demolished: Path) -> None:
    """Teardown and health are checked too — a hub can survive in either alone."""
    justfile = demolished / _EXTENSIONS / "common/upagent/justfile"
    justfile.write_text(
        justfile.read_text().replace(
            "upagent-status:\n    python3 {{_UPAGENT}} status\n",
            "upagent-status:\n"
            "    python3 {{_UPAGENT}} status\n"
            "    runtime=$(python3 {{justfile_directory()}}"
            "/.shared-llm/public/extensions/common/specialist/hub.py runtime-dir)\n",
        )
    )
    report = phase_4_report(demolished)

    assert not report.of("single-service-bring-up").passed
    assert "upagent-status" in report.of("single-service-bring-up").evidence


def test_c2_catches_an_engine_directory_left_on_disk(demolished: Path) -> None:
    """Untracked leftovers count: an orphaned module directory is still an implementation."""
    _write(demolished / _EXTENSIONS / f"this_repo/{SECOND_HUB}/hub.py", _ENGINE_STUB)
    report = phase_4_report(demolished)

    assert not report.of("no-second-hub-module").passed
    assert "this_repo/specialist" in report.of("no-second-hub-module").evidence


def test_c3_catches_a_command_that_outlives_its_engine(demolished: Path) -> None:
    """The inverse half-demolition: the engine file is deleted but its recipe still runs it."""
    (demolished / _EXTENSIONS / "common/upagent/recruiter.py").unlink()
    report = phase_4_report(demolished)

    assert not report.of("no-second-hub-command").passed
    assert "does not exist" in report.of("no-second-hub-command").evidence


def test_c3_catches_a_dangling_import_that_breaks_every_recipe(demolished: Path) -> None:
    """Deleting a module without its import line takes the whole justfile down with it."""
    justfile = demolished / "justfile"
    justfile.write_text(
        f"import '{_EXTENSIONS}/common/{SECOND_HUB}/justfile'\n" + justfile.read_text()
    )
    report = phase_4_report(demolished)

    assert not report.of("no-second-hub-command").passed
    assert "just --summary` failed" in report.of("no-second-hub-command").evidence


def test_c3_catches_a_surviving_specialist_hub_recipe(demolished: Path) -> None:
    _write(
        demolished / _EXTENSIONS / f"common/{SECOND_HUB}/hub.py",
        _ENGINE_STUB,
    )
    _write(
        demolished / _EXTENSIONS / f"common/{SECOND_HUB}/justfile",
        f"{SECOND_HUB_COMMAND} *ARGS:\n"
        "    python3 {{justfile_directory()}}"
        f"/{_EXTENSIONS}/common/{SECOND_HUB}/hub.py {{{{ARGS}}}}\n",
    )
    justfile = demolished / "justfile"
    justfile.write_text(
        f"import '{_EXTENSIONS}/common/{SECOND_HUB}/justfile'\n" + justfile.read_text()
    )
    report = phase_4_report(demolished)

    assert not report.of("no-second-hub-command").passed
    assert SECOND_HUB_COMMAND in report.of("no-second-hub-command").evidence


def test_c4_catches_token_validation_left_behind(demolished: Path) -> None:
    """The other named scenario: the command is gone but the token door still guards orders."""
    _write(
        demolished / _EXTENSIONS / "common/upagent/contracts.py",
        "def parse_order(order: dict) -> dict:\n"
        '    if "consult_token" in order and not order["consult_token"]:\n'
        '        raise ValueError("order.json: `consult_token` must be a non-empty string")\n'
        "    return order\n",
    )
    report = phase_4_report(demolished)

    assert not report.of("no-consult-token-mechanism").passed
    assert "contracts.py:2" in report.of("no-consult-token-mechanism").evidence


def test_c4_catches_token_issuance_left_behind(demolished: Path) -> None:
    _write(
        demolished / _EXTENSIONS / "common/upagent/state.py",
        "import uuid\n\n"
        "def up() -> dict:\n"
        '    return {"consult_token": uuid.uuid4().hex}\n',
    )
    report = phase_4_report(demolished)

    assert not report.of("no-consult-token-mechanism").passed
    assert "state.py:4" in report.of("no-consult-token-mechanism").evidence


def test_c4_catches_the_token_passed_as_a_call_keyword(demolished: Path) -> None:
    """Found by running the check against a real half-demolished tree, not by design: a
    `helper(consult_token=...)` call site is live code that an AST walk over names, attributes
    and parameters alone does not see. `recruiter_test.py` has exactly this shape."""
    _write(
        demolished / _EXTENSIONS / "common/upagent/fixtures.py",
        "def build(path, **fields):\n"
        "    return fields\n"
        "\n"
        "def shaped(path):\n"
        '    return build(path, consult_token="issued-token")\n',
    )
    report = phase_4_report(demolished)

    assert not report.of("no-consult-token-mechanism").passed
    assert "call keyword" in report.of("no-consult-token-mechanism").evidence


def test_c1_is_not_satisfied_by_a_bring_up_that_starts_nothing(demolished: Path) -> None:
    """"One service" must mean one, not none — otherwise emptying `upagent-up` passes C1."""
    justfile = demolished / _EXTENSIONS / "common/upagent/justfile"
    justfile.write_text(
        justfile.read_text().replace("    python3 {{_UPAGENT}} up {{F}}\n", "    @true\n")
    )
    report = phase_4_report(demolished)

    assert not report.of("single-service-bring-up").passed
    assert "must start `upagent`" in report.of("single-service-bring-up").evidence


def test_c4_ignores_the_word_in_comments_and_docstrings(demolished: Path) -> None:
    """THE VOCABULARY LINE. Prose that records the removal must not read as the mechanism.

    This is the difference between this criterion and `rg consult_token`: a file that talks
    about the token at length, in every prose position Python has, is clean.
    """
    _write(
        demolished / _EXTENSIONS / "common/upagent/history.py",
        '"""Consults no longer carry a consult_token: the hub that validated it is gone."""\n'
        "\n"
        "# The consult_token round-trip was issued at `up` and validated on the order path.\n"
        "\n"
        "def dispatch(order: dict) -> dict:\n"
        '    """Self-authenticating — there is no consult_token to check."""\n'
        "    return order\n",
    )
    report = phase_4_report(demolished)

    assert report.of("no-consult-token-mechanism").passed
    assert report.passed, report.render()


def test_c5_catches_a_documented_command_the_second_hub_still_owns(demolished: Path) -> None:
    """Prose that keeps sending workers to the old door fails even when the door still exists."""
    _write(
        demolished / _EXTENSIONS / f"common/{SECOND_HUB}/hub.py",
        _ENGINE_STUB,
    )
    _write(
        demolished / _EXTENSIONS / f"common/{SECOND_HUB}/justfile",
        f"{SECOND_HUB_COMMAND} *ARGS:\n"
        "    python3 {{justfile_directory()}}"
        f"/{_EXTENSIONS}/common/{SECOND_HUB}/hub.py {{{{ARGS}}}}\n",
    )
    justfile = demolished / "justfile"
    justfile.write_text(
        f"import '{_EXTENSIONS}/common/{SECOND_HUB}/justfile'\n" + justfile.read_text()
    )
    doc = demolished / PROTOCOL_DOCS[0]
    doc.write_text(doc.read_text() + f"\nPaste `just {SECOND_HUB_COMMAND} roster` into the brief.\n")
    report = phase_4_report(demolished)

    assert not report.of("one-request-door").passed
    assert SECOND_HUB_COMMAND in report.of("one-request-door").evidence


def test_c5_catches_prose_pointing_at_a_command_that_no_longer_exists(demolished: Path) -> None:
    """The failure mode of deleting the engine and forgetting the brief that names it."""
    doc = demolished / PROTOCOL_DOCS[0]
    doc.write_text(doc.read_text() + "\nRun `just specialist-hub consult <file>`.\n")
    report = phase_4_report(demolished)

    assert not report.of("one-request-door").passed
    assert "documented but undefined" in report.of("one-request-door").evidence


def test_c6_catches_a_capability_deleted_rather_than_moved(demolished: Path) -> None:
    """DELETION IS NOT MIGRATION. A gate that no longer loads its implementation fails here,
    which is what stops the other five criteria from being satisfiable by `rm`."""
    (demolished / MIGRATION_GATES[0]).write_text(
        "import importlib.util\n"
        "\n"
        "_spec = importlib.util.spec_from_file_location('roster', 'specialist/hub.py')\n"
        "_spec.loader.exec_module(importlib.util.module_from_spec(_spec))\n"
        "\n"
        "def test_the_capability_survived() -> None:\n"
        "    assert True\n"
    )
    report = phase_4_report(demolished)

    assert not report.passed
    assert not report.of("migrated-capability-enforced").passed


def test_c6_catches_the_gates_themselves_being_deleted(demolished: Path) -> None:
    """The obvious way to quiet C6 is to remove the gates. That is the loudest failure it has."""
    (demolished / MIGRATION_GATES[1]).unlink()
    report = phase_4_report(demolished)

    assert not report.of("migrated-capability-enforced").passed
    assert "missing" in report.of("migrated-capability-enforced").evidence


if __name__ == "__main__":
    report = phase_4_report()
    print(report.render())
    sys.exit(0 if report.passed else 1)
