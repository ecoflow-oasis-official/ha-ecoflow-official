"""A persistent STREAM system, fed by whichever member is currently master."""

import logging

from homeassistant import config_entries
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_SYSTEM_ID, DEFAULT_SYSTEM_ID, DOMAIN, PLATFORMS, SYSTEM_ENTRY, SYSTEM_NAME,
    DEVICE_GROUP_ID, SYSTEM_ENTITY_KEY, SYSTEM_NUMBER,
)
from .registers_schema import REGISTERS
from .system_naming import async_system_number, system_name

LOGGER = logging.getLogger(__name__)
STREAM_PRODUCTS = {"STREAM 5000", "STREAM AC 5000"}
RUNTIME_KEY = "_stream_systems"
MASTER_SEEN = "master_seen"
SYSTEM_ID_ASSIGNED = "system_id_assigned"
LEGACY_SYSTEM_ID = "legacy_system_id"


def _virtual_entries(hass):
    return [e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get(SYSTEM_ENTRY)]


def _rename_system(hass, entry, group, key):
    """Change the grouping address, preserving the entity/device identity."""
    number = async_system_number(hass, system_id(entry))
    hass.config_entries.async_update_entry(entry, data={
        **entry.data, CONF_SYSTEM_ID: group, DEVICE_GROUP_ID: int(group), SYSTEM_ENTITY_KEY: key,
        SYSTEM_NUMBER: number,
    }, unique_id=f"stream-system:{group}", title=system_name(number))


def async_bind_system(hass, member):
    """Bind only a live, device-confirmed UINT32 group; never allocate a new ID."""
    group_id = getattr(member, "_stream_group_id", None)
    old_system = getattr(member, "_stream_system", None)
    if (type(group_id) is not int or not 0 < group_id <= 0xFFFFFFFF
            or not member.last_update_success or member._reconnecting):
        if old_system is not None:
            old_system.async_detach(member.key)
            member._stream_system = None
        return None
    group = str(group_id)
    old_group = member.entry.data.get(LEGACY_SYSTEM_ID) or system_id(member.entry)
    entries = _virtual_entries(hass)
    canonical = next((e for e in entries if system_id(e) == group), None)
    legacy = next((e for e in entries if old_group is not None and not str(old_group).isdigit()
                   and system_id(e) == old_group
                   and DEVICE_GROUP_ID not in e.data), None)
    systems = hass.data.setdefault(DOMAIN, {}).setdefault(RUNTIME_KEY, {})
    if canonical is None and legacy is not None:
        # Rekey an existing virtual system in place so sensor unique IDs,
        # entity IDs, device ID, user settings and history remain untouched.
        key = legacy.data.get(SYSTEM_ENTITY_KEY, f"stream-system:{old_group}")
        _rename_system(hass, legacy, group, key)
        canonical = legacy
        previous = systems.pop(old_group, None)
        if previous is None:
            previous = StreamSystemCoordinator(hass, group)
        previous.group = group
        previous.number = legacy.data[SYSTEM_NUMBER]
        previous.device_info["name"] = system_name(previous.number)
        displaced = systems.get(group)
        systems[group] = previous
        if displaced is not None and displaced is not previous:
            for other in list(displaced.members.values()):
                displaced.async_detach(other.key)
                previous.async_attach(other)
    legacy_link = ({LEGACY_SYSTEM_ID: old_group}
                   if legacy is not None and canonical is not legacy else {})
    hass.config_entries.async_update_entry(member.entry, data={
        **member.entry.data, CONF_SYSTEM_ID: group, DEVICE_GROUP_ID: group_id,
        SYSTEM_ID_ASSIGNED: True, **legacy_link,
    })
    system = get_system(hass, group)
    if old_system is not None and old_system is not system:
        old_system.async_detach(member.key)
    newly_attached = system.members.get(member.key) is not member
    if newly_attached:
        system.async_attach(member)
    else:
        system.async_update_source()
    if canonical is not None:
        # This also covers migration before HA starts loading the virtual entry.
        if system.entry is None:
            system.entry = canonical
        if newly_attached:
            async_migrate_system_entities(hass, system)
    if legacy is not None and canonical is not legacy:
        system.async_merge_legacy_entry(legacy)
    return system


