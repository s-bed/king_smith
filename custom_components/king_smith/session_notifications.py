"""Send optional Companion App prompts for durably saved sessions."""

import asyncio
from dataclasses import dataclass, field
import logging
from uuid import uuid4

from homeassistant.components.device_automation import (
    DeviceAutomationType,
    InvalidDeviceAutomationConfig,
    async_get_device_automation_platform,
)
from homeassistant.const import EVENT_SERVICE_REGISTERED
from homeassistant.core import EventOrigin, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.start import async_at_started

from .const import (
    CONF_GARMIN_ACCOUNT,
    CONF_MINIMUM_MOVING_SECONDS,
    CONF_NOTIFICATION_DEVICE,
    DEFAULT_MINIMUM_MOVING_SECONDS,
)
from .garmin_accounts import garmin_account
from .session_manager import SessionError

_LOGGER = logging.getLogger(__name__)
_MOBILE_APP = "mobile_app"
_REVIEW_STATUSES = {"awaiting_review", "failed_local", "needs_review", "queued", "blocked"}
_APPROVABLE_STATUSES = {"awaiting_review", "failed_local"}
_ACTION_PREFIX = "king_smith_review"


@dataclass(frozen=True)
class _Prompt:
    phone: str
    account: str | None
    status: str
    attempts: int
    token: str = field(default_factory=lambda: uuid4().hex, compare=False)


