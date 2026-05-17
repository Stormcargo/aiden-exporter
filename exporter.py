import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from fellow_aiden import FellowAiden
from loguru import logger
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Gauge, generate_latest

LABELS = ["brewer_name"]

BREWING = Gauge("fellow_aiden_brewing", "1 if currently brewing", LABELS)
CARAFE_PRESENT = Gauge("fellow_aiden_carafe_present", "1 if carafe inserted", LABELS)
HEATER_ON = Gauge("fellow_aiden_heater_on", "1 if heating element active", LABELS)
LID_CLOSED = Gauge("fellow_aiden_lid_closed", "1 if lid closed", LABELS)
MISSING_WATER = Gauge("fellow_aiden_missing_water", "1 if water tank empty", LABELS)
SS_BASKET = Gauge(
    "fellow_aiden_single_brew_basket_present", "1 if single-serve basket present", LABELS
)
BATCH_BASKET = Gauge("fellow_aiden_batch_brew_basket_present", "1 if batch basket present", LABELS)
PUMP_ON = Gauge("fellow_aiden_pump_on", "1 if pump is active", LABELS)
CLEANING = Gauge("fellow_aiden_cleaning", "1 if cleaning cycle active", LABELS)
RINSING = Gauge("fellow_aiden_rinsing", "1 if rinsing cycle active", LABELS)
SHOWER_HEAD_PRESENT = Gauge("fellow_aiden_shower_head_present", "1 if shower head present", LABELS)
CLOUD_CONNECTED = Gauge("fellow_aiden_cloud_connected", "1 if connected to Fellow cloud", LABELS)
BREWING_TEMP = Gauge(
    "fellow_aiden_brewing_water_temperature_c",
    "Current brewing water temperature (°C; NaN when not brewing)",
    LABELS,
)
TOTAL_WATER_ML = Gauge("fellow_aiden_total_water_volume_ml", "Lifetime water used (mL)", LABELS)
LAST_BREW_WATER_ML = Gauge(
    "fellow_aiden_last_brew_water_volume_ml", "Last brew water volume (mL)", LABELS
)
WATER_QUANTITY_ML = Gauge(
    "fellow_aiden_water_quantity_ml", "Current water quantity setting (mL)", LABELS
)
TOTAL_BREW_CYCLES = Gauge("fellow_aiden_total_brew_cycles", "Total brew cycle count", LABELS)
BREW_START_TS = Gauge(
    "fellow_aiden_last_brew_start_timestamp_seconds", "Unix timestamp of last brew start", LABELS
)
BREW_END_TS = Gauge(
    "fellow_aiden_last_brew_end_timestamp_seconds", "Unix timestamp of last brew end", LABELS
)
PROFILES_COUNT = Gauge("fellow_aiden_profiles_count", "Number of brew profiles", LABELS)
SCHEDULES_COUNT = Gauge("fellow_aiden_schedules_count", "Number of configured schedules", LABELS)
SCRAPE_SUCCESS = Gauge("fellow_aiden_scrape_success", "1 if last poll succeeded", LABELS)
SCRAPE_DURATION = Gauge(
    "fellow_aiden_scrape_duration_seconds", "Duration of the last API poll in seconds", LABELS
)
LAST_SCRAPE_TS = Gauge(
    "fellow_aiden_last_scrape_timestamp_seconds", "Unix timestamp of last successful scrape", LABELS
)
DEVICE_INFO = Gauge(
    "fellow_aiden_device_info",
    "Device info",
    LABELS + ["selected_profile_id", "selected_profile_name", "firmware_version", "serial_number"],
)
PROFILE_RATIO = Gauge("fellow_aiden_active_profile_ratio", "Active profile brew ratio", LABELS)
PROFILE_TEMP = Gauge(
    "fellow_aiden_active_profile_temperature_c",
    "Active profile target brew temperature (°C; NaN if not set)",
    LABELS,
)
PROFILE_BLOOM_ENABLED = Gauge(
    "fellow_aiden_active_profile_bloom_enabled", "1 if active profile has bloom enabled", LABELS
)
PROFILE_BLOOM_TEMP = Gauge(
    "fellow_aiden_active_profile_bloom_temperature_c",
    "Active profile bloom temperature (°C; NaN if bloom disabled)",
    LABELS,
)
PROFILE_BLOOM_DURATION = Gauge(
    "fellow_aiden_active_profile_bloom_duration_seconds",
    "Active profile bloom duration (seconds; NaN if bloom disabled)",
    LABELS,
)
LAST_BREW_PROFILE_INFO = Gauge(
    "fellow_aiden_last_brew_profile_info",
    "Info about the last used brew profile",
    LABELS + ["profile_id", "profile_name"],
)
EXPORTER_INFO = Gauge(
    "fellow_aiden_exporter_info",
    "Exporter build info",
    ["version"],
)

