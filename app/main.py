from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routes.api import router
from app.services.job_queue import get_job_queue_processor


@asynccontextmanager
async def lifespan(_app: FastAPI):
    processor = get_job_queue_processor()
    processor.start()
    try:
        yield
    finally:
        processor.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)
