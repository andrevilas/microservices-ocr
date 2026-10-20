"""HTTP transport for trusted, worker-owned document download contexts."""
from __future__ import annotations

import asyncio

from starlette.responses import Response, StreamingResponse

from app.application import ApplicationError


class DocumentDownloadResponse(Response):
    """Keep the source context open across every actual ASGI send."""

    def __init__(self, context_factory):
        # Initialize Response's interface without rendering document content.
        super().__init__(content=None)
        self._context_factory = context_factory

    async def __call__(self, scope, receive, send):
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                # Sending headers may itself block or fail.
                started = True
            await send(message)

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
                    await response(scope, receive, tracked_send)
            except TimeoutError:
                if not deadline.expired():
                    raise
                error = ApplicationError(
                    408, "operation_timeout", "Document operation timed out.")
                if started:
                    # Abort the existing response; never send replacement JSON
                    # or a successful terminal body after a partial download.
                    raise error from None
                raise error from None
