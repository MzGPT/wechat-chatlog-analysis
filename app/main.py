from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, Response
from .db import init_db
from .background import install_background
from .routers import health, messages, chats, contacts, ai, send, hooks, configs, sync, reports, compat, market, email, extensions, news, folo
from .db import SessionLocal
from .models import Message
import orjson
import os


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="WeChat Chatlog Analysis API")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(messages.router)
    app.include_router(chats.router)
    app.include_router(contacts.router)
    app.include_router(ai.router)
    app.include_router(send.router)
    app.include_router(hooks.router)
    app.include_router(configs.router)
    app.include_router(sync.router)
    app.include_router(reports.router)
    app.include_router(compat.router)
    app.include_router(email.router)
    app.include_router(extensions.router)
    app.include_router(news.router)
    app.include_router(folo.router)
    app.include_router(market.router)

    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    static_dir = os.path.abspath(static_dir)
    if not os.path.exists(static_dir):
        os.makedirs(static_dir, exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def index():
        """Serve the unified UI only from static/index.html.
        We intentionally deprecate legacy pages (0811/0801) to avoid confusion.
        """
        index_path = os.path.join(static_dir, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
        return Response("UI not found", media_type="text/plain", status_code=404)

    @app.get("/ui/legacy")
    def legacy_ui():
        # Deprecated permanently to avoid confusion with unified static UI
        return Response("Legacy UI removed", status_code=404)

    install_background(app)
    return app


app = create_app()
