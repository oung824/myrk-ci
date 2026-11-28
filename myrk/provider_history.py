from dataclasses import replace
from hashlib import sha256

from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel


_SDK_FRAMING_ANNOUNCEMENT_SHA256 = "71a9057ee7f3cc430394243f6e2e6a1bb103aa06ab179fdc459d75c06403ad25"


class LegacySystemPromptModel(WrapperModel):
    """Keep the original position of the SDK announcement in the outgoing request."""

    def prepare_messages(
        self,
        messages: list[ModelMessage],
        model_request_parameters: ModelRequestParameters | None = None,
    ) -> list[ModelMessage]:
        prepared = super().prepare_messages(messages, model_request_parameters)
        if self.profile.get("supports_inline_system_prompts", False):
            return prepared

        for index, message in enumerate(prepared):
            if not isinstance(message, ModelRequest):
                continue
            if not message.parts or not isinstance(announcement := message.parts[0], SystemPromptPart):
                break
            if sha256(announcement.content.encode("utf-8")).hexdigest() != _SDK_FRAMING_ANNOUNCEMENT_SHA256:
                break
            if any(part is announcement for original in messages if isinstance(original, ModelRequest) for part in original.parts):
                break

            end = 1
            for part in message.parts[1:]:
                if not isinstance(part, SystemPromptPart) or part.scope != "conversation":
                    break
                end += 1
            if end == 1:
                break
            parts = [*message.parts[1:end], announcement, *message.parts[end:]]
            return [*prepared[:index], replace(message, parts=parts), *prepared[index + 1:]]
        return prepared
