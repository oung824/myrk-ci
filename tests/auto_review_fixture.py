"""Synthetic service fixture for independent review and one-time user authorization."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from pydantic_ai import models
from pydantic_ai.messages import UserPromptPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from myrk.desktop_service import main

models.ALLOW_MODEL_REQUESTS = False

async def reply(messages, info):
    if info.instructions and '你是独立动作审核者' in info.instructions:
        payload=json.loads(next(part.content for message in messages for part in message.parts if isinstance(part,UserPromptPart)))
        override=payload['one_time_user_authorization']
        yield json.dumps({'decision':'allow' if override else 'deny','risk':'high','authorization':'high' if override else 'low','rule':'general','reason':'此动作具体风险已展示；本人单次授权后才满足审核条件。','same_denied_outcome':not override})
        return
    since_user=[]
    for message in messages:
        for part in message.parts:
            if isinstance(part,UserPromptPart):since_user=[]
            elif isinstance(part,ToolReturnPart):since_user.append(part)
    if not since_user:
        target=Path(os.environ['MYRK_DATA_DIRECTORY']).parent/'outside.txt'
        yield {0:DeltaToolCall(name='create_text',json_args=json.dumps({'path':str(target),'content':'reviewed-once'}),tool_call_id='create'+str(len(messages)))}
    else:yield '本轮处理结束，具体动作见记录。'

@asynccontextmanager
async def factory(key):
    yield FunctionModel(stream_function=reply, profile={"context_window": 128000, "supports_inline_system_prompts": True})

if __name__=='__main__':main(data_directory=Path(sys.argv[1]),model_factory=factory)