def system_id(entry):
    """Only an assigned group (or a pre-existing explicit group) is shared."""
    group = str(entry.data.get(CONF_SYSTEM_ID) or DEFAULT_SYSTEM_ID).strip() or DEFAULT_SYSTEM_ID
    if entry.data.get(SYSTEM_ENTRY) or entry.data.get(SYSTEM_ID_ASSIGNED) or group != DEFAULT_SYSTEM_ID:
        return group
    return None


def get_system(hass, group):
    systems = hass.data.setdefault(DOMAIN, {}).setdefault(RUNTIME_KEY, {})
    if group not in systems:
        systems[group] = StreamSystemCoordinator(hass, group)
    return systems[group]


class StreamSystemCoordinator(DataUpdateCoordinator):
    """Push-only entity coordinator; members own all TCP links and polling."""

    def __init__(self, hass, group):
        super().__init__(hass, LOGGER, config_entry=None, name=SYSTEM_NAME)
        stored = next((e for e in _virtual_entries(hass) if system_id(e) == group), None)
        self.key = (stored.data.get(SYSTEM_ENTITY_KEY, f"stream-system:{group}")
                    if stored is not None else f"stream-system:{group}")
        self.group = group
        self.number = async_system_number(hass, group) if stored is not None else 1
        self.entry = None
        self._entry_platforms_loaded = False
        self._entry_task = None
        self._discovery_requested = False
        self.members = {}
        self._unsub_members = {}
        self.active_master = None
        self._merge_tasks = {}
        self.data = {}
        self.last_update_success = False
        self.registers = [r for r in REGISTERS if r.get("region", "").startswith("S")
                          and STREAM_PRODUCTS.intersection(r.get("products", []))]
        self.device_info = {
            "identifiers": {(DOMAIN, self.key)},
            "name": system_name(self.number),
            "manufacturer": "EcoFlow",
            "model": "STREAM System",
        }

    def async_attach(self, member):
        """Keep polling even when the member has no enabled D entities."""
        self.async_detach(member.key)
        self.members[member.key] = member
        member._stream_system = self
        self._unsub_members[member.key] = member.async_add_listener(self.async_update_source)
        self.async_update_source()

    def async_detach(self, key):
        if unsub := self._unsub_members.pop(key, None):
            unsub()
        self.members.pop(key, None)
        self.async_update_source()

    def _select_master(self):
        candidates = [m for m in self.members.values()
                      if m.last_update_success and not m._reconnecting
                      and m._stream_role == 1 and type(m._stream_role) is int
                      and (not str(self.group).isdigit()
                           or getattr(m, "_stream_group_id", None) == int(self.group))]
        # Keep a working source during transient overlapping election flags.
        if self.active_master in candidates:
            return self.active_master
        return min(candidates, key=lambda m: m.key, default=None)

    def async_update_source(self):
        master = self._select_master()
        self.active_master = master
        if master is None:
            self.data = {}
            if self.last_update_success:
                self.async_set_update_error(UpdateFailed("No online STREAM system master"))
            self._schedule_entry_sync()
            return
        if self.entry is not None and not self.entry.data.get(MASTER_SEEN):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, MASTER_SEEN: True})
        if self.entry is not None:
            connection = {key: master.entry.data[key] for key in ("host", "port", "unit_id")
                          if key in master.entry.data}
            if any(self.entry.data.get(key) != value for key, value in connection.items()):
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**self.entry.data, **connection})
        # Never merge the old master's cache into the new master's readings.
        self.async_set_updated_data(dict(master._stream_system_values))
        self._schedule_entry_sync()

    def async_merge_legacy_entry(self, entry):
        """Queue duplicate cleanup once; the canonical system always survives."""
        if entry.entry_id not in self._merge_tasks:
            self._merge_tasks[entry.entry_id] = self.hass.async_create_task(
                self._async_merge_legacy_entry(entry))

    async def _async_merge_legacy_entry(self, entry):
        try:
            # Discovery/setup can still be in flight. Leave the old system
            # intact until a canonical entry exists and can own its entities.
            if self.entry is None:
                return
            old = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if old is not None and old._entry_platforms_loaded:
                if not await self.hass.config_entries.async_unload_platforms(entry, PLATFORMS):
                    return
                old._entry_platforms_loaded = False
            registry = er.async_get(self.hass)
            device = dr.async_get(self.hass).async_get_or_create(
                config_entry_id=self.entry.entry_id, **self.device_info)
            old_key = entry.data.get(SYSTEM_ENTITY_KEY, f"stream-system:{system_id(entry)}")
            moved = False
            for entity in list(registry.entities.values()):
                if entity.platform != DOMAIN or entity.config_entry_id != entry.entry_id:
                    continue
                prefix = f"{old_key}-"
                if not entity.unique_id.startswith(prefix):
                    continue
                new_id = f"{self.key}-{entity.unique_id[len(prefix):]}"
                if registry.async_get_entity_id(entity.domain, DOMAIN, new_id):
                    # The established canonical sensor remains authoritative.
                    registry.async_remove(entity.entity_id)
                else:
                    registry.async_update_entity(entity.entity_id, new_unique_id=new_id,
                        config_entry_id=self.entry.entry_id, device_id=device.id)
                    moved = True
            if old is not None:
                for key in list(old.members):
                    old.async_detach(key)
            await self.hass.config_entries.async_remove(entry.entry_id)
            for physical in self.hass.config_entries.async_entries(DOMAIN):
                if physical.data.get(LEGACY_SYSTEM_ID) == system_id(entry):
                    data = dict(physical.data)
                    data.pop(LEGACY_SYSTEM_ID)
                    self.hass.config_entries.async_update_entry(physical, data=data)
            if moved and self._entry_platforms_loaded:
                await self.hass.config_entries.async_reload(self.entry.entry_id)
        except Exception:
            LOGGER.exception("Could not merge legacy STREAM system %s", entry.entry_id)
        finally:
            self._merge_tasks.pop(entry.entry_id, None)

    def _only_confirmed_slaves(self):
        """Do not clean up during startup, outages or partial member loading."""
        entries = [e for e in self.hass.config_entries.async_entries(DOMAIN)
                   if not e.data.get(SYSTEM_ENTRY) and (
                       system_id(e) == self.group or (self.group == DEFAULT_SYSTEM_ID
                       and system_id(e) is None))
                   and e.data.get("product") in STREAM_PRODUCTS]
        if not entries:
            return False
        return all((member := self.members.get(e.entry_id)) is not None
                   and member.last_update_success and not member._reconnecting
                   and type(member._stream_role) is int and member._stream_role == 0
                   for e in entries)

    def _schedule_entry_sync(self):
        create = self.active_master is not None and (
            (self.entry is None and not self._discovery_requested)
            or (self.entry is not None and self._entry_platforms_loaded is False))
        cleanup = (self.entry is not None and not self.entry.data.get(MASTER_SEEN)
                   and self._only_confirmed_slaves())
        if (create or cleanup) and (self._entry_task is None or self._entry_task.done()):
            self._entry_task = self.hass.async_create_task(self._async_sync_entry())

    async def _async_sync_entry(self):
        """Create on first confirmed master; remove legacy slave-only entries."""
        try:
            if self.active_master is not None:
                if self.entry is None:
                    self._discovery_requested = True
                    await async_ensure_system_entry(self.hass, self.group)
                elif self._entry_platforms_loaded is False:
                    await self.hass.config_entries.async_reload(self.entry.entry_id)
            elif (self.entry is not None and not self.entry.data.get(MASTER_SEEN)
                  and self._only_confirmed_slaves()):
                await self.hass.config_entries.async_remove(self.entry.entry_id)
                self._discovery_requested = False
        except Exception:
            self._discovery_requested = False
            LOGGER.exception("Could not reconcile STREAM system entry for %s", self.group)

    async def async_write(self, offset, dtype, value, length=8):
        """Resolve the master at service-call time, for every control platform."""
        self.async_update_source()
        master = self.active_master
        if master is None:
            raise UpdateFailed("No online STREAM system master")
        if offset not in {r["offset"] for r in self.registers}:
            raise UpdateFailed("Not a STREAM system register")
        await master.async_write(offset, dtype, value, length)
        self.async_update_source()

    async def _async_update_data(self):
        self.async_update_source()
        if self.active_master is None:
            raise UpdateFailed("No online STREAM system master")
        return self.data


