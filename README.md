# aiden-exporter

Prometheus exporter for the [Fellow Aiden](https://fellowproducts.com/products/aiden) coffee brewer. Polls Fellow's cloud API on a configurable interval and exposes device state, brew history, and usage stats as Prometheus metrics.

> **Note:** The Aiden has no local API — all data flows through Fellow's cloud. An internet connection is required.

## Acknowledgements

This project is built on top of [9b/fellow-aiden](https://github.com/9b/fellow-aiden), a Python library for interacting with the Fellow Aiden brewer. All API communication is handled by that library — go give it a star.

## Prerequisites

- [UV](https://docs.astral.sh/uv/getting-started/installation/) — Python package manager
- [Task](https://taskfile.dev/installation/) — task runner (optional but recommended)
- A Fellow account with an Aiden registered

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `FELLOW_EMAIL` | yes | — | Fellow account email |
| `FELLOW_PASSWORD` | yes | — | Fellow account password |
| `EXPORTER_PORT` | no | `9090` | Port the metrics HTTP server listens on |
| `SCRAPE_INTERVAL` | no | `60` | Seconds between API polls |

## Running locally

```bash
task run
```

Or without Task:

```bash
uv sync
FELLOW_EMAIL=you@example.com FELLOW_PASSWORD=secret uv run python exporter.py
```

Check metrics are flowing:

```bash
task metrics
# or
curl http://localhost:9090/metrics | grep fellow_aiden
```

## Running with Docker

```bash
# Build image
task build

# Run (reads credentials from .env)
task docker-run
```

## Kubernetes deployment

The container exposes port `9090`. Supply credentials via a `Secret` and reference it in your `Deployment`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aiden-exporter
spec:
  replicas: 1
  selector:
    matchLabels:
      app: aiden-exporter
  template:
    metadata:
      labels:
        app: aiden-exporter
    spec:
      containers:
        - name: exporter
          image: your-registry/aiden-exporter:latest
          ports:
            - containerPort: 9090
          envFrom:
            - secretRef:
                name: fellow-credentials
---
apiVersion: v1
kind: Secret
metadata:
  name: fellow-credentials
stringData:
  FELLOW_EMAIL: you@example.com
  FELLOW_PASSWORD: secret
```

If you use **kube-prometheus-stack**, add a `ServiceMonitor`:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: aiden-exporter
spec:
  selector:
    matchLabels:
      app: aiden-exporter
  endpoints:
    - port: metrics
      interval: 60s
```

## Metrics reference

| Metric | Type | Description |
|---|---|---|
| `fellow_aiden_brewing` | Gauge | 1 if currently brewing |
| `fellow_aiden_carafe_present` | Gauge | 1 if carafe inserted |
| `fellow_aiden_heater_on` | Gauge | 1 if heating element active |
| `fellow_aiden_lid_closed` | Gauge | 1 if lid closed |
| `fellow_aiden_missing_water` | Gauge | 1 if water tank empty |
| `fellow_aiden_single_brew_basket_present` | Gauge | 1 if single-serve basket present |
| `fellow_aiden_batch_brew_basket_present` | Gauge | 1 if batch basket present |
| `fellow_aiden_total_water_volume_ml` | Gauge | Lifetime water used (mL) |
| `fellow_aiden_last_brew_water_volume_ml` | Gauge | Last brew water volume (mL) |
| `fellow_aiden_total_brew_cycles` | Gauge | Total brew cycle count |
| `fellow_aiden_last_brew_start_timestamp_seconds` | Gauge | Unix timestamp of last brew start |
| `fellow_aiden_last_brew_end_timestamp_seconds` | Gauge | Unix timestamp of last brew end |
| `fellow_aiden_profiles_count` | Gauge | Number of saved brew profiles |
| `fellow_aiden_schedules_count` | Gauge | Number of configured schedules |
| `fellow_aiden_device_info` | Gauge | Always 1; labels: `brewer_name`, `selected_profile_id` |
| `fellow_aiden_scrape_success` | Gauge | 1 if last API poll succeeded, 0 if failed |
| `fellow_aiden_last_scrape_timestamp_seconds` | Gauge | Unix timestamp of last successful poll |

All metrics carry a `brewer_name` label set to the device's display name.
