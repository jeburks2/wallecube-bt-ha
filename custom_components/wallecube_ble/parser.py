# SPDX-License-Identifier: GPL-3.0-or-later
"""Parser for the WalleCube UPS F0B1 telemetry notification frame.

Frame layout (40 bytes, all multi-byte fields little-endian), confirmed
against live app readings on 2026-08-22:

  offset  size  field                     scale
  ------  ----  ------------------------  --------------------
  0-1     u16   message type              const 0x0051
  2-3     u16   input_voltage             / 1000  -> V
  4-5     u16   input_current             / 1000  -> A
  6-7     u16   output_voltage            / 1000  -> V
  8-9     u16   output_current            / 1000  -> A
  10-11   u16   battery_percent           / 10    -> %
  12-13   u16   battery_voltage           / 1000  -> V  (== sum of 4 cells)
  14-15   u16   cell_1_voltage            unscaled -> mV
  16-17   u16   cell_2_voltage            unscaled -> mV
  18-19   u16   cell_3_voltage            unscaled -> mV
  20-21   u16   cell_4_voltage            unscaled -> mV
  22-23   s16   battery_current           / 1000  -> A (signed; + = charging)
  24-25   s16   battery_temperature       / 10    -> deg C (SIGNED)
  26-27   u16   leftSecs                  runtime remaining - see below
  28-29   u16   unknown                   (fluctuates; app never reads it)
  30-33   u32   total_consumption         / 1e6   -> kWh (monotonic)
  34-37   u32   unknown                   (observed const 0)
  38-39   u16   status_flags              bit field, see const.STATUS_*

Offset 10 was previously mis-labelled "rated capacity (const 1000)". The
app's `measureDecoder` divides it by **10** (every voltage/current field
uses 1000) and files it under `batteryCapacity`, so 1000 means 100.0%.
It only looked constant because this pack has been full throughout every
capture so far. Confirmed structurally from the disassembly rather than
by observing it move - see README.

The 38-39 status word's bit positions come from the app's measureDecoder
(each `and` mask traced through its stack slot to the map key it feeds),
and are exposed as binary sensors - see const.STATUS_*.

`leftSecs` is offset **26**, and on this model it is a dead field. A live
AC-loss capture (462 frames, 357 of them with the mains actually
disconnected) had offset 26 reading a constant `1` in every single frame.
Offset 28 is not it either - it jitters up and down (188 of 356 steps
downward, i.e. a random walk) rather than counting anything down, and the
app never reads it. So this UPS reports no runtime-remaining figure over
BLE, and no sensor is exposed for one.

Also included below: encrypt/decrypt helpers, the F0C1 request/response
settings channel, and the F0B* stored-settings blocks - see const.py's
module docstring for the crypto details. Everything is read-only except
build_setting_write(), which drives the single-byte settings.
"""
from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass, field

from Crypto.Cipher import AES

from .const import (
    F0C1_NONCE,
    MSG_SET_LCD_OFF_TIME,
    SETTING_BLOCK_MARKER,
    TELEMETRY_FRAME_LEN,
    TELEMETRY_HEADER,
    ZERO_IV,
)

@dataclass
class WalleCubeSettings:
    """Slow-changing device configuration, read separately from telemetry.

    Every field stays None until its first successful read, so a setting
    we couldn't fetch simply has no sensor value rather than a wrong one.
    """

    lcd_off_time_seconds: int | None = None
    sleep_time_seconds: int | None = None
    sleep_min_current_ma: int | None = None
    buzzer_mode: int | None = None  # 0=mute, 1=beep once, 2=repeat
    display_temperature_unit: int | None = None  # 0=Celsius, 1=Fahrenheit
    display_language: int | None = None  # 0=English, 1=Chinese
    # The F0B2 block, in wire order, named from the app's own
    # upsSettingsDecoder: adapterCurrent, enChargeLimit, workVol,
    # stopChargeLim, pwrokDectLim. First two milliamps, last three
    # millivolts. charge_current_limit and stop_charge_voltage are
    # derived by the app from the other three and are never written
    # directly here - see number.py.
    adapter_current_ma: int | None = None
    charge_current_limit_ma: int | None = None
    working_voltage_mv: int | None = None
    stop_charge_voltage_mv: int | None = None
    power_ok_voltage_mv: int | None = None
    wifi_connected: bool | None = None
    wifi_rssi: int | None = None
    wifi_ssid: str | None = None
    wifi_ip_address: str | None = None


