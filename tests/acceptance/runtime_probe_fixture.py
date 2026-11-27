"""Explicit subprocess probes; normal test discovery does not collect this file."""

import asyncio
import logging
import sys


def test_real_async_generator_close_failure():
    retained = []

    async def generator():
        try:
            yield "actual synthetic generator yield"
        finally:
            raise RuntimeError("synthetic-generator-close-8149")

    async def work():
        item = generator()
        retained.append(item)
        assert await anext(item) == "actual synthetic generator yield"

    asyncio.run(work())


def test_real_unhandled_task_failure():
    async def fail():
        raise RuntimeError("synthetic-unhandled-task-9271")

    async def work():
        asyncio.create_task(fail())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(work())


def test_controlled_action_diagnostic_is_not_an_asyncio_shutdown_failure():
    logging.getLogger("myrk.actions").error("Task exception was never retrieved: controlled synthetic action stderr")
    print("an error occurred during closing of asynchronous generator: controlled action output", file=sys.stderr)
