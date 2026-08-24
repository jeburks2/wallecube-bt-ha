# SPDX-License-Identifier: GPL-3.0-or-later
"""Connection + notification handling for the WalleCube UPS."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import timedelta

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ADAPTER_ATTRS,
    CHAR_ADAPTER_CONFIG_UUID,
    CHAR_BUZZER_MODE_UUID,
    CHAR_CMD_C1_UUID,
    CHAR_LANGUAGE_UUID,
    CHAR_SLEEP_CONFIG_UUID,
    CHAR_TELEMETRY_UUID,
    CHAR_TEMP_UNIT_UUID,
    DOMAIN,
    MSG_GET_LCD_OFF_TIME,
    MSG_GET_WIFI_STATUS,
    NOTIFY_TIMEOUT,
    SETTINGS_POLL_INTERVAL,
)
from .parser import (
    WalleCubeSettings,
    WalleCubeTelemetry,
    build_f0c1_query,
    build_lcd_off_time_write,
    build_setting_write,
    parse_f0c1_response,
    parse_lcd_off_time_payload,
    parse_setting_block,
    parse_telemetry,
    parse_wifi_status_payload,
)

_LOGGER = logging.getLogger(__name__)

# The device notifies telemetry roughly once per second on its own
# schedule once subscribed - give it a generous window on first connect
# before giving up.
FIRST_NOTIFY_TIMEOUT = 15

# How long to wait for a response to an F0C1 query before giving up on
# that poll (best-effort - does not fail the whole coordinator update).
F0C1_RESPONSE_TIMEOUT = 5

# The device needs a moment to apply a setting before it reads back the
# new value; reading immediately returns the old one.
WRITE_SETTLE_DELAY = 1.5

# (characteristic, settings attribute names) for the plain-GATT-read
# settings blocks. The number of attribute names is also how many uint16
# fields the block carries.
SETTING_BLOCKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CHAR_SLEEP_CONFIG_UUID, ("sleep_time_seconds", "sleep_min_current_ma")),
    (CHAR_BUZZER_MODE_UUID, ("buzzer_mode",)),
    (CHAR_TEMP_UNIT_UUID, ("display_temperature_unit",)),
    (CHAR_LANGUAGE_UUID, ("display_language",)),
    (CHAR_ADAPTER_CONFIG_UUID, ADAPTER_ATTRS),
)


class WalleCubeCoordinator(DataUpdateCoordinator[WalleCubeTelemetry]):
    """Maintains a persistent BLE connection and pushes telemetry updates.

    This is push-based (GATT notify) for telemetry - update_interval is
    only used as a "have we gone quiet, reconnect" watchdog. The F0C1
    settings channel (LCD off-time) is request/response, so it's actively
    polled once per update_interval instead, best-effort.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        aes_key: bytes | None,
        auth_code: bytes | None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=NOTIFY_TIMEOUT),
        )
        self.address = address
        self._aes_key = aes_key
        self._auth_code = auth_code
        self._client: BleakClient | None = None
        self._notified_event = asyncio.Event()
        # Settings live on the coordinator rather than on each telemetry
        # frame: they're polled on their own much slower schedule, and
        # every incoming frame just carries a reference to them.
        self._settings = WalleCubeSettings()
        self._f0c1_pending: dict[int, asyncio.Event] = {}
        self._last_settings_poll: float | None = None

    @property
    def settings_read_available(self) -> bool:
        """Whether the F0B* settings blocks can be read.

        These are plain GATT reads, so they need only the AES key - no
        auth code and no request frame.
        """
        return self._aes_key is not None

    @property
    def settings_channel_available(self) -> bool:
        """Whether the F0C1 request/response channel is usable.

        Unlike the F0B* reads this sends a request frame, which the device
        only honours if it carries the right auth code.
        """
        return self._aes_key is not None and self._auth_code is not None

    @property
    def ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)

    async def _async_update_data(self) -> WalleCubeTelemetry:
        """Called by the watchdog interval; (re)connects if needed."""
        if self._client is None or not self._client.is_connected:
            await self._connect()

        if self.data is None:
            # Freshly (re)connected - the device pushes telemetry on its
            # own ~1/sec schedule, so give it a moment to arrive rather
            # than assuming it's already here.
            try:
                await asyncio.wait_for(self._notified_event.wait(), timeout=FIRST_NOTIFY_TIMEOUT)
            except TimeoutError as err:
                raise UpdateFailed(
                    f"Connected but received no telemetry notification within {FIRST_NOTIFY_TIMEOUT}s"
                ) from err

        if self.data is None:
            raise UpdateFailed("Not yet received a telemetry notification")

        if self._settings_poll_due():
            await self._poll_settings()

        return self.data

    def _settings_poll_due(self) -> bool:
        if not self.settings_read_available:
            return False
        if self._last_settings_poll is None:
            return True
        return (time.monotonic() - self._last_settings_poll) >= SETTINGS_POLL_INTERVAL

    async def _poll_settings(self) -> None:
        """Best-effort refresh of the slow-changing device settings.

        Never raises: a settings read failing must not take the telemetry
        stream down with it. Each block is read independently so one bad
        characteristic doesn't cost us the rest.
        """
        assert self._client is not None
        for uuid, attrs in SETTING_BLOCKS:
            await self._read_setting_block(uuid, attrs)
        if self.settings_channel_available:
            await self._query_f0c1(MSG_GET_LCD_OFF_TIME)
            await self._query_f0c1(MSG_GET_WIFI_STATUS)

        self._last_settings_poll = time.monotonic()
        self.async_update_listeners()

    async def async_write_setting(
        self,
        uuid: str,
        attrs: Sequence[str],
        values: Sequence[int],
        verify_indices: Sequence[int] | None = None,
    ) -> None:
        """Write an F0B* settings block and confirm it by reading it back.

        The whole block is written at once because that is how the device
        takes it - changing one field of a multi-field block means
        re-sending the others unchanged.

        Verification is not optional here. A misaligned frame does not
        fail cleanly: the device reads whatever bytes land in the payload
        and clamps them into its valid ranges, so a bad write looks
        successful at the GATT layer while quietly storing wrong values.

        `verify_indices` limits the check to the fields we actually meant
        to set. Some fields are device-managed and get recomputed on
        write - the adapter block's enChargeLimit is derived as
        adapterCurrent - 1000 - so comparing those would report a failure
        for what is really the device correcting us. Whatever it stores is
        read back into our state either way.
        """
        self._assert_writable()
        frame = build_setting_write(values, self._aes_key, self._auth_code)
        try:
            await self._client.write_gatt_char(uuid, frame, response=True)
        except (BleakError, TimeoutError) as err:
            raise HomeAssistantError(f"Failed to write setting: {err}") from err

        await asyncio.sleep(WRITE_SETTLE_DELAY)
        got = await self._read_setting_raw(uuid, len(attrs))
        if got is None:
            raise HomeAssistantError("Wrote the setting but could not read it back")
        checked = range(len(values)) if verify_indices is None else verify_indices
        mismatched = [i for i in checked if got[i] != values[i]]
        if mismatched:
            detail = ", ".join(
                f"{attrs[i]}: asked {values[i]}, reads {got[i]}" for i in mismatched
            )
            raise HomeAssistantError(
                f"Device did not store the requested value ({detail}) - it may "
                f"have clamped it to an allowed range"
            )
        for attr, value in zip(attrs, got):
            setattr(self._settings, attr, value)
        self.async_update_listeners()

    async def async_write_lcd_off_time(self, seconds: int) -> None:
        """Set the LCD off-time over F0C1, then re-query to confirm."""
        self._assert_writable()
        frame = build_lcd_off_time_write(seconds, self._aes_key, self._auth_code)
        try:
            await self._client.write_gatt_char(CHAR_CMD_C1_UUID, frame, response=True)
        except (BleakError, TimeoutError) as err:
            raise HomeAssistantError(f"Failed to write LCD off-time: {err}") from err

        await asyncio.sleep(WRITE_SETTLE_DELAY)
        await self._query_f0c1(MSG_GET_LCD_OFF_TIME)
        if self._settings.lcd_off_time_seconds != seconds:
            raise HomeAssistantError(
                f"Device did not store the requested LCD off-time (asked for "
                f"{seconds}, reads {self._settings.lcd_off_time_seconds})"
            )
        self.async_update_listeners()

    def _assert_writable(self) -> None:
        if not self.settings_channel_available:
            raise HomeAssistantError(
                "Writing settings needs both the AES key and the auth code "
                "to be configured in the integration's options"
            )
        if self._client is None or not self._client.is_connected:
            raise HomeAssistantError("Not connected to the UPS")

    async def _read_setting_raw(self, uuid: str, count: int) -> tuple[int, ...] | None:
        assert self._client is not None
        try:
            raw = bytes(await self._client.read_gatt_char(uuid))
        except Exception as err:  # noqa: BLE001 - best-effort
            _LOGGER.debug("Settings read of %s failed (non-fatal): %s", uuid, err)
            return None
        values = parse_setting_block(raw, self._aes_key, count)
        if values is None:
            _LOGGER.debug("Unrecognized settings block from %s: %s", uuid, raw.hex())
        return values

    async def _read_setting_block(self, uuid: str, attrs: tuple[str, ...]) -> None:
        values = await self._read_setting_raw(uuid, len(attrs))
        if values is None:
            return
        for attr, value in zip(attrs, values):
            setattr(self._settings, attr, value)

    async def _query_f0c1(self, msg_type: int) -> None:
        """Send one F0C1 query and wait for its matching response."""
        assert self._client is not None
        event = asyncio.Event()
        self._f0c1_pending[msg_type] = event
        try:
            query = build_f0c1_query(msg_type, self._aes_key, self._auth_code)
            await self._client.write_gatt_char(CHAR_CMD_C1_UUID, query, response=True)
            await asyncio.wait_for(event.wait(), timeout=F0C1_RESPONSE_TIMEOUT)
        except Exception as err:  # noqa: BLE001 - deliberately broad, best-effort poll
            _LOGGER.debug("F0C1 query 0x%02X failed (non-fatal): %s", msg_type, err)
        finally:
            self._f0c1_pending.pop(msg_type, None)

    async def _connect(self) -> None:
        """Single, gentle connection attempt - no retry-with-backoff.

        This device's BLE stack is fragile: bleak_retry_connector's
        establish_connection() does up to ~9 internal retries on its own,
        and empirically that hammering is what wedges the UPS's BLE
        stack (requiring the device's reset button to recover - confirmed
        both from the Home Assistant logs and by reproducing it via raw
        `bluetoothctl`). A single plain BleakClient connect, exactly like
        the one that works reliably from a standalone script, is used
        instead. If this fails, let the coordinator's own backoff
        (update_interval) space out the next attempt rather than
        retrying rapidly here.
        """
        device = self.ble_device
        if device is None:
            raise UpdateFailed(f"Device {self.address} not visible to any Bluetooth adapter/proxy")

        self._notified_event.clear()
        client = BleakClient(device, disconnected_callback=self._on_disconnect)
        try:
            await client.connect()
            await client.start_notify(CHAR_TELEMETRY_UUID, self._on_notify)
            if self.settings_channel_available:
                await client.start_notify(CHAR_CMD_C1_UUID, self._on_f0c1_notify)
        except (BleakError, TimeoutError) as err:
            raise UpdateFailed(f"Failed to connect/subscribe: {err}") from err

        self._client = client
        # Re-read settings on the next update rather than trusting values
        # cached from before the device dropped off.
        self._last_settings_poll = None
        _LOGGER.debug("Connected and subscribed to telemetry for %s", self.address)

    @callback
    def _on_notify(self, _handle: int, data: bytearray) -> None:
        telemetry = parse_telemetry(bytes(data))
        if telemetry is None:
            _LOGGER.debug("Ignoring non-telemetry or malformed frame: %s", bytes(data).hex())
            return
        # Settings are polled on their own schedule; every frame just
        # points at the coordinator's current copy of them.
        telemetry.settings = self._settings
        self.async_set_updated_data(telemetry)
        self._notified_event.set()

    @callback
    def _on_f0c1_notify(self, _handle: int, data: bytearray) -> None:
        parsed = parse_f0c1_response(bytes(data), self._aes_key, self._auth_code)
        if parsed is None:
            _LOGGER.debug("Ignoring unrecognized F0C1 frame: %s", bytes(data).hex())
            return
        msg_type, payload = parsed

        if msg_type == MSG_GET_LCD_OFF_TIME:
            seconds = parse_lcd_off_time_payload(payload)
            if seconds is not None:
                self._settings.lcd_off_time_seconds = seconds
        elif msg_type == MSG_GET_WIFI_STATUS:
            status = parse_wifi_status_payload(payload)
            if status is not None:
                self._settings.wifi_connected = status["connected"]
                self._settings.wifi_rssi = status["rssi"]
                self._settings.wifi_ssid = status["ssid"]
                self._settings.wifi_ip_address = status["ip_address"]
        else:
            _LOGGER.debug("Unhandled F0C1 message type 0x%02X: %s", msg_type, payload.hex())

        if (event := self._f0c1_pending.get(msg_type)) is not None:
            event.set()

    @callback
    def _on_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("Disconnected from %s, will reconnect on next update", self.address)
        self._client = None

    async def async_shutdown(self) -> None:
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()
        await super().async_shutdown()
