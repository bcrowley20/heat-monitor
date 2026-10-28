"""JSON and CSV for every dataset: /api/<name>?start=YYYY-MM-DD&end=YYYY-MM-DD, or /api/<name>.csv"""

import csv
import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session, sessionmaker

from heatmon.config import Config
from heatmon.web import data
from heatmon.web.components import date_range


def router(cfg: Config, sessions: sessionmaker[Session]) -> APIRouter:
    r = APIRouter(prefix="/api")

    @r.get("/{name}")
    def dataset(name: str, start: str | None = None, end: str | None = None):
        csv_wanted = name.endswith(".csv")
        name = name.removesuffix(".csv")
        if name not in data.DATASETS:
            raise HTTPException(404, f"Unknown dataset. Try one of: {', '.join(data.DATASETS)}")
        s, e = date_range(cfg, start, end)
        rows = data.DATASETS[name](data.Ctx(cfg, sessions, s, e))
        if not csv_wanted:
            return rows
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        return Response(buf.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{name}_{s}_{e}.csv"'})

    return r
