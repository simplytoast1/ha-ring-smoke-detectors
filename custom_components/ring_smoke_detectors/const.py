"""Constants for the Ring Smoke Detectors integration."""

DOMAIN = "ring_smoke_detectors"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_LOCATION_IDS = "location_ids"

CLIENT_API_BASE = "https://api.ring.com/clients_api/"
DEVICE_API_BASE = "https://api.ring.com/devices/v1/"
APP_API_BASE = "https://prd-api-us.prd.rings.solutions/api/v1/"

API_VERSION = 11

KIDDE_DEVICE_TYPE_SMOKE_ONLY = "comp.bluejay.sensor_bluejay_ws"
KIDDE_DEVICE_TYPE_SMOKE_CO = "comp.bluejay.sensor_bluejay_wsc"
KIDDE_DEVICE_TYPE_SMOKE_CO_BATTERY = "comp.bluejay.sensor_bluejay_sc"

KIDDE_KIND_SMOKE_ONLY = "sensor_bluejay_ws"
KIDDE_KIND_SMOKE_CO = "sensor_bluejay_wsc"
KIDDE_KIND_SMOKE_CO_BATTERY = "sensor_bluejay_sc"
