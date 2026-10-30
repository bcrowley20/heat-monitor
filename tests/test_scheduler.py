import asyncio
import logging

import pytest

from heatmon import logs
from heatmon.config import Secrets
from heatmon.db.models import SourceStatus
from heatmon.scheduler import build_sources, next_delay, run_all


class FakeSource:
    interval_s = 0

    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, 0

    async def poll(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("GET https://api.example.com/?apiKey=test-wu-key failed")
        return 1


async def test_failing_source_does_not_stop_others(sessions):
    good, bad = FakeSource("good"), FakeSource("bad", fail=True)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run_all([good, bad], sessions), 0.2)

    assert good.calls > 1 and bad.calls > 1
    with sessions() as db:
        assert db.get(SourceStatus, "good").last_success_ts is not None
        bad_status = db.get(SourceStatus, "bad")
    assert bad_status.last_success_ts is None
    assert "test-wu-key" not in bad_status.last_error_msg and "***" in bad_status.last_error_msg


def test_next_delay():
    assert next_delay(120, 0) == 120
    assert next_delay(120, 1) == 240
    assert next_delay(120, 10) == 900  # capped
    assert next_delay(7200, 3) == 7200  # never below the normal interval


def test_build_sources_skips_missing_credentials(cfg, sessions):
    cfg.secrets = Secrets(_env_file=None, wu_api_key="k")
    assert [s.name for s in build_sources(cfg, sessions)] == ["wunderground", "open-meteo", "summaries", "alerts"]


def test_log_output_masks_secrets(tmp_path, capsys, monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", list(root.handlers))  # restored after the test
    monkeypatch.setattr(logs, "_secrets", [])
    logs.setup_logging(tmp_path / "test.log", ["s3cret"])
    try:
        raise ValueError("url?key=s3cret")
    except ValueError:
        logging.getLogger("t").exception("failed with s3cret")
    for h in root.handlers:
        h.close()
    text = (tmp_path / "test.log").read_text() + capsys.readouterr().err
    assert "s3cret" not in text and text.count("***") >= 2
