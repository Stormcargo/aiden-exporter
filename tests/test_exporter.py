import json
from unittest.mock import MagicMock, patch

import pytest
from fellow_aiden import FellowAiden
from prometheus_client import REGISTRY

import exporter
from exporter import (
    PatchedFellowAiden,
    _bool,
    update_metrics,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def get_metric(name: str, labels: dict) -> float | None:
    """Read a gauge value from the Prometheus registry by name and label set."""
    for metric in REGISTRY.collect():
        if metric.name == name:
            for sample in metric.samples:
                if all(sample.labels.get(k) == v for k, v in labels.items()):
                    return sample.value
    return None


def make_aiden() -> PatchedFellowAiden:
    """Construct a PatchedFellowAiden without hitting the network."""
    aiden = object.__new__(PatchedFellowAiden)
    aiden.SESSION = MagicMock()
    aiden.BASE_URL = FellowAiden.BASE_URL
    aiden.API_DEVICES = FellowAiden.API_DEVICES
    aiden.API_PROFILES = FellowAiden.API_PROFILES
    aiden.API_SCHEDULES = FellowAiden.API_SCHEDULES
    aiden._log = MagicMock()
    aiden._brewer_id = "test-device-id"
    aiden._device_config = {}
    aiden._profiles = []
    aiden._schedules = []
    return aiden


SAMPLE_CONFIG = {
    "brewing": True,
    "carafePresent": True,
    "heaterOn": True,
    "lidClosed": False,
    "missingWater": False,
    "singleBrewBasketPresent": False,
    "batchBrewBasketPresent": True,
    "totalWaterVolumeL": 1000.0,
    "brewingWaterVolumeMl": 500,
    "totalBrewingCycles": 42,
    "brewStartTime": "1700000000",
    "brewEndTime": "1700000600",
    "ibSelectedProfileId": "p1",
    "displayName": "Aiden",
}


# ── _bool ─────────────────────────────────────────────────────────────────────


class TestBoolHelper:
    def test_true(self):
        assert _bool(True) == 1.0

    def test_false(self):
        assert _bool(False) == 0.0

    def test_none(self):
        assert _bool(None) == 0.0

    def test_truthy_int(self):
        assert _bool(1) == 1.0

    def test_falsy_int(self):
        assert _bool(0) == 0.0


# ── _fetch_profiles / _fetch_schedules ────────────────────────────────────────


class TestFetchHelpers:
    def test_fetch_profiles_success(self):
        aiden = make_aiden()
        profiles = [{"id": "p0", "title": "Light"}]
        aiden.SESSION.get.return_value = MagicMock(
            status_code=200,
            content=json.dumps(profiles).encode(),
        )
        assert aiden._fetch_profiles() == profiles

    def test_fetch_profiles_error_returns_empty(self):
        aiden = make_aiden()
        aiden.SESSION.get.return_value = MagicMock(status_code=500)
        assert aiden._fetch_profiles() == []

    def test_fetch_schedules_success(self):
        aiden = make_aiden()
        schedules = [{"id": "s0", "enabled": True}]
        aiden.SESSION.get.return_value = MagicMock(
            status_code=200,
            content=json.dumps(schedules).encode(),
        )
        assert aiden._fetch_schedules() == schedules

    def test_fetch_schedules_error_returns_empty(self):
        aiden = make_aiden()
        aiden.SESSION.get.return_value = MagicMock(status_code=404)
        assert aiden._fetch_schedules() == []


# ── PatchedFellowAiden.__device ───────────────────────────────────────────────


class TestPatchedDevice:
    def test_uses_embedded_profiles_when_present(self):
        aiden = make_aiden()
        profiles = [{"id": "p0", "title": "Light"}]
        device_payload = [{"id": "dev-1", "profiles": profiles, "schedules": []}]
        aiden.SESSION.get.return_value = MagicMock(
            status_code=200,
            content=json.dumps(device_payload).encode(),
        )
        aiden._FellowAiden__device()
        assert aiden._profiles == profiles
        # Only one GET call — no fallback to the profiles endpoint
        assert aiden.SESSION.get.call_count == 1

    def test_empty_profile_list_is_not_treated_as_absent(self):
        """profiles: [] in the response must NOT trigger a fallback API call."""
        aiden = make_aiden()
        device_payload = [{"id": "dev-1", "profiles": [], "schedules": []}]
        aiden.SESSION.get.return_value = MagicMock(
            status_code=200,
            content=json.dumps(device_payload).encode(),
        )
        aiden._FellowAiden__device()
        assert aiden._profiles == []
        assert aiden.SESSION.get.call_count == 1

    def test_falls_back_to_api_when_keys_absent(self):
        aiden = make_aiden()
        profiles = [{"id": "p0", "title": "Light"}]
        device_payload = [{"id": "dev-1"}]  # no profiles / schedules keys

        def side_effect(url, **kwargs):
            mock = MagicMock(status_code=200)
            if "profiles" in url:
                mock.content = json.dumps(profiles).encode()
            elif "schedules" in url:
                mock.content = json.dumps([]).encode()
            else:
                mock.content = json.dumps(device_payload).encode()
            return mock

        aiden.SESSION.get.side_effect = side_effect
        aiden._FellowAiden__device()
        assert aiden._profiles == profiles
        # device + profiles + schedules = 3 calls
        assert aiden.SESSION.get.call_count == 3


# ── update_metrics ────────────────────────────────────────────────────────────


class TestUpdateMetrics:
    def test_all_gauges_set_correctly(self):
        aiden = make_aiden()
        aiden.get_device_config = MagicMock(return_value=SAMPLE_CONFIG)
        aiden.get_profiles = MagicMock(return_value=[{}, {}])
        aiden.get_schedules = MagicMock(return_value=[{}])

        brewer = "TestBrewer_AllGauges"
        update_metrics(aiden, brewer)
        lbl = {"brewer_name": brewer}

        assert get_metric("fellow_aiden_brewing", lbl) == 1.0
        assert get_metric("fellow_aiden_carafe_present", lbl) == 1.0
        assert get_metric("fellow_aiden_heater_on", lbl) == 1.0
        assert get_metric("fellow_aiden_lid_closed", lbl) == 0.0
        assert get_metric("fellow_aiden_missing_water", lbl) == 0.0
        assert get_metric("fellow_aiden_batch_brew_basket_present", lbl) == 1.0
        assert get_metric("fellow_aiden_total_water_volume_ml", lbl) == 1000.0
        assert get_metric("fellow_aiden_last_brew_water_volume_ml", lbl) == 500.0
        assert get_metric("fellow_aiden_total_brew_cycles", lbl) == 42.0
        assert get_metric("fellow_aiden_profiles_count", lbl) == 2.0
        assert get_metric("fellow_aiden_schedules_count", lbl) == 1.0
        assert get_metric("fellow_aiden_scrape_success", lbl) == 1.0

    def test_zero_timestamps_when_never_brewed(self):
        aiden = make_aiden()
        config = {**SAMPLE_CONFIG, "brewStartTime": "0", "brewEndTime": "0"}
        aiden.get_device_config = MagicMock(return_value=config)
        aiden.get_profiles = MagicMock(return_value=[])
        aiden.get_schedules = MagicMock(return_value=[])

        brewer = "TestBrewer_Timestamps"
        update_metrics(aiden, brewer)
        lbl = {"brewer_name": brewer}

        assert get_metric("fellow_aiden_last_brew_start_timestamp_seconds", lbl) == 0.0
        assert get_metric("fellow_aiden_last_brew_end_timestamp_seconds", lbl) == 0.0

    def test_missing_optional_fields_default_to_zero(self):
        aiden = make_aiden()
        aiden.get_device_config = MagicMock(return_value={})
        aiden.get_profiles = MagicMock(return_value=[])
        aiden.get_schedules = MagicMock(return_value=[])

        brewer = "TestBrewer_Defaults"
        update_metrics(aiden, brewer)
        lbl = {"brewer_name": brewer}

        assert get_metric("fellow_aiden_brewing", lbl) == 0.0
        assert get_metric("fellow_aiden_total_water_volume_ml", lbl) == 0.0
        assert get_metric("fellow_aiden_total_brew_cycles", lbl) == 0.0


# ── main env-var validation ───────────────────────────────────────────────────


class TestMain:
    def test_exits_without_email(self, monkeypatch):
        monkeypatch.delenv("FELLOW_EMAIL", raising=False)
        monkeypatch.delenv("FELLOW_PASSWORD", raising=False)
        with pytest.raises(SystemExit):
            exporter.main()

    def test_exits_without_password(self, monkeypatch):
        monkeypatch.setenv("FELLOW_EMAIL", "test@example.com")
        monkeypatch.delenv("FELLOW_PASSWORD", raising=False)
        with pytest.raises(SystemExit):
            exporter.main()

    def test_auth_failure_raises(self, monkeypatch):
        monkeypatch.setenv("FELLOW_EMAIL", "bad@example.com")
        monkeypatch.setenv("FELLOW_PASSWORD", "wrong")
        with patch("exporter.PatchedFellowAiden") as MockClass:
            MockClass.side_effect = Exception("Email or password incorrect.")
            with pytest.raises(Exception, match="Email or password incorrect."):
                exporter.main()
