"""Every command in docker-compose.yml must reach the shell as a single line.

This is a lint, not a behaviour test, and it exists because of a real failure. A YAML folded
scalar (`>`) removes line breaks only on lines sitting at the block's base indentation. Indent a
continuation line to line it up nicely — which is the natural thing to do — and YAML keeps the
break. The db-bootstrap entrypoint was written that way and arrived at `sh` as four lines, the
second of them beginning `-v`, so the container exited 127 with "sh: -v: not found" and the stack
would not start.

Nothing about that is visible when reading the file; it only shows in what Compose resolves. So
the rule enforced here is the blunt one: no newline in any command or entrypoint, whatever style
produced it. Single-line argv lists satisfy it and cannot be indented back into a fault.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

COMPOSE_FILES = sorted(Path(__file__).resolve().parents[2].glob("docker-compose*.yml"))


def scripts() -> list[tuple[str, str, str, str]]:
    """(compose file, service, key, script) for every command and entrypoint defined."""
    found = []
    for path in COMPOSE_FILES:
        doc = yaml.safe_load(path.read_text()) or {}
        for service, spec in (doc.get("services") or {}).items():
            for key in ("command", "entrypoint"):
                value = spec.get(key)
                if value is None:
                    continue
                text = " ".join(value) if isinstance(value, list) else str(value)
                found.append((path.name, service, key, text))
    return found


def ids(case: tuple[str, str, str, str]) -> str:
    return f"{case[0]}:{case[1]}.{case[2]}"


assert COMPOSE_FILES, "no docker-compose file found — this test would silently pass"
CASES = scripts()
assert CASES, "no commands found in the compose files — the parser is wrong, not the files"


@pytest.mark.parametrize("case", CASES, ids=ids)
def test_command_is_a_single_line(case: tuple[str, str, str, str]) -> None:
    """A line break inside a command is a break in the shell, not a break on the page."""
    _, service, key, text = case
    assert "\n" not in text, (
        f"{service}.{key} spans lines. The shell will treat each as a separate command. "
        f"Write it as a single-line argv list:\n{text}"
    )


@pytest.mark.parametrize("case", CASES, ids=ids)
def test_shell_command_parses(case: tuple[str, str, str, str]) -> None:
    """`sh -n` on anything handed to `sh -c`, so a quoting mistake fails here and not at start-up."""
    _, service, key, text = case
    if " -c " not in f" {text} " or not text.startswith("sh"):
        pytest.skip(f"{service}.{key} is not an sh -c command")
    script = text.split(" -c ", 1)[1]
    sh = shutil.which("sh")
    assert sh, "no sh on PATH"
    result = subprocess.run([sh, "-n", "-c", script], capture_output=True, text=True)  # noqa: S603
    assert result.returncode == 0, f"{service}.{key} is not valid shell:\n{result.stderr}"
