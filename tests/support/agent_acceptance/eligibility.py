"""Evidence for the three frozen paid routes; missing bounds keep every route closed."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from myrk.provider import ProviderConfig

RouteStandardVersion = Literal["2026-11-16", "2026-11-26"]


def deepseek_thinking_disabled(body: dict[str, object]) -> bool:
    """Read effective mode from a DeepSeek Chat Completions request."""
    thinking = body.get("thinking")
    if thinking is None:
        return body.get("reasoning_effort") == "none"
    return isinstance(thinking, dict) and thinking.get("type") == "disabled"


class EligibilityGap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str
    required_evidence: str


class EligibilityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: str
    scope: str
    observed_at: datetime


class RouteEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    standard_version: RouteStandardVersion = "2026-11-16"
    entry_point: Literal["ordinary_new_chat"] | None = Field(default=None, exclude_if=lambda value: value is None)
    protocol: Literal["deepseek", "google"]
    role: Literal["product", "judge"]
    model: str
    actual_model: str
    endpoint: str
    configuration: ProviderConfig
    observed_at: datetime
    expires_at: datetime | None
    thinking: dict[str, object]
    input_limit: int = 32768
    total_output_limit: int
    price_currency: Literal["USD"] = "USD"
    price_input_per_million: str
    price_output_per_million: str
    price_tier: str
    reservation_microusd: int
    paid_extras_enabled: Literal[False] = False
    status: Literal["unqualified"] = "unqualified"
    sending_enabled: Literal[False] = False
    evidence: tuple[EligibilityEvidence, ...]
    unresolved: tuple[EligibilityGap, ...]


def preparation_eligibility(*, observed_at: datetime | None = None,
                            deepseek_standard: RouteStandardVersion = "2026-11-26") -> tuple[RouteEligibility, ...]:
    observed = observed_at or datetime.now(timezone.utc)
    gaps = tuple(EligibilityGap(code=code, required_evidence=required) for code, required in (
        ("input_billing_bound", "A conservative pre-send bound for the entire native input, including history, tools, materials and protocol overhead; local text estimates are insufficient."),
        ("total_output_billing_bound", "Exact-model evidence that the native output cap bounds all charged thinking and other output; configured reserve or visible text is insufficient."),
        ("account_usd_settlement", "Previously authorized evidence that this exact service account is available and actually settles in USD; no account access is performed here."),
        ("surcharge_bound", "A verified upper bound for all applicable taxes, payment fees and other surcharges inside the USD180 plan; unknown is not zero."),
        ("native_settings_binding", "A final native HTTP observation from the frozen public product entry matching this exact route and effective thinking setting."),
    ))
    high_new_chat = deepseek_standard == "2026-11-26"
    routes = (
        ("deepseek", "product", "deepseek-flash", "DeepSeek-V4.1-Flash-1017", "https://api.deepseek.com", None,
         {"reasoning_effort": "high" if high_new_chat else "none"},
         4096, "0.30", "1.20", "peak uncached", 14746,
         "https://api-docs.deepseek.com/quick_start/pricing/"),
        ("google", "product", "gemini-2.5-flash", "gemini-2.5-flash", "https://generativelanguage.googleapis.com",
         datetime(2026, 11, 29, 15, 59, tzinfo=timezone.utc), {"thinking_budget": 2048}, 4096,
         "0.30", "2.50", "text input; charged thinking included in output price", 20071,
         "https://aistudio.google.com/docs/pricing"),
        ("google", "judge", "gemini-2.5-pro", "gemini-2.5-pro", "https://generativelanguage.googleapis.com",
         datetime(2026, 11, 29, 15, 59, tzinfo=timezone.utc), {"thinking_budget": 128}, 8192,
         "1.25", "10.00", "input at most 200k; charged thinking included in output price", 122880,
         "https://aistudio.google.com/docs/pricing"),
    )
    snapshot = datetime(2026, 11, 15, 16, tzinfo=timezone.utc)
    result = []
    for protocol, role, model, actual, endpoint, expiry, thinking, output, input_price, output_price, tier, reserve, source in routes:
        role_gaps = gaps
        if role == "judge":
            role_gaps += (EligibilityGap(code="judge_minimum_thinking", required_evidence=
                "The 128-token native legacy candidate still needs exact-model evidence that it is the minimum supported setting; product compatibility never grants judge eligibility."),)
        result.append(RouteEligibility.model_validate(dict(protocol=protocol, role=role, model=model, actual_model=actual, endpoint=endpoint,
            standard_version=deepseek_standard if protocol == "deepseek" else "2026-11-16",
            entry_point="ordinary_new_chat" if protocol == "deepseek" and high_new_chat else None,
            configuration=ProviderConfig.model_validate({"name": "Gemini" if protocol == "google" else "DeepSeek",
                "protocol": protocol, "model": model, "base_url": endpoint, "output_token_reserve": output}),
            observed_at=observed, expires_at=expiry, thinking=thinking, total_output_limit=output,
            price_input_per_million=input_price, price_output_per_million=output_price, price_tier=tier,
            reservation_microusd=reserve, evidence=(EligibilityEvidence(source=source, observed_at=snapshot,
                scope="2026-11-16 specification price/model snapshot only; no account, token-bound, speed, cache or native-capacity qualification"),),
            unresolved=role_gaps)))
    return tuple(result)


class NativeEligibilityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_sha256: str
    route_current: bool
    native_settings_match: bool
    sending_enabled: Literal[False] = False
    unresolved: tuple[str, ...]
    manual_review_required: bool