@dataclass
class WalleCubeTelemetry:
    input_voltage: float
    input_current: float
    output_voltage: float
    output_current: float
    battery_voltage: float
    cell_voltages: tuple[int, int, int, int]  # millivolts
    battery_current: float
    battery_temperature: float
    status_flags: int
    battery_percent: float
    total_consumption_kwh: float
    settings: WalleCubeSettings = field(default_factory=WalleCubeSettings)

    @property
    def output_power(self) -> float:
        """Watts delivered to the load. The device doesn't report power
        directly - the app's own display computes it the same way."""
        return round(self.output_voltage * self.output_current, 2)

    @property
    def input_power(self) -> float:
        """Watts drawn from the adapter."""
        return round(self.input_voltage * self.input_current, 2)

    @property
    def battery_power(self) -> float:
        """Watts into (+) or out of (-) the battery."""
        return round(self.battery_voltage * self.battery_current, 2)

    @property
    def cell_voltage_diff(self) -> int:
        """Max-min spread across the 4 cell voltages, in mV. A growing
        spread over time is the classic early sign of a cell going bad /
        pack imbalance."""
        return max(self.cell_voltages) - min(self.cell_voltages)


def parse_telemetry(data: bytes) -> WalleCubeTelemetry | None:
    """Parse a raw F0B1 notification payload. Returns None if it doesn't
    look like a valid telemetry frame."""
    if len(data) != TELEMETRY_FRAME_LEN:
        return None

    msg_type = struct.unpack_from("<H", data, 0)[0]
    if msg_type != TELEMETRY_HEADER:
        return None

    input_voltage = struct.unpack_from("<H", data, 2)[0] / 1000
    input_current = struct.unpack_from("<H", data, 4)[0] / 1000
    output_voltage = struct.unpack_from("<H", data, 6)[0] / 1000
    output_current = struct.unpack_from("<H", data, 8)[0] / 1000
    battery_percent = struct.unpack_from("<H", data, 10)[0] / 10
    battery_voltage = struct.unpack_from("<H", data, 12)[0] / 1000
    cells = struct.unpack_from("<4H", data, 14)
    battery_current = struct.unpack_from("<h", data, 22)[0] / 1000
    # Signed: the app reads this as int16, so sub-zero temperatures would
    # otherwise wrap to ~6553 degrees.
    battery_temperature = struct.unpack_from("<h", data, 24)[0] / 10
    total_consumption_kwh = struct.unpack_from("<I", data, 30)[0] / 1_000_000
    status_flags = struct.unpack_from("<H", data, 38)[0]

    return WalleCubeTelemetry(
        input_voltage=input_voltage,
        input_current=input_current,
        output_voltage=output_voltage,
        output_current=output_current,
        battery_voltage=battery_voltage,
        cell_voltages=cells,
        battery_current=battery_current,
        battery_temperature=battery_temperature,
        status_flags=status_flags,
        battery_percent=battery_percent,
        total_consumption_kwh=total_consumption_kwh,
    )


# --- F0C1 settings channel (AES-CBC, zero IV) --------------------------
# aes_key/auth_code are per-device secrets supplied via the config entry's
# options (see const.CONF_AES_KEY/CONF_AUTH_CODE) - never hardcoded here.

def _f0c1_encrypt(plaintext: bytes, aes_key: bytes) -> bytes:
    if len(plaintext) % 16 != 0:
        plaintext = plaintext + bytes(16 - (len(plaintext) % 16))
    return AES.new(aes_key, AES.MODE_CBC, iv=ZERO_IV).encrypt(plaintext)


def _f0c1_decrypt(ciphertext: bytes, aes_key: bytes) -> bytes:
    return AES.new(aes_key, AES.MODE_CBC, iv=ZERO_IV).decrypt(ciphertext)


def build_f0c1_query(msg_type: int, aes_key: bytes, auth_code: bytes) -> bytes:
    """Build a read-only F0C1 query frame: a header with an empty payload.

    Layout is type | payload_len | nonce | authCode (see const.py). With
    payload_len = 0 this is purely a request for the current value, and
    reproduces the app's own captured LCD off-time query byte-for-byte.
    """
    plaintext = struct.pack("<BBH", msg_type, 0, F0C1_NONCE) + auth_code
    return _f0c1_encrypt(plaintext, aes_key)


def parse_f0c1_response(
    ciphertext: bytes, aes_key: bytes, auth_code: bytes
) -> tuple[int, bytes] | None:
    """Decrypt an F0C1 notification into (msg_type, payload).

    Returns None if it doesn't decrypt to a well-formed frame carrying our
    own auth code - which is also how we ignore frames meant for someone
    else, or garbage from a wrong key.
    """
    if not ciphertext or len(ciphertext) % 16:
        return None
    plaintext = _f0c1_decrypt(ciphertext, aes_key)
    if len(plaintext) < 8 or plaintext[4:8] != auth_code:
        return None
    msg_type = plaintext[0]
    payload_len = plaintext[1]
    payload = plaintext[8 : 8 + payload_len]
    if len(payload) != payload_len:
        return None
    return msg_type, payload


