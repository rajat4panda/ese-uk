"""Code-review rules enforced as tests (LLD 2.1, 4, 7.2, 10)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from app.models.enums import PG_ENUMS
from app.services.audit import ACTIONS

APP = Path(__file__).resolve().parents[2] / "app"
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _modules():
    for path in APP.rglob("*.py"):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_status_only_assigned_by_lifecycle_executor():
    """LLD 7.2: no code path other than the executor writes application.status."""
    offenders = []
    for path, tree in _modules():
        if path.name == "lifecycle.py":
            continue
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(
                node, ast.AugAssign | ast.AnnAssign) else []  # fmt: skip
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr == "status" and isinstance(t.value, ast.Name):
                    if t.value.id in {"app", "application", "a", "target"}:
                        offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert offenders == []


def test_services_have_no_http_imports():
    """LLD 2.1: services contain no FastAPI imports, so the worker can call them."""
    offenders = []
    for path in (APP / "services").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
                if any(n.split(".")[0] in {"fastapi", "starlette"} for n in names):
                    offenders.append(path.name)
    assert offenders == []


def test_every_audit_action_written_is_in_the_vocabulary():
    """LLD 10: the action list is closed."""
    written = set()
    for _path, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "write" and len(node.args) >= 3:
                arg = node.args[2]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    written.add(arg.value)
    assert written, "expected to find audit.write calls"
    assert written <= ACTIONS, written - ACTIONS


def test_unknown_audit_action_refused():
    import pytest

    from app.services.audit import UnknownAuditAction, write

    with pytest.raises(UnknownAuditAction):
        write(None, None, "application.teleport")  # type: ignore[arg-type]


def test_migration_enums_match_application_enums():
    """The Python enums must equal what the migrations actually build in Postgres.

    The baseline freezes the original lists; later migrations may extend one with ADD VALUE. Both
    are read here, so a value added in Python with no migration behind it still fails — which is
    the failure this guard exists to catch.
    """
    import re

    spec = importlib.util.spec_from_file_location("m0001", MIGRATIONS / "0001_baseline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    built = {name: list(values) for name, values in module.ENUMS.items()}

    add_value = re.compile(
        r"ALTER TYPE (\w+) ADD VALUE(?: IF NOT EXISTS)? '([^']+)'"
        r"(?:\s+(BEFORE|AFTER) '([^']+)')?",
        re.IGNORECASE,
    )
    for path in sorted(MIGRATIONS.glob("0*.py")):
        if path.name.startswith("0001_"):
            continue
        for enum_name, value, position, anchor in add_value.findall(path.read_text(encoding="utf-8")):
            values = built.setdefault(enum_name, [])
            if value in values:
                continue
            if anchor and anchor in values:
                at = values.index(anchor) + (0 if position.upper() == "BEFORE" else 1)
                values.insert(at, value)
            else:
                values.append(value)

    assert set(built) == set(PG_ENUMS)
    for name, cls in PG_ENUMS.items():
        assert tuple(built[name]) == tuple(m.value for m in cls), name


def test_migrations_are_forward_only():
    for path in MIGRATIONS.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert "raise NotImplementedError" in src, path.name


def test_reporting_deadline_is_only_stamped_at_creation():
    """LLD 8.2: a reporting deadline is evidence of what the deadline was, so nothing may move it.

    It is computed once, in events.create_event, from the working-day calendar. A later
    recalculation — however well meant — would change the answer to "was this reported in time?"
    after the fact.
    """
    offenders = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(
                node, ast.AugAssign | ast.AnnAssign) else []  # fmt: skip
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr == "deadline_date":
                    offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    # The only assignment is the keyword argument inside the ReportableEvent constructor, which is
    # not an attribute assignment, so any hit here is a mutation of an existing row.
    assert offenders == []


def test_every_scheduled_job_is_registered_with_the_scheduler():
    """A job that exists but is never scheduled is a control nobody is running (LLD 13)."""
    from app.workers.jobs.compliance import JOBS
    from app.workers.scheduler import SCHEDULE

    assert set(JOBS) == set(SCHEDULE), "JOBS and SCHEDULE have drifted apart"