_ready = threading.Event()
_shutdown = threading.Event()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress per-request access log
        pass

    def do_GET(self):
        if self.path == "/healthz":
            self._text(200, b"ok")
        elif self.path == "/readyz":
            self._text(200, b"ok") if _ready.is_set() else self._text(503, b"not ready")
        elif self.path in ("/metrics", "/"):
            output = generate_latest(REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(output)
        else:
            self._text(404, b"not found")

    def _text(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


class PatchedFellowAiden(FellowAiden):
    """Works around a library bug where the /devices endpoint no longer
    embeds profiles/schedules in its response body. Falls back to the
    dedicated profiles and schedules endpoints when keys are absent."""

    def _FellowAiden__device(self):
        device_url = self.BASE_URL + self.API_DEVICES
        response = self.SESSION.get(device_url, params={"dataType": "real"})
        if response.status_code == 401:
            self._FellowAiden__auth()
            response = self.SESSION.get(device_url, params={"dataType": "real"})
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            logger.warning("Rate limited by Fellow API (Retry-After: {}s)", retry_after)
            raise Exception("rate limited")
        parsed = json.loads(response.content)
        self._device_config = parsed[0]
        self._brewer_id = self._device_config["id"]
        self._profiles = (
            self._device_config["profiles"]
            if "profiles" in self._device_config
            else self._fetch_profiles()
        )
        self._schedules = (
            self._device_config["schedules"]
            if "schedules" in self._device_config
            else self._fetch_schedules()
        )

    def _fetch_profiles(self) -> list:
        url = self.BASE_URL + self.API_PROFILES.format(id=self._brewer_id)
        r = self.SESSION.get(url)
        if r.status_code == 429:
            logger.warning(
                "Rate limited fetching profiles (Retry-After: {}s)",
                r.headers.get("Retry-After", "unknown"),
            )
            return []
        return json.loads(r.content) if r.status_code == 200 else []

    def _fetch_schedules(self) -> list:
        url = self.BASE_URL + self.API_SCHEDULES.format(id=self._brewer_id)
        r = self.SESSION.get(url)
        if r.status_code == 429:
            logger.warning(
                "Rate limited fetching schedules (Retry-After: {}s)",
                r.headers.get("Retry-After", "unknown"),
            )
            return []
        return json.loads(r.content) if r.status_code == 200 else []


def _bool(val) -> float:
    return 1.0 if val else 0.0


def _opt_float(val) -> float:
    return float(val) if val is not None else float("nan")


def update_metrics(aiden: PatchedFellowAiden, brewer_name: str) -> None:
    start = time.monotonic()
    config = aiden.get_device_config(remote=True)
    profiles = aiden.get_profiles()
    schedules = aiden.get_schedules()

    n = brewer_name

    BREWING.labels(n).set(_bool(config.get("brewing")))
    CARAFE_PRESENT.labels(n).set(_bool(config.get("carafePresent")))
    HEATER_ON.labels(n).set(_bool(config.get("heaterOn")))
    LID_CLOSED.labels(n).set(_bool(config.get("lidClosed")))
    MISSING_WATER.labels(n).set(_bool(config.get("missingWater")))
    SS_BASKET.labels(n).set(_bool(config.get("singleBrewBasketPresent")))
    BATCH_BASKET.labels(n).set(_bool(config.get("batchBrewBasketPresent")))
    PUMP_ON.labels(n).set(_bool(config.get("pumpOn")))
    CLEANING.labels(n).set(_bool(config.get("cleaning")))
    RINSING.labels(n).set(_bool(config.get("rinsing")))
    SHOWER_HEAD_PRESENT.labels(n).set(_bool(config.get("showerHeadPresent")))
    CLOUD_CONNECTED.labels(n).set(_bool(config.get("isConnected")))

    BREWING_TEMP.labels(n).set(_opt_float(config.get("brewingWaterTemperatureC")))
    TOTAL_WATER_ML.labels(n).set(float(config.get("totalWaterVolumeL", 0)))
    LAST_BREW_WATER_ML.labels(n).set(float(config.get("brewingWaterVolumeMl", 0)))
    WATER_QUANTITY_ML.labels(n).set(float(config.get("ibWaterQuantity", 0)))
    TOTAL_BREW_CYCLES.labels(n).set(float(config.get("totalBrewingCycles", 0)))
    BREW_START_TS.labels(n).set(float(config.get("brewStartTime") or 0))
    BREW_END_TS.labels(n).set(float(config.get("brewEndTime") or 0))

    PROFILES_COUNT.labels(n).set(len(profiles))
    SCHEDULES_COUNT.labels(n).set(len(schedules))

    selected_profile = str(config.get("ibSelectedProfileId", ""))
    active_profile = next(
        (p for p in profiles if str(p.get("id")) == selected_profile),
        {},
    )
    profile_name = str(active_profile.get("title", selected_profile))
    firmware = str(config.get("firmwareVersion", ""))
    serial = str(config.get("serialNumber", ""))
    DEVICE_INFO.labels(n, selected_profile, profile_name, firmware, serial).set(1)

    PROFILE_RATIO.labels(n).set(_opt_float(active_profile.get("ratio")))
    PROFILE_TEMP.labels(n).set(_opt_float(active_profile.get("overallTemperature")))
    PROFILE_BLOOM_ENABLED.labels(n).set(_bool(active_profile.get("bloomEnabled")))
    PROFILE_BLOOM_TEMP.labels(n).set(_opt_float(active_profile.get("bloomTemperature")))
    PROFILE_BLOOM_DURATION.labels(n).set(_opt_float(active_profile.get("bloomDuration")))

    last_brew_profile = max(
        (p for p in profiles if (p.get("lastUsedTime") or 0) > 0),
        key=lambda p: p.get("lastUsedTime", 0),
        default={},
    )
    LAST_BREW_PROFILE_INFO.clear()
    if last_brew_profile:
        LAST_BREW_PROFILE_INFO.labels(
            n,
            str(last_brew_profile.get("id", "")),
            str(last_brew_profile.get("title", "")),
        ).set(1)

    SCRAPE_DURATION.labels(n).set(time.monotonic() - start)
    SCRAPE_SUCCESS.labels(n).set(1)
    LAST_SCRAPE_TS.labels(n).set(time.time())


def _handle_shutdown(*_) -> None:
    _ready.clear()  # fail readyz immediately so k8s stops routing traffic
    _shutdown.set()


def poll_loop(aiden: PatchedFellowAiden, brewer_name: str, interval: int) -> None:
    prev_brewing: bool | None = None

    while True:
        try:
            update_metrics(aiden, brewer_name)
            _ready.set()

            config = aiden.get_device_config()
            curr_brewing = bool(config.get("brewing"))

            if prev_brewing is not None and curr_brewing != prev_brewing:
                if curr_brewing:
                    logger.info("Brew started on {}", brewer_name)
                else:
                    logger.info("Brew finished on {}", brewer_name)

            prev_brewing = curr_brewing
            logger.info("Poll succeeded")
        except Exception as e:
            msg = str(e).lower()
            if "email or password" in msg or "incorrect" in msg or "unauthorized" in msg:
                logger.warning("Auth error, reauthenticating: {}", e)
                try:
                    aiden.authenticate()
                except Exception as re:
                    logger.error("Reauthentication failed: {}", re)
            else:
                logger.error("Poll failed: {}", e)
            SCRAPE_SUCCESS.labels(brewer_name).set(0)
        time.sleep(interval)


def main() -> None:
    email = os.environ.get("FELLOW_EMAIL")
    password = os.environ.get("FELLOW_PASSWORD")
    port = int(os.environ.get("EXPORTER_PORT", "9090"))
    interval = int(os.environ.get("SCRAPE_INTERVAL", "60"))

    if not email or not password:
        raise SystemExit("FELLOW_EMAIL and FELLOW_PASSWORD environment variables are required")

    EXPORTER_INFO.labels(version=os.environ.get("APP_VERSION", "dev")).set(1)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    logger.info("Authenticating with Fellow API")
    aiden = PatchedFellowAiden(email, password)
    brewer_name = aiden.get_display_name()
    logger.info("Connected to brewer: {}", brewer_name)

    threading.Thread(target=poll_loop, args=(aiden, brewer_name, interval), daemon=True).start()

    server = HTTPServer(("", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Exporter running on :{} (scrape interval: {}s)", port, interval)

    _shutdown.wait()
    logger.info("Shutting down")
    server.shutdown()


if __name__ == "__main__":
    main()
