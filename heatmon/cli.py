"""Command line: heatmon auth nest | run | serve | collect | backfill"""

import argparse
import asyncio
import logging
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from heatmon.config import load_config
from heatmon.db.session import init_db, sync_zones
from heatmon.logs import setup_logging

log = logging.getLogger("heatmon")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="heatmon")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    sub = p.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="one-time sign-in for a data source")
    auth.add_argument("service", choices=["nest"])
    sub.add_parser("run", help="collect data and serve the web UI (normal use)")
    sub.add_parser("serve", help="serve the web UI only")
    sub.add_parser("collect", help="collect data only")
    bf = sub.add_parser("backfill", help="load past weather for a date range")
    bf.add_argument("--start", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    bf.add_argument("--end", type=date.fromisoformat, help="YYYY-MM-DD (default: yesterday)")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg.log_path, cfg.secrets.all_values())
    sessions = init_db(cfg.db_path)
    sync_zones(sessions, cfg.zones)

    match args.command:
        case "auth":
            _auth_nest(cfg)
        case "run" | "serve":
            import uvicorn

            from heatmon.web.app import create_app
            app = create_app(cfg, sessions, collect=args.command == "run")
            uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)
        case "collect":
            from heatmon.scheduler import build_sources, run_all
            asyncio.run(run_all(build_sources(cfg, sessions), sessions))
        case "backfill":
            from heatmon.backfill import backfill
            end = args.end or date.today() - timedelta(days=1)
            asyncio.run(backfill(cfg, sessions, args.start, end))


def _auth_nest(cfg) -> None:
    from heatmon.sources.nest import auth_url, exchange_code

    s = cfg.secrets
    if not (s.google_client_id and s.google_client_secret and s.sdm_project_id):
        raise SystemExit("Set the HEATMON_GOOGLE_* and HEATMON_SDM_PROJECT_ID values in .env first.")
    print("1. Open this link in a browser and sign in with the Google account that owns the Nests.")
    print("   Allow access to every thermostat.\n")
    print("   " + auth_url(s.sdm_project_id, s.google_client_id) + "\n")
    print("2. You will end up on google.com. Copy the full address from the address bar.")
    pasted = input("3. Paste it here: ").strip()
    code = parse_qs(urlparse(pasted).query).get("code", [pasted])[0]
    exchange_code(cfg, code)
    print(f"Saved the Nest token to {cfg.token_path}. Start the app with `heatmon run`.")
