"""Synchronize acceptance ledger writes and in-process cancellation."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import wraps
from pathlib import Path
from pydantic_ai import CancellationToken
from typing import Concatenate, ParamSpec, Protocol, TypeVar


ACTIVE_CONTROLS: dict[Path, list[tuple[str | None, CancellationToken]]] = {}


class _LedgerTransactionHost(Protocol):
    def transaction(self) -> AbstractContextManager[None]: ...


Receiver = TypeVar("Receiver", bound=_LedgerTransactionHost)
P = ParamSpec("P")


T = TypeVar("T")


def ledger_mutation(operation: Callable[Concatenate[Receiver, P], T]) -> Callable[Concatenate[Receiver, P], T]:
    @wraps(operation)
    def locked(self: Receiver, /, *args: P.args, **kwargs: P.kwargs) -> T:
        with self.transaction():
            return operation(self, *args, **kwargs)
    return locked
