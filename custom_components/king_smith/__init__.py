"""The Walkingpad integration."""
from __future__ import annotations
import logging
from datetime import timedelta


from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.components import bluetooth
from homeassistant.const import Platform, CONF_NAME, CONF_MAC, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .coordinator import WalkingPadCoordinator
from .walking_pad import WalkingPadApi
from .session_manager import SessionManager
from .session_services import register_session_services

PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.NUMBER, Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Load the durable recorder even when the treadmill is unavailable."""
    hass.data.setdefault(DOMAIN, {})

    def account_ready(entity_id):
        entity = er.async_get(hass).async_get(entity_id)
        if entity is None or entity.platform != "garmin_connect":
            return False
        entry = hass.config_entries.async_get_entry(entity.config_entry_id)
        return entry is not None and entry.state == ConfigEntryState.LOADED

    manager = SessionManager(hass, Store(hass, 1, "king_smith_sessions"), account_ready)
    hass.data[DOMAIN]["session_manager"] = manager
    register_session_services(hass, manager)
    await manager.async_initialize()
    cancel_tick = async_track_time_interval(hass, manager.async_tick, timedelta(seconds=5))

    async def shutdown(_event):
        cancel_tick()
        await manager.async_shutdown()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Walkingpad from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    address = entry.data.get(CONF_MAC)

    ble_device = bluetooth.async_ble_device_from_address(
        hass, entry.data.get(CONF_MAC), connectable=True
    )
    name = entry.data.get(CONF_NAME) or DOMAIN
    walking_pad = WalkingPadApi(
        name, ble_device, address=address,
        device_resolver=lambda: bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        ),
    )
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, walking_pad.mac)},
        name=name, manufacturer="King Smith",
    )
    manager = hass.data[DOMAIN]["session_manager"]
    manager.attach(entry.entry_id, device.id, name)
    coordinator = WalkingPadCoordinator(hass, walking_pad, device.id)

    def receive_snapshot(sample):
        hass.async_create_task(manager.async_receive(entry.entry_id, sample))

    remove_snapshot = walking_pad.register_snapshot_callback(receive_snapshot)

    hass.data[DOMAIN][entry.entry_id] = {
        "device": walking_pad,
        "coordinator": coordinator,
        "session_manager": manager,
        "remove_snapshot": remove_snapshot,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        data["remove_snapshot"]()
        await data["coordinator"].async_shutdown()
        await data["session_manager"].async_tick()

    bluetooth.async_rediscover_address(hass, entry.data.get(CONF_MAC))

    return unload_ok

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
