"""FastAPI app with the NiceGUI pages mounted on it."""

import asyncio

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from nicegui import app as nicegui_app
from nicegui import ui
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.scheduler import build_sources, run_all
from heatmon.web import status


def create_app(cfg: Config, sessions: sessionmaker[Session], collect: bool) -> FastAPI:
    app = FastAPI()
    sources = build_sources(cfg, sessions) if collect else []

    @app.get("/")
    def home():
        return RedirectResponse("/status")

    status.register(cfg, sessions, {s.name for s in sources})

    if sources:
        async def start_collector():
            nicegui_app.state.collector = asyncio.create_task(run_all(sources, sessions))
        nicegui_app.on_startup(start_collector)

    ui.run_with(app, title="Heating Monitor", show_welcome_message=False)
    return app
