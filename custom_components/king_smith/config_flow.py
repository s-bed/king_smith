"""Config flow for Walkingpad."""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from homeassistant import config_entries
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
)

from .const import (
    CONF_GARMIN_ACCOUNT,
    CONF_MINIMUM_MOVING_SECONDS,
    CONF_NOTIFICATION_DEVICE,
    DEFAULT_MINIMUM_MOVING_SECONDS,
    DOMAIN,
)
from .garmin_accounts import list_garmin_accounts
from .schema_compat import vol

if TYPE_CHECKING:
    from homeassistant.components import bluetooth

_NO_ACCOUNT = "none"


class WalkingpadConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Walkingpad."""

    VERSION = 0.1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> SessionReviewOptionsFlow:
        """Open the per-WalkingPad review settings."""
        return SessionReviewOptionsFlow(entry)

    def __init__(self) -> None:
        """Initialize the available device choices for this flow."""
        self.devices: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle setup started from Add integration."""
        return await self.async_step_device(user_input)

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(
            device_registry.format_mac(discovery_info.address)
        )
        self._abort_if_unique_id_configured()

        self.devices = [f"{discovery_info.address} ({discovery_info.name})"]
        return await self.async_step_device()

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle setting up a device."""
        if user_input is None:
            schema_mac = str
            if self.devices:
                schema_mac = vol.In(self.devices)
            schema = vol.Schema(
                {vol.Required(CONF_NAME): str, vol.Required(CONF_MAC): schema_mac}
            )
            return self.async_show_form(step_id="device", data_schema=schema)

        user_input[CONF_MAC] = user_input[CONF_MAC][:17]
        unique_id = device_registry.format_mac(user_input[CONF_MAC])
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)


class SessionReviewOptionsFlow(config_entries.OptionsFlow):
    """Configure review without restarting the Bluetooth connection."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        """Read the current options for the form."""
        self._initial_options = entry.options

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Show or save the review selectors."""
        if user_input is not None:
            options = {**self._initial_options, **user_input}
            if CONF_NOTIFICATION_DEVICE not in user_input:
                options.pop(CONF_NOTIFICATION_DEVICE, None)
            if options.get(CONF_GARMIN_ACCOUNT) == _NO_ACCOUNT:
                options.pop(CONF_GARMIN_ACCOUNT)
            return self.async_create_entry(title="", data=options)

        choices = [{"value": _NO_ACCOUNT, "label": "No default account"}]
        choices.extend(
            {"value": account["entry_id"], "label": account["name"]}
            for account in list_garmin_accounts(self.hass)
        )
        selected = self._initial_options.get(CONF_GARMIN_ACCOUNT)
        if selected and not any(choice["value"] == selected for choice in choices):
            choices.append({"value": selected, "label": "Saved account (unavailable)"})
        schema = vol.Schema({
            vol.Required(CONF_GARMIN_ACCOUNT, default=_NO_ACCOUNT): SelectSelector(
                SelectSelectorConfig(options=choices)
            ),
            vol.Optional(CONF_NOTIFICATION_DEVICE): DeviceSelector(
                DeviceSelectorConfig(filter={"integration": "mobile_app"})
            ),
            vol.Required(CONF_MINIMUM_MOVING_SECONDS, default=DEFAULT_MINIMUM_MOVING_SECONDS): NumberSelector(
                NumberSelectorConfig(min=0, max=3600, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX)
            ),
        })
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, self._initial_options),
        )
