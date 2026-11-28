"""Collect, load and verify frozen acceptance source snapshots."""

import inspect
import json
import zipfile
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from pydantic import TypeAdapter
from tests.support.agent_acceptance.core import AcceptancePlan, CaseRegistry, SharedControlError
from tests.support.agent_acceptance.preparation_models import (
    CandidateFreezeReview,
    LiveAuthorization,
    PreparedBundle,
    SOURCE,
    _LiveLedger,
    bundle_fingerprint,
    digest,
)
from typing import Any


def bound_files(plan: AcceptancePlan, registry: CaseRegistry) -> dict[str, str]:
    if getattr(plan, "quality_packet", None) is not None:
        from tests.support.agent_acceptance import builtin_registry
        canonical = builtin_registry(quality_packet=plan.quality_packet)
        if registry.select(plan) != canonical.select(plan):
            raise SharedControlError("Quality packet registry differs from the canonical frozen adapter.")
    paths = {*SOURCE.joinpath("myrk").rglob("*.py"), *SOURCE.joinpath("myrk").glob("*.json"),
             *Path(__file__).parent.rglob("*.py"), *Path(__file__).parent.rglob("*.mjs"),
             *SOURCE.joinpath("tests/acceptance").glob("*.py"),
             SOURCE / "tests/__init__.py", SOURCE / "pyproject.toml"}
    paths.update(SOURCE / name for name in ("conftest.py", "tests/conftest.py", "tests/acceptance/conftest.py",
        "tests/acceptance/test_selected.py", "scripts/run_checks.py", "requirements.txt", "requirements-dev.txt", "requirements.lock")
        if (SOURCE / name).is_file())
    paths.update(path for directory in (SOURCE / "tests/acceptance/materials", SOURCE / "tests/acceptance/plans")
        for path in directory.rglob("*") if path.is_file())
    paths.update(Path(__file__).parent.rglob(".gitattributes"))
    for case, _ in registry.select(plan):
        paths.add(Path(inspect.getfile(case.run)))
        paths.update(case.bindings)
    files = {}
    for path in sorted(paths):
        path = path.resolve()
        if not path.is_relative_to(SOURCE) or not path.is_file():
            raise SharedControlError(f"Execution binding must be a source file: {path}")
        files[path.relative_to(SOURCE).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return files


def bundle_material_root(bundle: PreparedBundle) -> Path:
    return bundle.root if bundle.candidate_directory is None else bundle.root / ".candidates" / bundle.candidate_directory


def candidate_plan_matches(previous: PreparedBundle, plan: AcceptancePlan, *, material_root: Path = SOURCE,
                           source_reader: Callable[[Path], bytes] | None = None) -> bool:
    """Only reviewed rubric/package identity may change; tasks and all caps persist."""
    if plan == previous.plan:
        return True
    old = previous.plan
    if (old.quality_packet is None or plan.quality_packet is None
            or old.model_dump(exclude={"executions", "quality_packet"}) != plan.model_dump(exclude={"executions", "quality_packet"})
            or len(old.executions) != len(plan.executions)
            or any(left.model_dump(exclude={"material_path"}) != right.model_dump(exclude={"material_path"})
                for left,right in zip(old.executions, plan.executions))):
        return False
    from tests.support.agent_acceptance.quality import QualityMaterials, read_quality_manifest
    def manifest(path: Path) -> Any:
        if source_reader is None:
            return read_quality_manifest(path)
        raw = source_reader(path)
        value = QualityMaterials.model_validate_json(raw)
        if digest({key: body for key, body in json.loads(raw).items() if key != "binding"}) != value.binding:
            raise SharedControlError("Archived quality manifest identity changed.")
        return value
    old_materials = manifest(bundle_material_root(previous) / old.quality_packet.path)
    new_materials = manifest(material_root / plan.quality_packet.path)
    new_bytes = source_reader(material_root / plan.quality_packet.path) if source_reader else (material_root / plan.quality_packet.path).read_bytes()
    if (sha256(new_bytes).hexdigest() != plan.quality_packet.sha256
            or old_materials.model_dump(exclude={"source_path", "source_version", "criteria_version", "standards", "tasks", "files", "binding", "usage_authority"})
                != new_materials.model_dump(exclude={"source_path", "source_version", "criteria_version", "standards", "tasks", "files", "binding", "usage_authority"})
            or (old_materials.usage_authority or {}).get("path") != (new_materials.usage_authority or {}).get("path")
            or len(old_materials.tasks) != len(new_materials.tasks)):
        return False
    for left,right in zip(old_materials.tasks,new_materials.tasks):
        if (left.model_dump(exclude={"input_path", "gold_path", "domain_standards"}) != right.model_dump(exclude={"input_path", "gold_path", "domain_standards"})
                or old_materials.files[left.input_path.as_posix()] != new_materials.files[right.input_path.as_posix()]
                or old_materials.files[left.gold_path.as_posix()] != new_materials.files[right.gold_path.as_posix()]):
            return False
    for kind,standard in old_materials.standards.items():
        changed = new_materials.standards.get(kind, {}).get("dimensions", {})
        for name,dimension in standard.get("dimensions", {}).items():
            if dimension.get("critical") and (changed.get(name, {}).get("critical") is not True
                    or changed[name].get("applicable") != dimension.get("applicable")):
                return False
    return True


def read_prepared_snapshot(root: Path, *, binding: str | None = None) -> PreparedBundle:
    """Read immutable genesis/candidate originals without claiming current-source readiness."""
    return _read_prepared_storage(root, root, binding=binding)


def read_prepared_archive(storage: Path, *, binding: str | None = None) -> PreparedBundle:
    """Verify preserved originals offline; this does not create a live gate or authority."""
    try:
        with zipfile.ZipFile(storage) as archive:
            original = PreparedBundle.model_validate_json(archive.read("prepared.json"))
    except (OSError, ValueError) as error:
        raise SharedControlError(f"Archived genesis cannot be read: {error}") from error
    return _read_prepared_storage(original.root, storage, binding=binding)


def _read_prepared_storage(root: Path, storage: Path, *, binding: str | None) -> PreparedBundle:
    frozen_archive: zipfile.ZipFile | None = None
    def document(path: Path) -> bytes:
        return frozen_archive.read(path.relative_to(storage).as_posix()) if frozen_archive is not None else path.read_bytes()
    def material_bytes(snapshot: Path, name: str) -> bytes:
        if frozen_archive is not None:
            return document(snapshot / name)
        if storage == root:
            return (snapshot / name).read_bytes()
        with zipfile.ZipFile(snapshot / "source.zip") as archive:
            return archive.read(name)
    def archived_source(path: Path) -> bytes:
        relative = path.relative_to(storage)
        if relative.parts[0] == ".candidates":
            return material_bytes(storage / Path(*relative.parts[:2]), Path(*relative.parts[2:]).as_posix())
        return material_bytes(storage, relative.as_posix())
    try:
        if storage != root:
            frozen_archive = zipfile.ZipFile(storage)
        original = TypeAdapter(dict[str, Any]).validate_json(document(storage / "ledger.json"))
        if original.get("integrity") != digest({key: value for key, value in original.items() if key != "integrity"}):
            raise SharedControlError("Live accounting integrity changed; no terminal HTTP sent.")
        ledger = _LiveLedger.model_validate(original)
        previous_binding = ledger.binding
        previous_bundle = PreparedBundle.model_validate_json(document(storage / "prepared.json"))
        if previous_bundle.binding != ledger.binding or bundle_fingerprint(previous_bundle) != ledger.binding:
            raise SharedControlError("Genesis prepared original changed; no candidate can replace it.")
        if ledger.candidates and any(sha256(material_bytes(storage, name)).hexdigest() != expected for name, expected in previous_bundle.files.items()):
            raise SharedControlError("Genesis frozen source originals changed.")
        active: dict[str, str] = {}
        activation_number = 1
        for item in ledger.candidates:
            record_root = storage / ".candidates" / item["directory"]
            decision_path = storage / item["review_path"]
            if (not record_root.resolve().is_relative_to(storage.resolve())
                    or any(part.is_symlink() or part.is_junction() for part in (record_root, *record_root.parents))
                    or decision_path != record_root / "candidate-review.json"
                    or sha256(document(decision_path)).hexdigest() != item["review_sha256"]):
                raise SharedControlError("Candidate review location or original changed.")
            decision = CandidateFreezeReview.model_validate_json(document(decision_path))
            record = PreparedBundle.model_validate_json(document(record_root / "prepared.json"))
            grant = LiveAuthorization.model_validate(item["grant"])
            if (not activation_number <= item["before_request"] <= len(ledger.attempts) + 1
                    or item["previous_binding"] != previous_binding or decision.previous_binding != previous_binding
                    or item["candidate_binding"] != decision.candidate_binding or record.binding != decision.candidate_binding
                    or record.candidate_directory != item["directory"] or record.root != root.resolve()
                    or bundle_fingerprint(record) != record.binding or grant.binding != record.binding
                    or not candidate_plan_matches(previous_bundle.model_copy(update={"root": storage}), record.plan, material_root=record_root,
                        source_reader=archived_source if storage != root else None)
                    or sorted(decision.affected_execution_ids) != item["affected_execution_ids"]
                    or decision.accounting_root.resolve() != root.resolve() or record.plan.parent_budget is None
                    or decision.parent_id != record.plan.parent_budget.parent_id
                    or ledger.authorization is None
                    or grant.model_dump(exclude={"binding", "authorization_id"}) != ledger.authorization.model_dump(exclude={"binding", "authorization_id"})
                    or any(sha256(material_bytes(record_root, name)).hexdigest() != expected for name,expected in record.files.items())):
                raise SharedControlError("Candidate chain differs from its exact reviewed originals.")
            active.update({name: record.binding for name in decision.affected_execution_ids})
            activation_number = item["before_request"]
            previous_binding = record.binding
            previous_bundle = record
        if active != ledger.active_candidates:
            raise SharedControlError("Active candidate map differs from the reviewed freeze chain.")
        selected = binding or (ledger.candidates[-1]["candidate_binding"] if ledger.candidates else ledger.binding)
        candidate = next((item for item in ledger.candidates if item["candidate_binding"] == selected), None)
        snapshot = storage if selected == ledger.binding else storage / ".candidates" / candidate["directory"] if candidate else None
        if snapshot is None or any(part.is_symlink() or part.is_junction() for part in (snapshot, *snapshot.parents)):
            raise SharedControlError("Candidate binding is missing or uses a redirected path.")
        bundle = PreparedBundle.model_validate_json(document(snapshot / "prepared.json"))
        if (bundle.root != root.resolve() or storage / bundle_material_root(bundle).relative_to(root) != snapshot or bundle.binding != selected
                or bundle_fingerprint(bundle) != bundle.binding or bundle.sending_enabled):
            raise SharedControlError("Prepared candidate identity changed; no connection acquired.")
        if candidate is not None:
            review = storage / candidate["review_path"]
            if (not review.resolve().is_relative_to(storage.resolve()) or frozen_archive is None and not review.is_file()
                    or sha256(document(review)).hexdigest() != candidate["review_sha256"]):
                raise SharedControlError("Candidate review original changed; no connection acquired.")
        if any(sha256(material_bytes(snapshot, name)).hexdigest() != expected
               for name, expected in bundle.files.items()):
            raise SharedControlError("Frozen material binding changed; no connection acquired.")
        return bundle
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        raise SharedControlError(f"Prepared execution binding cannot be read: {error}") from error

    finally:
        if frozen_archive is not None:
            frozen_archive.close()


def verify_prepared(root: Path, *, registry: CaseRegistry, binding: str | None = None) -> PreparedBundle:
    from tests.support.agent_acceptance.reporting import environment_facts
    bundle = read_prepared_snapshot(root, binding=binding)
    if (not bundle.eligibility or not bundle.budget_proposals or not bundle.prices
            or SOURCE not in (bundle.source_root, bundle_material_root(bundle))
            or bound_files(bundle.plan, registry) != bundle.files or environment_facts() != bundle.environment):
        raise SharedControlError("Prepared execution binding changed; no connection acquired.")
    return bundle
