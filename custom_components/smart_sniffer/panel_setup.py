"""The SMART Sniffer panel in Home Assistant's sidebar (v0.9).

Registered once per Home Assistant run, from async_setup, and never removed:
an entry reload, the removal of the last entry or a remove then add can
never lose it (with every entry deleted the page shows the + card alone).
All of it sits in one guarded function: a failure is logged once and
monitoring goes on, because entry setup depends on nothing made here.

- Static files: /smart_sniffer_static/ is custom_components/smart_sniffer/
  panel/ and nothing else. Every URL carries ?v=<integration version>+<digest
  of the page's files>, so an update (or a changed file under the same
  version) is picked up after the restart without a stale cache.
- The panel: "SMART Sniffer" in the sidebar, admins only, a web component
  (panel.js) that frames the page (index.html) and hands it a small bridge
  to the user's own Home Assistant websocket.
- The websocket commands the page uses (panel_ws.py).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .panel_data import VERSION

_LOGGER = logging.getLogger(__name__)

STATIC_URL = "/smart_sniffer_static"
PANEL_URL_PATH = "smart-sniffer"
PANEL_TITLE = "SMART Sniffer"
PANEL_ICON = "mdi:harddisk"
PANEL_ELEMENT = "smart-sniffer-panel"
PANEL_DIR = Path(__file__).parent / "panel"

# hass.data key: the panel was set up (or tried) in this run.
PANEL_SETUP = "smart_sniffer_panel_setup"


async def _register_static(hass: HomeAssistant) -> None:
    """The panel's files, on Home Assistant 2024.4 and on 2024.7 and later
    (async_register_static_paths, which replaced register_static_path)."""
    try:
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415
    except ImportError:
        StaticPathConfig = None  # noqa: N806 - Home Assistant before 2024.7
    if StaticPathConfig is not None and hasattr(hass.http, "async_register_static_paths"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(PANEL_DIR), False)]
        )
    else:
        hass.http.register_static_path(STATIC_URL, str(PANEL_DIR), False)


ASSET_FILES = ("panel.js", "index.html", "cc.js", "cc.css", "alpine.min.js")


def asset_token() -> str:
    """The integration version plus a short digest of the page's files
    ("0.9.0-dev+1a2b3c4d"): a changed file is never served from a cache,
    even when the version stays the same (development builds)."""
    digest = hashlib.sha1(usedforsecurity=False)
    for name in ASSET_FILES:
        try:
            digest.update((PANEL_DIR / name).read_bytes())
        except OSError:
            digest.update(name.encode())
    return f"{VERSION}+{digest.hexdigest()[:8]}"


async def _register_panel(hass: HomeAssistant) -> None:
    from homeassistant.components import panel_custom  # noqa: PLC0415

    token = await hass.async_add_executor_job(asset_token)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{STATIC_URL}/panel.js?v={token}",
        embed_iframe=False,
        require_admin=True,
        config={"version": token, "static": STATIC_URL},
    )


async def async_setup_panel(hass: HomeAssistant) -> bool:
    """Static files, panel and websocket commands, once per run. True when
    the panel was set up this run (now or before)."""
    if PANEL_SETUP in hass.data:
        return bool(hass.data[PANEL_SETUP])
    hass.data[PANEL_SETUP] = False
    try:
        from . import panel_ws  # noqa: PLC0415

        panel_ws.async_register_info(hass)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("SMART Sniffer: smart_sniffer/info could not be registered")
    try:
        from . import panel_ws  # noqa: PLC0415

        panel_ws.async_register_commands(hass)
        await _register_static(hass)
        await _register_panel(hass)
    except Exception:  # noqa: BLE001 - the panel never stops monitoring
        _LOGGER.exception("SMART Sniffer panel could not be set up; monitoring continues")
        return False
    hass.data[PANEL_SETUP] = True
    return True
