"""FastAPI ilova kirish nuqtasi."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger

configure_logging()
log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", env=settings.app_env)
    yield
    log.info("shutdown")


app = FastAPI(title="AI Call-Center Agent", version="0.1.0", lifespan=lifespan)

# CORS: allow the browser frontend (separate origin) to call the API. Auth is via
# the Authorization header (not cookies), so allow_credentials stays False and a
# wildcard origin is safe. Tighten CORS_ALLOW_ORIGINS to the frontend URL later.
_cors_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")
