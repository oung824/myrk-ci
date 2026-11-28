"""Verify native provider input and reserve the associated accounting entry."""

import gzip
import httpx2
import json
import os
from base64 import b64encode
from collections.abc import Iterator
from datetime import datetime, timezone
from hashlib import sha256
from myrk.provider import ProviderConfig
from pathlib import Path
from pydantic import TypeAdapter
from tests.support.agent_acceptance.core import Protocol, SharedControlError
from tests.support.agent_acceptance.eligibility import NativeEligibilityObservation, deepseek_thinking_disabled
from tests.support.agent_acceptance.preparation_models import CaseBudgetExceeded, _ReservedAttempt, digest
from tests.support.agent_acceptance.preparation_snapshot import bundle_material_root
from tests.support.agent_acceptance.preparation_transaction import ledger_mutation
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from tests.support.agent_acceptance.core import CaseRegistry
    from tests.support.agent_acceptance.preparation_models import LiveAuthorization, PreparedBundle, _LiveLedger
    from tests.support.agent_acceptance.preparation_transaction import _LedgerTransactionHost
    from typing import Protocol as TypingProtocol

    class _RequestGateHost(_LedgerTransactionHost, TypingProtocol):
        registry: CaseRegistry
        authorization: LiveAuthorization | None
        bound_execution_id: str | None

        def prepared(self) -> PreparedBundle: ...
        def validate(self, *, case_id: str, protocol: Protocol) -> tuple[PreparedBundle, LiveAuthorization, _LiveLedger]: ...
        def execution_id(self, case_id: str, protocol: Protocol) -> str: ...
        def stop_key(self, case_id: str, protocol: Protocol) -> str: ...
        def _save_ledger(self, ledger: _LiveLedger) -> None: ...
        def _parent_binding(self, bundle: PreparedBundle, grant: LiveAuthorization, *, activate: bool, required: bool) -> None: ...


