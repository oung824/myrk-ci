"""Actual harmless OS enforcement through the official executor's public messages."""

import asyncio
import base64
import sys
from pathlib import Path

import pytest

from myrk.executor import ExecutorError, OfficialExecutor, POWERSHELL
from myrk.permissions import AccessSettings


def test_nested_workspace_write_grant_keeps_active_runtime_files_read_only(tmp_path: Path) -> None:
    runtime = tmp_path / "owned-runtime-fixture"
    folder = runtime / "dependencies"
    folder.mkdir(parents=True)
    target = folder / "fixture.txt"
    target.write_bytes(b"startup-runtime-version")
    async def run():
        executor = OfficialExecutor(tmp_path / "records" / "executor", runtime_roots=[runtime])
        try:
            sandbox = await executor.verify_scope(folder, AccessSettings(writable_roots=[str(folder)]))
            with pytest.raises(ExecutorError):
                await executor.request("fs/writeFile", {"path": target.as_uri(), "dataBase64": "Y2hhbmdlZA==", "followSymlinks": False, "sandbox": sandbox})
            assert await executor.read_bytes(target, folder, AccessSettings(writable_roots=[str(folder)])) == b"startup-runtime-version"
        finally:
            await executor.close()
    asyncio.run(run())


def test_full_access_really_removes_the_local_file_sandbox(tmp_path: Path) -> None:
    async def run():
        folder = tmp_path / "task"
        folder.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"before")
        executor = OfficialExecutor(tmp_path / "records" / "executor")
        access = AccessSettings(sandbox="full_access", writable_roots=[str(folder)])
        try:
            sandbox = await executor.verify_scope(folder, access)
            result = await executor.run([POWERSHELL, "-NoProfile", "-Command", "[IO.File]::WriteAllText('" + str(outside).replace("'", "''") + "','full-access-result')"], folder, sandbox=sandbox)
            assert result["sandboxType"] == "none"
            assert result["exitCode"] == 0
            assert await executor.read_bytes(outside, folder, access) == b"full-access-result"
        finally:
            await executor.close()
    asyncio.run(run())


def test_explicitly_disallowed_native_loopback_fails_without_falling_back(tmp_path: Path) -> None:
    from myrk.permissions import NetworkSettings
    async def run():
        executor = OfficialExecutor(tmp_path / "records" / "executor")
        try:
            with pytest.raises(ExecutorError, match="allow_local_binding=false"):
                await executor.verify_scope(tmp_path, AccessSettings(writable_roots=[str(tmp_path)], network=NetworkSettings(allow_local_binding=False)))
            assert not (tmp_path / "unexpected.txt").exists()
        finally:
            await executor.close()
    asyncio.run(run())

