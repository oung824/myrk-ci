"""Acceptance cases permit only explicitly injected model transports."""

import httpx
import httpx2
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import shutil
import traceback
import pytest
from uuid import uuid4
from pydantic_ai import models

from tests.support.agent_acceptance import AcceptancePlan, builtin_registry
from myrk.project_paths import project_root


KNOWN_SHUTDOWN_RESULTS = pytest.StashKey[list[dict]]()


@pytest.fixture(autouse=True)
def capture_original_save_failures():
    from tests.support.agent_acceptance.save_diagnostics import observe_saves

    root = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"])
    with observe_saves(project_root(Path(__file__)), root / "reports/save-diagnostics" / (uuid4().hex + ".json")):
        yield


@pytest.fixture
def acceptance_public_root(request):
    """Retain each public native result together with its exact pytest node."""
    group = "formal-scenarios" if request.path.name == "test_formal_scenarios.py" else "ordinary-public"
    root = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports" / group / uuid4().hex
    yield root
    result = root / "result.json"
    if result.is_file():
        body = result.read_bytes()
        saved = json.loads(body)
        (root / "pytest-node.json").write_text(json.dumps({"nodeid": request.node.nodeid,
            "run_id": saved["run_id"], "result_path": "result.json", "result_sha256": sha256(body).hexdigest(),
            "cases": [{"case_id": case["case_id"], "protocol": case["protocol"]} for case in saved["cases"]]},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def require_clean_asyncio_shutdown(caplog, request):
    from tests.support.agent_acceptance.shutdown import NODES, classify_known_shutdown, record_open_history

    caplog.set_level(logging.ERROR, logger="asyncio")
    reports = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]) / "reports"
    before = set(reports.rglob("result.json")) if request.node.nodeid in NODES else set()
    yield
    observed = [(phase, record) for phase in ("setup", "call", "teardown") for record in caplog.get_records(phase)
        if record.name == "asyncio" and record.levelno >= logging.ERROR]
    results = set(reports.rglob("result.json")) - before if request.node.nodeid in NODES else set()
    if not observed:
        if len(results) == 1:
            record_open_history(results.pop(), nodeid=request.node.nodeid)
        return
    directory = reports / "asyncio-qualification" / uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    formatter = caplog.handler.formatter or logging.Formatter("%(levelname)s %(name)s: %(message)s")
    original = directory / "asyncio-original.log"
    original.write_bytes(("\n".join(formatter.format(record) for _, record in observed) + "\n").encode("utf-8"))
    diagnostic = directory / "qualification_error.json"
    diagnostic.write_text(json.dumps({
        "category": "unhandled_asyncio_failure", "nodeid": request.node.nodeid,
        "historical_issue_status": "unknown", "product_fix_claimed": False,
        "original_log": original.name, "records": [{"phase": phase, "logger": record.name,
            "level": record.levelname, "message": record.getMessage(), "created": record.created,
            "traceback": "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else record.exc_text or ""}
            for phase, record in observed]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if len(results) == 1:
        result = results.pop()
        classified = classify_known_shutdown(result, diagnostic, nodeid=request.node.nodeid)
        if classified is not None:
            known = request.config.stash.get(KNOWN_SHUTDOWN_RESULTS, [])
            known.append({"nodeid": request.node.nodeid, "result": str(result), "issue": classified["issue"]})
            request.config.stash[KNOWN_SHUTDOWN_RESULTS] = known
            return
    pytest.fail(f"Unhandled asyncio failure invalidated acceptance qualification; original log: {original}", pytrace=False)


def pytest_terminal_summary(terminalreporter, config):
    for item in config.stash.get(KNOWN_SHUTDOWN_RESULTS, []):
        terminalreporter.write_line(f"Product failed: review.stop/deepseek known shutdown; history unknown; result: {item['result']}; issue: {item['issue']}", red=True)


def pytest_addoption(parser):
    group = parser.getgroup("agent acceptance")
    group.addoption("--acceptance-plan", default=None, help="Fixed JSON acceptance plan")
    group.addoption("--acceptance-case", action="append", default=[], help="Required case identifier; repeat to select more")
    group.addoption("--acceptance-capability", action="append", default=[], help="Select every registered case of a capability")
    group.addoption("--acceptance-protocol", action="append", default=[], choices=["deepseek", "openai-chat", "google"])
    group.addoption("--acceptance-mode", default=None, choices=["offline", "prepare-live", "live"])
    group.addoption("--acceptance-root", default=None, help="Exclusive final root for prepare-live")
    group.addoption("--acceptance-live-request", default=None, help="Explicit prepared root and separately supplied live authorization JSON")
    group.addoption("--acceptance-list", action="store_true", help="Print the capability/case catalogue with -s")
    group.addoption("--acceptance-profile", default="full", choices=["core", "full"],
                    help="Core omits unselected full-desktop checks; explicit cases still execute")


def pytest_configure(config):
    config.addinivalue_line("markers", "acceptance_desktop: complete Electron owner/service lifecycle qualification")


def pytest_collection_modifyitems(config, items):
    if config.getoption("acceptance_profile") != "core":
        return
    explicit_nodes = [value.split("::") for value in config.args if "::" in value]
    omitted = [item for item in items if item.get_closest_marker("acceptance_desktop") is not None
               and not any(item.path == Path(node[0]).resolve() and item.nodeid.partition("::")[2].startswith("::".join(node[1:]))
                           for node in explicit_nodes)]
    if omitted:
        config.hook.pytest_deselected(items=omitted)
        items[:] = [item for item in items if item not in omitted]


@pytest.fixture
def acceptance_registry(acceptance_live_request):
    if acceptance_live_request is not None:
        from tests.support.agent_acceptance.preparation import read_prepared_snapshot
        saved = read_prepared_snapshot(acceptance_live_request.run_root, binding=acceptance_live_request.grant.binding)
        return builtin_registry(quality_packet=saved.plan.quality_packet)
    return builtin_registry()


@pytest.fixture
def prepared_root():
    root = project_root(Path(__file__).resolve().parents[2]) / ".scratch" / "acceptance-prepared" / uuid4().hex
    yield root
    if root.exists():
        shutil.rmtree(root)


@pytest.fixture
def acceptance_live_request(request):
    from tests.support.agent_acceptance.preparation import LiveExecutionRequest
    path = request.config.getoption("acceptance_live_request")
    return LiveExecutionRequest.model_validate_json(Path(path).read_bytes()) if path else None


@pytest.fixture
def acceptance_plan(request, acceptance_registry, acceptance_live_request):
    config = request.config
    if acceptance_live_request is not None:
        from tests.support.agent_acceptance.preparation import verify_prepared
        if any(config.getoption(key) for key in ("acceptance_plan", "acceptance_case", "acceptance_capability", "acceptance_protocol")) or config.getoption("acceptance_mode") not in {None, "live"}:
            raise pytest.UsageError("An explicit live request cannot be overridden by another plan or selector.")
        value = acceptance_live_request
        bundle = verify_prepared(value.run_root, registry=acceptance_registry, binding=value.grant.binding)
        return AcceptancePlan(cases=[value.case_id], protocols=[value.protocol], mode="live") if value.case_id and value.protocol else bundle.plan.model_copy(update={"mode": "live"})
    path = config.getoption("acceptance_plan")
    if path:
        if any(config.getoption(key) for key in ("acceptance_case", "acceptance_capability", "acceptance_protocol", "acceptance_mode")):
            raise pytest.UsageError("A fixed plan cannot be overridden by selection options.")
        return AcceptancePlan.model_validate_json(Path(path).read_text(encoding="utf-8"))
    cases = list(config.getoption("acceptance_case"))
    catalogue = acceptance_registry.capabilities()
    for capability in config.getoption("acceptance_capability"):
        if capability not in catalogue:
            raise pytest.UsageError(f"Required capability is missing: {capability}")
        cases.extend(catalogue[capability])
    return AcceptancePlan(cases=list(dict.fromkeys(cases)) or ["ordinary.question"],
                          protocols=config.getoption("acceptance_protocol") or ["deepseek"],
                          mode=config.getoption("acceptance_mode") or "offline")


@pytest.fixture(autouse=True)
def offline_model_access(monkeypatch):
    async def reject_async(_transport, _request):
        raise AssertionError("Acceptance attempted an uninjected asynchronous transport")

    def reject_sync(_transport, _request):
        raise AssertionError("Acceptance attempted an uninjected synchronous transport")

    for module in (httpx, httpx2):
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", reject_async)
        monkeypatch.setattr(module.HTTPTransport, "handle_request", reject_sync)
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
