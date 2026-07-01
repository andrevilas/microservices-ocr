from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routes.api import router
from app.routes.auth import auth_router
from app.services.job_queue import get_job_queue_processor
from app.services.user_store import get_user_store


@asynccontextmanager
async def lifespan(_app: FastAPI):
    processor = get_job_queue_processor()
    processor.start()
    user_store = get_user_store()
    user_store.ensure_admin(
        name=settings.admin_name,
        email=settings.admin_email,
        password=settings.admin_password,
    )
    try:
        yield
    finally:
        processor.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)
app.include_router(auth_router)
