"""Offer one upload destination per Garmin Connect account."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er


def list_garmin_accounts(hass, account_ready=None) -> list[dict]:
    """Offer one explicit account destination from enabled Garmin sensors."""
    grouped = {}
    for entity in er.async_get(hass).entities.values():
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
            (account_ready(entity.entity_id) if account_ready else entry.state == ConfigEntryState.LOADED)
            and state is not None
            and state.state not in {"unavailable", "unknown"}
        )
        grouped.setdefault(entity.config_entry_id, []).append((entity, entry, state, available))

    accounts = []
    for entry_id, sensors in grouped.items():
        sensors.sort(key=lambda candidate: (not candidate[3], candidate[0].entity_id))
        entity, entry, state, available = sensors[0]
        friendly_name = state.attributes.get("friendly_name") if state is not None else None
        accounts.append({
            "entry_id": entry_id,
            "entity_id": entity.entity_id,
            "entity_ids": sorted(candidate[0].entity_id for candidate in sensors),
            "name": entry.title or friendly_name or entity.entity_id,
            "available": available,
        })
    return sorted(accounts, key=lambda account: (account["name"].casefold(), account["entity_id"]))


def garmin_account(hass, entry_id: str | None) -> dict | None:
    """Resolve the current sensor for a saved account preference."""
    if entry_id is None:
        return None
    return next(
        (account for account in list_garmin_accounts(hass) if account["entry_id"] == entry_id),
        None,
    )
