DOMAIN = "tuya_ble_lock"
STORAGE_KEY = "tuya_ble_lock_credentials"
STORAGE_KEY_DEVICES = "tuya_ble_lock_devices"
STORAGE_VERSION = 1

# GATT UUIDs (FD50 defaults — auto-detected at runtime via _resolve_gatt_uuids)
SERVICE_UUID = "0000fd50-0000-1000-8000-00805f9b34fb"
WRITE_UUID   = "00000001-0000-1001-8001-00805f9b07d0"
NOTIFY_UUID  = "00000002-0000-1001-8001-00805f9b07d0"
MANUFACTURER_ID = 0x07D0

# Command codes (Tuya BLE protocol)
CMD_DEVICE_INFO    = 0x0000
CMD_PAIR           = 0x0001
CMD_DP_WRITE_V3    = 0x0002
CMD_DP_WRITE_V4    = 0x0027
CMD_DEVICE_STATUS  = 0x0003
CMD_TIME_V1        = 0x8011
CMD_TIME_V2        = 0x8012
CMD_RECV_DP        = 0x8001
CMD_DP_REPORT_V4   = 0x8006
CMD_DP_EVENT_V4    = 0x8007

# Security flags
SEC_NONE        = 0
SEC_AUTH_KEY     = 1
SEC_AUTH_SESSION = 2
SEC_LOGIN_KEY   = 4
SEC_SESSION_KEY = 5
SEC_COMM_KEY    = 6
# btScyChannel (new security) — used by K3 BLE PRO 2 and similar protocol 5.0 locks
SEC_NEW_SEC         = 14  # key = MD5(local_key + sec_key)
SEC_NEW_SEC_SESSION = 15  # key = MD5(local_key + sec_key + srand)

# Credential types
CRED_PASSWORD    = 0x01
CRED_CARD        = 0x02
CRED_FINGERPRINT = 0x03
CRED_FACE        = 0x04

# Enrollment stages
STAGE_START    = 0x00
STAGE_PROGRESS = 0xFC
STAGE_FAILED   = 0xFD
STAGE_CANCEL   = 0xFE
STAGE_DONE     = 0xFF

STAGE_NAMES = {
    0x00: "STARTED",
    0xFC: "IN_PROGRESS",
    0xFD: "FAILED",
    0xFE: "CANCELLED",
    0xFF: "COMPLETE",
}

# Config entry data keys (v2 hub entry — cloud account)
CONF_TUYA_EMAIL   = "tuya_email"
CONF_TUYA_PASSWORD = "tuya_password"
CONF_TUYA_COUNTRY = "tuya_country_code"
CONF_TUYA_REGION  = "tuya_region"

# Device store keys (per-lock, in .storage)
CONF_DEVICE_MAC   = "device_mac"
CONF_DEVICE_UUID  = "device_uuid"
CONF_LOGIN_KEY    = "login_key"
CONF_VIRTUAL_ID   = "virtual_id"
CONF_AUTH_KEY     = "auth_key"
CONF_PRODUCT_ID   = "product_id"
# btScyChannel credentials
CONF_LOCAL_KEY   = "local_key"   # full 16-char local_key (ASCII) from cloud
CONF_SEC_KEY     = "sec_key"     # 16-char sec_key from cloud
CONF_CHECK_CODE  = "check_code"  # 8-digit unlock verification code (from cloud DP71)
