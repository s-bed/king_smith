"""Public session actions used by automations and the review blueprint."""

from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .schema_compat import vol
from .session_manager import SessionError


def register_session_services(hass, manager) -> None:
    """Register account-aware actions without exposing arbitrary file uploads."""
    base = {vol.Required("device_id"): cv.string}
    session_fields = {**base, vol.Required("session_id"): cv.string}

    async def list_sessions(call):
        return {"sessions": manager.list_sessions(call.data["device_id"], call.data.get("status"))}

    async def upload_session(call):
        try:
            session = await manager.async_upload(
                call.data["device_id"], call.data["session_id"],
                call.data["garmin_entity_id"], call.data.get("retry", False),
                expected_attempts=call.data.get("expected_attempts"),
            )
        except SessionError as err:
            raise HomeAssistantError(str(err)) from err
        return {"session": session}

    async def discard_session(call):
        try:
            session = await manager.async_discard(call.data["device_id"], call.data["session_id"])
        except SessionError as err:
            raise HomeAssistantError(str(err)) from err
        return {"session": session}

    hass.services.async_register(DOMAIN, "list_sessions", list_sessions,
        schema=vol.Schema({**base, vol.Optional("status"): cv.string}),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "upload_session", upload_session,
        schema=vol.Schema({**session_fields, vol.Required("garmin_entity_id"): cv.entity_id,
                           vol.Optional("retry", default=False): cv.boolean,
                           vol.Optional("expected_attempts"): cv.positive_int}),
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "discard_session", discard_session,
        schema=vol.Schema(session_fields), supports_response=SupportsResponse.OPTIONAL)
