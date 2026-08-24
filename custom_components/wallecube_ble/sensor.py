# SPDX-License-Identifier: GPL-3.0-or-later
"""Sensor entities for the WalleCube UPS."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ADDRESS,
    PERCENTAGE,
    EntityCategory,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WalleCubeCoordinator
from .parser import WalleCubeTelemetry

# Enum sensor value maps. Anything outside these returns None rather than
# inventing a label for a value we've never observed.
BUZZER_MODES = {0: "mute", 1: "beep_once", 2: "repeat"}
TEMPERATURE_UNITS = {0: "celsius", 1: "fahrenheit"}

# The five uint16s in the F0B2 power-adapter config block, named from the
# app's own upsSettingsDecoder field names (in order: adapterCurrent,
# enChargeLimit, workVol, stopChargeLim, pwrokDectLim). The first two are
# milliamps and the last three millivolts; all are divided by 1000 so they
# display as amps and volts.
#   (key, display name, settings attribute, device class, unit)
ADAPTER_CONFIG_FIELDS = (
    ("adapter_current", "Adapter current", "adapter_current_ma",
     SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    ("charge_current_limit", "Charge current limit", "charge_current_limit_ma",
     SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    ("working_voltage", "Working voltage", "working_voltage_mv",
     SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    ("stop_charge_voltage", "Stop-charge voltage", "stop_charge_voltage_mv",
     SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    ("power_ok_voltage", "Power-OK detect voltage", "power_ok_voltage_mv",
     SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
)


def _adapter_field(telemetry: WalleCubeTelemetry, attr: str) -> float | None:
    """One field of the adapter config block, scaled to amps/volts."""
    raw = getattr(telemetry.settings, attr, None)
    return None if raw is None else raw / 1000


@dataclass(frozen=True, kw_only=True)
class WalleCubeSensorDescription(SensorEntityDescription):
    value_fn: Callable[[WalleCubeTelemetry], float | int | str | None]


SENSORS: tuple[WalleCubeSensorDescription, ...] = (
    WalleCubeSensorDescription(
        key="input_voltage",
        translation_key="input_voltage",
        name="Input voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.input_voltage,
    ),
    WalleCubeSensorDescription(
        key="input_current",
        translation_key="input_current",
        name="Input current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.input_current,
    ),
    WalleCubeSensorDescription(
        key="output_voltage",
        translation_key="output_voltage",
        name="Output voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.output_voltage,
    ),
    WalleCubeSensorDescription(
        key="output_current",
        translation_key="output_current",
        name="Output current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.output_current,
    ),
    WalleCubeSensorDescription(
        key="output_power",
        translation_key="output_power",
        name="Output power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.output_power,
    ),
    WalleCubeSensorDescription(
        key="input_power",
        translation_key="input_power",
        name="Input power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.input_power,
    ),
    WalleCubeSensorDescription(
        key="battery_power",
        translation_key="battery_power",
        name="Battery power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda t: t.battery_power,
    ),
    WalleCubeSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        name="Battery pack voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.battery_voltage,
    ),
    WalleCubeSensorDescription(
        key="battery_current",
        translation_key="battery_current",
        name="Battery current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.battery_current,
    ),
    WalleCubeSensorDescription(
        key="battery_temperature",
        translation_key="battery_temperature",
        name="Battery temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.battery_temperature,
    ),
    WalleCubeSensorDescription(
        key="cell_voltage_diff",
        translation_key="cell_voltage_diff",
        name="Cell voltage difference",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.cell_voltage_diff,
    ),
    WalleCubeSensorDescription(
        key="battery_percent",
        translation_key="battery_percent",
        name="Battery percentage",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.battery_percent,
    ),
    # Raw status word, off by default. The named bits are exposed as
    # binary sensors; this is here so an unexplained state (or a bit we
    # haven't seen set yet) can be read directly without a BLE capture.
    WalleCubeSensorDescription(
        key="status_flags",
        translation_key="status_flags",
        name="Status flags",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda t: f"0x{t.status_flags:04X}",
    ),
    WalleCubeSensorDescription(
        key="total_consumption",
        translation_key="total_consumption",
        name="Total energy consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda t: t.total_consumption_kwh,
    ),
) + tuple(
    WalleCubeSensorDescription(
        key=f"cell_{i + 1}_voltage",
        translation_key=f"cell_{i + 1}_voltage",
        name=f"Cell {i + 1} voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=(lambda idx: lambda t: t.cell_voltages[idx])(i),
    )
    for i in range(4)
)

# Stored device settings read straight off the F0B* characteristics. These
# need only the AES key (no auth code), because they're plain GATT reads.
SETTING_SENSORS: tuple[WalleCubeSensorDescription, ...] = (
    WalleCubeSensorDescription(
        key="sleep_time",
        translation_key="sleep_time",
        name="Sleep timeout",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: t.settings.sleep_time_seconds,
    ),
    WalleCubeSensorDescription(
        key="sleep_min_current",
        translation_key="sleep_min_current",
        name="Sleep current threshold",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: t.settings.sleep_min_current_ma,
    ),
    WalleCubeSensorDescription(
        key="buzzer_mode",
        translation_key="buzzer_mode",
        name="Buzzer mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(BUZZER_MODES.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: BUZZER_MODES.get(t.settings.buzzer_mode),
    ),
    WalleCubeSensorDescription(
        key="display_temperature_unit",
        translation_key="display_temperature_unit",
        name="Display temperature unit",
        device_class=SensorDeviceClass.ENUM,
        options=list(TEMPERATURE_UNITS.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: TEMPERATURE_UNITS.get(t.settings.display_temperature_unit),
    ),
) + tuple(
    WalleCubeSensorDescription(
        key=key,
        translation_key=key,
        name=name,
        device_class=device_class,
        native_unit_of_measurement=unit,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=(lambda a: lambda t: _adapter_field(t, a))(attr),
    )
    for key, name, attr, device_class, unit in ADAPTER_CONFIG_FIELDS
)

# Only added when the F0C1 request/response channel is usable, i.e. both
# the AES key and the auth code are configured (Options flow).
F0C1_SENSORS: tuple[WalleCubeSensorDescription, ...] = (
    WalleCubeSensorDescription(
        key="lcd_off_time",
        translation_key="lcd_off_time",
        name="LCD off-time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda t: t.settings.lcd_off_time_seconds,
    ),
    WalleCubeSensorDescription(
        key="wifi_ssid",
        translation_key="wifi_ssid",
        name="Wi-Fi SSID",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: t.settings.wifi_ssid,
    ),
    WalleCubeSensorDescription(
        key="wifi_ip_address",
        translation_key="wifi_ip_address",
        name="Wi-Fi IP address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda t: t.settings.wifi_ip_address,
    ),
    WalleCubeSensorDescription(
        key="wifi_rssi",
        translation_key="wifi_rssi",
        name="Wi-Fi signal strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda t: t.settings.wifi_rssi,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WalleCubeCoordinator = hass.data[DOMAIN][entry.entry_id]
    descriptions = SENSORS
    if coordinator.settings_read_available:
        descriptions = descriptions + SETTING_SENSORS
    if coordinator.settings_channel_available:
        descriptions = descriptions + F0C1_SENSORS
    async_add_entities(
        WalleCubeSensor(coordinator, entry, description) for description in descriptions
    )


class WalleCubeSensor(CoordinatorEntity[WalleCubeCoordinator], SensorEntity):
    entity_description: WalleCubeSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WalleCubeCoordinator,
        entry: ConfigEntry,
        description: WalleCubeSensorDescription,
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
    def native_value(self) -> float | int | str | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