class _RequestGate:
    def inspect_frozen_material(self: "_RequestGateHost", request: httpx2.Request, *, material_root: Path | None = None) -> None:
        bundle = self.prepared()
        execution = next((item for item in bundle.plan.executions if item.execution_id == self.bound_execution_id), None)
        if execution is None:
            return
        material = bundle_material_root(bundle) / execution.material_path
        if bundle.plan.quality_packet is not None:
            from tests.support.agent_acceptance.quality_runtime import load_frozen_input, model_prompts
            if material_root is not None:
                from tests.support.agent_acceptance.formal import verify_authority
                authority, authorized_gate = verify_authority(Path(os.environ["MYRK_ACCEPTANCE_AUTHORITY"]))
                if authority.scenario_root != material_root or authorized_gate.authorization != self.authorization or authorized_gate.bound_execution_id != self.bound_execution_id:
                    raise SharedControlError("Actual quality material root differs from the verified formal authority.")
            materials = model_prompts(load_frozen_input(material.read_bytes(), bundle.plan.quality_packet), root=material_root)
        else:
            materials = (material.read_text(encoding="utf-8").strip(),)
        body = json.loads(request.content)
        def strings(value: Any) -> Iterator[str]:
            if isinstance(value, str):
                yield value
                if value.startswith(("{", "[")):
                    try:
                        decoded = json.loads(value)
                    except ValueError:
                        return
                    yield from strings(decoded)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
        if not any(expected and expected in value for expected in materials for value in strings(body.get("contents", body.get("messages", [])))):
            raise SharedControlError("Frozen material did not enter the actual native model input; labels cannot replace a variant.")

    def inspect_native_request(self: "_RequestGateHost", request: httpx2.Request, *, protocol: Protocol,
                               role: Literal["product", "judge"], config: ProviderConfig,
                               now: datetime | None = None) -> NativeEligibilityObservation:
        """Observe the final request without acquiring a connection or authorizing a send."""
        bundle = self.prepared()
        observed = now or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            raise ValueError("Eligibility observation time requires a timezone.")
        routes = [item for item in bundle.eligibility if item.protocol == protocol and item.role == role]
        unresolved = [gap.code for item in routes for gap in item.unresolved]
        current = bool(routes)
        match = False
        if len(routes) == 1:
            route = routes[0]
            try:
                body = TypeAdapter(dict[str, Any]).validate_json(request.content)
            except ValueError as error:
                raise SharedControlError("Native request is not a JSON object; no terminal send allowed.") from error
            current = (config == route.configuration and (route.expires_at is None or observed < route.expires_at))
            if config != route.configuration:
                unresolved.append("configuration_changed")
            if route.expires_at is not None and observed >= route.expires_at:
                unresolved.append("route_expired")
            if protocol == "google":
                generation = body.get("generationConfig", {})
                if not isinstance(generation, dict):
                    raise SharedControlError("Native generationConfig is not an object; no terminal send allowed.")
                url = str(request.url).split("?", 1)[0]
                endpoints = {route.endpoint + "/v1beta/models/" + route.model + ":" + operation
                             for operation in ("generateContent", "streamGenerateContent")}
                candidate_count = generation.get("candidateCount", generation.get("candidate_count", 1))
                max_output_tokens = generation.get("maxOutputTokens")
                match = (request.method == "POST" and url in endpoints
                         and "generation_config" not in body
                         and not {"candidateCount", "candidate_count"}.issubset(generation)
                         and type(candidate_count) is int and candidate_count == 1
                         and "thinking_config" not in generation
                         and generation.get("thinkingConfig") == route.thinking
                         and "max_output_tokens" not in generation
                         and type(max_output_tokens) is int and 0 < max_output_tokens <= route.total_output_limit)
            else:
                max_tokens = body.get("max_tokens")
                unsupported_title = False
                if route.standard_version == "2026-11-26":
                    thinking = body.get("thinking")
                    thinking_match = (route.entry_point == "ordinary_new_chat"
                        and body.get("reasoning_effort") == route.thinking.get("reasoning_effort") == "high"
                        and (thinking is None or isinstance(thinking, dict) and thinking.get("type") == "enabled"))
                    output_match = type(max_tokens) is int and max_tokens == route.total_output_limit
                    title = body.get("title")
                    unsupported_title = "title" in body and (not isinstance(title, dict) or title.get("enabled") is not False)
                else:
                    thinking_match = deepseek_thinking_disabled(body)
                    output_match = type(max_tokens) is int and 0 < max_tokens <= route.total_output_limit
                match = (request.method == "POST" and str(request.url) == route.endpoint + "/chat/completions"
                         and body.get("model") == route.model and thinking_match and output_match)
                tools = body.get("tools")
                safety_check = body.get("safety_check")
                unsupported_tools = tools is not None and (
                    not isinstance(tools, list)
                    or any(not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools)
                )
                nondefault_safety = safety_check is not None and (
                    not isinstance(safety_check, dict) or safety_check.get("level") != "low"
                )
                if unsupported_tools or nondefault_safety or unsupported_title:
                    match = False
                    unresolved.append("non_text_or_paid_feature")
            native_nodes = [body]
            if protocol == "google":
                native_nodes.append(generation)
                tools = body.get("tools", [])
                if isinstance(tools, list):
                    native_nodes.extend(item for item in tools if isinstance(item, dict))
                contents = body.get("contents", [])
                if isinstance(contents, list):
                    for content in [body.get("systemInstruction"), *contents]:
                        if isinstance(content, dict) and isinstance(content.get("parts"), list):
                            native_nodes.extend(part for part in content["parts"] if isinstance(part, dict))
            else:
                messages = body.get("messages", [])
                if isinstance(messages, list):
                    for message in messages:
                        if isinstance(message, dict) and isinstance(message.get("content"), list):
                            native_nodes.extend(part for part in message["content"] if isinstance(part, dict))
            restricted = {"googleSearch", "google_search", "googleSearchRetrieval", "google_search_retrieval",
                          "urlContext", "url_context", "codeExecution", "code_execution", "cachedContent", "cached_content",
                          "inlineData", "inline_data", "fileData", "file_data", "audioConfig", "audio_config",
                          "image_url", "input_audio", "video_url", "web_search_options"}
            for value in native_nodes:
                modalities = value.get("responseModalities", value.get("response_modalities"))
                if (restricted.intersection(value) or {"responseModalities", "response_modalities"}.issubset(value)
                        or (modalities is not None and modalities != ["TEXT"])):
                    match = False
                    unresolved.append("non_text_or_paid_feature")
                    break
            if not match:
                unresolved.append("native_settings_mismatch")
        else:
            unresolved.append("unsupported_paid_route")
        return NativeEligibilityObservation(request_sha256=sha256(request.content).hexdigest(), route_current=current,
            native_settings_match=match, unresolved=tuple(unresolved), manual_review_required=role == "judge")

    @ledger_mutation
    def reserve(self: "_RequestGateHost", *, case_id: str, protocol: Protocol, actor: str, purpose: str,
                delegation_id: str | None = None, product_number: int | None = None,
                role: Literal["product", "judge"] = "product", product_step_id: str | None = None, sdk_input_id: str | None = None,
                request_sha256: str | None = None, judge_call_id: str | None = None, product_records_root: Path | None = None) -> int:
        bundle, grant, ledger = self.validate(case_id=case_id, protocol=protocol)
        product_owner = None
        if self.registry.cases[case_id].product_stop_scope == "task":
            record_root_allowed = product_records_root is not None and product_records_root.resolve().is_relative_to(bundle_material_root(bundle) / ".scratch")
            if bundle.candidate_directory is not None and bundle.plan.quality_packet is not None and case_id.startswith("quality."):
                from tests.support.agent_acceptance.formal import verify_authority
                authority_path = os.environ.get("MYRK_ACCEPTANCE_AUTHORITY")
                if authority_path is None:
                    raise SharedControlError("A quality candidate requires its actual paired data-root authority.")
                authority, actual_gate = verify_authority(Path(authority_path))
                record_root_allowed = (authority.scenario_root is not None and product_records_root == authority.scenario_root / "records"
                    and actual_gate.authorization == self.authorization and actual_gate.bound_execution_id == self.bound_execution_id
                    and authority.case_id == case_id and authority.protocol == protocol)
            if role != "product" or product_records_root is None or (not product_step_id and purpose != "compaction") or not record_root_allowed:
                raise SharedControlError("Task-scoped sends require the actual frozen product record root and RequestAttempt step.")
            from myrk.conversation import ConversationRecord
            matches = []
            for source in product_records_root.rglob("*.json"):
                if source.parent != product_records_root and not source.parent.name.endswith("-delegations"):
                    continue
                raw = source.read_bytes()
                record = ConversationRecord.model_validate_json(raw)
                for turn in record.turns:
                    if turn.task is None:
                        continue
                    requests = [*turn.request_attempts, *(request for action in turn.actions if action.review is not None for request in action.review.request_attempts)]
                    if any((request.number, request.step_id, request.actor, request.purpose, request.delegation_id) == (product_number, product_step_id, actor, purpose, delegation_id)
                            and (purpose != "compaction" or turn.status == "running" and request.status == "sent") for request in requests):
                        matches.append({"task_id": turn.task.id, "path": str(source), "sha256": sha256(raw).hexdigest(), "bytes": len(raw), "original_codec": "gzip-base64-v1", "original_compressed_base64": b64encode(gzip.compress(raw, mtime=0)).decode("ascii")})
            task_ids = {item["task_id"] for item in matches}
            if len(task_ids) != 1:
                raise SharedControlError("Native request does not resolve to one actual product Task; caller task labels cannot authorize a send.")
            task_id = next(iter(task_ids))
            product_owner = {"task_id": task_id, "execution_id": self.execution_id(case_id, protocol), "sources": matches}
            if f"{product_owner['execution_id']}:{task_id}" in ledger.product_task_stops:
                raise CaseBudgetExceeded("Actual product Task was stopped; a new step, actor or child request cannot resume it.")
        prices = [item for item in bundle.prices if item.protocol == protocol and item.role == role]
        if len(prices) != 1:
            raise SharedControlError("No frozen request price for this role/protocol.")
        price = prices[0].reservation_microusd
        attempts = ledger.attempts
        execution = next((item for item in bundle.plan.executions if item.execution_id == self.bound_execution_id), None)
        key = execution.execution_id if execution else case_id
        owned = [item for item in attempts if (item.execution_id == key if execution else item.case_id == key)]
        reason = None
        if (len(attempts) >= grant.total_limit or len(owned) >= grant.case_limits.get(key, 0)):
            reason = "request budget exhausted"
        assert grant.total_microusd is not None
        if (sum(item.occupied_microusd for item in attempts) + price > grant.total_microusd or
                sum(item.occupied_microusd for item in owned) + price > grant.case_microusd[key]):
            reason = "cost budget exhausted"
        parent = bundle.plan.parent_budget
        if execution is not None and parent is not None:
            if role != execution.role or key in ledger.execution_results:
                raise SharedControlError("Frozen execution role changed or already completed; no replacement send allowed.")
            if role == "judge" and (product_number is not None or judge_call_id != sdk_input_id
                    or not judge_call_id or purpose != execution.judge_purpose or actor != "judge"):
                raise SharedControlError("Judge reservation lacks its actual SDK-owned call identity.")
            if role == "product" and judge_call_id is not None:
                raise SharedControlError("Product reservation cannot borrow judge identity.")
            if not ledger.phase_microusd:
                ledger.phase_microusd = dict(parent.phase_microusd)
                ledger.reserve_microusd = parent.reserve_microusd
            started = {item.execution_id for item in attempts} | set(ledger.execution_results) | {key}
            remaining = [item for item in bundle.plan.executions if item.required and item.execution_id not in started]
            if len(attempts) + 1 + sum(item.request_cap for item in remaining) > grant.total_limit:
                reason = "unexecuted mandatory request envelope protected"
            if sum(item.occupied_microusd for item in attempts) + price + sum(item.cost_cap_microusd for item in remaining) > grant.total_microusd:
                reason = "unexecuted mandatory cost envelope protected"
            if (sum(item.occupied_microusd for item in attempts if item.stage == execution.stage) + price
                    + sum(item.cost_cap_microusd for item in remaining if item.stage == execution.stage) > ledger.phase_microusd[execution.stage]):
                reason = "stage cost budget exhausted or mandatory envelope protected"
            if sum(item.state in {"reserved", "http_started"} for item in attempts) >= parent.inflight_limit:
                reason = "inflight request limit exhausted"
        if reason:
            if ledger.authorization is None:
                ledger.authorization = grant
                ledger.authorization_binding = digest(grant.model_dump(mode="json"))
            ledger.case_stops[self.stop_key(case_id, protocol)] = {"reason": reason,
                "unfinished": "Remaining work was not authorized to send."}
            self._save_ledger(ledger)
            raise CaseBudgetExceeded(reason + "; no terminal HTTP sent.")
        self._parent_binding(bundle,grant,activate=True,required=False)
        number = len(attempts) + 1
        if ledger.authorization is None:
            ledger.authorization_binding = digest(grant.model_dump(mode="json"))
            ledger.authorization = grant
        ledger.attempts.append(_ReservedAttempt(number=number, authorization_id=grant.authorization_id, case_id=case_id,
            protocol=protocol, actor=actor, purpose=purpose, delegation_id=delegation_id, product_number=product_number,
            execution_id=self.execution_id(case_id, protocol), role=role, product_step_id=product_step_id, reservation_microusd=price, occupied_microusd=price,
            sdk_input_id=sdk_input_id, request_sha256=request_sha256, judge_call_id=judge_call_id, stage=execution.stage if execution else ""))
        if product_owner is not None:
            ledger.product_request_owners[str(number)] = product_owner
        self._save_ledger(ledger)
        return number
