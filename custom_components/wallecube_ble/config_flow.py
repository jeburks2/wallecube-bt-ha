# SPDX-License-Identifier: GPL-3.0-or-later
"""Config flow for WalleCube UPS (BLE, local)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
    ADAPTER_ATTRS,
    CHAR_ADAPTER_CONFIG_UUID,
    CONF_AES_KEY,
    CONF_AUTH_CODE,
    DOMAIN,
)
from .parser import derive_adapter_block

CONF_ADAPTER_VOLTAGE = "adapter_voltage"
CONF_ADAPTER_CURRENT = "adapter_current"

MAX_OUTPUT_WATTS = 150          # W150 rating
VOLTAGE_RANGE = (9.0, 20.0)
CURRENT_RANGE = (0.5, 10.0)


class WalleCubeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery (via manifest bluetooth matcher) and manual setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WalleCubeOptionsFlow()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovered_name or "WalleCube UPS",
                data={CONF_ADDRESS: self._discovered_address},
            )
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._discovered_name or ""},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual entry, in case the bluetooth matcher doesn't fire (e.g.
        the device isn't currently advertising with its local name)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="WalleCube UPS", data={CONF_ADDRESS: address})

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
            description_placeholders={"example": "AA:BB:CC:DD:EE:FF"},
        )

class WalleCubeOptionsFlow(OptionsFlow):
    """Device configuration: crypto material, and the power adapter.

    Adapter voltage/current live here rather than as entities because
    they describe the PSU you physically attached: set once when hardware
    changes, and not applied until the device's reset button is pressed.

    The crypto values are specific to YOUR OWN UPS - never share them or
    commit them. See the README for how to extract them. Leaving them blank
    just means the settings entities aren't created - all telemetry
    sensors work fine without them.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["credentials", "adapter"]
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            aes_key = user_input.get(CONF_AES_KEY, "").strip()
            auth_code = user_input.get(CONF_AUTH_CODE, "").strip()
            if aes_key and not _is_valid_hex_key(aes_key):
                errors[CONF_AES_KEY] = "invalid_aes_key"
            elif auth_code and not auth_code.isdigit():
                errors[CONF_AUTH_CODE] = "invalid_auth_code"
            else:
                return self.async_create_entry(
                    title="",
                    data={CONF_AES_KEY: aes_key, CONF_AUTH_CODE: auth_code},
                )

        current = self.config_entry.options
        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_AES_KEY, default=current.get(CONF_AES_KEY, "")): str,
                    vol.Optional(CONF_AUTH_CODE, default=current.get(CONF_AUTH_CODE, "")): str,
                }
            ),
            errors=errors,
        )

    async def async_step_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Write adapter voltage/current, deriving the rest of the block.

        Nothing is stored in the config entry - the device owns these - so
        the form is seeded from what it currently reports.
        """
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None or not coordinator.settings_channel_available:
            return self.async_abort(reason="adapter_unavailable")

        settings = coordinator.data.settings if coordinator.data else None
        volts = settings.working_voltage_mv / 1000 if settings and settings.working_voltage_mv else None
        amps = settings.adapter_current_ma / 1000 if settings and settings.adapter_current_ma else None
        if volts is None or amps is None:
            return self.async_abort(reason="adapter_not_read_yet")

        errors: dict[str, str] = {}
        if user_input is not None:
            new_v = float(user_input[CONF_ADAPTER_VOLTAGE])
            new_a = float(user_input[CONF_ADAPTER_CURRENT])
            if new_v * new_a > MAX_OUTPUT_WATTS:
                errors["base"] = "exceeds_power_rating"
            else:
                # Derive the whole block. Preserving the previously-read
                # thresholds instead is what produced a block the device
                # could not reconcile, and it resolved that by dropping
                # the working voltage to match them.
                block = derive_adapter_block(
                    int(round(new_a * 1000)), int(round(new_v * 1000))
                )
                try:
                    await coordinator.async_write_setting(
                        CHAR_ADAPTER_CONFIG_UUID,
                        ADAPTER_ATTRS,
                        block,
                        verify_indices=(0, 2),
                    )
                except HomeAssistantError:
                    errors["base"] = "write_failed"
                else:
                    persistent_notification.async_create(
                        self.hass,
                        title="WalleCube UPS - reset button required",
                        message=(
                            f"Adapter settings saved to the UPS "
                            f"({new_v:g} V, {new_a:g} A, power-off "
                            f"{block[4] / 1000:g} V).\n\n"
                            "**Now press the reset button on the device.** "
                            "The new voltage and current do not take effect "
                            "until you do."
                        ),
                        notification_id=f"{DOMAIN}_adapter_reset_required",
                    )
                    return self.async_abort(reason="adapter_saved")

        derived = derive_adapter_block(int(round(amps * 1000)), int(round(volts * 1000)))
        return self.async_show_form(
            step_id="adapter",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADAPTER_VOLTAGE, default=volts): vol.All(
                        vol.Coerce(float), vol.Range(min=VOLTAGE_RANGE[0], max=VOLTAGE_RANGE[1])
                    ),
                    vol.Required(CONF_ADAPTER_CURRENT, default=amps): vol.All(
                        vol.Coerce(float), vol.Range(min=CURRENT_RANGE[0], max=CURRENT_RANGE[1])
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "watts": str(MAX_OUTPUT_WATTS),
                "power_off": f"{derived[4] / 1000:g}",
            },
        )


def _is_valid_hex_key(value: str) -> bool:
    try:
        return len(bytes.fromhex(value)) == 16
    except ValueError:
        return False
