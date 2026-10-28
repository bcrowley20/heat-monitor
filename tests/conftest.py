import json
from pathlib import Path

import pytest

from heatmon import logs
from heatmon.config import Config, Secrets, Zone
from heatmon.db.session import init_db, sync_zones

FIXTURES = Path(__file__).parent / "fixtures"
ZONES = ["Hallway", "Great Room", "Master Bedroom", "Guest Bedroom"]


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Config:
    secrets = Secrets(
        _env_file=None,
        google_client_id="test-client-id",
        google_client_secret="test-client-secret",
        sdm_project_id="test-project",
        wu_api_key="test-wu-key",
        centriconnect_user_id="test-cc-user",
        centriconnect_device_id="test-cc-device",
        centriconnect_device_auth="test-cc-auth",
    )
    monkeypatch.setattr(logs, "_secrets", secrets.all_values())
    return Config(data_dir=tmp_path, latitude=47.2, longitude=-120.94,
                  zones=[Zone(name=n) for n in ZONES], secrets=secrets)


@pytest.fixture
def sessions(cfg):
    s = init_db(cfg.db_path)
    sync_zones(s, cfg.zones)
    return s
