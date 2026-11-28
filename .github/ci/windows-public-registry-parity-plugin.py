"""Use ordinary registration and a fresh local authority for selected controls."""

import runpy

import pytest


@pytest.fixture(autouse=True)
def public_registry_parity(request, monkeypatch):
    from tests.support import agent_acceptance

    root = request.config.rootpath
    runner = runpy.run_path(str(root / ".github/ci/windows-public-ordinary-child.py"))
    monkeypatch.setattr(agent_acceptance, "builtin_registry", runner["ordinary_registry"])
    if request.node.path.name != "test_quality_materials_human_gate.py":
        yield
        return

    authority = agent_acceptance.initialize_quality_usage_authority()
    directory = authority.parent
    try:
        yield
    finally:
        (directory / "records").rmdir()
        (directory / "evidence").rmdir()
        (directory / "index.json").unlink()
        authority.unlink()
        directory.rmdir()
