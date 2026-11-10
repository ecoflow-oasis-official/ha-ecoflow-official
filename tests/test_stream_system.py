"""Virtual system identity, master failover, controls and registry migration."""

from enum import Enum
import ast
import asyncio
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import test_device_identity as identity
from test_device_identity import load_methods
from test_device_identity import COMPONENT


class FakeCoordinator:
    def __init__(self, hass, *args, **kwargs):
        self.hass = hass
        self.data = {}
        self.last_update_success = True
        self.listeners = []

    def async_set_updated_data(self, data):
        self.data = data
        self.last_update_success = True
        for listener in list(self.listeners):
            listener()

    def async_set_update_error(self, error):
        self.last_update_success = False
        for listener in list(self.listeners):
            listener()

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class Hider(Enum):
    INTEGRATION = "integration"
    USER = "user"


class StreamSystemTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entries = []
        self.entities = {}
        self.registry = SimpleNamespace(entities=self.entities,
                                        async_get_entity_id=self.find_entity,
                                        async_update_entity=self.update_entity,
                                        async_remove=lambda key: self.entities.pop(key))
        self.devices = SimpleNamespace(async_get_or_create=Mock(
            return_value=SimpleNamespace(id="stable-system-device")))
        self.hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(
            async_entries=lambda domain: self.entries,
            async_forward_entry_setups=AsyncMock(), flow=SimpleNamespace(async_init=AsyncMock())))
        self.tasks = []
        def create_task(coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task
        self.hass.async_create_task = create_task
        self.hass.config_entries.async_update_entry = lambda e, **kw: [
            setattr(e, key, value) for key, value in kw.items()]
        self.hass.config_entries.async_reload = AsyncMock()
        self.hass.config_entries.async_remove = AsyncMock()
        ns = load_methods("const.py", {})
        load_methods("registers_schema.py", ns)
        ns.update(logging=logging, DataUpdateCoordinator=FakeCoordinator,
                  config_entries=SimpleNamespace(SOURCE_INTEGRATION_DISCOVERY="integration_discovery"),
                  dr=SimpleNamespace(async_get=lambda hass: self.devices),
                  er=SimpleNamespace(async_get=lambda hass: self.registry, RegistryEntryHider=Hider))
        self.ns = load_methods("system.py", ns)
        self.system = self.ns["get_system"](self.hass, "default")

    async def asyncTearDown(self):
        if self.tasks:
            await asyncio.gather(*self.tasks)

    def find_entity(self, domain, platform, unique_id):
        return next((e.entity_id for e in self.entities.values()
                     if (e.domain, e.platform, e.unique_id) == (domain, platform, unique_id)), None)

    def update_entity(self, key, **changes):
        entity = self.entities[key]
        for attr, value in changes.items():
            setattr(entity, "unique_id" if attr == "new_unique_id" else attr, value)

    def member(self, key, role, values=None):
        member = FakeCoordinator(self.hass)
        member.key = key
        member.entry = SimpleNamespace(entry_id=key, data={"product": "STREAM 5000", "system_id_assigned": True})
        self.entries.append(member.entry)
        member.data = {0x1001: role}
        member._stream_role = role
        member._stream_system_values = values or {}
        member._reconnecting = False
        member.async_write = AsyncMock()
        self.system.async_attach(member)
        return member

    def role(self, member, role, values=None):
        member.data = {0x1001: role}
        member._stream_role = role
        member._stream_system_values = values or {}
        for listener in list(member.listeners):
            listener()

    def old_entity(self, entry, offset, hidden=None, domain="sensor"):
        entity = SimpleNamespace(
            entity_id=f"{domain}.{entry}_{offset}", domain=domain,
            platform=self.ns["DOMAIN"], config_entry_id=entry,
            unique_id=f"{entry}-{offset:04X}", hidden_by=hidden,
            device_id=f"physical-{entry}", name="My custom name", options={"precision": 1})
        self.entities[entity.entity_id] = entity
        return entity

    def virtual_entry(self):
        entry = SimpleNamespace(entry_id="virtual", data={"stream_system": True, "system_id": "default"})
        self.entries.append(entry)
        self.system.entry = entry
        self.system._entry_platforms_loaded = True
        return entry

    async def test_switching_master_preserves_system_identity_and_drops_old_cache(self):
        first = self.member("a", 1, {0x020E: 90, 0x0208: 100})
        second = self.member("b", 0)
        key, info = self.system.key, self.system.device_info
        self.assertEqual(info["name"], "EcoFLow STREAM System")
        self.assertNotIn("serial_number", info)
        self.role(first, 0)
        self.assertFalse(self.system.last_update_success)
        self.role(second, 1, {0x020E: 40})
        self.assertIs(self.system.active_master, second)
        self.assertEqual(self.system.data, {0x020E: 40})
        self.assertEqual(self.system.key, key)
        self.assertIs(self.system.device_info, info)

    async def test_slave_only_does_not_discover_or_create_virtual_device(self):
        self.member("slave-a", 0)
        self.member("slave-b", 0)
        self.system.async_update_source()
        self.assertEqual(self.tasks, [])
        self.hass.config_entries.flow.async_init.assert_not_awaited()
        self.devices.async_get_or_create.assert_not_called()
        self.assertIsNone(self.system.entry)

    async def test_slave_becoming_master_creates_system_once(self):
        member = self.member("a", 0)
        self.role(member, 1, {0x020E: 50})
        self.system.async_update_source()
        await asyncio.gather(*self.tasks)
        self.system.async_update_source()
        self.hass.config_entries.flow.async_init.assert_awaited_once()
        self.assertEqual(self.system.data[0x020E], 50)

    async def test_master_demoted_before_discovery_task_does_not_create_system(self):
        member = self.member("a", 1)
        self.role(member, 0)
        await asyncio.gather(*self.tasks)
        self.hass.config_entries.flow.async_init.assert_not_awaited()

    async def test_upgrade_removes_legacy_slave_only_entry_and_its_entities(self):
        self.member("slave", 0)
        entry = self.virtual_entry()
        old = self.old_entity(entry.entry_id, 0x020E)
        async def remove(entry_id):
            self.entries.remove(entry)
            self.entities.pop(old.entity_id)
            self.hass.data[self.ns["DOMAIN"]].pop(entry_id, None)
            self.system.entry = None
        self.hass.config_entries.async_remove.side_effect = remove
        await self.ns["async_setup_system_entry"](self.hass, entry)
        await asyncio.gather(*self.tasks)
        self.hass.config_entries.async_remove.assert_awaited_once_with(entry.entry_id)
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()
        self.devices.async_get_or_create.assert_not_called()
        self.assertNotIn(old.entity_id, self.entities)

    async def test_upgrade_waits_for_unknown_members_before_cleanup(self):
        self.member("slave", 0)
        unknown = self.member("unknown", None)
        entry = self.virtual_entry()
        await self.ns["async_setup_system_entry"](self.hass, entry)
        self.assertEqual(self.tasks, [])
        self.role(unknown, 1)
        await asyncio.gather(*self.tasks)
        self.hass.config_entries.async_remove.assert_not_awaited()
        self.hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
        self.assertTrue(entry.data["master_seen"])

    async def test_previously_confirmed_system_survives_election_gap(self):
        member = self.member("a", 1)
        entry = self.virtual_entry()
        await self.ns["async_setup_system_entry"](self.hass, entry)
        await asyncio.gather(*self.tasks)
        self.role(member, 0)
        await asyncio.gather(*self.tasks)
        self.assertTrue(entry.data["master_seen"])
        self.assertFalse(self.system.last_update_success)
        self.hass.config_entries.async_remove.assert_not_awaited()

    async def test_controls_route_to_current_master_and_reject_no_master(self):
        first = self.member("a", 1)
        second = self.member("b", 0)
        await self.system.async_write(0x2200, "UINT16", 20)
        first.async_write.assert_awaited_once_with(0x2200, "UINT16", 20, 8)
        self.role(first, 0)
        self.role(second, 1)
        await self.system.async_write(0x2200, "UINT16", 30)
        second.async_write.assert_awaited_once_with(0x2200, "UINT16", 30, 8)
        self.role(second, 0)
        with self.assertRaisesRegex(RuntimeError, "No online"):
            await self.system.async_write(0x2200, "UINT16", 40)
        self.assertEqual(second.async_write.await_count, 1)

    async def test_offline_and_reconnecting_masters_are_not_sources(self):
        first = self.member("a", 1, {0x020E: 90})
        second = self.member("b", 1, {0x020E: 40})
        first.last_update_success = False
        self.system.async_update_source()
        self.assertIs(self.system.active_master, second)
        second._reconnecting = True
        self.system.async_update_source()
        self.assertFalse(self.system.last_update_success)
        self.assertEqual(self.system.data, {})

    async def test_election_overlap_is_stable_and_detach_selects_other_master(self):
        first = self.member("b", 1)
        second = self.member("a", 1)
        self.assertIs(self.system.active_master, first)
        self.system.async_detach(first.key)
        self.assertIs(self.system.active_master, second)
        self.assertEqual(first.listeners, [])
        self.system.async_detach(second.key)
        self.assertFalse(self.system.last_update_success)
        self.assertEqual(self.system.members, {})

    async def test_repeated_data_refresh_and_multiple_groups(self):
        first = self.member("a", 1, {0x020E: 90})
        listener = Mock()
        self.system.async_add_listener(listener)
        first._stream_system_values = {0x020E: 91}
        for callback in first.listeners:
            callback()
        self.assertEqual(self.system.data[0x020E], 91)
        listener.assert_called_once()
        other = self.ns["get_system"](self.hass, "second-installation")
        self.assertNotEqual(other.key, self.system.key)
        self.assertFalse(other.last_update_success)
        self.assertEqual(other.members, {})

    async def test_registry_migration_preserves_entity_ids_history_and_user_settings(self):
        self.member("master", 1)
        self.member("slave", 0)
        chosen = self.old_entity("master", 0x020E, Hider.INTEGRATION)
        identity, options = chosen.entity_id, chosen.options
        duplicate = self.old_entity("slave", 0x020E)
        control = self.old_entity("master", 0x2200, Hider.USER, "number")
        device = self.old_entity("master", 0x2202)
        foreign_entry = SimpleNamespace(entry_id="foreign", data={"product": "STREAM 5000", "system_id": "other"})
        self.entries.append(foreign_entry)
        foreign = self.old_entity("foreign", 0x020E)
        self.virtual_entry()
        self.ns["async_migrate_system_entities"](self.hass, self.system)
        self.assertEqual(chosen.entity_id, identity)
        self.assertEqual(chosen.unique_id, f"{self.system.key}-020E")
        self.assertEqual(chosen.config_entry_id, "virtual")
        self.assertEqual(chosen.device_id, "stable-system-device")
        self.assertEqual(chosen.name, "My custom name")
        self.assertIs(chosen.options, options)
        self.assertIsNone(chosen.hidden_by)
        self.assertIs(control.hidden_by, Hider.USER)
        self.assertNotIn(duplicate.entity_id, self.entities)
        self.assertEqual(device.device_id, "physical-master")
        self.assertEqual(foreign.device_id, "physical-foreign")
        self.ns["async_migrate_system_entities"](self.hass, self.system)
        self.assertEqual(chosen.entity_id, identity)

    async def test_virtual_setup_without_any_online_device_does_not_open_tcp(self):
        entry = self.virtual_entry()
        entry.data["master_seen"] = True
        result = await self.ns["async_setup_system_entry"](self.hass, entry)
        self.assertTrue(result)
        self.assertFalse(self.system.last_update_success)
        self.assertIs(self.hass.data[self.ns["DOMAIN"]][entry.entry_id], self.system)
        self.hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(entry, self.ns["PLATFORMS"])
        self.assertTrue(all(r["region"].startswith("S") for r in self.system.registers))

    async def test_system_entry_flow_deduplicates_independent_of_master_serial(self):
        harness = identity.IdentityTests()
        harness.setUp()
        flow = harness.flow()
        result = await flow.async_step_integration_discovery({"system_id": "default"})
        self.assertEqual(result["unique_id"], "stream-system:default")
        self.assertEqual(result["title"], "EcoFLow STREAM System")
        stored = SimpleNamespace(unique_id=result["unique_id"], data=result["data"], source="integration_discovery")
        flow = harness.flow([stored])
        with self.assertRaisesRegex(Exception, "already_configured"):
            await flow.async_step_integration_discovery({"system_id": "default"})

    async def test_group_is_allocated_internally_not_reconfigured(self):
        harness = identity.IdentityTests()
        harness.setUp()
        flow = harness.flow()
        flow.async_abort = Mock(return_value={"type": "abort"})
        result = await flow.async_step_reconfigure({"system_id": "second"})
        self.assertEqual(result["type"], "abort")
        flow.async_abort.assert_called_once_with(reason="temporary_system_id")

    async def test_restart_keeps_virtual_identity_with_a_different_master(self):
        self.member("old-master", 1, {0x020E: 80})
        old = self.old_entity("old-master", 0x020E)
        entry = self.virtual_entry()
        self.ns["async_migrate_system_entities"](self.hass, self.system)
        identity = (old.entity_id, old.unique_id, old.device_id)
        self.hass.data = {}  # Simulate HA runtime state being rebuilt.
        self.system = self.ns["get_system"](self.hass, "default")
        self.member("new-master", 1, {0x020E: 81})
        await self.ns["async_setup_system_entry"](self.hass, entry)
        self.assertEqual((old.entity_id, old.unique_id, old.device_id), identity)
        self.assertEqual(self.system.data[0x020E], 81)

    async def test_polling_cache_discards_values_from_previous_master_tenure(self):
        harness = identity.ProductTests()
        harness.setUp()
        coord = harness.coordinator(serial="ES210019")
        coord._update_stream_system_values({0x1001: 0, 0x020E: 90})
        self.assertEqual(coord._stream_system_values, {})
        coord._update_stream_system_values({0x1001: 1, 0x020E: 40})
        self.assertEqual(coord._stream_system_values, {0x020E: 40})
        coord._update_stream_system_values({0x1001: 0})
        coord._update_stream_system_values({0x1001: 1, 0x0208: 100})
        self.assertEqual(coord._stream_system_values, {0x0208: 100})

    async def test_disconnect_requires_fresh_master_confirmation(self):
        member = self.member("a", 1, {0x020E: 50})
        member._stream_role = None  # TCP recovered, election register not read yet.
        member._stream_system_values = {}
        self.system.async_update_source()
        self.assertFalse(self.system.last_update_success)
        with self.assertRaisesRegex(RuntimeError, "No online"):
            await self.system.async_write(0x2200, "UINT16", 20)
        self.role(member, 1, {0x020E: 51})
        self.assertTrue(self.system.last_update_success)
        self.assertEqual(self.system.data[0x020E], 51)

    async def test_system_control_readback_uses_full_poll_table(self):
        harness = identity.ProductTests()
        harness.setUp()
        coord = harness.coordinator(serial="ES210019")
        coord.registers = [r for r in coord.registers if r["region"].startswith("D")]
        coord._stream_role = 1
        coord.link.write_value = AsyncMock()
        coord.link.read_one.return_value = 25
        coord.async_update_listeners = Mock()
        await coord.async_write(0x2200, "UINT16", 25)
        coord.link.write_value.assert_awaited_once_with(0x2200, "UINT16", 25, 8)
        self.assertEqual(coord._stream_system_values[0x2200], 25)
        self.assertEqual(coord.data[0x2200], 25)

    async def test_physical_and_virtual_entry_lifecycles_remain_independent(self):
        entry = SimpleNamespace(entry_id="physical", title="Old title", data={
            "host": "192.0.2.1", "product": "STREAM 5000"})
        callbacks = []
        entry.async_on_unload = callbacks.append
        self.entries.append(entry)
        physical = FakeCoordinator(self.hass)
        physical.key, physical.entry, physical.product = entry.entry_id, entry, "STREAM 5000"
        physical._reconnecting, physical._data_cache = False, {}
        physical.registers = [r for r in self.ns["REGISTERS"] if physical.product in r["products"]]
        physical._poll_registers = list(physical.registers)
        physical.link = SimpleNamespace(read_one=AsyncMock(return_value=1))
        for method in ("async_config_entry_first_refresh", "async_start_connection_monitor",
                       "async_start_heartbeat", "async_refresh_device_display", "async_close"):
            setattr(physical, method, AsyncMock())
        old = self.old_entity(entry.entry_id, 0x020E)
        virtual = self.virtual_entry()
        self.system.entry = None  # The discovery flow below creates the entry.
        seen = {}
        async def forward(config_entry, platforms):
            coord = self.hass.data[self.ns["DOMAIN"]][config_entry.entry_id]
            seen[config_entry.entry_id] = list(coord.registers)
        self.hass.config_entries.async_forward_entry_setups.side_effect = forward
        self.hass.config_entries.async_update_entry = lambda e, **kw: [
            setattr(e, key, value) for key, value in kw.items()]
        self.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        # Simulate a system discovery completing while physical setup awaits it.
        async def discover(*args, **kwargs):
            return await self.ns["async_setup_system_entry"](self.hass, virtual)
        self.hass.config_entries.flow.async_init.side_effect = discover
        ns = dict(self.ns)
        load_methods("visibility.py", ns)
        ns.update(HomeAssistant=object, CONF_HOST="host",
                  EcoFlowModbusCoordinator=lambda *args: physical,
                  _migrate_error_code_entity=Mock())
        tree = ast.parse((COMPONENT / "__init__.py").read_text(encoding="utf-8"))
        tree.body = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                     and n.name in ("async_setup_entry", "async_unload_entry")]
        exec(compile(tree, "__init__.py", "exec"), ns)
        self.assertTrue(await ns["async_setup_entry"](self.hass, entry))
        await asyncio.gather(*self.tasks)
        self.assertTrue(all(r["region"].startswith("D") for r in seen[entry.entry_id]))
        self.assertTrue(all(r["region"].startswith("S") for r in seen[virtual.entry_id]))
        self.assertEqual(old.config_entry_id, virtual.entry_id)
        self.assertTrue(await ns["async_unload_entry"](self.hass, entry))
        self.assertIn(virtual.entry_id, self.hass.data[self.ns["DOMAIN"]])
        self.assertFalse(self.system.last_update_success)
        self.assertEqual(old.config_entry_id, virtual.entry_id)
        physical.async_close.assert_awaited_once()
        self.assertTrue(await ns["async_unload_entry"](self.hass, virtual))

    def unassigned(self, key, role, created="2026-11-10T10:00:00"):
        member = FakeCoordinator(self.hass)
        member.key = key
        member.entry = SimpleNamespace(entry_id=key, created_at=created,
            data={"product": "STREAM 5000", "system_id": "default"})
        self.entries.append(member.entry)
        member._stream_role = role
        member._reconnecting = False
        member._stream_system_values = {0x020E: 50}
        return member

    async def test_each_new_master_gets_a_different_saved_system(self):
        first = self.unassigned("master-a", 1)
        second = self.unassigned("master-b", 1)
        a = self.ns["async_bind_temporary_system"](self.hass, first)
        b = self.ns["async_bind_temporary_system"](self.hass, second)
        self.assertNotEqual(a.key, b.key)
        self.assertIs(a.active_master, first)
        self.assertIs(b.active_master, second)
        self.assertTrue(first.entry.data["system_id_assigned"])
        self.assertEqual(first.entry.data["system_id"], "temporary:master-a")
        self.assertIs(self.ns["async_bind_temporary_system"](self.hass, first), a)

    async def test_restart_reuses_assigned_group_without_allocating_another(self):
        member = self.unassigned("master", 1)
        assigned = self.ns["async_assign_temporary_system_id"](self.hass, member)
        member._stream_role = None  # Role still requires a fresh response.
        self.assertEqual(self.ns["async_assign_temporary_system_id"](self.hass, member), assigned)
        system = self.ns["async_bind_temporary_system"](self.hass, member)
        self.assertIsNone(system.active_master)

    async def test_unassigned_slave_creates_system_only_on_promotion(self):
        member = self.unassigned("slave", 0)
        self.assertIsNone(self.ns["async_bind_temporary_system"](self.hass, member))
        self.hass.config_entries.flow.async_init.assert_not_awaited()
        self.assertNotIn("system_id_assigned", member.entry.data)
        member._stream_role = 1
        self.assertIsNotNone(self.ns["async_bind_temporary_system"](self.hass, member))

    async def test_new_master_does_not_take_established_default_system(self):
        old = self.unassigned("old-master", 1, "2026-11-01T10:00:00")
        new = self.unassigned("new-master", 1, "2026-11-10T10:00:00")
        self.virtual_entry()
        # Even if the new device finishes setup first, reserve old history
        # for the existing oldest physical entry.
        self.assertEqual(self.ns["async_assign_temporary_system_id"](self.hass, new), "temporary:new-master")
        self.assertEqual(self.ns["async_assign_temporary_system_id"](self.hass, old), "default")

    async def test_existing_explicit_group_is_preserved_for_known_members(self):
        member = self.unassigned("known-slave", 0)
        member.entry.data["system_id"] = "existing-system"
        self.assertEqual(self.ns["async_assign_temporary_system_id"](self.hass, member), "existing-system")

    async def test_new_master_never_claims_an_orphaned_older_virtual_system(self):
        legacy = self.virtual_entry()
        legacy.created_at = "2026-11-01T10:00:00"
        new = self.unassigned("new-master", 1, "2026-11-10T10:00:00")
        self.assertEqual(self.ns["async_assign_temporary_system_id"](self.hass, new), "temporary:new-master")

    async def test_unknown_invalid_and_offline_roles_do_not_allocate(self):
        for role in (None, 0, True, "1", 2):
            member = self.unassigned(str(role), role)
            self.assertIsNone(self.ns["async_assign_temporary_system_id"](self.hass, member))
        member = self.unassigned("offline", 1)
        member.last_update_success = False
        self.assertIsNone(self.ns["async_assign_temporary_system_id"](self.hass, member))

    async def test_slave_setup_listener_allocates_system_on_actual_role_update(self):
        physical = self.unassigned("promoted", 0)
        entry = physical.entry
        entry.title = "Old title"
        entry.data["host"] = "192.0.2.1"
        entry.async_on_unload = Mock()
        physical._data_cache = {}
        physical.product = "STREAM 5000"
        physical.registers = [r for r in self.ns["REGISTERS"] if physical.product in r["products"]]
        physical.link = SimpleNamespace(read_one=AsyncMock(return_value=0))
        for method in ("async_config_entry_first_refresh", "async_start_connection_monitor",
                       "async_start_heartbeat", "async_refresh_device_display", "async_close"):
            setattr(physical, method, AsyncMock())
        ns = dict(self.ns)
        load_methods("visibility.py", ns)
        ns.update(HomeAssistant=object, CONF_HOST="host", EcoFlowModbusCoordinator=lambda *args: physical,
                  _migrate_error_code_entity=Mock())
        tree = ast.parse((COMPONENT / "__init__.py").read_text(encoding="utf-8"))
        tree.body = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_setup_entry"]
        exec(compile(tree, "__init__.py", "exec"), ns)
        self.assertTrue(await ns["async_setup_entry"](self.hass, entry))
        self.assertFalse(hasattr(physical, "_stream_system"))
        self.assertTrue(all(r["region"].startswith("D") for r in physical.registers))
        physical._stream_role = 1
        physical._stream_system_values = {0x020E: 61}
        physical.async_set_updated_data({0x1001: 1})
        await asyncio.gather(*self.tasks)
        self.assertEqual(entry.data["system_id"], "temporary:promoted")
        self.assertEqual(physical._stream_system.data, {0x020E: 61})
        self.hass.config_entries.flow.async_init.assert_awaited_once()
