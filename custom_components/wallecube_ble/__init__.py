# SPDX-License-Identifier: GPL-3.0-or-later
"""The WalleCube UPS (BLE, local) integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import CONF_AES_KEY, CONF_AUTH_CODE, DOMAIN
from .coordinator import WalleCubeCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]


def _parse_aes_key(value: str | None) -> bytes | None:
    if not value:
        return None
    return bytes.fromhex(value.strip())


def _parse_auth_code(value: str | None) -> bytes | None:
    if not value:
        return None
    return int(value.strip()).to_bytes(4, "little")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    address = entry.data[CONF_ADDRESS]
    aes_key = _parse_aes_key(entry.options.get(CONF_AES_KEY))
    auth_code = _parse_auth_code(entry.options.get(CONF_AUTH_CODE))
    coordinator = WalleCubeCoordinator(hass, address, aes_key, auth_code)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Establish the first connection now so entities have data at startup.
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: WalleCubeCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
