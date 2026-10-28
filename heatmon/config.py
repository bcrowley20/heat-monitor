"""Settings: secrets come from .env, everything else from config.yaml."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="HEATMON_", extra="ignore")

    google_client_id: str = ""
    google_client_secret: str = ""
    sdm_project_id: str = ""
    wu_api_key: str = ""
    centriconnect_user_id: str = ""
    centriconnect_device_id: str = ""
    centriconnect_device_auth: str = ""

    def all_values(self) -> list[str]:
        return [v for _, v in self if v]


class Zone(BaseModel):
    name: str
    nest_device_id: str | None = None
    loops: int | None = None
    area_sqft: float | None = None
    exposure: str | None = None
    notes: str | None = None


class Intervals(BaseModel):
    nest_s: int = Field(120, ge=60)
    weather_s: int = 600
    tank_s: int = 7200
    open_meteo_s: int = 3600
    summaries_s: int = 3600


class Propane(BaseModel):
    tank_size_gal: float = 1000
    # A rise in tank level bigger than this is a fill, not sensor noise.
    delivery_threshold_pct: float = 5
    # Rough starting estimates, used only to spread usage between tank readings across days.
    base_gal_per_day: float = 1.0
    gal_per_hdd: float = 0.15
    # Price used to value usage. Leave unset to use the most recent invoice's price.
    price_per_gal: float | None = None


class Config(BaseModel):
    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 8080
    timezone: str = "America/Los_Angeles"
    station_id: str = "KWACLEEL38"
    hdd_base_f: float = 65
    latitude: float
    longitude: float
    intervals: Intervals = Intervals()
    propane: Propane = Propane()
    zones: list[Zone] = []
    secrets: Secrets = Field(default_factory=Secrets, exclude=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "heatmon.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "nest_token.json"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "heatmon.log"


def load_config(path: Path) -> Config:
    data = yaml.safe_load(path.read_text()) or {}
    cfg = Config(**data)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
