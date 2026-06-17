"""Constants for Mi Fitness integration."""

DOMAIN = "mi_fitness"

CONF_USER_ID       = "user_id"
CONF_C_USER_ID     = "c_user_id"
CONF_SSECURITY     = "ssecurity"
CONF_SERVICE_TOKEN = "service_token"
CONF_REGION        = "region"
CONF_PHONE_ID      = "phone_id"

# Path B — auto login
CONF_USERNAME      = "username"
CONF_PASSWORD      = "password"
CONF_AUTH_METHOD   = "auth_method"
CONF_PASS_TOKEN    = "pass_token"    # passToken — used for silent serviceToken refresh

AUTH_METHOD_TOKENS   = "tokens"    # Path A: manual token entry
AUTH_METHOD_PASSWORD = "password"  # Path B: username + password auto-login

REGIONS = ["us", "de", "sg", "cn", "ru", "i2"]
DEFAULT_REGION = "de"

SCAN_INTERVAL_MINUTES = 15

# Approximate watermark corresponding to ~30 days before 2026-05-30.
# Used as the default starting point on first install so users don't scan
# from absolute zero. Xiaomi watermarks are global sequential IDs (~78B/day).
# Update this value with each major release.
DEFAULT_START_WATERMARK = 157_500_000_000_000

STORAGE_KEY = "mi_fitness_watermarks"
STORAGE_VERSION = 1

# Sensor keys in the API response
KEY_STEPS              = "steps"
KEY_HEART_RATE         = "heart_rate"
KEY_RESTING_HEART_RATE = "resting_heart_rate"
KEY_SLEEP              = "sleep"
KEY_CALORIES           = "calories"
KEY_WEIGHT             = "weight"
KEY_VITALITY           = "vitality"
KEY_VALID_STAND        = "valid_stand"
KEY_SPO2               = "spo2"

# Device info
DEVICE_MANUFACTURER = "Xiaomi"
DEVICE_MODEL        = "Mi Fitness Account"
