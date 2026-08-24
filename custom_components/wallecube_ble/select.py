# SPDX-License-Identifier: GPL-3.0-or-later
"""Writable device settings, exposed as selects.

Two things keep writing narrow. Each setting lives on its own
characteristic, so the characteristic - not an opcode inside the frame -
is what selects the command; a malformed buzzer frame cannot land
somewhere else. And `doSystemReset` targets F0B5, which this integration
never writes to at all.

These three are all display/alert preferences with no electrical effect.
The numeric settings live in number.py, and the power-adapter block -
which does carry voltage thresholds - is confined to the options flow.

Every write is confirmed by reading the value back - see
coordinator.async_write_setting.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CHAR_BUZZER_MODE_UUID, CHAR_LANGUAGE_UUID, CHAR_TEMP_UNIT_UUID, DOMAIN
from .coordinator import WalleCubeCoordinator
from .parser import WalleCubeSettings

BUZZER_MODES: dict[int, str] = {0: "mute", 1: "beep_once", 2: "repeat"}
# Both of these only change what the UPS's own LCD shows - they have no
# effect on the values reported over BLE, or on this integration's units.
DISPLAY_LANGUAGES: dict[int, str] = {0: "english", 1: "chinese"}
DISPLAY_TEMP_UNITS: dict[int, str] = {0: "celsius", 1: "fahrenheit"}


@dataclass(frozen=True, kw_only=True)
class WalleCubeSelectDescription(SelectEntityDescription):
    characteristic: str
    settings_attr: str
    values: dict[int, str]
    current_fn: Callable[[WalleCubeSettings], int | None]


SELECTS: tuple[WalleCubeSelectDescription, ...] = (
    WalleCubeSelectDescription(
        key="buzzer_mode",
        translation_key="buzzer_mode_select",
        name="Buzzer mode",
        entity_category=EntityCategory.CONFIG,
        options=list(BUZZER_MODES.values()),
        characteristic=CHAR_BUZZER_MODE_UUID,
        settings_attr="buzzer_mode",
        values=BUZZER_MODES,
        current_fn=lambda s: s.buzzer_mode,
    ),
    WalleCubeSelectDescription(
        key="display_language",
        translation_key="display_language_select",
        name="Display language",
        entity_category=EntityCategory.CONFIG,
        options=list(DISPLAY_LANGUAGES.values()),
        characteristic=CHAR_LANGUAGE_UUID,
        settings_attr="display_language",
        values=DISPLAY_LANGUAGES,
        current_fn=lambda s: s.display_language,
    ),
    WalleCubeSelectDescription(
        key="display_temperature_unit",
        translation_key="display_temperature_unit_select",
        name="Display temperature unit",
        entity_category=EntityCategory.CONFIG,
        options=list(DISPLAY_TEMP_UNITS.values()),
        characteristic=CHAR_TEMP_UNIT_UUID,
        settings_attr="display_temperature_unit",
        values=DISPLAY_TEMP_UNITS,
        current_fn=lambda s: s.display_temperature_unit,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WalleCubeCoordinator = hass.data[DOMAIN][entry.entry_id]
    # Writing needs the auth code as well as the key, unlike the reads.
    if not coordinator.settings_channel_available:
        return
    async_add_entities(
        WalleCubeSelect(coordinator, entry, description) for description in SELECTS
    )


class WalleCubeSelect(CoordinatorEntity[WalleCubeCoordinator], SelectEntity):
    entity_description: WalleCubeSelectDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WalleCubeCoordinator,
        entry: ConfigEntry,
        description: WalleCubeSelectDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = f"{address}_{description.key}_select"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=entry.title,
            manufacturer="WalleCube",
        )

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        raw = self.entity_description.current_fn(self.coordinator.data.settings)
        return self.entity_description.values.get(raw)

    async def async_select_option(self, option: str) -> None:
        desc = self.entity_description
        value = next(k for k, v in desc.values.items() if v == option)
        await self.coordinator.async_write_setting(
            desc.characteristic, (desc.settings_attr,), (value,)
        )
        self.async_write_ha_state()