async def async_ensure_system_entry(hass, group):
    """The config flow deduplicates concurrent member discovery."""
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={CONF_SYSTEM_ID: group},
    )


def async_migrate_system_entities(hass, system):
    """Move one existing S entity per point, preserving its entity ID/history."""
    if system.entry is None or (
        system.active_master is None and not system.entry.data.get(MASTER_SEEN)
        and not _has_hardware_group(system.entry, system.group)
    ):
        return
    system.number = async_system_number(hass, system.group)
    system.device_info["name"] = system_name(system.number)
    title = getattr(system.entry, "title", "")
    suffix = title[len(SYSTEM_NAME):] if title.startswith(SYSTEM_NAME) else None
    if suffix is not None and (not suffix or suffix.strip().isdigit()
            or (suffix.startswith(" (") and suffix.endswith(")") and suffix[2:-1].isdigit())):
        hass.config_entries.async_update_entry(system.entry, title=system.device_info["name"])
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=system.entry.entry_id, **system.device_info)
    dr.async_get(hass).async_update_device(device.id, name=system.device_info["name"])
    registry = er.async_get(hass)
    sources = {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)
               if not e.data.get(SYSTEM_ENTRY) and system_id(e) == system.group
               and e.data.get("product") in STREAM_PRODUCTS}
    offsets = {r["offset"] for r in system.registers}
    # Prefer migration from the currently active master if duplicates exist.
    entities = sorted(list(registry.entities.values()), key=lambda e: (
        e.config_entry_id != getattr(getattr(system.active_master, "entry", None), "entry_id", None),
        e.entity_id,
    ))
    for entity in entities:
        if entity.platform != DOMAIN or entity.config_entry_id not in sources:
            continue
        prefix = f"{entity.config_entry_id}-"
        if not entity.unique_id.startswith(prefix):
            continue
        try:
            offset = int(entity.unique_id[len(prefix):], 16)
        except ValueError:
            continue
        if offset not in offsets:
            continue
        new_id = f"{system.key}-{offset:04X}"
        existing = registry.async_get_entity_id(entity.domain, DOMAIN, new_id)
        if existing:
            # A duplicate member's S entity must not stay on its physical device.
            registry.async_remove(entity.entity_id)
            continue
        changes = {"new_unique_id": new_id, "config_entry_id": system.entry.entry_id,
                   "device_id": device.id}
        if entity.hidden_by is er.RegistryEntryHider.INTEGRATION:
            changes["hidden_by"] = None
        registry.async_update_entity(entity.entity_id, **changes)
    _rename_default_entity_ids(hass, system)


