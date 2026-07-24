"""Constants for the Ring Smoke Detectors integration."""

DOMAIN = "ring_smoke_detectors"

CONF_REFRESH_TOKEN = "refresh_token"

CLIENT_API_BASE = "https://api.ring.com/clients_api/"
DEVICE_API_BASE = "https://api.ring.com/devices/v1/"
APP_API_BASE = "https://prd-api-us.prd.rings.solutions/api/v1/"

API_VERSION = 11

# Asset "kind" prefix shared by all Kidde detector models
KIDDE_KIND_PREFIX = "sensor_bluejay"

KIDDE_KIND_SMOKE_ONLY = "sensor_bluejay_ws"
KIDDE_KIND_SMOKE_CO = "sensor_bluejay_wsc"
KIDDE_KIND_SMOKE_CO_BATTERY = "sensor_bluejay_sc"

# WebSocket deviceType values are the kind prefixed with "comp.bluejay."
KIDDE_DEVICE_TYPE_SMOKE_ONLY = f"comp.bluejay.{KIDDE_KIND_SMOKE_ONLY}"
KIDDE_DEVICE_TYPE_SMOKE_CO = f"comp.bluejay.{KIDDE_KIND_SMOKE_CO}"
KIDDE_DEVICE_TYPE_SMOKE_CO_BATTERY = f"comp.bluejay.{KIDDE_KIND_SMOKE_CO_BATTERY}"

KIDDE_MODEL_NAMES = {
    KIDDE_KIND_SMOKE_ONLY: "Smart Smoke Alarm (Wired)",
    KIDDE_KIND_SMOKE_CO: "Smart Smoke + CO Alarm (Wired)",
    KIDDE_KIND_SMOKE_CO_BATTERY: "Smart Smoke + CO Alarm (Battery)",
}


def model_name(device_type: str) -> str:
    """Human-readable model name for a deviceType or asset kind."""
    kind = device_type.rsplit(".", 1)[-1]
    return KIDDE_MODEL_NAMES.get(kind, device_type)


# Impulse type sent when the physical test button is pressed
IMPULSE_ALARM_TESTING = "alarm.testing"

# Event fired on the HA bus for detector events (test button, etc.)
EVENT_RING_SMOKE_DETECTORS = f"{DOMAIN}_event"

EVENT_TYPE_TEST = "test"


def signal_test_event(entry_id: str, zid: str) -> str:
    """Dispatcher signal name for test button events on a device."""
    return f"{DOMAIN}_{entry_id}_test_{zid}"
