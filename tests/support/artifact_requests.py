import json

import httpx2

from myrk.conversation import TaskScope
from myrk.provider import deepseek_model


def reply(tool=None, arguments=None, text='已交付。', call_id='call'):
    delta = {'role': 'assistant'}
    if tool:
        delta['tool_calls'] = [{'index': 0, 'id': call_id, 'type': 'function',
            'function': {'name': tool, 'arguments': json.dumps(arguments or {})}}]
    else:
        delta['content'] = text
    chunk = {'id': call_id, 'object': 'chat.completion.chunk', 'created': 1, 'model': 'deepseek-flash',
        'choices': [{'index': 0, 'delta': delta, 'finish_reason': 'tool_calls' if tool else 'stop'}]}
    return httpx2.Response(200, text=f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n',
        headers={'content-type': 'text/event-stream'})


async def ask_with_transport(conversation, root, respond, **kwargs):
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        async with deepseek_model('synthetic-only', http_client=http) as model:
            return await conversation.ask('处理并明确交付报告。', document=None,
                scope=TaskScope(root=str(root), allowed_actions=['read', 'edit', 'check'],
                    provider='deepseek', recipients=['deepseek']),
                model=model, **kwargs)
