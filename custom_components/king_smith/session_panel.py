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


def _garmin_accounts(hass, registry, account_ready) -> list[dict]:
    """Offer one explicit account destination from enabled Garmin sensors."""
    grouped = {}
    for entity in registry.entities.values():
        if (
            entity.platform != "garmin_connect"
            or not entity.entity_id.startswith("sensor.")
            or entity.disabled_by is not None
            or entity.config_entry_id is None
        ):
            continue
        entry = hass.config_entries.async_get_entry(entity.config_entry_id)
        if entry is None or entry.domain != "garmin_connect":
            continue
        state = hass.states.get(entity.entity_id)
        available = bool(
            account_ready(entity.entity_id)
            and state is not None
            and state.state not in {"unavailable", "unknown"}
        )
        grouped.setdefault(entity.config_entry_id, []).append((entity, entry, state, available))

    accounts = []
    for sensors in grouped.values():
        sensors.sort(key=lambda candidate: (not candidate[3], candidate[0].entity_id))
        entity, entry, state, available = sensors[0]
        friendly_name = state.attributes.get("friendly_name") if state is not None else None
        accounts.append({
            "entity_id": entity.entity_id,
            "entity_ids": sorted(candidate[0].entity_id for candidate in sensors),
            "name": entry.title or friendly_name or entity.entity_id,
            "available": available,
        })
    return sorted(accounts, key=lambda account: (account["name"].casefold(), account["entity_id"]))


async def async_setup_panel(hass, manager) -> None:
    """Serve only application code statically; send fitness data over HA auth."""
    from homeassistant.components import frontend, panel_custom, websocket_api
    from homeassistant.helpers import entity_registry as er

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
            "garmin_accounts": _garmin_accounts(hass, er.async_get(hass), manager.account_ready),
            "garmin_upload_available": hass.services.has_service("garmin_connect", "upload_activity"),
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
            module_url=url + "?v=0.1.3-dev.5", embed_iframe=False,
            trust_external=False, require_admin=True,
        )
    domain_data["session_panel_registered"] = True
