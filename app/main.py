from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routes.api import router

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)
