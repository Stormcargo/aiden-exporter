import http.client
import json
import math
import threading
from http.server import HTTPServer
from unittest.mock import MagicMock, patch

import pytest
from fellow_aiden import FellowAiden
from prometheus_client import REGISTRY

import exporter
from exporter import (
    PatchedFellowAiden,
    _bool,
    _Handler,
    _opt_float,
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


@pytest.fixture()
def http_server():
    """Spin up the exporter HTTP handler on a random port for the duration of the test."""
    server = HTTPServer(("", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield port
    server.shutdown()


@pytest.fixture(autouse=True)
def reset_ready():
    """Ensure _ready is cleared before and after every test."""
    exporter._ready.clear()
    yield
    exporter._ready.clear()


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


# ── _opt_float ────────────────────────────────────────────────────────────────


class TestOptFloat:
    def test_numeric_value(self):
        assert _opt_float(96.0) == 96.0

    def test_none_returns_nan(self):
        assert math.isnan(_opt_float(None))

    def test_zero_is_not_nan(self):
        assert _opt_float(0) == 0.0


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

    def test_fetch_profiles_rate_limited_returns_empty_and_logs(self, caplog):
        aiden = make_aiden()
        aiden.SESSION.get.return_value = MagicMock(status_code=429, headers={"Retry-After": "30"})
        with caplog.at_level("WARNING", logger="fellow_aiden_exporter"):
            result = aiden._fetch_profiles()
        assert result == []

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

    def test_fetch_schedules_rate_limited_returns_empty_and_logs(self, caplog):
        aiden = make_aiden()
        aiden.SESSION.get.return_value = MagicMock(status_code=429, headers={"Retry-After": "60"})
        with caplog.at_level("WARNING", logger="fellow_aiden_exporter"):
            result = aiden._fetch_schedules()
        assert result == []


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

    def test_reauthenticates_on_401(self):
        aiden = make_aiden()
        aiden._FellowAiden__auth = MagicMock()
        ok_payload = [{"id": "dev-1", "profiles": [], "schedules": []}]
        aiden.SESSION.get.side_effect = [
            MagicMock(status_code=401, headers={}),
            MagicMock(status_code=200, content=json.dumps(ok_payload).encode()),
        ]
        aiden._FellowAiden__device()
        aiden._FellowAiden__auth.assert_called_once()
        assert aiden._brewer_id == "dev-1"

    def test_rate_limited_device_raises(self):
        aiden = make_aiden()
        aiden.SESSION.get.return_value = MagicMock(status_code=429, headers={"Retry-After": "30"})
        with pytest.raises(Exception, match="rate limited"):
            aiden._FellowAiden__device()

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

    def test_scrape_duration_is_set_and_non_negative(self):
        aiden = make_aiden()
        aiden.get_device_config = MagicMock(return_value=SAMPLE_CONFIG)
        aiden.get_profiles = MagicMock(return_value=[])
        aiden.get_schedules = MagicMock(return_value=[])

        brewer = "TestBrewer_Duration"
        update_metrics(aiden, brewer)

        duration = get_metric("fellow_aiden_scrape_duration_seconds", {"brewer_name": brewer})
        assert duration is not None
        assert duration >= 0.0

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

    def test_last_brew_profile_info_set_when_profile_has_last_used_time(self):
        aiden = make_aiden()
        profiles = [
            {"id": "p1", "title": "Light", "lastUsedTime": 1700000600},
            {"id": "p2", "title": "Dark", "lastUsedTime": 0},
        ]
        config = {**SAMPLE_CONFIG, "ibSelectedProfileId": "p1"}
        aiden.get_device_config = MagicMock(return_value=config)
        aiden.get_profiles = MagicMock(return_value=profiles)
        aiden.get_schedules = MagicMock(return_value=[])

        brewer = "TestBrewer_LastProfile"
        update_metrics(aiden, brewer)

        val = get_metric(
            "fellow_aiden_last_brew_profile_info",
            {"brewer_name": brewer, "profile_id": "p1", "profile_name": "Light"},
        )
        assert val == 1.0

    def test_brewing_temp_nan_when_not_brewing(self):
        aiden = make_aiden()
        config = {**SAMPLE_CONFIG, "brewingWaterTemperatureC": None}
        aiden.get_device_config = MagicMock(return_value=config)
        aiden.get_profiles = MagicMock(return_value=[])
        aiden.get_schedules = MagicMock(return_value=[])

        brewer = "TestBrewer_TempNaN"
        update_metrics(aiden, brewer)

        val = get_metric("fellow_aiden_brewing_water_temperature_c", {"brewer_name": brewer})
        assert math.isnan(val)


# ── HTTP handler ──────────────────────────────────────────────────────────────


class TestHTTPHandler:
    def test_healthz_always_200(self, http_server):
        conn = http.client.HTTPConnection("localhost", http_server, timeout=2)
        conn.request("GET", "/healthz")
        r = conn.getresponse()
        assert r.status == 200
        assert r.read() == b"ok"

    def test_readyz_503_before_first_scrape(self, http_server):
        conn = http.client.HTTPConnection("localhost", http_server, timeout=2)
        conn.request("GET", "/readyz")
        r = conn.getresponse()
        assert r.status == 503

    def test_readyz_200_after_first_scrape(self, http_server):
        exporter._ready.set()
        conn = http.client.HTTPConnection("localhost", http_server, timeout=2)
        conn.request("GET", "/readyz")
        r = conn.getresponse()
        assert r.status == 200

    def test_metrics_endpoint_returns_prometheus_text(self, http_server):
        conn = http.client.HTTPConnection("localhost", http_server, timeout=2)
        conn.request("GET", "/metrics")
        r = conn.getresponse()
        assert r.status == 200
        assert b"fellow_aiden" in r.read()

    def test_unknown_path_404(self, http_server):
        conn = http.client.HTTPConnection("localhost", http_server, timeout=2)
        conn.request("GET", "/does-not-exist")
        r = conn.getresponse()
        assert r.status == 404


# ── poll_loop ─────────────────────────────────────────────────────────────────


class TestPollLoop:
    def _run_one_iteration(self, monkeypatch, aiden, brewer):
        """Run poll_loop for exactly one iteration by making time.sleep raise."""
        monkeypatch.setattr("exporter.time.sleep", MagicMock(side_effect=StopIteration))
        with pytest.raises(StopIteration):
            exporter.poll_loop(aiden, brewer, 15)

    def test_sets_ready_on_success(self, monkeypatch):
        aiden = make_aiden()
        aiden.get_device_config = MagicMock(return_value=SAMPLE_CONFIG)
        aiden.get_profiles = MagicMock(return_value=[])
        aiden.get_schedules = MagicMock(return_value=[])

        self._run_one_iteration(monkeypatch, aiden, "PollLoop_Ready")
        assert exporter._ready.is_set()

    def test_sets_scrape_success_on_success(self, monkeypatch):
        aiden = make_aiden()
        aiden.get_device_config = MagicMock(return_value=SAMPLE_CONFIG)
        aiden.get_profiles = MagicMock(return_value=[])
        aiden.get_schedules = MagicMock(return_value=[])

        brewer = "PollLoop_Success"
        self._run_one_iteration(monkeypatch, aiden, brewer)
        assert get_metric("fellow_aiden_scrape_success", {"brewer_name": brewer}) == 1.0

    def test_sets_scrape_failed_on_error(self, monkeypatch):
        aiden = make_aiden()
        aiden.get_device_config = MagicMock(side_effect=Exception("network error"))

        brewer = "PollLoop_Fail"
        self._run_one_iteration(monkeypatch, aiden, brewer)
        assert get_metric("fellow_aiden_scrape_success", {"brewer_name": brewer}) == 0.0

    def test_reauthenticates_on_auth_error(self, monkeypatch):
        aiden = make_aiden()
        aiden.get_device_config = MagicMock(side_effect=Exception("Email or password incorrect."))
        aiden.authenticate = MagicMock()

        self._run_one_iteration(monkeypatch, aiden, "PollLoop_Auth")
        aiden.authenticate.assert_called_once()

    def _run_two_iterations(self, monkeypatch, aiden, brewer):
        sleep_calls = 0

        def fake_sleep(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise StopIteration

        monkeypatch.setattr("exporter.time.sleep", fake_sleep)
        with pytest.raises(StopIteration):
            exporter.poll_loop(aiden, brewer, 15)

    def test_logs_brew_started(self, monkeypatch):
        aiden = make_aiden()
        # Each iteration: update_metrics calls get_device_config(remote=True),
        # then poll_loop calls get_device_config() for state-change detection.
        aiden.get_device_config = MagicMock(
            side_effect=[
                {**SAMPLE_CONFIG, "brewing": False},  # iter 1 – update_metrics
                {**SAMPLE_CONFIG, "brewing": False},  # iter 1 – state check
                {**SAMPLE_CONFIG, "brewing": True},  # iter 2 – update_metrics
                {**SAMPLE_CONFIG, "brewing": True},  # iter 2 – state check
            ]
        )
        aiden.get_profiles = MagicMock(return_value=[])
        aiden.get_schedules = MagicMock(return_value=[])

        messages = []
        from loguru import logger

        handler = logger.add(lambda m: messages.append(m), format="{message}")
        try:
            self._run_two_iterations(monkeypatch, aiden, "BrewLogger")
        finally:
            logger.remove(handler)

        assert any("Brew started" in m for m in messages)

    def test_logs_brew_finished(self, monkeypatch):
        aiden = make_aiden()
        aiden.get_device_config = MagicMock(
            side_effect=[
                {**SAMPLE_CONFIG, "brewing": True},  # iter 1 – update_metrics
                {**SAMPLE_CONFIG, "brewing": True},  # iter 1 – state check
                {**SAMPLE_CONFIG, "brewing": False},  # iter 2 – update_metrics
                {**SAMPLE_CONFIG, "brewing": False},  # iter 2 – state check
            ]
        )
        aiden.get_profiles = MagicMock(return_value=[])
        aiden.get_schedules = MagicMock(return_value=[])

        messages = []
        from loguru import logger

        handler = logger.add(lambda m: messages.append(m), format="{message}")
        try:
            self._run_two_iterations(monkeypatch, aiden, "BrewFinished")
        finally:
            logger.remove(handler)

        assert any("Brew finished" in m for m in messages)


# ── shutdown handler ──────────────────────────────────────────────────────────


class TestShutdownHandler:
    def test_clears_ready_and_sets_shutdown(self):
        exporter._ready.set()
        exporter._shutdown.clear()

        exporter._handle_shutdown()

        assert not exporter._ready.is_set()
        assert exporter._shutdown.is_set()

        exporter._shutdown.clear()  # restore for other tests

    def test_readyz_returns_503_after_shutdown_signal(self, http_server):
        exporter._ready.set()
        exporter._handle_shutdown()
        exporter._shutdown.clear()

        conn = http.client.HTTPConnection("localhost", http_server, timeout=2)
        conn.request("GET", "/readyz")
        r = conn.getresponse()
        assert r.status == 503


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

    def test_happy_path_starts_and_shuts_down(self, monkeypatch):
        monkeypatch.setenv("FELLOW_EMAIL", "test@example.com")
        monkeypatch.setenv("FELLOW_PASSWORD", "secret")
        monkeypatch.setenv("EXPORTER_PORT", "19090")

        with (
            patch("exporter.PatchedFellowAiden") as MockAiden,
            patch("exporter.HTTPServer") as MockServer,
            patch("exporter.threading.Thread"),
        ):
            MockAiden.return_value.get_display_name.return_value = "Aiden"
            exporter._shutdown.set()  # unblock _shutdown.wait() immediately
            exporter.main()
            exporter._shutdown.clear()

        MockAiden.assert_called_once_with("test@example.com", "secret")
        MockServer.return_value.shutdown.assert_called_once()