class SessionNotifications:
    """Approve an individual session from the selected phone."""

    def __init__(self, hass, manager) -> None:
        """Track outstanding prompts for the durable session manager."""
        self.hass = hass
        self.manager = manager
        self._prompts: dict[str, _Prompt] = {}
        self._deciding: set[str] = set()
        self._lock = asyncio.Lock()

    @callback
    def async_listen(self) -> None:
        """Listen for saved sessions, Companion App actions, and startup."""
        self.hass.bus.async_listen("king_smith_session_ready", self._async_session_updated)
        self.hass.bus.async_listen("king_smith_session_updated", self._async_session_updated)
        self.hass.bus.async_listen("mobile_app_notification_action", self._async_action)
        self.hass.bus.async_listen(EVENT_SERVICE_REGISTERED, self._async_notify_registered)
        async_at_started(self.hass, self._async_started)

    async def _async_started(self, _hass) -> None:
        await self._async_refresh()

    async def _async_notify_registered(self, event) -> None:
        if event.data["domain"] == "notify" and event.data["service"].startswith("mobile_app_"):
            await self._async_refresh()

    async def async_options_updated(self, _hass, entry) -> None:
        """Apply new review settings to pending sessions for this WalkingPad."""
        await self._async_refresh(entry.entry_id)

    async def _async_refresh(self, entry_id: str | None = None) -> None:
        for session in self.manager.list_all_sessions():
            if entry_id is None or entry_id == session["entry_id"]:
                await self._async_update(session["session_id"])

    async def _async_session_updated(self, event) -> None:
        if isinstance(session_id := event.data.get("session_id"), str):
            await self._async_update(session_id)

    def _prompt_for(self, session: dict) -> _Prompt | None:
        if session["status"] not in _REVIEW_STATUSES:
            return None
        entry = self.hass.config_entries.async_get_entry(session["entry_id"])
        if entry is None or entry.disabled_by is not None:
            return None
        options = entry.options
        minimum = options.get(CONF_MINIMUM_MOVING_SECONDS, DEFAULT_MINIMUM_MOVING_SECONDS)
        if session["moving_seconds"] >= minimum and (phone := options.get(CONF_NOTIFICATION_DEVICE)):
            return _Prompt(phone, options.get(CONF_GARMIN_ACCOUNT), session["status"], session["attempts"])
        return None

    async def _async_update(self, session_id: str, error: str | None = None) -> None:
        async with self._lock:
            session = self.manager.get_summary(session_id)
            prompt = self._prompt_for(session) if session is not None else None
            previous = self._prompts.get(session_id)
            tag = f"{_ACTION_PREFIX}_{session_id}"
            if previous and (prompt is None or previous.phone != prompt.phone):
                self._prompts.pop(session_id, None)
                await self._async_send(previous.phone, "clear_notification", {"tag": tag})
            if prompt is None or (prompt == previous and error is None):
                return

            account = garmin_account(self.hass, prompt.account)
            prefix = f"{_ACTION_PREFIX}|{session_id}|{prompt.token}|"
            actions = []
            message = f"{session['moving_seconds'] / 60:.1f} min walking, {session['distance_m'] / 1000:.2f} km, {session['steps']} steps."
            if session["quality_notes"]:
                message += " " + " ".join(session["quality_notes"])
            if prompt.status == "needs_review":
                message += " Check Garmin before retrying from WalkingPad sessions."
            elif prompt.status == "blocked":
                message += " Upload approved. Waiting for the selected Garmin account or action."
            elif prompt.status == "queued":
                message += " Upload approved and queued."
            elif account:
                message += f" Upload to {account['name']}?"
                actions.append(_action(prefix + "upload", "Upload to Garmin"))
            else:
                message += " Choose a default Garmin account in the WalkingPad settings to upload from your phone."
            if error:
                message += f" {error}"
            actions.append(_action(prefix + "keep", "Keep locally"))
            self._prompts[session_id] = prompt
            path = "/walkingpad-sessions"
            if not await self._async_send(
                prompt.phone, message,
                {"tag": tag, "actions": actions, "url": path, "clickAction": path},
                title=f"WalkingPad session · {session['name']}",
            ):
                self._prompts.pop(session_id, None)

    async def _async_action(self, event) -> None:
        action = event.data.get("action")
        if event.origin is not EventOrigin.remote or not isinstance(action, str):
            return
        parts = action.split("|")
        if len(parts) != 4 or parts[0] != _ACTION_PREFIX or parts[3] not in {"upload", "keep"}:
            return
        _, session_id, token, choice = parts
        prompt = self._prompts.get(session_id)
        if prompt is None or prompt.token != token or session_id in self._deciding:
            return
        session = self.manager.get_summary(session_id)
        if session is None or prompt != self._prompt_for(session) or not self._from_phone(event, prompt.phone):
            return
        if choice == "upload" and prompt.status not in _APPROVABLE_STATUSES:
            return

        self._deciding.add(session_id)
        try:
            if choice == "keep":
                await self.manager.async_discard(session["device_id"], session_id)
            else:
                account = garmin_account(self.hass, prompt.account)
                if account is None:
                    raise SessionError("The selected Garmin account is unavailable.")
                await self.manager.async_upload(session["device_id"], session_id, account["entity_id"])
        except SessionError as err:
            await self._async_update(session_id, str(err))
        finally:
            self._deciding.discard(session_id)

    def _from_phone(self, event, phone: str) -> bool:
        device = dr.async_get(self.hass).async_get(phone)
        if device is None or event.context.user_id is None:
            return False
        return any(
            entry is not None and entry.domain == _MOBILE_APP
            and entry.data.get("user_id") == event.context.user_id
            for entry in (self.hass.config_entries.async_get_entry(entry_id) for entry_id in device.config_entries)
        )

    async def _async_send(self, phone: str, message: str, data: dict, *, title: str | None = None) -> bool:
        config = {"domain": _MOBILE_APP, "device_id": phone, "type": "notify", "message": message, "data": data}
        if title:
            config["title"] = title
        try:
            platform = await async_get_device_automation_platform(self.hass, _MOBILE_APP, DeviceAutomationType.ACTION)
            await platform.async_call_action_from_config(self.hass, config, {}, None)
        except (HomeAssistantError, InvalidDeviceAutomationConfig) as err:
            _LOGGER.warning("Could not send WalkingPad session notification: %s", err)
            return False
        return True


def _action(identifier: str, title: str) -> dict:
    return {"action": identifier, "title": title, "authenticationRequired": True}
