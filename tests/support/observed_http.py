from collections.abc import Callable, Coroutine

import httpx2

from myrk.observed_http import ObservedHTTPTransport


def observed_mock_transport(
    respond: Callable[[httpx2.Request], httpx2.Response] | Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]],
) -> ObservedHTTPTransport:
    return ObservedHTTPTransport(httpx2.MockTransport(respond))
