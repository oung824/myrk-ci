"""Expose actual project instruction selection through the public service."""

from contextlib import asynccontextmanager
from pathlib import Path
import sys

from pydantic_ai import models
from pydantic_ai.models.function import FunctionModel

from myrk.desktop_service import main


async def reply(_messages, info):
    yield "完整指导" if "CURRENT-PROJECT-GUIDANCE" in info.instructions else "被项目设置裁短"


@asynccontextmanager
async def factory(_key):
    yield FunctionModel(stream_function=reply, profile={"context_window": 128000, "supports_json_schema_output": True, "supports_json_object_output": True})


if __name__ == "__main__":
    models.ALLOW_MODEL_REQUESTS = False
    main(data_directory=Path(sys.argv[1]), model_factory=factory)
