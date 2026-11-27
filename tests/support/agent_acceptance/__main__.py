"""The maintained acceptance preparation and original-output review commands."""

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter

from tests.support.agent_acceptance import HumanReview, builtin_registry, export_review, import_reviews, review_report
from tests.support.agent_acceptance.preparation import verify_prepared, read_prepared_snapshot
from tests.support.agent_acceptance.historical_reuse import (HistoricalChange, HistoricalApplicabilityReview,
    ReadOnlyHistoricalRegrade, HistoricalMaterialUseReview, import_historical_material_use, import_historical_group, import_historical_applicability,
    import_historical_regrade, historical_reuse_report, export_historical_report)
from tests.support.agent_acceptance.output_review import ReviewOriginal


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    frozen_package = commands.add_parser("freeze-execution-package")
    frozen_package.add_argument("destination", type=Path)
    frozen_package.add_argument("--quality-packet", type=Path)
    combined = commands.add_parser("execution-package-report")
    combined.add_argument("--quality-packet", type=Path)
    combined.add_argument("--review-package", type=Path)
    combined.add_argument("--historical-group", type=Path, action="append", default=[])
    combined.add_argument("--quality-review-package", type=Path)
    combined.add_argument("--human-baseline", type=Path)
    verify_package = commands.add_parser("verify-execution-package")
    verify_package.add_argument("root", type=Path)
    prepared = commands.add_parser("verify-prepared")
    prepared.add_argument("root", type=Path)
    export = commands.add_parser("export-review")
    export.add_argument("result", type=Path)
    export.add_argument("destination", type=Path)
    export.add_argument("--final-reviewer")
    export.add_argument("--standards", type=Path)
    load = commands.add_parser("import-review")
    load.add_argument("package", type=Path)
    load.add_argument("reviews", type=Path)
    report = commands.add_parser("review-report")
    report.add_argument("package", type=Path)
    historical = commands.add_parser("import-history")
    historical.add_argument("result", type=Path)
    historical.add_argument("destination", type=Path)
    historical.add_argument("change", type=Path)
    historical.add_argument("--review-package", type=Path)
    historical.add_argument("--state-originals", type=Path)
    for name in ("import-history-applicability", "import-history-regrade", "import-history-material-use"):
        entry = commands.add_parser(name)
        entry.add_argument("group", type=Path)
        entry.add_argument("decision", type=Path)
    historical_report = commands.add_parser("history-report")
    historical_report.add_argument("group", type=Path)
    historical_export = commands.add_parser("export-history-report")
    historical_export.add_argument("group", type=Path)
    historical_export.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "freeze-execution-package":
        from tests.support.agent_acceptance.integrated_package import freeze_integrated_execution_package
        from tests.support.agent_acceptance.quality_packet import QualityPacketRef
        reference = QualityPacketRef.model_validate_json(arguments.quality_packet.read_bytes()) if arguments.quality_packet else None
        path = freeze_integrated_execution_package(arguments.destination, registry=builtin_registry(quality_packet=reference), quality_packet=reference)
        print(json.dumps({"package": str(path), "sending_authorized": False}, ensure_ascii=False))
    elif arguments.command == "execution-package-report":
        from tests.support.agent_acceptance.integrated_package import integrated_execution_package
        from tests.support.agent_acceptance.quality_packet import QualityPacketRef
        reference = QualityPacketRef.model_validate_json(arguments.quality_packet.read_bytes()) if arguments.quality_packet else None
        report_value = integrated_execution_package(registry=builtin_registry(quality_packet=reference), quality_packet=reference,
            review_package=arguments.review_package, historical_groups=tuple(arguments.historical_group),
            quality_review_package=arguments.quality_review_package, baseline=arguments.human_baseline)
        print(json.dumps(report_value, ensure_ascii=False, indent=2))
    elif arguments.command == "verify-execution-package":
        from tests.support.agent_acceptance.integrated_package import verify_integrated_execution_package
        print(json.dumps(verify_integrated_execution_package(arguments.root, registry=builtin_registry()), ensure_ascii=False))
    elif arguments.command == "verify-prepared":
        saved = read_prepared_snapshot(arguments.root)
        registry = builtin_registry(quality_packet=saved.plan.quality_packet)
        print(verify_prepared(arguments.root, registry=registry).model_dump_json())
    elif arguments.command == "export-review":
        standards = json.loads(arguments.standards.read_bytes()) if arguments.standards else None
        path = export_review(arguments.result, arguments.destination, final_reviewer=arguments.final_reviewer, standards=standards)
        print(json.dumps({"package": str(path)}, ensure_ascii=False))
    elif arguments.command == "import-review":
        reviews = TypeAdapter(list[HumanReview]).validate_json(arguments.reviews.read_bytes())
        print(json.dumps(import_reviews(arguments.package, reviews), ensure_ascii=False, indent=2))
    elif arguments.command == "review-report":
        print(json.dumps(review_report(arguments.package), ensure_ascii=False, indent=2))
    elif arguments.command == "import-history":
        state = TypeAdapter(dict[str, ReviewOriginal]).validate_json(arguments.state_originals.read_bytes()) if arguments.state_originals else None
        path = import_historical_group(arguments.result, destination=arguments.destination,
            change=HistoricalChange.model_validate_json(arguments.change.read_bytes()),
            review_package=arguments.review_package, state_originals=state)
        print(json.dumps({"group": str(path)}, ensure_ascii=False))
    elif arguments.command == "import-history-applicability":
        print(json.dumps(import_historical_applicability(arguments.group, HistoricalApplicabilityReview.model_validate_json(arguments.decision.read_bytes())), ensure_ascii=False, indent=2))
    elif arguments.command == "import-history-material-use":
        print(json.dumps(import_historical_material_use(arguments.group, HistoricalMaterialUseReview.model_validate_json(arguments.decision.read_bytes())), ensure_ascii=False, indent=2))
    elif arguments.command == "import-history-regrade":
        print(json.dumps(import_historical_regrade(arguments.group, ReadOnlyHistoricalRegrade.model_validate_json(arguments.decision.read_bytes())), ensure_ascii=False, indent=2))
    elif arguments.command == "export-history-report":
        print(json.dumps({"report": str(export_historical_report(arguments.group, arguments.destination))}, ensure_ascii=False))
    else:
        print(json.dumps(historical_reuse_report(arguments.group), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
