import json
import logging
import os
import threading
import time

from fellow_aiden import FellowAiden
from prometheus_client import Gauge, start_http_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

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
TOTAL_WATER_ML = Gauge("fellow_aiden_total_water_volume_ml", "Lifetime water used (mL)", LABELS)
LAST_BREW_WATER_ML = Gauge(
    "fellow_aiden_last_brew_water_volume_ml", "Last brew water volume (mL)", LABELS
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
LAST_SCRAPE_TS = Gauge(
    "fellow_aiden_last_scrape_timestamp_seconds", "Unix timestamp of last successful scrape", LABELS
)
DEVICE_INFO = Gauge("fellow_aiden_device_info", "Device info", LABELS + ["selected_profile_id"])


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
        return json.loads(r.content) if r.status_code == 200 else []

    def _fetch_schedules(self) -> list:
        url = self.BASE_URL + self.API_SCHEDULES.format(id=self._brewer_id)
        r = self.SESSION.get(url)
        return json.loads(r.content) if r.status_code == 200 else []


def _bool(val) -> float:
    return 1.0 if val else 0.0


def update_metrics(aiden: PatchedFellowAiden, brewer_name: str) -> None:
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

    TOTAL_WATER_ML.labels(n).set(float(config.get("totalWaterVolumeL", 0)))
    LAST_BREW_WATER_ML.labels(n).set(float(config.get("brewingWaterVolumeMl", 0)))
    TOTAL_BREW_CYCLES.labels(n).set(float(config.get("totalBrewingCycles", 0)))
    BREW_START_TS.labels(n).set(float(config.get("brewStartTime") or 0))
    BREW_END_TS.labels(n).set(float(config.get("brewEndTime") or 0))

    PROFILES_COUNT.labels(n).set(len(profiles))
    SCHEDULES_COUNT.labels(n).set(len(schedules))

    selected_profile = str(config.get("ibSelectedProfileId", ""))
    DEVICE_INFO.labels(n, selected_profile).set(1)

    SCRAPE_SUCCESS.labels(n).set(1)
    LAST_SCRAPE_TS.labels(n).set(time.time())


def poll_loop(aiden: PatchedFellowAiden, brewer_name: str, interval: int) -> None:
    while True:
        try:
            update_metrics(aiden, brewer_name)
            log.info("Poll succeeded")
        except Exception as e:
            msg = str(e).lower()
            if "email or password" in msg or "incorrect" in msg or "unauthorized" in msg:
                log.warning("Auth error, reauthenticating: %s", e)
                try:
                    aiden.authenticate()
                except Exception as re:
                    log.error("Reauthentication failed: %s", re)
            else:
                log.error("Poll failed: %s", e)
            SCRAPE_SUCCESS.labels(brewer_name).set(0)
        time.sleep(interval)


def main() -> None:
    email = os.environ.get("FELLOW_EMAIL")
    password = os.environ.get("FELLOW_PASSWORD")
    port = int(os.environ.get("EXPORTER_PORT", "9090"))
    interval = int(os.environ.get("SCRAPE_INTERVAL", "60"))

    if not email or not password:
        raise SystemExit("FELLOW_EMAIL and FELLOW_PASSWORD environment variables are required")

    log.info("Authenticating with Fellow API")
    aiden = PatchedFellowAiden(email, password)
    brewer_name = aiden.get_display_name()
    log.info("Connected to brewer: %s", brewer_name)

    t = threading.Thread(target=poll_loop, args=(aiden, brewer_name, interval), daemon=True)
    t.start()

    start_http_server(port)
    log.info("Exporter running on :%d (scrape interval: %ds)", port, interval)

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
