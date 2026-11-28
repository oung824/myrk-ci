"""Provide the audited ordinary registration for a public behavior projection."""
from functools import lru_cache
import json

import pytest


def pytest_addoption(parser):
    parser.addoption("--myrk-split-group", required=True)


@lru_cache(maxsize=None)
def selected_nodes(root, group):
    source = root / ".github/ci/windows-public-split-selection.json"
    selection = json.loads(source.read_text(encoding="utf-8"))[group]
    return frozenset(selection["node_ids"])


@pytest.fixture(autouse=True)
def ordinary_registry_fixture(request, monkeypatch):
    group = request.config.getoption("--myrk-split-group")
    nodes = selected_nodes(request.config.rootpath, group)
    if request.node.nodeid not in nodes:
        pytest.fail("A test outside the audited behavior scope was selected.", pytrace=False)

    from tests.support.agent_acceptance import CaseRegistry
    from tests.support.agent_acceptance.catalogue import complete_catalogue
    from tests.support.agent_acceptance.ordinary import register

    def make_registry():
        registry = CaseRegistry()
        register(registry)
        complete_catalogue(registry)
        return registry

    if request.node.nodeid == "tests/acceptance/test_selected.py::test_selected_plan":
        path = request.node.path.with_name("conftest.py").resolve()
        conftest = request.config.pluginmanager.get_plugin(str(path))
        if conftest is None:
            pytest.fail("The acceptance conftest was not loaded.", pytrace=False)
        monkeypatch.setattr(conftest, "builtin_registry", make_registry)
    else:
        monkeypatch.setattr(request.module, "builtin_registry", make_registry)
