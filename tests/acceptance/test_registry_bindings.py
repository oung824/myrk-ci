"""Public bindings for formal acceptance cases."""

from itertools import chain
from pathlib import Path

from tests.support.agent_acceptance import builtin_registry


def test_formal_registry_preserves_binding_order_and_discovers_current_material(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2]
    materials = source / "tests/acceptance/materials/deliverables"
    plans = source / "tests/acceptance/plans"
    original_iterdir = Path.iterdir

    def with_temporary_materials(directory):
        entries = original_iterdir(directory)
        return chain(entries, sorted(original_iterdir(tmp_path), reverse=True)) if directory == materials else entries

    last = tmp_path / "z.txt"
    first = tmp_path / "a.txt"
    added = tmp_path / "b.txt"
    last.touch()
    first.touch()
    monkeypatch.setattr(Path, "iterdir", with_temporary_materials)
    source_binding = source / "tests/support/process_receipt.py"
    trailing_bindings = (
        plans / "deepseek-key-behavior.json",
        plans / "gemini-key-compatibility.json",
        source / "tests/support/agent_acceptance/catalogue.py",
    )
    watched = {source_binding, *trailing_bindings, first, added, last}
    old_registry = builtin_registry()
    first.unlink()
    added.touch()
    new_registry = builtin_registry()
    for case_id in ("formal.question", "formal.restart"):
        before = old_registry.cases[case_id].bindings
        after = new_registry.cases[case_id].bindings
        assert tuple(path for path in before if path in watched) == (source_binding, first, last, *trailing_bindings)
        assert tuple(path for path in after if path in watched) == (source_binding, added, last, *trailing_bindings)