def test_native_network_wait_keeps_file_messages_responsive_and_denies_new_tuple(tmp_path: Path) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from myrk.permissions import NetworkSettings
    received = []
    requests = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204)
            self.end_headers()
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    async def run():
        waiting, release = asyncio.Event(), asyncio.Event()
        async def decide(params):
            requests.append(params)
            waiting.set()
            await release.wait()
            return len(requests) == 1
        executor = OfficialExecutor(tmp_path / "records" / "executor", network_decision=decide)
        network = NetworkSettings(allow_local_binding=True)
        access = AccessSettings(writable_roots=[str(tmp_path)], network=network)
        fixture = tmp_path / "fixture.txt"
        fixture.write_bytes(b"duplex-read-result")
        script = "import os,http.client,urllib.parse,sys;p=urllib.parse.urlsplit(os.environ['HTTP_PROXY']);c=http.client.HTTPConnection(p.hostname,p.port);c.request('POST',sys.argv[1],b'approved-network-body');print(c.getresponse().status)"
        try:
            sandbox = await executor.verify_scope(tmp_path, access)
            running = asyncio.create_task(executor.run([sys.executable, "-c", script, f"http://127.0.0.1:{server.server_port}/receive"], tmp_path, sandbox=sandbox, network=network))
            await asyncio.wait_for(waiting.wait(), 10)
            assert received == []
            assert await asyncio.wait_for(executor.read_bytes(fixture, tmp_path, access), 5) == b"duplex-read-result"
            release.set()
            result = await running
            assert result["exitCode"] == 0 and result["stdout"].strip() == "204", result
            denied = await executor.run([sys.executable, "-c", script, f"http://localhost:{server.server_port}/receive"], tmp_path, sandbox=sandbox, network=network)
            assert denied["stdout"].strip() == "403", denied
            assert received == [b"approved-network-body"]
            assert [item["request"] for item in requests] == [
                {"protocol": "http", "host": "127.0.0.1", "port": server.server_port},
                {"protocol": "http", "host": "localhost", "port": server.server_port},
            ]
            assert requests[0]["processId"] != requests[1]["processId"]
        finally:
            await executor.close()
    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_official_program_returns_all_output_after_the_program_has_exited(tmp_path: Path) -> None:
    async def run():
        executor = OfficialExecutor(tmp_path / "records" / "executor")
        try:
            sandbox = await executor.verify_scope(tmp_path, AccessSettings(writable_roots=[str(tmp_path)]))
            result = await executor.run([sys.executable, "-c", "import os;os.write(1,b'A'*90000+b'\\nreal-tail-1937\\n');os.write(2,b'B'*90000+b'\\nerror-tail-1937\\n')"], tmp_path, sandbox=sandbox)
            assert result["exitCode"] == 0
            assert result["stdout"] == "A" * 90000 + "\nreal-tail-1937\n"
            assert result["stderr"] == "B" * 90000 + "\nerror-tail-1937\n"
        finally:
            await executor.close()
    asyncio.run(run())


def test_official_file_and_process_messages_cannot_bypass_restricted_access(tmp_path: Path) -> None:
    allowed, outside, denied = (tmp_path / name for name in ("allowed", "outside", "denied"))
    for folder in (allowed, outside, denied, allowed / ".git"):
        folder.mkdir(parents=True, exist_ok=True)
    original = outside / "original.txt"
    original.write_bytes(b"outside-original")
    secret = denied / "fixture.txt"
    secret.write_bytes(b"explicit-deny-fixture")
    protected = allowed / ".git" / "fixture.txt"
    protected.write_bytes(b"protected-original")
    access = AccessSettings(writable_roots=[str(allowed)], denied_paths=[str(denied)])

    async def run():
        executor = OfficialExecutor(tmp_path / "records" / "executor")
        try:
            sandbox = await executor.verify_scope(allowed, access)
            content = base64.b64encode(b"allowed-new-content").decode("ascii")
            await executor.request("fs/writeFile", {"path": (allowed / "created.txt").as_uri(), "dataBase64": content, "followSymlinks": False, "sandbox": sandbox})
            assert await executor.read_bytes(allowed / "created.txt", allowed, access) == b"allowed-new-content"
            for target in (original, protected):
                with pytest.raises(ExecutorError):
                    await executor.request("fs/writeFile", {"path": target.as_uri(), "dataBase64": content, "followSymlinks": False, "sandbox": sandbox})
            with pytest.raises(ExecutorError):
                await executor.read_bytes(secret, allowed, access)
            command = f"[IO.File]::ReadAllText('{str(secret).replace(chr(39), chr(39) * 2)}')"
            result = await executor.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command], allowed, sandbox=sandbox)
            assert result["sandboxType"] == "windowsMxc" and result["exitCode"] != 0
            assert "explicit-deny-fixture" not in result["stdout"]
            readonly = AccessSettings(writable_roots=[str(allowed)], read_only=True)
            readonly_sandbox = await executor.verify_scope(allowed, readonly)
            with pytest.raises(ExecutorError):
                await executor.request("fs/writeFile", {"path": (allowed / "reviewer.txt").as_uri(), "dataBase64": content, "sandbox": readonly_sandbox})
            assert original.read_bytes() == b"outside-original"
            assert protected.read_bytes() == b"protected-original"
            assert not (allowed / "reviewer.txt").exists()
        finally:
            await executor.close()
    asyncio.run(run())
