"""Phase 2 of the v2 rewrite: the runbook and design tools (laya_plan.md
§3.1, §3.2), against a real SQLite engine. Episodes pull these one entry at
a time instead of carrying them in every prompt, so the index line, the
upserts and the size cap are what matter."""
from __future__ import annotations

import pytest

from JFI.models import SessionRecord, get_engine, get_session
from JFI.tool.design_tools import design_get, design_index, design_set, make_design_tools
from JFI.tool.result_cap import cap_result
from JFI.tool.runbook_tools import make_runbook_tools, runbook_get, runbook_index, runbook_set


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(tmp_path)
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="s", repo_path="."))
        db.commit()
    return engine


# ------------------------------------------------------------------ runbook

def test_runbook_upsert_and_get(engine):
    assert "Added" in runbook_set(engine, "s", "run", "uv run uvicorn app.main:app", notes="port 8000")
    assert runbook_get(engine, "s", "run") == ("run (unverified): uv run uvicorn app.main:app\n"
                                               "  notes: port 8000")
    assert "Updated" in runbook_set(engine, "s", "run", "uv run uvicorn app.main:app --port 9000")
    assert "--port 9000" in runbook_get(engine, "s", "run")


def test_changing_a_command_resets_verified(engine):
    runbook_set(engine, "s", "test", "pytest", verified=True)
    assert "(verified)" in runbook_get(engine, "s", "test")
    runbook_set(engine, "s", "test", "uv run pytest")
    assert "(unverified)" in runbook_get(engine, "s", "test")
    runbook_set(engine, "s", "test", "uv run pytest", verified=True)
    assert "(verified)" in runbook_get(engine, "s", "test")


def test_runbook_index_is_one_line_of_names(engine):
    assert runbook_index(engine, "s") == "Runbook: empty."
    runbook_set(engine, "s", "setup", "uv sync", verified=True)
    runbook_set(engine, "s", "run", "uv run app")
    index = runbook_index(engine, "s")
    assert "\n" not in index
    assert "run, setup ✓" in index and "runbook_get(name)" in index


def test_runbook_refusals_start_with_error(engine):
    for result in (runbook_set(engine, "s", "Run It", "x"), runbook_set(engine, "s", "run", "  "),
                   runbook_get(engine, "s", "missing")):
        assert result.startswith("Error"), result


def test_runbook_is_per_session(engine):
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="other", repo_path="."))
        db.commit()
    runbook_set(engine, "s", "run", "npm run a")
    runbook_set(engine, "other", "run", "npm run b")
    assert runbook_get(engine, "s", "run").endswith(": npm run a")


# ------------------------------------------------------------------ design

def test_design_upsert_get_and_index(engine):
    design_set(engine, "s", "stack", "stack", "Python 3.12, FastAPI, SQLModel")
    design_set(engine, "s", "contract", "api->persistence", "api gets a session via get_session()")
    assert design_get(engine, "s", "contract", "api->persistence") == \
        "contract / api->persistence: api gets a session via get_session()"
    assert "Updated" in design_set(engine, "s", "stack", "stack", "Python 3.13")
    assert design_get(engine, "s", "stack", "stack").endswith("Python 3.13")
    index = design_index(engine, "s")
    assert index.startswith("Design: contract: api->persistence; stack: stack")
    assert design_get(engine, "s") == index


def test_design_lookup_by_kind_or_key_alone(engine):
    design_set(engine, "s", "convention", "tests", "tests mirror app/ under tests/")
    design_set(engine, "s", "convention", "naming", "snake_case modules")
    by_kind = design_get(engine, "s", "convention")
    assert "naming" in by_kind and "tests" in by_kind
    assert design_get(engine, "s", key="naming") == "convention / naming: snake_case modules"


def test_design_refusals_start_with_error(engine):
    for result in (design_set(engine, "s", "wish", "k", "t"), design_set(engine, "s", "stack", "", "t"),
                   design_get(engine, "s", "contract", "missing"), design_get(engine, "s", "nope")):
        assert result.startswith("Error"), result


def test_factories_bind_the_session_and_role(engine):
    rb = make_runbook_tools(engine, "s", role="architect")
    ds = make_design_tools(engine, "s", role="architect")
    rb["runbook_set"]("build", "uv run build")
    ds["design_set"]("assumption", "users", "single user")
    assert rb["runbook_get"]("build").startswith("build (unverified)")
    assert ds["design_get"]("assumption", "users").endswith("single user")


# ------------------------------------------------------------------ size cap

def test_big_results_are_cut_with_a_hint(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "200")  # 800 characters
    text = "\n".join(f"line {i}: " + "x" * 40 for i in range(100))
    capped = cap_result(text, "Call runbook_get(name) for one entry.")
    assert len(capped) < 1000
    assert capped.endswith("Call runbook_get(name) for one entry.]")
    assert "truncated" in capped
    assert cap_result("short", "hint") == "short"


def test_runbook_listing_respects_the_cap(engine, monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "200")
    for i in range(40):
        runbook_set(engine, "s", f"step_{i}", "echo " + "y" * 60)
    assert "truncated" in runbook_get(engine, "s")


def test_runbook_refuses_kill_by_name_and_a_browser_as_e2e(engine):
    """Observed on the stui runs: `stop` was `taskkill /F /IM node.exe`
    (kills every Node process on the machine) even after the prompt said not
    to, and `e2e` was `start http://localhost:5173`, which checks nothing."""
    from JFI.tool.runbook_tools import runbook_set
    assert runbook_set(engine, "s", "stop", "taskkill /F /IM node.exe").startswith("Error")
    assert runbook_set(engine, "s", "stop", "pkill -f vite").startswith("Error")
    assert runbook_set(engine, "s", "e2e", "start http://localhost:5173").startswith("Error")
    assert not runbook_set(engine, "s", "stop", "taskkill /PID 4242 /F").startswith("Error")
    assert not runbook_set(engine, "s", "e2e", "npm run build && node scripts/check-dist.js").startswith("Error")


def test_runbook_commands_must_start_with_a_program(engine):
    """Observed on the stui run (gemma): e2e was the sentence "Compare current
    view with portfolio-dashboard.html visually" -- the reviewer runs e2e and
    reads its exit code, so prose can never pass or fail."""
    from JFI.tool.runbook_tools import runbook_set
    assert runbook_set(engine, "s", "e2e", "Compare current view with the original visually").startswith("Error")
    assert runbook_set(engine, "s", "test", "Run all the tests").startswith("Error")
    for name, command in (("e2e", "npm run build && node scripts/check.js"), ("test_one", "npx vitest run {test_id}"),
                          ("setup", "uv sync"), ("build", "./build.sh"), ("run", "python -m app"),
                          ("stop", "Ctrl+C in the dev server terminal"), ("view", "http://localhost:5173")):
        assert not runbook_set(engine, "s", name, command).startswith("Error"), (name, command)
