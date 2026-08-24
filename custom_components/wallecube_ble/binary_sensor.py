# SPDX-License-Identifier: GPL-3.0-or-later
"""Binary sensors for the WalleCube UPS status bit field.

The bit positions are taken from the app's own `measureDecoder`, by
tracing each `and` mask through the stack slot it is stored in to the key
it is filed under in the result map - not guessed. See
const.STATUS_* and the README.

All four words this device has ever produced, from a live AC-loss capture
(462 frames, 2026-08-23):

    0x0401  on mains, idle          acOk
    0x0481  on mains, charging      acOk + charging
    0x0101  running on battery      discharging
    0x0501  mains restored,         acOk + discharging
            not yet charging

That last one is why `on_battery` is derived from acOk rather than from
the discharging bit: discharging only tracks the sign of the battery
current, so it can be set with the mains plugged in.

Bit 0x0001 is set in every frame ever captured and the app never reads it.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    STATUS_AC_OK,
    STATUS_CHARGING,
    STATUS_DISCHARGING,
    STATUS_OVERLOAD,
    STATUS_SHUTDOWN_IMMINENT,
)
from .coordinator import WalleCubeCoordinator
from .parser import WalleCubeTelemetry


@dataclass(frozen=True, kw_only=True)
class WalleCubeBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[WalleCubeTelemetry], bool]


BINARY_SENSORS: tuple[WalleCubeBinarySensorDescription, ...] = (
    WalleCubeBinarySensorDescription(
        key="ac_present",
        translation_key="ac_present",
        name="AC present",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda t: bool(t.status_flags & STATUS_AC_OK),
    ),
    # Derived from acOk, NOT from the discharging bit. A live AC-loss
    # capture produced the word 0x0501 - acOk AND discharging both set -
    # once mains returned but before charging resumed, because the
    # discharging bit only tracks the sign of the battery current. Keying
    # this off that bit reported "on battery" with the mains plugged in.
    WalleCubeBinarySensorDescription(
        key="on_battery",
        translation_key="on_battery",
        name="On battery",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda t: not t.status_flags & STATUS_AC_OK,
    ),
    # The raw bit, which is genuinely "battery current is negative" and
    # can be set while running on mains. Off by default; `on_battery`
    # above is the one you want for automations.
    WalleCubeBinarySensorDescription(
        key="discharging",
        translation_key="discharging",
        name="Battery discharging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda t: bool(t.status_flags & STATUS_DISCHARGING),
    ),
    WalleCubeBinarySensorDescription(
        key="overload",
        translation_key="overload",
        name="Overload",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda t: bool(t.status_flags & STATUS_OVERLOAD),
    ),
    WalleCubeBinarySensorDescription(
        key="shutdown_imminent",
        translation_key="shutdown_imminent",
        name="Shutdown imminent",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda t: bool(t.status_flags & STATUS_SHUTDOWN_IMMINENT),
    ),
    # Off by default: this bit tracks the charger's actual duty, so on a
    # full pack it flips on nearly every telemetry tick as the float
    # current swings either side of zero. That churn is why an earlier
    # release dropped a "battery charging" sensor built on this same bit.
    # It is genuine device behaviour rather than noise, so it's kept -
    # just not inflicted on anyone by default.
    WalleCubeBinarySensorDescription(
        key="charging",
        translation_key="charging",
        name="Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda t: bool(t.status_flags & STATUS_CHARGING),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WalleCubeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WalleCubeBinarySensor(coordinator, entry, description)
        for description in BINARY_SENSORS
    )


class WalleCubeBinarySensor(CoordinatorEntity[WalleCubeCoordinator], BinarySensorEntity):
    entity_description: WalleCubeBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WalleCubeCoordinator,
        entry: ConfigEntry,
        description: WalleCubeBinarySensorDescription,
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
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