def _has_hardware_group(entry, group):
    value = entry.data.get(DEVICE_GROUP_ID)
    return type(value) is int and 0 < value <= 0xFFFFFFFF and str(value) == group


def _rename_default_entity_ids(hass, system):
    """Remove the exact old group prefix; keep custom IDs and stable unique IDs."""
    if not str(system.group).isdigit():
        return
    registry = er.async_get(hass)
    old_prefix = f"ecoflow_stream_system_{system.group}_"
    new_prefix = "ecoflow_stream_system" + (f"_{system.number}" if system.number > 1 else "") + "_"
    for entity in list(registry.entities.values()):
        if (entity.platform != DOMAIN or entity.config_entry_id != system.entry.entry_id
                or not entity.unique_id.startswith(f"{system.key}-")):
            continue
        domain, _, object_id = entity.entity_id.partition(".")
        if not object_id.startswith(old_prefix):
            continue
        new_id = f"{domain}.{new_prefix}{object_id[len(old_prefix):]}"
        if new_id == entity.entity_id:
            continue
        if new_id in registry.entities:
            LOGGER.warning("Cannot rename %s to occupied entity ID %s", entity.entity_id, new_id)
            continue
        registry.async_update_entity(entity.entity_id, new_entity_id=new_id)


async def async_setup_system_entry(hass, entry):
    system = get_system(hass, system_id(entry))
    system.entry = entry
    system._entry_platforms_loaded = None  # Setup in progress, not a reload request.
    system.async_update_source()
    hass.data[DOMAIN][entry.entry_id] = system
    has_hardware_group = _has_hardware_group(entry, system.group)
    if (system.active_master is None and not entry.data.get(MASTER_SEEN)
            and not has_hardware_group):
        # Legacy versions created this even for slave-only groups. Wait for
        # actual members before deciding whether to remove or activate it.
        system._entry_platforms_loaded = False
        return True
    async_migrate_system_entities(hass, system)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        system._entry_platforms_loaded = False
        raise
    system._entry_platforms_loaded = True
    return True
