"""Use the validation library used by the running Home Assistant release."""

from homeassistant.helpers import config_validation as cv

if type(cv.PLATFORM_SCHEMA).__module__.startswith("probatio"):
    import probatio as vol
else:
    import voluptuous as vol

__all__ = ["vol"]
