"""Stable discovery entry points for tools kept outside the model's tool declarations."""

from typing import Any
from hashlib import sha256
from dataclasses import replace
from collections.abc import Callable
import inspect
import json

from pydantic import ValidationError
from pydantic_ai import Agent, RunContext
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets.function import FunctionToolset

from myrk.actions import ToolOrigin, tool_origin


def _description(definition: ToolDefinition) -> dict[str, Any]:
    payload = {'name': definition.name, 'description': definition.description, 'schema': definition.parameters_json_schema}
    return {**payload, 'schema_version': sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()}


class ExtensionCatalog(FunctionToolset[None]):
    def __init__(self, *, on_schema_refresh_required: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.on_schema_refresh_required = on_schema_refresh_required
        self._refresh_required: set[str] = set()

    def install(self, agent: Agent[None, str]) -> None:
        @agent.tool
        async def search_extensions(ctx: RunContext[None], query: str) -> dict[str, Any]:
            """Find available extension tools by exact name or words in their description; then describe a selected tool."""
            tools = await self.get_tools(ctx)
            query = query.strip().casefold()
            matches: list[dict[str, Any]] = []
            for name, tool in sorted(tools.items()):
                definition = tool.tool_def
                keywords = (definition.metadata or {}).get('search_keywords', [])
                text = ' '.join([name, definition.description or '', *keywords]).casefold()
                if not query or query in text:
                    matches.append({'name': name, 'description': definition.description})
            matches.sort(key=lambda item: item['name'].casefold() != query)
            return {'status': 'ok', 'tools': matches}

        @agent.tool
        async def describe_extension(ctx: RunContext[None], name: str) -> dict[str, Any]:
            """Get an available extension's exact parameter schema and current schema_version before calling it."""
            selected = (await self.get_tools(ctx)).get(name)
            if selected is None:
                return {'status': 'unavailable', 'name': name, 'next': 'search_extensions'}
            self._refresh_required.discard(name)
            return {'status': 'ok', **_description(selected.tool_def)}

        @agent.tool(sequential=True)
        async def call_extension(ctx: RunContext[None], name: str, schema_version: str, arguments: dict[str, Any]) -> Any:
            """Call a described extension with its current schema_version and arguments through the existing execution boundary."""
            selected = (await self.get_tools(ctx)).get(name)
            if selected is None:
                return {'status': 'unavailable', 'name': name, 'next': 'search_extensions'}
            if schema_version != _description(selected.tool_def)['schema_version'] or name in self._refresh_required:
                if name not in self._refresh_required and self.on_schema_refresh_required is not None:
                    self.on_schema_refresh_required()
                self._refresh_required.add(name)
                return {'status': 'schema_refresh_required', 'name': name, 'next': 'describe_extension',
                        'message': 'Schema version does not match or needs refresh; describe the tool again before calling. No action was executed.'}
            actual_ctx = replace(ctx, tool_name=name)
            try:
                validated = selected.args_validator.validate_python(arguments, context=ctx.validation_context)
            except ValidationError as error:
                return {'status': 'invalid_arguments', 'name': name, 'errors': [
                    {'field': '.'.join(str(part) for part in item['loc']), 'type': item['type'], 'message': item['msg']}
                    for item in error.errors(include_url=False, include_context=False, include_input=False)
                ], 'next': 'correct arguments and call_extension'}
            if selected.args_validator_func is not None:
                result = selected.args_validator_func(actual_ctx, **validated)
                if inspect.isawaitable(result):
                    await result
            token = tool_origin.set(ToolOrigin(name, arguments, 'extension', schema_version))
            try:
                return await self.call_tool(name, validated, actual_ctx, selected)
            finally:
                tool_origin.reset(token)
