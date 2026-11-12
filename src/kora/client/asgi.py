import asyncio
from collections.abc import Awaitable, Callable, MutableMapping

import httpx

Message = MutableMapping[str, object]
Scope = MutableMapping[str, object]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class AsgiResponseLimit(RuntimeError):
    pass


class AsgiResponseIncomplete(RuntimeError):
    pass


class InProcessTransport:
    def __init__(self, application: ASGIApp, max_connections: int) -> None:
        self.application = application
        self.slots = asyncio.Semaphore(max_connections)

    async def receive(
        self, request: httpx.Request, limit: int, sending: Callable[[], None]
    ) -> httpx.Response:
        size = 0
        exceeded = False
        completed = False

        async def bounded(scope: Scope, receive: Receive, send: Send) -> None:
            async def bounded_send(message: Message) -> None:
                nonlocal size, exceeded, completed
                if message["type"] == "http.response.body":
                    body: object = message.get("body", b"")
                    if not isinstance(body, bytes):
                        raise TypeError("ASGI response body must be bytes")
                    if len(body) > limit - size:
                        exceeded = True
                        raise AsgiResponseLimit("ASGI response exceeds its byte limit")
                    size += len(body)
                await send(message)
                if message["type"] == "http.response.body" and not message.get(
                    "more_body", False
                ):
                    completed = True

            sending()
            await self.application(scope, receive, bounded_send)
            if not completed:
                raise AsgiResponseIncomplete("ASGI response did not finish")

        async with (
            self.slots,
            httpx.ASGITransport(app=bounded, raise_app_exceptions=False) as transport,
        ):
            response = await transport.handle_async_request(request)
            if exceeded:
                await response.aclose()
                raise AsgiResponseLimit("ASGI response exceeds its byte limit")
            if not completed:
                await response.aclose()
                raise AsgiResponseIncomplete("ASGI response did not finish")
            return response

    async def aclose(self) -> None:
        return
