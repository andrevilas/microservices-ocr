"""Preserve FileResponse Range handling while pinning an owned artifact directory."""
from fastapi.responses import FileResponse


class LeasedFileResponse(FileResponse):
    def __init__(self, store, job_id, owner, *, filename):
        self.store, self.job_id = store, job_id
        job = store.acquire_artifacts(job_id, owner)
        try:
            from app.application import ApplicationError
            if job.status != "completed" or not job.output_pdf_path or not job.output_pdf_path.is_file():
                raise ApplicationError(409, "artifact_unavailable", "Artifact is not available.")
            super().__init__(job.output_pdf_path, filename=filename, media_type="application/pdf")
        except BaseException:
            store.release_artifacts(job_id)
            raise

    async def __call__(self, scope, receive, send):
        try:
            # A pathsend server could consume the path after this call returns.
            # Force FileResponse's own chunked/range transfer inside the lease.
            scope = {**scope, "extensions": {k: v for k, v in scope.get("extensions", {}).items() if k != "http.response.pathsend"}}
            await super().__call__(scope, receive, send)
        finally:
            self.store.release_artifacts(self.job_id)
