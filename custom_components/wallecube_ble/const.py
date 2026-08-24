# SPDX-License-Identifier: GPL-3.0-or-later
"""Constants for the WalleCube UPS BLE integration.

Protocol reverse-engineered from the official WalleCube app
(com.wallecube.ups) via Dart AOT disassembly recovered with blutter, plus
the app's own leftover debug print()s captured via logcat, on 2026-08-22
and extended against app v1.0.24 on 2026-08-23. See project README for the
full writeup.

Three groups of characteristics are used:

- F0B1: notify, sent in CLEARTEXT roughly once per second while connected.
  Battery/input/output telemetry - see parser.py for the frame layout.

- F0B2/F0B3/F0B4/F0B8/F0B9 (service F0A1): the device's stored *settings*,
  one setting per characteristic, AES-encrypted with the same key as F0C1.
  Reads are plain GATT reads - no auth code and no request frame is
  needed, which makes them completely safe to poll. Payload is a 0x51
  marker byte followed by little-endian uint16 fields; see
  parser.parse_setting_block(). Each mapping below was confirmed by
  round-trip (change it in the official app, read it back, revert, read
  again) on 2026-08-23.

- F0C1 (service F0A2): write + notify, AES-128-CBC encrypted (zero IV,
  zero-padded to a 16-byte multiple). A request/response settings channel.
  Frame layout:

      type(1) | payload_len(1) | nonce(2) | authCode(4 LE) | payload

  A query is that frame with payload_len = 0. The nonce is a fixed 0x7724
  in the app's own code and the device does not appear to validate it.

The AES key and "auth code" are DERIVED FROM YOUR OWN DEVICE
(MD5(fixed_app_salt + a per-device value) -> hex string -> raw key bytes)
and are NOT universal across other WalleCube units - deliberately not
hardcoded/committed here. Each installation configures its own via the
integration's Options flow. See README for how to extract them.

Writing is supported for the buzzer mode, display language, temperature
unit, the two-field sleep block on F0B4, and the five-field adapter block
on F0B2 - see parser.build_setting_write(). Writes DO need the auth code,
unlike reads, and every write is verified by reading the value back.

What makes writing safe is that the *characteristic* is the command
selector, not an opcode inside the frame: each setting has its own, so a
malformed frame cannot be reinterpreted as some other command.
doSystemReset targets F0B5 (CHAR_SYSTEM_RESET_UUID) and nothing else
uses it, so it is unreachable from any write we make.

The F0B2 adapter block carries real electrical thresholds and its five
fields must agree with each other, so it is written only from the options
flow, with all five derived - see parser.derive_adapter_block().

No pairing/bonding is required for a plain GATT connection - confirmed via
`bluetoothctl connect` (Paired: no, Bonded: no, Connected: yes).
"""

DOMAIN = "wallecube_ble"

# Config-entry option keys for the per-device crypto material - see module
# docstring. Both optional. The AES key alone unlocks the F0B* settings
# sensors; the F0C1 sensors (LCD off-time, Wi-Fi) need the auth code too.
CONF_AES_KEY = "aes_key"  # hex string, 32 chars (16 bytes)
CONF_AUTH_CODE = "auth_code"  # decimal string - e.g. "123456789"

SERVICE_UUID = "0000f0a1-0000-1000-8000-00805f9b34fb"
CHAR_TELEMETRY_UUID = "0000f0b1-0000-1000-8000-00805f9b34fb"  # notify, cleartext, ~1/sec
CHAR_CMD_C1_UUID = "0000f0c1-0000-1000-8000-00805f9b34fb"  # write+notify, AES settings channel

# NEVER write to this. doSystemReset is the only thing that targets it,
# and nothing else uses it - which is precisely why writing to any OTHER
# characteristic cannot be misread as a reset.
CHAR_SYSTEM_RESET_UUID = "0000f0b5-0000-1000-8000-00805f9b34fb"

# --- F0B* stored-settings characteristics (readable, AES-encrypted) ---
# The tuple value is how many uint16 fields the payload carries.
CHAR_ADAPTER_CONFIG_UUID = "0000f0b2-0000-1000-8000-00805f9b34fb"  # 5x u16, semantics unconfirmed
CHAR_BUZZER_MODE_UUID = "0000f0b3-0000-1000-8000-00805f9b34fb"  # 1x u16
CHAR_SLEEP_CONFIG_UUID = "0000f0b4-0000-1000-8000-00805f9b34fb"  # 2x u16: seconds, mA
CHAR_LANGUAGE_UUID = "0000f0b8-0000-1000-8000-00805f9b34fb"  # 1x u16: 0=English, 1=Chinese
CHAR_TEMP_UNIT_UUID = "0000f0b9-0000-1000-8000-00805f9b34fb"  # 1x u16: 0=C, 1=F

# The F0B2 block's fields in wire order. Shared by the coordinator (to
# read them) and the options flow (to write them).
ADAPTER_ATTRS: tuple[str, ...] = (
    "adapter_current_ma",
    "charge_current_limit_ma",
    "working_voltage_mv",
    "stop_charge_voltage_mv",
    "power_ok_voltage_mv",
)

SETTING_BLOCK_MARKER = 0x51

TELEMETRY_FRAME_LEN = 40
TELEMETRY_HEADER = 0x0051

# Bits of the u16 status word at telemetry offset 38. Taken from the app's
# measureDecoder by tracing each `and` mask through the stack slot it
# lands in to the key it is filed under in the result map - these are
# read out of the code, not inferred from observation.
#
# Corroboration from live frames: on mains the word reads 0x0401 and
# flips to 0x0481 while float-charging, i.e. acOk set the whole time and
# the charging bit toggling. Bit 0x0001 is set in every frame ever
# captured and is not read by the app at all.
STATUS_OVERLOAD = 0x0004
STATUS_SHUTDOWN_IMMINENT = 0x0010
STATUS_CHARGING = 0x0080
STATUS_DISCHARGING = 0x0100
STATUS_AC_OK = 0x0400

# Seconds of no F0B1 notification before we consider the connection stale
# and attempt a reconnect.
NOTIFY_TIMEOUT = 30

# --- Settings-channel crypto ---
# ZERO_IV is fixed by the protocol itself (not a secret).
ZERO_IV = bytes(16)

# Fixed 2-byte field the app puts in every F0C1 request. Named "nonce" for
# want of a better word - the device echoes back a different value and
# does not appear to validate what we send.
F0C1_NONCE = 0x7724

# F0C1 message types (from the app's DeviceCoder encoders).
MSG_GET_WIFI_STATUS = 0x41
MSG_GET_LCD_OFF_TIME = 0x4C
MSG_SET_LCD_OFF_TIME = 0x4B

# Settings change rarely and every poll costs BLE round-trips on a device
# whose stack is easily upset, so they're polled far less often than the
# telemetry watchdog runs.
SETTINGS_POLL_INTERVAL = 600
