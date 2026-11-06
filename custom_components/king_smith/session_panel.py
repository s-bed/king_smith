"""An authenticated, administrator-only Home Assistant session browser."""

from pathlib import Path
from uuid import UUID

from .const import DOMAIN
from .schema_compat import vol
from .session_manager import SessionError


def _session_uuid(value: str) -> str:
    """Require a canonical session UUID, never a filesystem path."""
    try:
        parsed = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as err:
        raise vol.Invalid("Invalid session identifier") from err
    if parsed != value:
        raise vol.Invalid("Invalid session identifier")
    return parsed


async def async_setup_panel(hass, manager) -> None:
    """Serve only application code statically; send fitness data over HA auth."""
    from homeassistant.components import frontend, panel_custom, websocket_api

    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("session_panel_registered"):
        return

    @websocket_api.websocket_command({"type": "king_smith/sessions/list"})
    @websocket_api.require_admin
    @websocket_api.async_response
    async def list_sessions(hass, connection, message):
        connection.send_result(message["id"], {
            "sessions": manager.list_all_sessions(), "devices": manager.list_devices(),
            "storage_error": manager.last_error,
        })

    @websocket_api.websocket_command({
        "type": "king_smith/sessions/get", vol.Required("session_id"): _session_uuid,
    })
    @websocket_api.require_admin
    @websocket_api.async_response
    async def get_session(hass, connection, message):
        try:
            result = await manager.async_session_detail(message["session_id"])
        except SessionError as err:
            connection.send_error(message["id"], "not_found", str(err))
        else:
            connection.send_result(message["id"], result)

    @websocket_api.websocket_command({
        "type": "king_smith/sessions/download", vol.Required("session_id"): _session_uuid,
        vol.Required("format"): vol.In(["fit", "json"]),
    })
    @websocket_api.require_admin
    @websocket_api.async_response
    async def download_session(hass, connection, message):
        try:
            result = await manager.async_session_download(message["session_id"], message["format"])
        except SessionError as err:
            connection.send_error(message["id"], "file_unavailable", str(err))
        else:
            connection.send_result(message["id"], result)

    for command in (list_sessions, get_session, download_session):
        websocket_api.async_register_command(hass, command)

    url = "/king_smith_static/king-smith-panel.js"
    path = str(Path(__file__).parent / "frontend" / "king-smith-panel.js")
    if hasattr(hass.http, "async_register_static_paths"):
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths([StaticPathConfig(url, path, False)])
    else:
        hass.http.register_static_path(url, path, False)
    if not frontend.async_panel_exists(hass, "walkingpad-sessions"):
        await panel_custom.async_register_panel(
            hass, frontend_url_path="walkingpad-sessions", webcomponent_name="king-smith-panel",
            sidebar_title="WalkingPad sessions", sidebar_icon="mdi:walk",
            module_url=url + "?v=0.1.3-dev.4", embed_iframe=False,
            trust_external=False, require_admin=True,
        )
    domain_data["session_panel_registered"] = True
