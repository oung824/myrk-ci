"""Pinned official executor: transport and enforced operating-system access."""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from hashlib import sha256
import json
from importlib.resources import files
import os
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field

from myrk.permissions import AccessSettings, NetworkSettings

_RELEASE: dict[str, str] = json.loads(files("myrk").joinpath("executor-release.json").read_text(encoding="utf-8"))
EXECUTOR_RELEASE = _RELEASE["release"]
EXECUTOR_SHA256 = _RELEASE["sha256"]
POWERSHELL = r"C:\Program Files\PowerShell\7\pwsh.exe"


class ExecutorError(OSError):
    """The requested enforcement or execution could not be confirmed."""


class ExecutorRequestError(ExecutorError):
    """The connected executor explicitly reported NotFound (-32004) for one request."""


class FileConflict(ExecutorError):
    """The locked current file no longer matches the prepared original."""


class NetworkConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["http", "https_connect", "socks5_tcp", "socks5_udp"]
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(strict=True, ge=1, le=65535)


class NetworkPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    processId: str = Field(min_length=1, max_length=256)
    request: NetworkConnection


class OfficialExecutor:
    def __init__(self, directory: Path, *, binary: Path | None = None,
                 protected_paths: list[Path] | None = None,
                 runtime_roots: list[Path] | None = None,
                 network_decision: Callable[[dict[str, Any]], Awaitable[bool]] | None = None) -> None:
        self.directory = directory.resolve()
        self.runtime_roots = runtime_roots or []
        self.runtime_error: str | None = None
        if binary is None:
            from myrk.supervisor_runtime import SUPERVISOR_RUNTIME
            binary = SUPERVISOR_RUNTIME.binary
            self.runtime_roots = [*self.runtime_roots, SUPERVISOR_RUNTIME.directory, *SUPERVISOR_RUNTIME.host_roots]
            self.runtime_error = SUPERVISOR_RUNTIME.error
        self.binary = binary
        self.network_decision = network_decision
        self.protected_paths = [*(protected_paths if protected_paths is not None else [self.directory.parent]), *self.runtime_roots]
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.errors: asyncio.Task[bytes] | None = None
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.decisions: set[asyncio.Task[None]] = set()
        self.network_processes: dict[str, float] = {}
        self.process_decisions: dict[str, set[asyncio.Task[Any]]] = {}
        self.counter = 0
        self.start_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.environment_info: dict[str, Any] = {}
        self.verified_scopes: dict[str, str] = {}
        self.last_sandbox_type: str | None = None
        self.failure: ExecutorError | None = None

    async def start(self) -> None:
        async with self.start_lock:
            if self.process is not None:
                return
            if self.runtime_error is not None:
                raise ExecutorError("可信执行副本准备失败，已停止；没有回退到应用源码：" + self.runtime_error)
            if not self.binary.is_absolute() or not self.binary.is_file():
                raise ExecutorError("缺少固定官方执行程序；请设置 MYRK_EXECUTOR_PATH（指定执行程序完整位置的本机启动设置），未改用不受限制的执行方式。")
            if sha256(self.binary.read_bytes()).hexdigest() != EXECUTOR_SHA256:
                raise ExecutorError("官方执行程序的文件指纹不符，已停止。")
            home = self.directory / "home"
            temporary = self.directory / "temporary"
            home.mkdir(parents=True, exist_ok=True)
            temporary.mkdir(parents=True, exist_ok=True)
            (home / "config.toml").write_text('[analytics]\nenabled = false\n[otel]\nexporter = "none"\ntrace_exporter = "none"\nmetrics_exporter = "none"\n', encoding="utf-8")
            environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "SystemDrive", "COMSPEC", "PATHEXT", "PATH", "USERPROFILE", "USERNAME", "PROGRAMFILES", "PROGRAMDATA") if key in os.environ}
            environment.update(CODEX_HOME=str(home), TEMP=str(temporary), TMP=str(temporary), APPDATA=str(home / "roaming"), LOCALAPPDATA=str(home / "local"))
            self.process = await asyncio.create_subprocess_exec(str(self.binary), "--listen", "stdio", "--strict-config", "-c", "analytics.enabled=false", "-c", 'otel.exporter="none"', "-c", 'otel.trace_exporter="none"', "-c", 'otel.metrics_exporter="none"', cwd=self.directory, env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=0x08000000,
                limit=16 * 1024 * 1024)
            assert self.process.stderr is not None
            self.errors = asyncio.create_task(self.process.stderr.read())
            self.reader = asyncio.create_task(self._read_messages())
            response = await self.request("initialize", {"clientName": "Myrk"})
            info = response.get("environmentInfo")
            if not isinstance(info, dict) or not isinstance(info.get("capabilities"), dict):
                raise ExecutorError("执行程序没有确认所需电脑系统限制能力，已停止。")
            self.environment_info = info
            await self._send({"method": "initialized", "params": {}})

    async def _send(self, message: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        async with self.write_lock:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.process.stdin.drain()

    async def _read_messages(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ExecutorError("执行程序返回不合要求的消息，已停止。")
                if "method" in message and "id" in message:
                    task = asyncio.create_task(self._answer_network(message))
                    self.decisions.add(task)
                    task.add_done_callback(self.decisions.discard)
                elif isinstance(message.get("id"), int) and message["id"] in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            error = message["error"]
                            if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                                raise ExecutorError("执行程序返回不合要求的错误消息，已停止。")
                            error_type = ExecutorRequestError if error["code"] == -32004 else ExecutorError
                            future.set_exception(error_type("执行程序拒绝或未完成动作：" + json.dumps(error, ensure_ascii=False)))
                        elif isinstance(message.get("result"), dict):
                            future.set_result(message["result"])
                        else:
                            future.set_exception(ExecutorError("执行程序返回缺少动作结果。"))
        except (OSError, ValueError) as error:
            self.failure = ExecutorError(str(error))
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ExecutorError(str(error)))
        finally:
            self.failure = self.failure or ExecutorError("执行程序连接已断开；未自动重做可能已发生的动作。")
            for task in list(self.decisions):
                task.cancel()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.failure)

    async def _answer_network(self, message: dict[str, Any]) -> None:
        allowed = False
        params = message.get("params")
        process_id = params.get("processId") if isinstance(params, dict) else None
        if not isinstance(process_id, str):
            process_id = ""
        task = asyncio.current_task()
        if process_id in self.network_processes and task is not None:
            self.process_decisions.setdefault(process_id, set()).add(task)
        try:
            if message["method"] == "network/policyRequest" and self.network_decision is not None and process_id in self.network_processes:
                request = NetworkPolicyRequest.model_validate(params)
                async with asyncio.timeout(self.network_processes[process_id]):
                    allowed = await self.network_decision(request.model_dump())
        except (OSError, ValueError, TimeoutError):
            allowed = False
        finally:
            if task is not None:
                self.process_decisions.get(process_id, set()).discard(task)
            if self.failure is None and self.process is not None and self.process.returncode is None:
                await self._send({"id": message["id"], "result": {"decision": {"type": "allow"} if allowed and process_id in self.network_processes else {"type": "deny", "reason": "Myrk did not approve this exact connection"}}})

    async def request(self, method: str, params: dict[str, Any], *, timeout: float = 15) -> dict[str, Any]:
        if self.failure is not None:
            raise self.failure
        self.counter += 1
        identifier = self.counter
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self._send({"id": identifier, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(identifier, None)

    def sandbox(self, cwd: Path, access: AccessSettings, *, extra_write: Path | None = None) -> dict[str, Any] | None:
        if access.sandbox == "full_access" and not access.read_only:
            return None
        roots = [Path(root).resolve() for root in access.writable_roots]
        entries: list[dict[str, Any]] = [{"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"}]
        if not access.read_only:
            entries.extend({"path": {"type": "path", "path": root.as_uri()}, "access": "write"} for root in [*roots, self.directory / "temporary"]
                           if not any(root.is_relative_to(runtime) for runtime in self.runtime_roots))
        for root in roots:
            entries.extend({"path": {"type": "path", "path": (root / name).as_uri()}, "access": "read"} for name in (".git", ".agents", ".codex", ".aws"))
        entries.extend({"path": {"type": "path", "path": path.resolve().as_uri()}, "access": "read"} for path in self.protected_paths)
        if extra_write is not None and not access.read_only:
            entries.append({"path": {"type": "path", "path": extra_write.as_uri()}, "access": "write"})
        entries.extend({"path": {"type": "path", "path": Path(path).resolve().as_uri()}, "access": "deny"} for path in access.denied_paths)
        return {"permissions": {"type": "managed", "file_system": {"type": "restricted", "entries": entries}, "network": "restricted"}, "cwd": cwd.as_uri(), "workspaceRoots": [root.as_uri() for root in roots], "windowsSandboxLevel": "mxc"}

    async def verify_scope(self, cwd: Path, access: AccessSettings, *, extra_write: Path | None = None) -> dict[str, Any] | None:
        if access.sandbox != "full_access" and extra_write is not None and any(extra_write.resolve().is_relative_to(path) for path in self.runtime_roots):
            raise ExecutorError("应用正在使用的运行资料不能作为任务的额外写入位置。")
        await self.start()
        sandbox = self.sandbox(cwd, access, extra_write=extra_write)
        if sandbox is not None and (self.environment_info["capabilities"].get("windowsMxc") is not True or self.environment_info["capabilities"].get("sandboxedFileStreaming") is not True):
            raise ExecutorError("执行程序没有确认所需电脑系统限制能力，已停止。")
        identity = json.dumps(sandbox, sort_keys=True)
        if identity not in self.verified_scopes:
            try:
                result = await self.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "exit 0"], cwd, sandbox=sandbox, network=access.network)
            except ExecutorRequestError as error:
                raise ExecutorError("系统访问范围自检未完成，未执行证据检查：" + str(error)) from error
            if result["exitCode"] != 0:
                raise ExecutorError("系统访问范围自检失败，未执行文件动作。")
            self.verified_scopes[identity] = result["sandboxType"]
        self.last_sandbox_type = self.verified_scopes[identity]
        return sandbox

    async def read_bytes(self, path: Path, cwd: Path, access: AccessSettings) -> bytes:
        sandbox = await self.verify_scope(cwd, access)
        handle_id = uuid4().hex
        await self.request("fs/open", {"handleId": handle_id, "path": path.as_uri(), "mode": "read", "sandbox": sandbox})
        content = bytearray()
        try:
            while True:
                response = await self.request("fs/readBlock", {"handleId": handle_id, "offset": len(content), "len": 32768})
                if not isinstance(response.get("chunk"), str) or not isinstance(response.get("eof"), bool):
                    raise ExecutorError("执行程序未返回有效文件片段。")
                block = base64.b64decode(response["chunk"], validate=True)
                content.extend(block)
                if response["eof"]:
                    return bytes(content)
                if not block:
                    raise ExecutorError("执行程序在文件结束前返回空片段，已停止。")
        finally:
            await self.request("fs/close", {"handleId": handle_id})

    async def metadata(self, path: Path, cwd: Path, access: AccessSettings) -> dict[str, Any]:
        sandbox = await self.verify_scope(cwd, access)
        return await self.request("fs/getMetadata", {"path": path.as_uri(), "followSymlinks": False, "sandbox": sandbox})

    async def list_directory(self, path: Path, cwd: Path, access: AccessSettings) -> list[dict[str, Any]]:
        sandbox = await self.verify_scope(cwd, access)
        result = await self.request("fs/readDirectory", {"path": path.as_uri(), "sandbox": sandbox})
        entries = result.get("entries")
        if not isinstance(entries, list) or any(not isinstance(entry, dict) or not isinstance(entry.get("fileName"), str) or not isinstance(entry.get("isDirectory"), bool) for entry in entries):
            raise ExecutorError("执行程序未返回有效目录资料。")
        return entries

    async def replace_existing(self, path: Path, data: bytes, expected_sha256: str, cwd: Path, access: AccessSettings, *, approved: bool = False) -> dict[str, Any]:
        sandbox = await self.verify_scope(cwd, access, extra_write=path if approved else None)
        encoded_path = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
        staged = self.directory / "staging" / uuid4().hex
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(data)
        encoded_source = base64.b64encode(str(staged).encode("utf-8")).decode("ascii")
        script = f"""$ErrorActionPreference='Stop'
$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
$replacement=[IO.File]::ReadAllBytes([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_source}')))
$stream=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read)
try {{
  $original=[byte[]]::new($stream.Length)
  $stream.ReadExactly($original)
  $digest=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($original)).ToLowerInvariant()
  if ($digest -ne '{expected_sha256}') {{ [Console]::Error.WriteLine('MYRK_FILE_CONFLICT'); exit 73 }}
  $stream.Position=0
  $stream.Write($replacement)
  $stream.SetLength($replacement.Length)
  $stream.Flush($true)
}} finally {{ $stream.Dispose() }}
"""
        encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            result = await self.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script], cwd, sandbox=sandbox)
        finally:
            staged.unlink(missing_ok=True)
        if result["exitCode"] == 73:
            raise FileConflict("原文在写入前已变化，未覆盖；请重新读取后再决定。")
        if result["exitCode"] != 0:
            raise ExecutorError("受限文件修改未成功：" + result["stderr"])
        return result

    async def create_file(self, path: Path, data: bytes, cwd: Path, access: AccessSettings, *, approved: bool = False, create_parents: list[Path] | None = None) -> dict[str, Any]:
        sandbox = await self.verify_scope(cwd, access, extra_write=path if approved else None)
        if approved and sandbox is not None and not access.read_only:
            entries = sandbox["permissions"]["file_system"]["entries"]
            deny = [entry for entry in entries if entry["access"] == "deny"]
            entries[:] = [entry for entry in entries if entry["access"] != "deny"]
            entries.extend({"path": {"type": "path", "path": parent.as_uri()}, "access": "write"} for parent in create_parents or [])
            creation_parent = create_parents[-1].parent if create_parents else path.parent
            entries.append({"path": {"type": "path", "path": creation_parent.as_uri()}, "access": "write"})
            entries.extend(deny)
        for parent in reversed(create_parents or []):
            await self.request("fs/createDirectory", {"path": parent.as_uri(), "recursive": False, "followSymlinks": False, "sandbox": sandbox})
        encoded_path = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
        staged = self.directory / "staging" / uuid4().hex
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(data)
        encoded_source = base64.b64encode(str(staged).encode("utf-8")).decode("ascii")
        script = f"""$ErrorActionPreference='Stop'
$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
[void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
$stream=[IO.File]::Open($path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
try {{ $stream.Write([IO.File]::ReadAllBytes([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_source}')))); $stream.Flush($true) }} finally {{ $stream.Dispose() }}
"""
        encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            result = await self.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script], cwd, sandbox=sandbox)
        finally:
            staged.unlink(missing_ok=True)
        if result["exitCode"] != 0:
            raise ExecutorError("新建未成功；已有同名文件不会覆盖：" + result["stderr"])
        return result

    async def delete_existing(self, path: Path, expected_sha256: str, cwd: Path, access: AccessSettings, *, approved: bool = False) -> dict[str, Any]:
        sandbox = await self.verify_scope(cwd, access, extra_write=path if approved else None)
        encoded_path = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
        script = f"""$ErrorActionPreference='Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class MyrkDelete {{
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 public static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint mode, uint flags, IntPtr template);
 [DllImport("kernel32.dll", SetLastError=true)]
 public static extern bool SetFileInformationByHandle(SafeFileHandle handle, int type, byte[] value, uint length);
}}
'@
$path=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
$handle=[MyrkDelete]::CreateFileW($path,2147549184,1,[IntPtr]::Zero,3,0,[IntPtr]::Zero)
if ($handle.IsInvalid) {{ throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error()) }}
$stream=[IO.FileStream]::new($handle,[IO.FileAccess]::Read)
try {{
 $original=[byte[]]::new($stream.Length); $stream.ReadExactly($original)
 $digest=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($original)).ToLowerInvariant()
 if ($digest -ne '{expected_sha256}') {{ [Console]::Error.WriteLine('MYRK_FILE_CONFLICT'); exit 73 }}
 if (-not [MyrkDelete]::SetFileInformationByHandle($handle,4,[byte[]]@(1),1)) {{ throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error()) }}
}} finally {{ $stream.Dispose() }}
"""
        encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        result = await self.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script], cwd, sandbox=sandbox)
        if result["exitCode"] == 73:
            raise FileConflict("原文在删除前已变化，未删除；请重新读取后再决定。")
        if result["exitCode"] != 0:
            raise ExecutorError("受限文件删除未成功：" + result["stderr"])
        return result

    async def run(self, argv: list[str], cwd: Path, *, sandbox: dict[str, Any] | None, timeout: float | None = 60,
                  output: Callable[[str, bytes], None] | None = None, environment: dict[str, str] | None = None,
                  started: Callable[[str], None] | None = None, network: NetworkSettings | None = None) -> dict[str, Any]:
        await self.start()
        process_id = uuid4().hex
        params: dict[str, Any] = {"processId": process_id, "argv": argv, "cwd": cwd.as_uri(), "envPolicy": {"inherit": "none", "ignoreDefaultExcludes": False, "exclude": [], "set": {}, "includeOnly": []}, "env": {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"), "WINDIR": os.environ.get("WINDIR", r"C:\Windows"), "TEMP": str(self.directory / "temporary"), "TMP": str(self.directory / "temporary"), **(environment or {})}, "tty": False, "pipeStdin": False, "arg0": None, "sandbox": sandbox, "enforceManagedNetwork": sandbox is not None}
        if sandbox is not None:
            if self.environment_info["capabilities"].get("networkProxyLaunch") is not True:
                raise ExecutorError("执行程序未确认受控联网能力，未启动程序。")
            network = network or NetworkSettings()
            params["networkProxy"] = {"proxy": {"enabled": True, "enableSocks5": True, "enableSocks5Udp": False, "allowUpstreamProxy": False, "dangerouslyAllowAllUnixSockets": False,
                "mode": "full", "domains": {**{host: "allow" for host in network.allowed_hosts}, **{host: "deny" for host in network.denied_hosts}}, "unixSockets": None, "allowLocalBinding": network.allow_local_binding},
                "policyDecisionTimeoutMs": int(network.decision_timeout_seconds * 1000), "executionId": process_id}
            self.network_processes[process_id] = network.decision_timeout_seconds
        sequence = 0
        launched = False
        collected: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            launch = await self.request("process/start", params)
            launched = True
            if launch.get("sandboxType") != ("none" if sandbox is None else "windowsMxc"):
                raise ExecutorError("实际执行方式未符合本次访问设置，已停止。")
            if started is not None:
                started(launch["sandboxType"])
            async with asyncio.timeout(timeout):
                while True:
                    result = await self.request("process/read", {"processId": process_id, "afterSeq": sequence, "maxBytes": 32768, "waitMs": 200})
                    sequence = result["nextSeq"] - 1
                    for chunk in result.get("chunks", []):
                        if chunk.get("stream") in collected:
                            block = base64.b64decode(chunk["chunk"], validate=True)
                            collected[chunk["stream"]].extend(block)
                            if output is not None:
                                output(chunk["stream"], block)
                    if result["closed"] and not result.get("chunks"):
                        result.update({name: bytes(data).decode("utf-8", errors="replace") for name, data in collected.items()})
                        result.update(sandboxType=launch["sandboxType"], processId=process_id)
                        return result
        finally:
            self.network_processes.pop(process_id, None)
            for decision in list(self.process_decisions.pop(process_id, set())):
                decision.cancel()
            if self.failure is None and self.process is not None and self.process.returncode is None:
                await self.request("process/terminate", {"processId": process_id})
            if launched and self.failure is None and self.process is not None and self.process.returncode is None:
                async with asyncio.timeout(5):
                    while True:
                        remaining = await self.request("process/read", {"processId": process_id, "afterSeq": sequence, "maxBytes": 32768, "waitMs": 50})
                        sequence = remaining["nextSeq"] - 1
                        for chunk in remaining.get("chunks", []):
                            if chunk.get("stream") in collected:
                                block = base64.b64decode(chunk["chunk"], validate=True)
                                collected[chunk["stream"]].extend(block)
                                if output is not None:
                                    output(chunk["stream"], block)
                        if remaining["closed"] and not remaining.get("chunks"):
                            break

    async def close(self) -> None:
        self.network_processes.clear()
        for decision in list(self.decisions):
            decision.cancel()
        await asyncio.gather(*self.decisions, return_exceptions=True)
        if self.process is not None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader is not None:
            await self.reader
        if self.errors is not None:
            await self.errors