def parse_lcd_off_time_payload(payload: bytes) -> int | None:
    """LCD off-time response payload -> seconds."""
    if len(payload) < 4:
        return None
    return struct.unpack_from("<I", payload, 0)[0]


def parse_wifi_status_payload(payload: bytes) -> dict | None:
    """Wi-Fi status response payload.

    Layout (confirmed against a live device on 2026-08-23):
      connected(1) rssi(int8) ip(4) gateway(4) netmask(4) ssid_len(1) ssid
    """
    if len(payload) < 15:
        return None
    connected = bool(payload[0])
    rssi = struct.unpack_from("<b", payload, 1)[0]
    ip = ".".join(str(b) for b in payload[2:6])
    ssid_len = payload[14]
    raw_ssid = payload[15 : 15 + ssid_len]
    # Compare byte lengths, not character counts - a multi-byte UTF-8 SSID
    # decodes to fewer characters than it occupies bytes.
    if len(raw_ssid) != ssid_len:
        return None
    ssid = raw_ssid.decode("utf-8", errors="replace")
    return {"connected": connected, "rssi": rssi, "ip_address": ip, "ssid": ssid}


# --- F0B* stored-settings blocks (plain GATT reads, AES-encrypted) ------
# No auth code and no request frame needed - just read and decrypt.

def build_setting_write(values: Sequence[int], aes_key: bytes, auth_code: bytes) -> bytes:
    """Build a write frame for an F0B* settings block.

        0x51 | 0x00 | authCode(4 LE) | FF FF FF FF | u16 per value

    zero-padded to an AES block. The payload begins at offset 10; the
    0xFFFFFFFF is a fixed part of the header, not a placeholder. Getting
    that offset wrong does NOT fail safe - the device reads whatever
    bytes land there and clamps them into its own valid ranges, so a
    misaligned frame silently writes plausible-looking wrong values.

    Confirmed on hardware for buzzer mode (single value) and sleep config
    (two values). A single-byte value and a u16 are equivalent here
    because of the zero padding, so everything is written as u16.
    """
    return _f0c1_encrypt(
        bytes([SETTING_BLOCK_MARKER, 0x00])
        + auth_code
        + b"\xff\xff\xff\xff"
        + b"".join(struct.pack("<H", v & 0xFFFF) for v in values),
        aes_key,
    )


def derive_adapter_block(adapter_current_ma: int, working_voltage_mv: int) -> tuple[int, ...]:
    """Expand the two user-settable adapter values into all five wire fields.

    `doUpsSettings` takes three arguments for five fields because the app
    derives the rest, and the device expects them to agree:

        enChargeLimit = adapterCurrent - 1000   (1 A reserved for the load)
        stopChargeLim = workVol x 0.965
        pwrokDectLim  = workVol x 0.958, rounded to the nearest 0.1 V

    Reproduces this device's factory block exactly from (3000, 12000):
    (3000, 2000, 12000, 11580, 11500).

    Deriving these rather than preserving whatever was read is not
    cosmetic. Writing a new working voltage alongside stale thresholds
    produces a block the device cannot reconcile, and it resolves the
    conflict by dropping the working voltage to match the thresholds -
    which is how a 12 V write came back as 9 V after a reset.
    """
    return (
        adapter_current_ma,
        max(0, adapter_current_ma - 1000),
        working_voltage_mv,
        round(working_voltage_mv * 0.965),
        round(working_voltage_mv * 0.958 / 100) * 100,
    )


def build_lcd_off_time_write(seconds: int, aes_key: bytes, auth_code: bytes) -> bytes:
    """Build the F0C1 frame that sets the LCD off-time.

        0x4B | 0x04 | nonce(2) | authCode(4 LE) | u32 seconds

    Same shape as any other F0C1 command - type, payload length, nonce,
    auth, payload - with a 4-byte payload. Confirmed on hardware.
    """
    return _f0c1_encrypt(
        struct.pack("<BBH", MSG_SET_LCD_OFF_TIME, 4, F0C1_NONCE)
        + auth_code
        + struct.pack("<I", seconds),
        aes_key,
    )


def parse_setting_block(ciphertext: bytes, aes_key: bytes, count: int) -> tuple[int, ...] | None:
    """Decrypt an F0B* settings characteristic into its uint16 fields.

    Payload is a 0x51 marker byte followed by `count` little-endian
    uint16s. Note the marker is NOT followed by a length byte - do not
    parse these with the F0C1 layout.
    """
    if not ciphertext or len(ciphertext) % 16:
        return None
    plaintext = _f0c1_decrypt(ciphertext, aes_key)
    if plaintext[0] != SETTING_BLOCK_MARKER:
        return None
    if len(plaintext) < 1 + 2 * count:
        return None
    return struct.unpack_from(f"<{count}H", plaintext, 1)
