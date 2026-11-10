"""A persistent STREAM system, fed by whichever member is currently master."""

import logging

from homeassistant import config_entries
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_SYSTEM_ID, DEFAULT_SYSTEM_ID, DOMAIN, PLATFORMS, SYSTEM_ENTRY, SYSTEM_NAME,
)
from .registers_schema import REGISTERS

LOGGER = logging.getLogger(__name__)
STREAM_PRODUCTS = {"STREAM 5000", "STREAM AC 5000"}
RUNTIME_KEY = "_stream_systems"
MASTER_SEEN = "master_seen"
SYSTEM_ID_ASSIGNED = "system_id_assigned"


def system_id(entry):
    """Only an assigned group (or a pre-existing explicit group) is shared."""
    group = str(entry.data.get(CONF_SYSTEM_ID) or DEFAULT_SYSTEM_ID).strip() or DEFAULT_SYSTEM_ID
    if entry.data.get(SYSTEM_ENTRY) or entry.data.get(SYSTEM_ID_ASSIGNED) or group != DEFAULT_SYSTEM_ID:
        return group
    return None


def async_assign_temporary_system_id(hass, member):
    """Temporary firmware workaround: each newly confirmed master gets a group."""
    entry = member.entry
    if (group := system_id(entry)) is not None:
        return group
    if (type(member._stream_role) is not int or member._stream_role != 1
            or not member.last_update_success or member._reconnecting):
        return None
    entries = list(hass.config_entries.async_entries(DOMAIN))
    group = f"temporary:{entry.entry_id}"
    # Keep the old default system's identity/history on its oldest physical
    # entry. A newly added master must never claim that established system.
    legacy = next((e for e in entries if e.data.get(SYSTEM_ENTRY)
                   and system_id(e) == DEFAULT_SYSTEM_ID), None)
    claimed = any(not e.data.get(SYSTEM_ENTRY) and e.data.get(SYSTEM_ID_ASSIGNED)
                  and system_id(e) == DEFAULT_SYSTEM_ID for e in entries)
    candidates = [e for e in entries if not e.data.get(SYSTEM_ENTRY)
                  and e.data.get("product") in STREAM_PRODUCTS
                  and str(e.data.get(CONF_SYSTEM_ID) or DEFAULT_SYSTEM_ID).strip() == DEFAULT_SYSTEM_ID]
    oldest = min(candidates, key=lambda e: (str(getattr(e, "created_at", "")), e.entry_id), default=None)
    predates_legacy = (legacy is not None and (
        not getattr(legacy, "created_at", None) or not getattr(entry, "created_at", None)
        or str(entry.created_at) <= str(legacy.created_at)))
    if predates_legacy and not claimed and oldest is entry:
        group = DEFAULT_SYSTEM_ID
    hass.config_entries.async_update_entry(entry, data={
        **entry.data, CONF_SYSTEM_ID: group, SYSTEM_ID_ASSIGNED: True})
    return group


def async_bind_temporary_system(hass, member):
    """Attach once, including when an initially unassigned slave becomes master."""
    if getattr(member, "_stream_system", None) is not None:
        return member._stream_system
    group = async_assign_temporary_system_id(hass, member)
    if group is None:
        return None
    system = get_system(hass, group)
    system.async_attach(member)
    async_migrate_system_entities(hass, system)
    return system


def get_system(hass, group):
    systems = hass.data.setdefault(DOMAIN, {}).setdefault(RUNTIME_KEY, {})
    if group not in systems:
        systems[group] = StreamSystemCoordinator(hass, group)
    return systems[group]


class StreamSystemCoordinator(DataUpdateCoordinator):
    """Push-only entity coordinator; members own all TCP links and polling."""

    def __init__(self, hass, group):
        super().__init__(hass, LOGGER, config_entry=None, name=SYSTEM_NAME)
        self.key = f"stream-system:{group}"
        self.group = group
        self.entry = None
        self._entry_platforms_loaded = False
        self._entry_task = None
        self._discovery_requested = False
        self.members = {}
        self._unsub_members = {}
        self.active_master = None
        self.data = {}
        self.last_update_success = False
        self.registers = [r for r in REGISTERS if r.get("region", "").startswith("S")
                          and STREAM_PRODUCTS.intersection(r.get("products", []))]
        self.device_info = {
            "identifiers": {(DOMAIN, self.key)},
            "name": SYSTEM_NAME,
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
                      and m._stream_role == 1 and type(m._stream_role) is int]
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
        # Never merge the old master's cache into the new master's readings.
        self.async_set_updated_data(dict(master._stream_system_values))
        self._schedule_entry_sync()

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
    ):
        return
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=system.entry.entry_id, **system.device_info)
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


async def async_setup_system_entry(hass, entry):
    system = get_system(hass, system_id(entry))
    system.entry = entry
    system._entry_platforms_loaded = None  # Setup in progress, not a reload request.
    system.async_update_source()
    hass.data[DOMAIN][entry.entry_id] = system
    if system.active_master is None and not entry.data.get(MASTER_SEEN):
        # Legacy versions created this even for slave-only groups. Wait for
        # actual members before deciding whether to remove or activate it.
        system._entry_platforms_loaded = False
        return True
    async_migrate_system_entities(hass, system)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    system._entry_platforms_loaded = True
    return True
