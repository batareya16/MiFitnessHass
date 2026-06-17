"""Mi Fitness Home Assistant integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import MiFitnessApiError, MiFitnessClient
from .const import (
    CONF_C_USER_ID,
    CONF_PHONE_ID,
    CONF_REGION,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    DEFAULT_REGION,
    DOMAIN,
)
from .coordinator import MiFitnessCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

# Lovelace cards bundled with the integration. Auto-registered as frontend
# resources so users don't have to add them manually.
_CARDS = (
    "mi-fitness-activity-card.js",
    "mi-fitness-activity-streak-card.js",
    "mi-fitness-sleep-card.js",
    "mi-fitness-sleep-streak-card.js",
)
_CARDS_URL_BASE = f"/{DOMAIN}/cards"


async def _async_register_cards(hass: HomeAssistant) -> None:
    """Serve and auto-load the bundled Lovelace cards (once per HA run)."""
    flag = f"{DOMAIN}_cards_registered"
    if hass.data.get(flag):
        return

    www_dir = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_CARDS_URL_BASE, str(www_dir), False)]
    )
    for card in _CARDS:
        # cache-bust on version bump so browsers pick up new card code
        add_extra_js_url(hass, f"{_CARDS_URL_BASE}/{card}")

    hass.data[flag] = True
    _LOGGER.debug("Mi Fitness: registered %d Lovelace cards", len(_CARDS))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mi Fitness from a config entry."""
    await _async_register_cards(hass)

    data = entry.data

    client = MiFitnessClient(
        user_id=data[CONF_USER_ID],
        c_user_id=data[CONF_C_USER_ID],
        ssecurity=data[CONF_SSECURITY],
        service_token=data[CONF_SERVICE_TOKEN],
        region=data.get(CONF_REGION, DEFAULT_REGION),
        phone_id=data.get(CONF_PHONE_ID, ""),
    )

    coordinator = MiFitnessCoordinator(hass, client, entry)

    # Load stored watermark before first refresh
    await coordinator.async_load_stored_state()

    try:
        await coordinator.async_config_entry_first_refresh()
    except MiFitnessApiError as exc:
        raise ConfigEntryNotReady(f"API not available: {exc}") from exc

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
