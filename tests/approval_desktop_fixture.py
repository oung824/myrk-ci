"""Controlled model boundary for real desktop/native approval acceptance."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from pydantic_ai import models
from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from myrk.desktop_service import main


async def reply(messages, info):
    directory = Path(os.environ["MYRK_APPROVAL_ACCEPTANCE_ROOT"])
    if info.instructions and "你是独立动作审核者" in info.instructions:
        payload = json.loads(next(part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)))
        goal = payload["goal"]
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            yield {0: DeltaToolCall(name="read_text", json_args='{"path":"review-evidence.txt"}', tool_call_id="review-read")}
            return
        assert "仅供独立审核核对" in str(results[-1].content)
        if "auto_stop" in goal:
            await asyncio.sleep(30)
        allowed = "auto_deny" not in goal or payload["one_time_user_authorization"]
        yield json.dumps({"decision": "allow" if allowed else "deny", "risk": "high", "authorization": "high" if allowed else "low", "rule": "general", "reason": "已只读核对真实资料，单个小文件及本次明确授权满足观察条件。" if allowed else "此文件写入的具体影响尚待本人明确同意，暂不允许执行。", "same_denied_outcome": not allowed}, ensure_ascii=False)
        return

    prompts = [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
    goal = next(prompt for prompt in prompts if prompt.startswith("桌面验收："))
    scenario = goal.removeprefix("桌面验收：")
    current_results = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                current_results = []
            elif isinstance(part, ToolReturnPart):
                current_results.append(part)
    if current_results:
        if scenario == "inside" and os.environ.get("MYRK_UI_LAYOUT_EVIDENCE") == "1":
            long_path = r"E:\Myrk\.scratch\受控界面验收" + r"\项目资料整理与逐项复核的长目录名称" * 6 + r"\完整中文记录与检查说明.md"
            yield (
                "本轮处理结束；具体文件、批准与审核结果见真实动作记录。\n\n"
                "## 受控离线界面验收\n\n"
                "以下段落、路径和代码只用于检查正式客户端的排版；没有调用真实模型，也没有创建示例长路径中的目录。"
                "本轮实际写入的中文产物仍由动作记录确认。\n\n"
                "检查长回答时，需要既能连续阅读中文，也能回到输入区继续提问。缩小窗口后，回答保留正常字号，"
                "页面内部可以上下滚动；复制回答和查看本轮记录仍应有明确入口。\n\n"
                "较长的资料位置应完整保留。它可以在允许的位置换行，不能挤开会话列表、输入框或窗口边缘。"
                "这里同时保留多段中文，检查段落间距、标点和多行阅读的实际效果。\n\n"
                f"示例资料位置：`{long_path}`。\n\n"
                "```python\n"
                f'source = r"{long_path}"\n'
                f'layout_marker = "{"controlled-layout-evidence-" * 24}"\n'
                "print(source, layout_marker)\n"
                "```\n\n"
                "以上代码仅作显示样例，不会执行。长代码应在代码块内部横向滚动，完整聊天与动作事实继续保留。"
            )
        else:
            yield "本轮处理结束；具体文件、批准与审核结果见真实动作记录。"
        return
    if scenario == "command_stop":
        arguments = ["-c", "from pathlib import Path; import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path('stop-child.pid').write_text(str(p.pid)); print('真实命令正在等待',flush=True); time.sleep(60)"]
        yield {0: DeltaToolCall(name="run_command", json_args=json.dumps({"program": os.environ["MYRK_PYTHON"], "arguments": arguments, "timeout_seconds": 60}), tool_call_id="actual-child-command")}
        return
    path = str(directory / f"{scenario}.txt")
    if scenario == "inside":
        path = "中文产物.md"
    yield {0: DeltaToolCall(name="create_text", json_args=json.dumps({"path": path, "content": f"真实桌面结果：{scenario}\n"}, ensure_ascii=False), tool_call_id="actual-create-" + scenario + str(len(messages)))}


@asynccontextmanager
async def factory(_key):
    yield FunctionModel(stream_function=reply, profile={"context_window": 128000, "supports_inline_system_prompts": True})


if __name__ == "__main__":
    models.ALLOW_MODEL_REQUESTS = False
    if receipts := os.environ.get("MYRK_PROCESS_RECEIPT_DIRECTORY"):
        from tests.support.process_receipt import write_process_receipt
        write_process_receipt(Path(receipts), __file__)
    main(data_directory=Path(os.environ["MYRK_DATA_DIRECTORY"]), model_factory=factory)
