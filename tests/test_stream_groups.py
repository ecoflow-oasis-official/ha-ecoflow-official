"""Firmware group identity, master IP changes and legacy device reuse."""
import asyncio
import ast
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_device_identity as identity
import test_stream_system as stream


class GroupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = stream.StreamSystemTests()
        self.h.setUp()
        self.ns = self.h.ns
        self.hass = self.h.hass
        self.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        self.bind = self.ns["async_bind_system"]

    async def asyncTearDown(self):
        while pending := [t for t in self.h.tasks if not t.done()]:
            await asyncio.gather(*pending)

    def member(self, key, group, role=1, host="192.0.2.1", old_group=None):
        member = self.h.unassigned(key, role)
        member._stream_group_id = group
        member.async_write = AsyncMock()
        member.entry.data.update(host=host, port=502, unit_id=1)
        if old_group is not None:
            member.entry.data.update(system_id=old_group, system_id_assigned=True)
        return member

    def virtual(self, group, key=None, entry_id="virtual"):
        data = {"stream_system": True, "system_id": group, "master_seen": True}
        if key:
            data["system_entity_key"] = key
        entry = SimpleNamespace(entry_id=entry_id, data=data,
            unique_id=f"stream-system:{group}", title="Existing system")
        self.h.entries.append(entry)
        system = self.ns["get_system"](self.hass, group)
        system.entry = entry
        system._entry_platforms_loaded = True
        self.hass.data[self.ns["DOMAIN"]][entry_id] = system
        return system, entry

    def sensor(self, system, offset, entity_id="sensor.existing_soc", domain="sensor"):
        entity = self.h.old_entity(system.entry.entry_id, offset, domain=domain)
        self.h.entities.pop(entity.entity_id)
        entity.entity_id = entity_id
        entity.unique_id = f"{system.key}-{offset:04X}"
        self.h.entities[entity_id] = entity
        return entity

    async def test_same_uint32_group_reuses_device_and_sensor_on_new_master_ip(self):
        system, entry = self.virtual("70000")
        first = self.member("old", 70000, host="192.0.2.1")
        second = self.member("new", 70000, role=0, host="192.0.2.2")
        sensor = self.sensor(system, 0x020E)
        before = (sensor.entity_id, sensor.unique_id, sensor.device_id, sensor.config_entry_id)
        self.assertIs(self.bind(self.hass, first), system)
        self.assertIs(self.bind(self.hass, second), system)
        self.assertEqual(entry.data["host"], "192.0.2.1")
        self.h.role(first, 0)
        self.h.role(second, 1, {0x020E: 65})
        self.assertIs(system.active_master, second)
        self.assertEqual(entry.data["host"], "192.0.2.2")
        self.assertEqual(system.data, {0x020E: 65})
        await system.async_write(0x2200, "UINT16", 20)
        first.async_write.assert_not_awaited()
        second.async_write.assert_awaited_once_with(0x2200, "UINT16", 20, 8)
        self.assertEqual(before, (sensor.entity_id, sensor.unique_id, sensor.device_id, sensor.config_entry_id))
        self.assertEqual(system.device_info["name"], "EcoFLow STREAM System")
        self.hass.config_entries.flow.async_init.assert_not_awaited()

    async def test_distinct_groups_never_share_sources_or_write_targets(self):
        first = self.member("a", 1)
        second = self.member("b", 0xFFFFFFFF)
        a, b = self.bind(self.hass, first), self.bind(self.hass, second)
        self.assertIsNot(a, b)
        self.assertEqual(a.group, "1")
        self.assertEqual(b.group, "4294967295")
        self.assertNotEqual(a.key, b.key)
        await a.async_write(0x2200, "UINT16", 10)
        first.async_write.assert_awaited_once()
        second.async_write.assert_not_awaited()

    async def test_missing_invalid_or_unconfirmed_group_does_not_create_virtual_entry(self):
        for group in (None, 0, -1, 0x100000000, True, "70000", 1.5):
            member = self.member(str(group), group)
            self.assertIsNone(self.bind(self.hass, member))
            self.assertNotIn("device_group_id", member.entry.data)
        self.assertEqual(self.h.tasks, [])

    async def test_legacy_temporary_system_is_rekeyed_without_changing_entities(self):
        system, entry = self.virtual("temporary:old")
        sensor = self.sensor(system, 0x020E)
        before = (system.key, sensor.entity_id, sensor.unique_id, sensor.device_id, sensor.options)
        first = self.member("old", 70000, old_group="temporary:old")
        self.assertIs(self.bind(self.hass, first), system)
        self.assertEqual(entry.data["system_id"], "70000")
        self.assertEqual(entry.data["system_entity_key"], "stream-system:temporary:old")
        self.assertEqual(entry.unique_id, "stream-system:70000")
        self.assertEqual(entry.title, "EcoFLow STREAM System")
        self.assertEqual(before, (system.key, sensor.entity_id, sensor.unique_id, sensor.device_id, sensor.options))
        new = self.member("new", 70000, host="192.0.2.2")
        first.last_update_success = False
        self.assertIs(self.bind(self.hass, new), system)
        self.assertEqual(entry.data["host"], "192.0.2.2")
        self.assertEqual(self.h.devices.async_update_device.call_args.kwargs["name"],
                         "EcoFLow STREAM System")
        self.hass.config_entries.flow.async_init.assert_not_awaited()

    async def test_restart_reuses_saved_legacy_entity_key_with_another_master(self):
        system, entry = self.virtual("70000", key="stream-system:temporary:old")
        sensor = self.sensor(system, 0x020E)
        before = (sensor.entity_id, sensor.unique_id, sensor.device_id)
        self.hass.data = {}
        member = self.member("new", 70000)
        restored = self.bind(self.hass, member)
        self.assertEqual(restored.key, "stream-system:temporary:old")
        self.assertIs(restored.entry, entry)
        self.assertEqual(before, (sensor.entity_id, sensor.unique_id, sensor.device_id))
        self.hass.config_entries.flow.async_init.assert_not_awaited()

    async def test_slave_loaded_before_legacy_owner_does_not_replace_legacy_identity(self):
        legacy, entry = self.virtual("temporary:old")
        sensor = self.sensor(legacy, 0x020E)
        before = (sensor.entity_id, sensor.unique_id, sensor.device_id)
        # The legacy config entry exists but has not loaded its coordinator yet.
        self.hass.data[self.ns["DOMAIN"]]["_stream_systems"].pop("temporary:old")
        self.hass.data[self.ns["DOMAIN"]].pop(entry.entry_id)
        slave = self.member("slave", 70000, role=0)
        placeholder = self.bind(self.hass, slave)
        owner = self.member("old", 70000, old_group="temporary:old")
        system = self.bind(self.hass, owner)
        self.assertIsNot(system, placeholder)
        self.assertEqual(system.key, "stream-system:temporary:old")
        self.assertIs(system.members["slave"], slave)
        self.assertIs(slave._stream_system, system)
        self.assertEqual(before, (sensor.entity_id, sensor.unique_id, sensor.device_id))

    async def test_group_change_does_not_rename_or_steal_old_system(self):
        old, entry = self.virtual("10")
        member = self.member("a", 10)
        self.bind(self.hass, member)
        member._stream_group_id = 20
        member._stream_system_values = {}
        new = self.bind(self.hass, member)
        self.assertIsNot(old, new)
        self.assertEqual(entry.data["system_id"], "10")
        self.assertEqual(old.members, {})
        self.assertFalse(old.last_update_success)
        self.assertEqual(new.group, "20")

    async def test_invalid_group_detaches_live_master_without_deleting_virtual_device(self):
        system, entry = self.virtual("10")
        member = self.member("a", 10)
        self.bind(self.hass, member)
        member._stream_group_id = None
        self.assertIsNone(self.bind(self.hass, member))
        self.assertIsNone(system.active_master)
        self.assertFalse(system.last_update_success)
        self.assertIn(entry, self.h.entries)
        self.hass.config_entries.async_remove.assert_not_awaited()

    async def test_discovery_race_only_creates_one_flow_for_same_group(self):
        self.bind(self.hass, self.member("a", 70000))
        self.bind(self.hass, self.member("b", 70000))
        await asyncio.gather(*self.h.tasks)
        self.hass.config_entries.flow.async_init.assert_awaited_once()
        self.assertEqual(self.hass.config_entries.flow.async_init.await_args.kwargs["data"],
                         {"system_id": "70000"})

    async def test_duplicate_legacy_entry_merges_into_established_sensor(self):
        main, entry = self.virtual("10")
        entry.data["device_group_id"] = 10
        established = self.sensor(main, 0x020E)
        legacy, duplicate = self.virtual("temporary:dup", entry_id="duplicate")
        redundant = self.sensor(legacy, 0x020E, "sensor.duplicate_soc")
        unique = self.sensor(legacy, 0x2200, "number.existing_limit", domain="number")
        member = self.member("dup", 10, old_group="temporary:dup")
        self.assertIs(self.bind(self.hass, member), main)
        await asyncio.gather(*self.h.tasks)
        self.assertIs(self.h.entities[established.entity_id], established)
        self.assertNotIn(redundant.entity_id, self.h.entities)
        self.assertEqual(unique.entity_id, "number.existing_limit")
        self.assertEqual(unique.config_entry_id, entry.entry_id)
        self.assertEqual(unique.unique_id, f"{main.key}-2200")
        self.hass.config_entries.async_unload_platforms.assert_awaited_once_with(duplicate, self.ns["PLATFORMS"])
        self.hass.config_entries.async_remove.assert_awaited_once_with(duplicate.entry_id)
        self.hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)

    async def test_failed_legacy_unload_keeps_registry_and_entry_intact(self):
        main, _ = self.virtual("10")
        legacy, entry = self.virtual("temporary:dup", entry_id="duplicate")
        sensor = self.sensor(legacy, 0x020E, "sensor.duplicate_soc")
        self.hass.config_entries.async_unload_platforms.return_value = False
        self.bind(self.hass, self.member("dup", 10, old_group="temporary:dup"))
        await asyncio.gather(*self.h.tasks)
        self.assertIn(sensor.entity_id, self.h.entities)
        self.assertEqual(sensor.config_entry_id, entry.entry_id)
        self.hass.config_entries.async_remove.assert_not_awaited()
        self.assertEqual(self.h.entries[-1].data["legacy_system_id"], "temporary:dup")
        # A later successful unload must still be able to find the old entry.
        self.hass.config_entries.async_unload_platforms.return_value = True
        member = main.members["dup"]
        self.bind(self.hass, member)
        await asyncio.gather(*self.h.tasks)
        self.hass.config_entries.async_remove.assert_awaited_once_with(entry.entry_id)
        self.assertNotIn("legacy_system_id", member.entry.data)

    async def test_poll_group_changes_clear_values_until_next_read(self):
        harness = identity.ProductTests()
        harness.setUp()
        coord = harness.coordinator(serial="ES210019")
        coord._update_stream_system_values({0x1004: 70000, 0x1001: 1, 0x020E: 50})
        self.assertEqual(coord._stream_group_id, 70000)
        self.assertEqual(coord._stream_system_values, {})
        coord._update_stream_system_values({0x020E: 51})
        self.assertEqual(coord._stream_system_values, {0x020E: 51})
        coord._update_stream_system_values({0x1004: 80000, 0x020E: 60})
        self.assertEqual(coord._stream_system_values, {})
        coord._update_stream_system_values({0x1004: None})
        self.assertIsNone(coord._stream_group_id)

    async def test_config_flow_names_system_with_group_and_deduplicates(self):
        harness = identity.IdentityTests()
        harness.setUp()
        result = await harness.flow().async_step_integration_discovery({"system_id": "70000"})
        self.assertEqual(result["title"], "EcoFLow STREAM System")
        self.assertEqual(result["unique_id"], "stream-system:70000")
        self.assertTrue(result["data"]["master_seen"])

    async def test_master_lost_between_device_creation_and_platform_setup_still_adds_entities(self):
        member = self.member("physical", 1234)
        # An entry created by the previous 1.3.2 code has no master_seen flag.
        entry = SimpleNamespace(entry_id="virtual", unique_id="stream-system:1234",
            data={"stream_system": True, "system_id": "1234", "device_group_id": 1234})
        self.h.entries.append(entry)
        system = self.bind(self.hass, member)  # Creates the virtual device before setup.
        self.h.devices.async_get_or_create.assert_called()
        self.h.role(member, None)  # Election gap, or a missed role response.
        self.assertTrue(await self.ns["async_setup_system_entry"](self.hass, entry))
        self.hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
            entry, self.ns["PLATFORMS"])
        self.assertTrue(system._entry_platforms_loaded)
        self.assertFalse(system.last_update_success)
        self.assertTrue(any(r["permission"] == "R" for r in system.registers))
        self.assertTrue(any(r["permission"] == "RW" for r in system.registers))
        before = (system.key, system.device_info["identifiers"])
        self.h.role(member, 1, {0x020E: 55})
        self.assertTrue(system.last_update_success)
        self.assertEqual(system.data[0x020E], 55)
        self.assertEqual(before, (system.key, system.device_info["identifiers"]))
        self.hass.config_entries.async_reload.assert_not_awaited()

    async def test_existing_hardware_system_loads_entities_before_physical_members_start(self):
        entry = SimpleNamespace(entry_id="virtual", data={"stream_system": True,
            "system_id": "1234", "device_group_id": 1234})
        self.h.entries.append(entry)
        await self.ns["async_setup_system_entry"](self.hass, entry)
        self.hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
            entry, self.ns["PLATFORMS"])
        system = self.hass.data[self.ns["DOMAIN"]][entry.entry_id]
        self.assertFalse(system.last_update_success)
        self.assertTrue(system._entry_platforms_loaded)

    async def test_offline_hardware_system_creates_sensor_and_control_entities(self):
        entry = SimpleNamespace(entry_id="virtual", data={"stream_system": True,
            "system_id": "1234", "device_group_id": 1234})
        self.h.entries.append(entry)
        added = {}
        async def forward(config_entry, platforms):
            for platform in platforms:
                namespace = dict(self.ns)
                tree = ast.parse((identity.COMPONENT / f"{platform}.py").read_text(encoding="utf-8"))
                functions = [node for node in tree.body if
                    (isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry")
                    or (platform == "number" and isinstance(node, ast.FunctionDef)
                        and node.name in ("_is_special", "_writable_numbers"))]
                def make_entity(coordinator, point, *args):
                    return SimpleNamespace(offset=point["offset"], coordinator=coordinator,
                        device_info=coordinator.device_info)
                namespace.update({name: make_entity for name in (
                    "EcoFlowSensor", "EcoFlowNumber", "EcoFlowSelect", "EcoFlowSwitch", "EcoFlowButton")})
                exec(compile(ast.Module(body=functions, type_ignores=[]), f"{platform}.py", "exec"), namespace)
                await namespace["async_setup_entry"](self.hass, config_entry,
                    lambda entities: added.update({platform: entities}))
        self.hass.config_entries.async_forward_entry_setups.side_effect = forward
        await self.ns["async_setup_system_entry"](self.hass, entry)
        system = self.hass.data[self.ns["DOMAIN"]][entry.entry_id]
        self.assertTrue(added["sensor"])
        self.assertTrue(added["number"])
        self.assertTrue(added["select"])
        self.assertEqual(set(added), set(self.ns["PLATFORMS"]))
        for entities in added.values():
            for entity in entities:
                self.assertIs(entity.coordinator, system)
                self.assertEqual(entity.device_info["identifiers"],
                    {(self.ns["DOMAIN"], "stream-system:1234")})
        self.assertCountEqual([entity.offset for entities in added.values() for entity in entities],
                              [point["offset"] for point in system.registers])
        self.assertFalse(system.last_update_success)

    async def test_platform_setup_failure_leaves_retryable_state(self):
        entry = SimpleNamespace(entry_id="virtual", data={"stream_system": True,
            "system_id": "1234", "device_group_id": 1234})
        self.h.entries.append(entry)
        self.hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("platform failed")
        with self.assertRaisesRegex(RuntimeError, "platform failed"):
            await self.ns["async_setup_system_entry"](self.hass, entry)
        system = self.hass.data[self.ns["DOMAIN"]][entry.entry_id]
        self.assertIs(system._entry_platforms_loaded, False)

    async def test_slave_with_confirmed_group_waits_for_master_before_creating_entry(self):
        member = self.member("slave", 70000, role=0)
        system = self.bind(self.hass, member)
        self.assertIsNotNone(system)
        self.hass.config_entries.flow.async_init.assert_not_awaited()
        self.assertEqual(self.h.tasks, [])
        self.h.role(member, 1, {0x020E: 20})
        await asyncio.gather(*self.h.tasks)
        self.hass.config_entries.flow.async_init.assert_awaited_once()

    async def test_group_register_is_read_only_uint32_on_both_ha_and_simulator(self):
        import runpy
        schema = runpy.run_path(str(identity.COMPONENT / "registers_schema.py"))["REGISTERS"]
        ha = next(r for r in schema if r["offset"] == 0x1004)
        sim_path = identity.COMPONENT.parents[2] / "modbus-tcp-simulator/registers.py"
        self.assertEqual(ha["dtype"], "UINT32")
        self.assertEqual(ha["permission"], "R")
        if not sim_path.exists():  # The standalone integration repository has no simulator.
            return
        sim = runpy.run_path(str(sim_path))
        definition = next(r for r in sim["REGISTER_DEFS"] if r["offset"] == 0x1004)
        self.assertEqual(definition["data_type"], "UINT32")
        self.assertEqual(definition["address"], 44101)
        for group in (1, 70000, 0xFFFFFFFF):
            words = sim["encode_value"](group, "UINT32")
            self.assertEqual(identity.load_codec()["decode_value"]("UINT32", words), group)
