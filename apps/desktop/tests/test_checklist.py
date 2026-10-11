"""docs/design/PHASE-4-CHECKLIST.md and the tests that tick it stay in step.

Every item names its tests, and each of those tests carries the item's id as
``@pytest.mark.checklist(...)``; every id a test carries is an item of the checklist.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKLIST = HERE.parents[2] / "docs" / "design" / "PHASE-4-CHECKLIST.md"
ITEM = re.compile(r"^\| ([A-Z]-\d\d) \|")


def checklist_items() -> dict[str, tuple[list[str], str]]:
    """Each item's id -> (the tests named in its row, its tick)."""
    items: dict[str, tuple[list[str], str]] = {}
    for line in CHECKLIST.read_text(encoding="utf-8").splitlines():
        match = ITEM.match(line)
        if not match:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        tests = re.findall(r"`(test_[a-z0-9_]+)`", cells[-2])
        items[match[1]] = (tests, cells[-1])
    return items


def marked_tests() -> dict[str, set[str]]:
    """Each test function in this suite -> the checklist ids its marker names."""
    found: dict[str, set[str]] = {}
    for path in HERE.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            ids: set[str] = set()
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "checklist"
                ):
                    ids.update(arg.value for arg in decorator.args if isinstance(arg, ast.Constant))
            assert node.name not in found or not ids, f"{node.name} is defined twice"
            found.setdefault(node.name, set()).update(ids)
    return found


def test_every_item_is_ticked_by_tests_that_carry_its_id() -> None:
    items = checklist_items()
    tests = marked_tests()
    assert len(items) >= 90, "the checklist was not parsed"
    problems = []
    for item, (named, _tick) in items.items():
        if not named:
            problems.append(f"{item} names no test")
        for name in named:
            if name not in tests:
                problems.append(f"{item}: {name} does not exist")
            elif item not in tests[name]:
                problems.append(f"{item}: {name} does not carry @pytest.mark.checklist({item!r})")
    assert problems == []


def test_every_marker_names_an_item_and_is_listed_there() -> None:
    items = checklist_items()
    problems = []
    for name, ids in marked_tests().items():
        for item in ids:
            if item not in items:
                problems.append(f"{name} names {item}, which the checklist does not have")
            elif name not in items[item][0]:
                problems.append(f"{name} ticks {item} but the checklist row does not name it")
    assert problems == []
