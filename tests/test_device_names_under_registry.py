"""Device names in the real device and entity registries (GH #50, v0.8.4).

Runs Home Assistant's own registries in a temporary config directory: the
name sync, the update listener, entity ids across a rename, shared drives,
and setup when the sync fails. Skipped without Home Assistant. Run it under
the declared floor (2024.4.0) and a current release: from 2026.8 a drive
registered by two entries is two devices, before it is one.

Host names, addresses and serials are invented.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant import config_entries  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402

DOMAIN = "smart_sniffer"
naming = importlib.import_module("custom_components.smart_sniffer.device_naming")
coordinator_mod = importlib.import_module("custom_components.smart_sniffer.coordinator")

_NEW_ENTITY_IDS = "object_id_base" in er.EntityRegistry.async_get_or_create.__code__.co_varnames


def _package_init():
    """The package's __init__.py, run under the package's name for its
    relative imports but not put in sys.modules (conftest keeps a bare
    package there)."""
    path = Path(naming.__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location("custom_components.smart_sniffer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_entry(entry_id, title, data, source="user"):
    params = config_entries.ConfigEntry.__init__.__code__.co_varnames
    kwargs = dict(
        data=data, domain=DOMAIN, entry_id=entry_id, minor_version=1, options={},
        source=source, title=title, unique_id=entry_id, version=1,
        discovery_keys={}, subentries_data=None,
    )
    return config_entries.ConfigEntry(**{k: v for k, v in kwargs.items() if k in params})


async def _new_hass(config_dir):
    from homeassistant import loader
    from homeassistant.helpers import area_registry, entity, frame

    hass = HomeAssistant(str(config_dir))
    for module in (loader, entity, frame, dr):
        if hasattr(module, "async_setup"):
            module.async_setup(hass)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await area_registry.async_load(hass)
    for name in ("category_registry", "floor_registry", "label_registry"):
        try:
            module = importlib.import_module(f"homeassistant.helpers.{name}")
        except ImportError:
            continue
        await module.async_load(hass)
    await dr.async_load(hass)
    await er.async_load(hass)
    hass.updates = []
    hass.bus.async_listen(
        dr.EVENT_DEVICE_REGISTRY_UPDATED,
        lambda event: hass.updates.append(event.data)
        if event.data.get("action") == "update" else None,
    )
    return hass


def _run(tmp_path, body):
    async def main():
        hass = await _new_hass(tmp_path)
        try:
            await body(hass)
        finally:
            await hass.async_stop(force=True)
    asyncio.run(main())


def _add(hass, entry_id, title, source="user", **data):
    entry = _make_entry(entry_id, title, {"host": "192.168.1.20", "port": 9099, **data}, source)
    hass.config_entries._entries[entry_id] = entry
    return entry


def _device(hass, entry, ident, name, model=None, serial=None):
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, ident)},
        name=name, model=model, serial_number=serial,
    )


def _entity(hass, entry, device_id, unique_id, name):
    """Register an entity the way the entity platform does: the entity id
    from the device's name and the entity's own (has_entity_name)."""
    kwargs = dict(config_entry=entry, device_id=device_id, has_entity_name=True,
                  original_name=name)
    if _NEW_ENTITY_IDS:
        kwargs["object_id_base"] = name
    else:
        device = dr.async_get(hass).async_get(device_id)
        kwargs["suggested_object_id"] = f"{device.name_by_user or device.name} {name}"
    return er.async_get(hass).async_get_or_create("sensor", DOMAIN, unique_id, **kwargs).entity_id


def _name(hass, device_id):
    return dr.async_get(hass).async_get(device_id).name


def _all_devices(hass):
    """Every device. The registry's devices iterate as ids before 2026.10
    and as entries from then on (and are no longer a mapping)."""
    registry = dr.async_get(hass)
    devices = list(registry.devices)
    if devices and isinstance(devices[0], str):
        devices = [registry.async_get(device_id) for device_id in devices]
    return devices


def _devices_with(hass, ident):
    return [d for d in _all_devices(hass) if (DOMAIN, ident) in d.identifiers]


async def _settled(hass):
    await hass.async_block_till_done()
    count = len(hass.updates)
    hass.updates.clear()
    return count


# --- the sync ----------------------------------------------------------------------------


def test_sync_renames_a_drive_once(tmp_path):
    async def body(hass):
        entry = _add(hass, "E1", "SMART Sniffer (192.168.1.20:9099)", agent_hostname="nas1")
        drive = _device(hass, entry, "S1", "Samsung (S1)", "Samsung", "S1")
        await _settled(hass)
        naming.async_sync_names(hass, entry)
        assert _name(hass, drive.id) == "nas1 Samsung (S1)"
        assert await _settled(hass) == 1
        naming.async_sync_names(hass, entry)
        assert await _settled(hass) == 0
        assert hass.data[DOMAIN]["E1"][naming.NAMED_HOST] == "nas1"
    _run(tmp_path, body)


def test_user_names_and_entity_ids_are_kept(tmp_path):
    async def body(hass):
        entry = _add(hass, "E1", "SMART Sniffer (192.168.1.20:9099)", agent_hostname="nas1")
        drive = _device(hass, entry, "S1", "Samsung (S1)", "Samsung", "S1")
        mine = _device(hass, entry, "S2", "Samsung (S2)", "Samsung", "S2")
        dr.async_get(hass).async_update_device(mine.id, name_by_user="Backup disk")
        old_id = _entity(hass, entry, drive.id, "E1_S1_temperature", "Temperature")
        assert old_id == "sensor.samsung_s1_temperature"
        naming.async_sync_names(hass, entry)
        device = dr.async_get(hass).async_get(mine.id)
        assert device.name_by_user == "Backup disk" and device.name == "nas1 Samsung (S2)"
        # The existing entity keeps its id; a new one takes the host.
        assert _entity(hass, entry, drive.id, "E1_S1_temperature", "Temperature") == old_id
        assert _entity(hass, entry, drive.id, "E1_S1_hours", "Power On Hours") == (
            "sensor.nas1_samsung_s1_power_on_hours"
        )
    _run(tmp_path, body)


def test_v081_agent_pool_and_disk_usage_devices_are_renamed(tmp_path):
    async def body(hass):
        entry = _add(hass, "E1", "SMART Sniffer (192.168.1.20:9099)", agent_hostname="pve-nas")
        agent = _device(hass, entry, "E1_agent", "SMART Sniffer (192.168.1.20:9099)", "Agent")
        pool = _device(hass, entry, "E1_zpool_rpool", "ZFS pool rpool (192.168.1.20:9099)", "ZFS Pool")
        usage = _device(hass, entry, "E1_filesystems", "Disk Usage (192.168.1.20:9099)",
                        "Filesystem Monitor")
        naming.async_sync_names(hass, entry)
        assert _name(hass, agent.id) == "SMART Sniffer (pve-nas)"
        assert _name(hass, pool.id) == "pve-nas ZFS pool rpool"
        assert _name(hass, usage.id) == "pve-nas Disk Usage"
    _run(tmp_path, body)


def test_ip_fallback_names_and_a_custom_title(tmp_path):
    async def body(hass):
        entry = _add(hass, "E1", "Basement NAS")
        agent = _device(hass, entry, "E1_agent", "Basement NAS", "Agent")
        drive = _device(hass, entry, "S1", "Samsung (S1)", "Samsung", "S1")
        pool = _device(hass, entry, "E1_zpool_tank", "ZFS pool tank (192.168.1.20)", "ZFS Pool")
        naming.async_sync_names(hass, entry)
        assert _name(hass, agent.id) == "Basement NAS"
        assert _name(hass, drive.id) == "192.168.1.20 Samsung (S1)"
        assert _name(hass, pool.id) == "192.168.1.20 ZFS pool tank"
    _run(tmp_path, body)


def test_app_and_replica_entries(tmp_path):
    app = "0449a086-smart-sniffer-agent"

    async def body(hass):
        real = _add(hass, "E1", f"SMART Sniffer ({app}:9099)", host=app, agent_mock=False)
        replica = _add(hass, "E2", f"SMART Sniffer ({app}:9100)", host=app, port=9100,
                       agent_mock=True)
        devices = [
            (_device(hass, real, "E1_agent", real.title, "Agent"), "SMART Sniffer (HAOS)"),
            (_device(hass, real, "SER-H1", "WDC X (SER-H1)", "WDC X", "SER-H1"), "HAOS WDC X (SER-H1)"),
            (_device(hass, replica, "E2_agent", replica.title, "Agent"), "SMART Sniffer (Replica)"),
            (_device(hass, replica, "MOCK-R1", "WDC Y (MOCK-R1)", "WDC Y", "MOCK-R1"),
             "Replica WDC Y (MOCK-R1)"),
        ]
        naming.async_sync_names(hass, real)
        naming.async_sync_names(hass, replica)
        assert [_name(hass, d.id) for d, _ in devices] == [name for _, name in devices]
    _run(tmp_path, body)


def test_discovered_older_agents_keep_their_discovery_name(tmp_path):
    """Owner decision after the VM999 bench: a discovered agent before
    v0.8.3 keeps the name discovery found, in its case; one added by address
    is named by the address."""
    async def body(hass):
        zima = _add(hass, "E1", "SMART Sniffer (ZimaOS)", source="zeroconf", host="10.0.0.10")
        typed = _add(hass, "E2", "SMART Sniffer (10.0.10.210:9099)", host="10.0.10.210")
        devices = [
            (_device(hass, zima, "E1_agent", zima.title, "Agent"), "SMART Sniffer (ZimaOS)"),
            (_device(hass, zima, "S1", "Samsung (S1)", "Samsung", "S1"), "ZimaOS Samsung (S1)"),
            (_device(hass, typed, "E2_agent", typed.title, "Agent"), "SMART Sniffer (10.0.10.210)"),
            (_device(hass, typed, "S2", "Samsung (S2)", "Samsung", "S2"), "10.0.10.210 Samsung (S2)"),
        ]
        naming.async_sync_names(hass, zima)
        naming.async_sync_names(hass, typed)
        assert [_name(hass, d.id) for d, _ in devices] == [name for _, name in devices]
    _run(tmp_path, body)


def test_a_drive_without_a_serial_is_named_the_same_by_both_writers(tmp_path):
    """VM999: the device info built "QEMU HARDDISK ()" and the sync
    "QEMU HARDDISK (dev-sda)", so every setup wrote the name twice."""
    async def body(hass):
        entry = _add(hass, "E1", "SMART Sniffer (nas1)", agent_hostname="nas1")
        info = naming.drive_device_info(SimpleNamespace(hass=hass, config_entry=entry), "dev-sda",
                                        {"model": "QEMU HARDDISK", "serial": ""}, "QEMU")
        assert info["name"] == "nas1 QEMU HARDDISK (dev-sda)"
        info.pop("via_device", None)
        info.pop("via_device_id", None)
        dr.async_get(hass).async_get_or_create(config_entry_id="E1", **info)
        await _settled(hass)
        naming.async_sync_names(hass, entry)
        assert await _settled(hass) == 0
    _run(tmp_path, body)


# --- the update listener -------------------------------------------------------------------


def _set_up_for_listener(hass, init, entry):
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {init._SETTINGS: init._settings(entry)}
    naming.async_sync_names(hass, entry)


def test_the_listener_renames_on_a_host_change_only(tmp_path):
    init = _package_init()

    async def body(hass):
        entry = _add(hass, "E1", "SMART Sniffer (192.168.1.20:9099)", agent_hostname="nas1")
        drive = _device(hass, entry, "S1", "Samsung (S1)", "Samsung", "S1")
        _set_up_for_listener(hass, init, entry)
        await _settled(hass)
        # A digest change: nothing to rename, nothing written.
        hass.config_entries.async_update_entry(entry, data={**entry.data, "agent_drives": "d" * 32})
        await init._async_update_listener(hass, entry)
        assert await _settled(hass) == 0
        # A host rename the agent_id confirmed.
        hass.config_entries.async_update_entry(entry, data={**entry.data, "agent_hostname": "nas1b"})
        await init._async_update_listener(hass, entry)
        assert _name(hass, drive.id) == "nas1b Samsung (S1)"
        assert await _settled(hass) == 1
    _run(tmp_path, body)


def test_the_reported_case_replaces_a_lower_case_name(tmp_path):
    """v0.8.3 stored host names in lower case. The next poll stores the case
    the agent reports and the listener renames; entity ids stay lower case."""
    init = _package_init()

    async def body(hass):
        entry = _add(hass, "E1", "SMART Sniffer (LAPTOP-2ONH2TAS)", source="zeroconf",
                     agent_hostname="laptop-2onh2tas")
        drive = _device(hass, entry, "S1", "Samsung (S1)", "Samsung", "S1")
        _set_up_for_listener(hass, init, entry)
        assert _name(hass, drive.id) == "laptop-2onh2tas Samsung (S1)"
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "agent_hostname": "LAPTOP-2ONH2TAS"}
        )
        await init._async_update_listener(hass, entry)
        assert _name(hass, drive.id) == "LAPTOP-2ONH2TAS Samsung (S1)"
        assert _entity(hass, entry, drive.id, "E1_S1_hours", "Power On Hours") == (
            "sensor.laptop_2onh2tas_samsung_s1_power_on_hours"
        )
    _run(tmp_path, body)


def test_address_to_host_name_keeps_entity_ids(tmp_path):
    """An agent before v0.8.3, then updated: names switch from the address
    to the host name without a reload; ids made meanwhile keep the address."""
    init = _package_init()

    async def body(hass):
        entry = _add(hass, "E1", "SMART Sniffer (192.168.1.20:9099)")
        drive = _device(hass, entry, "S1", "192.168.1.20 Samsung (S1)", "Samsung", "S1")
        old_id = _entity(hass, entry, drive.id, "E1_S1_temperature", "Temperature")
        assert old_id == "sensor.192_168_1_20_samsung_s1_temperature"
        _set_up_for_listener(hass, init, entry)
        assert await _settled(hass) == 0  # already named for the address
        hass.config_entries.async_update_entry(entry, data={**entry.data, "agent_hostname": "nas1"})
        await init._async_update_listener(hass, entry)
        assert _name(hass, drive.id) == "nas1 Samsung (S1)"
        assert er.async_get(hass).async_get(old_id) is not None
        assert _entity(hass, entry, drive.id, "E1_S1_temperature", "Temperature") == old_id
        assert _entity(hass, entry, drive.id, "E1_S1_hours", "Power On Hours") == (
            "sensor.nas1_samsung_s1_power_on_hours"
        )
    _run(tmp_path, body)


# --- a drive two entries report ------------------------------------------------------------


def _coordinator(hass, entry):
    return SimpleNamespace(hass=hass, config_entry=entry)


def test_a_shared_drive_is_named_the_same_from_either_entry(tmp_path):
    async def body(hass):
        e1 = _add(hass, "E1", "SMART Sniffer (nas1)", agent_hostname="nas1")
        e2 = _add(hass, "E2", "SMART Sniffer (nas0)", host="192.168.1.21", agent_hostname="nas0")
        for entry in (e1, e2):
            _device(hass, entry, "S1", "Samsung (S1)", "Samsung", "S1")
        devices = _devices_with(hass, "S1")
        if naming.PER_ENTRY_DEVICES:
            # Each entry owns its own device, named for its own host.
            assert len(devices) == 2
            for order in ((e1, e2), (e2, e1)):
                for entry in order:
                    naming.async_sync_names(hass, entry)
            names = sorted(d.name for d in _devices_with(hass, "S1"))
            assert names == ["nas0 Samsung (S1)", "nas1 Samsung (S1)"]
        else:
            # One device: the least (host, entry id) of its entries, from
            # every writer, whichever runs first and without any poll data.
            assert len(devices) == 1
            for order in ((e1, e2), (e2, e1)):
                for entry in order:
                    naming.async_sync_names(hass, entry)
                    assert devices[0].id and _name(hass, devices[0].id) == "nas0 Samsung (S1)"
            for entry in (e1, e2):
                info = naming.drive_device_info(_coordinator(hass, entry), "S1",
                                                {"model": "Samsung", "serial": "S1"}, "Samsung")
                assert info["name"] == "nas0 Samsung (S1)"
        await _settled(hass)
        for entry in (e2, e1, e2):
            naming.async_sync_names(hass, entry)
        assert await _settled(hass) == 0  # no flapping
    _run(tmp_path, body)


# --- setup -----------------------------------------------------------------------------


class _Coordinator:
    def __init__(self, hass, entry, *args):
        self.hass, self.config_entry, self.data = hass, entry, {}

    async def async_config_entry_first_refresh(self):
        return None


def _setup(tmp_path, monkeypatch, entry_data, sync_fails):
    init = _package_init()
    monkeypatch.setattr(init, "SmartSnifferCoordinator", _Coordinator)
    monkeypatch.setattr(init, "AgentHealthCoordinator", _Coordinator)
    if sync_fails:
        def boom(hass, entry):
            raise RuntimeError("registry exploded")
        monkeypatch.setattr(naming, "_sync_names", boom)
    result = {}

    async def body(hass):
        async def no_platforms(entry, platforms):
            return None
        hass.config_entries.async_forward_entry_setups = no_platforms
        entry = _make_entry("E1", "SMART Sniffer (nas1)", entry_data)
        hass.config_entries._entries["E1"] = entry
        _device(hass, entry, "S1", "Samsung (S1)", "Samsung", "S1")
        result["ok"] = await init.async_setup_entry(hass, entry)
        result["names"] = sorted(d.name for d in _all_devices(hass))
    _run(tmp_path, body)
    return result


def test_setup_succeeds_when_the_sync_raises(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    result = _setup(tmp_path, monkeypatch, {"host": "192.168.1.20", "port": 9099}, True)
    assert result["ok"] is True
    warnings = [r for r in caplog.records if "could not update the device names" in r.getMessage()]
    assert len(warnings) == 1


def test_setup_names_devices_and_survives_missing_data(tmp_path, monkeypatch):
    result = _setup(tmp_path, monkeypatch, {"host": "192.168.1.20", "agent_hostname": "nas1"}, False)
    assert result["ok"] is True
    assert result["names"] == ["SMART Sniffer (nas1)", "nas1 Samsung (S1)"]
    # No address and no host name at all: setup still succeeds.
    (tmp_path / "bare").mkdir()
    result = _setup(tmp_path / "bare", monkeypatch, {}, False)
    assert result["ok"] is True
    assert "Samsung (S1)" in result["names"]


# --- the entries a device belongs to (2026.10 deprecates config_entries) ---------


def test_device_entry_ids_reads_the_registry_without_the_deprecation(tmp_path, caplog):
    """The panel's live updates ask which of our entries a changed device
    belongs to. From 2026.10 DeviceEntry.config_entries logs a deprecation
    (it breaks in 2027.10); the helper reads config_entry_id where the core
    has it and config_entries before that, with the same answer."""
    device_link = importlib.import_module("custom_components.smart_sniffer.device_link")

    async def body(hass):
        entry = _add(hass, "e1", "nas1")
        device = _device(hass, entry, "S1", "nas1 Drive")
        with caplog.at_level(logging.WARNING):
            assert device_link.device_entry_ids(device) == {"e1"}
        assert "config_entries" not in caplog.text

    _run(tmp_path, body)
