# SPDX-License-Identifier: GPL-3.0-or-later
"""Writable numeric settings: sleep config, LCD off-time, adapter config.

Multi-field blocks (sleep on F0B4, adapter on F0B2) live in a single
characteristic, so changing one field means re-sending the whole block.
Each entity reads its siblings' current values and writes them back
unchanged alongside the new one.

The power-adapter config is deliberately NOT here - it lives in the
options flow (Settings > Devices & Services > Configure). It describes
the PSU you physically attached, it is set once when hardware changes,
and the UPS only applies it once its reset button is pressed. An entity
you can drag in a dashboard is the wrong shape for a value like that.

Writes verify only the field actually being set: siblings are echoed
back unchanged and some fields are device-managed, so comparing them
would report correct behaviour as a failure.

Min/max come from the device's own clamping behaviour, observed by
writing 0x0000/0xFFFF and reading back where it settled.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ADDRESS,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CHAR_SLEEP_CONFIG_UUID, DOMAIN
from .coordinator import WalleCubeCoordinator
from .parser import WalleCubeSettings

SLEEP_ATTRS = ("sleep_time_seconds", "sleep_min_current_ma")


@dataclass(frozen=True, kw_only=True)
class WalleCubeNumberDescription(NumberEntityDescription):
    value_fn: Callable[[WalleCubeSettings], int | None]
    # For a member of a multi-field block: which characteristic, the full
    # attribute list in wire order, and this field's position in it.
    # All None for the LCD off-time, which is a standalone F0C1 command.
    characteristic: str | None = None
    block_attrs: tuple[str, ...] | None = None
    block_index: int | None = None
    # Raw units per displayed unit (1000 for V/A stored as mV/mA).
    scale: int = 1


NUMBERS: tuple[WalleCubeNumberDescription, ...] = (
    WalleCubeNumberDescription(
        key="sleep_time_set",
        translation_key="sleep_time_set",
        name="Sleep timeout",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        native_min_value=20, native_max_value=7200, native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        characteristic=CHAR_SLEEP_CONFIG_UUID,
        block_attrs=SLEEP_ATTRS,
        block_index=0,
        value_fn=lambda s: s.sleep_time_seconds,
    ),
    WalleCubeNumberDescription(
        key="sleep_min_current_set",
        translation_key="sleep_min_current_set",
        name="Sleep current threshold",
        device_class=NumberDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        native_min_value=20, native_max_value=3000, native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        characteristic=CHAR_SLEEP_CONFIG_UUID,
        block_attrs=SLEEP_ATTRS,
        block_index=1,
        value_fn=lambda s: s.sleep_min_current_ma,
    ),
    WalleCubeNumberDescription(
        key="lcd_off_time_set",
        translation_key="lcd_off_time_set",
        name="LCD off-time",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        native_min_value=10, native_max_value=36000, native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda s: s.lcd_off_time_seconds,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WalleCubeCoordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.settings_channel_available:
        return
    async_add_entities(
        WalleCubeNumber(coordinator, entry, description) for description in NUMBERS
    )


class WalleCubeNumber(CoordinatorEntity[WalleCubeCoordinator], NumberEntity):
    entity_description: WalleCubeNumberDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WalleCubeCoordinator,
        entry: ConfigEntry,
        description: WalleCubeNumberDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        address = entry.data[CONF_ADDRESS]
        self._attr_unique_id = f"{address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=entry.title,
            manufacturer="WalleCube",
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data.settings)

    async def async_set_native_value(self, value: float) -> None:
        desc = self.entity_description

        if desc.characteristic is None:          # LCD off-time
            await self.coordinator.async_write_lcd_off_time(int(value))
            self.async_write_ha_state()
            return

        settings = self.coordinator.data.settings if self.coordinator.data else None
        block = [getattr(settings, a, None) for a in desc.block_attrs] if settings else []
        if not block or any(v is None for v in block):
            raise HomeAssistantError(
                "Current settings haven't been read from the UPS yet - "
                "try again shortly"
            )

        block[desc.block_index] = int(round(value * desc.scale))

        # Only check the field we actually set. Siblings are echoed back
        # unchanged, and device-managed ones (enChargeLimit) get
        # recomputed - flagging those as failures would be wrong.
        await self.coordinator.async_write_setting(
            desc.characteristic, desc.block_attrs, block,
            verify_indices=(desc.block_index,),
        )
        self.async_write_ha_state()
