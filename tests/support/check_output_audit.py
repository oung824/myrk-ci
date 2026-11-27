"""Stop a regression probe before it can write its forbidden report target."""

import os
import sys


blocked = os.environ["MYRK_CHECK_BLOCKED_OUTPUT"].casefold()


def prevent_output(event: str, arguments: tuple[object, ...]) -> None:
    if event == "open" and isinstance(arguments[0], (str, bytes, os.PathLike)):
        if os.path.abspath(os.fsdecode(arguments[0])).casefold() == blocked:
            raise RuntimeError("Test guard prevented an external report write")


sys.addaudithook(prevent_output)
