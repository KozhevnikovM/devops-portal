from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.presentation.routes.bookings import router

app = FastAPI(title="DevOps Portal")
app.include_router(router)
