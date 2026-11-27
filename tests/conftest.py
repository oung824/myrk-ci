import pytest
from pydantic_ai import models
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel


@pytest.fixture(autouse=True)
def prevent_paid_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)


@pytest.fixture(autouse=True)
def declared_function_model_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline models have an explicit simulated window; production has no fallback."""
    original = FunctionModel.__init__

    def initialized(self, *args, **kwargs):
        if kwargs.get("profile") is None:
            kwargs["profile"] = {"context_window": 128000, "supports_json_schema_output": True, "supports_json_object_output": True}
        original(self, *args, **kwargs)

    monkeypatch.setattr(FunctionModel, "__init__", initialized)


@pytest.fixture(autouse=True)
def declared_test_model_window(monkeypatch: pytest.MonkeyPatch) -> None:
    original = TestModel.__init__

    def initialized(self, *args, **kwargs):
        if kwargs.get("profile") is None:
            kwargs["profile"] = {"context_window": 128000}
        original(self, *args, **kwargs)

    monkeypatch.setattr(TestModel, "__init__", initialized)
