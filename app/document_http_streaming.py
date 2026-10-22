"""HTTP transport for trusted, worker-owned document download contexts."""
from __future__ import annotations

import asyncio

from starlette.requests import ClientDisconnect
from starlette.responses import Response, StreamingResponse

from app.application import ApplicationError


class DocumentDownloadResponse(Response):
    """Keep the source context open across every actual ASGI send."""

    def __init__(self, context_factory):
        # Initialize Response's interface without rendering document content.
        super().__init__(content=None)
        self._context_factory = context_factory

    async def _stream_with_disconnect(self, response, receive, send):
        caller = asyncio.current_task()
        initial_cancelling = caller.cancelling()
        disconnect_token = object()
        disconnect_cancelled = False
        streaming = True

        async def listen():
            nonlocal disconnect_cancelled
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    # An existing caller cancellation must retain precedence.
                    if streaming and caller.cancelling() == initial_cancelling:
                        disconnect_cancelled = caller.cancel(disconnect_token)
                    return

        listener = asyncio.create_task(listen())
        try:
            # Control exceptions from sends must stay in the calling task.
            await response.stream_response(send)
        except asyncio.CancelledError as error:
            if not (disconnect_cancelled
                    and error.args == (disconnect_token,)
                    and caller.cancelling() == initial_cancelling + 1):
                raise
        finally:
            streaming = False
            if disconnect_cancelled:
                # Remove only our request, including when an external request
                # raced it. Never consume the external cancellation count.
                caller.uncancel()
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

    async def __call__(self, scope, receive, send):
        async with self._context_factory() as download:
            deadline = asyncio.timeout(download.remaining_seconds)
            try:
                async with deadline:
                    # An already exhausted deadline must not expose headers.
                    await asyncio.sleep(0)
                    response = StreamingResponse(
                        download.iter_bytes(),
                        media_type=download.media_type,
                        headers={
                            "Content-Disposition":
                                f'attachment; filename="{download.filename}"',
                            "Content-Length": str(download.size_bytes),
                            "X-Content-SHA256": download.sha256,
                            "Cache-Control": "no-store",
                        },
                    )
                    try:
                        version = tuple(map(int, scope.get("asgi", {}).get(
                            "spec_version", "2.0").split(".")))
                        if version >= (2, 4):
                            try:
                                await response.stream_response(send)
                            except TimeoutError:
                                # TimeoutError is also an OSError, but a send's
                                # own timeout is not a client disconnect.
                                raise
                            except OSError as error:
                                raise ClientDisconnect() from error
                        else:
                            await self._stream_with_disconnect(response, receive, send)
                    finally:
                        await response.body_iterator.aclose()
            except TimeoutError:
                if not deadline.expired():
                    raise
                # After headers, abort without a replacement response or a
                # successful terminal body. Before headers, expose the same 408.
                raise ApplicationError(
                    408, "operation_timeout", "Document operation timed out.") from None
