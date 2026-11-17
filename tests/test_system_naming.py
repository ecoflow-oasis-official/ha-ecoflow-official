"""Stable display numbering and migration of group-derived entity IDs."""
import asyncio
from types import SimpleNamespace
import unittest

import test_device_identity as identity
import test_stream_system as stream


class NamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = stream.StreamSystemTests()
        self.h.setUp()
        self.hass, self.ns = self.h.hass, self.h.ns

    async def asyncTearDown(self):
        while pending := [t for t in self.h.tasks if not t.done()]:
            await asyncio.gather(*pending)

    def entry(self, group, number=None, created="", title=None):
        data = {"stream_system": True, "system_id": str(group),
                "device_group_id": int(group), "master_seen": True}
        if number is not None:
            data["system_number"] = number
        entry = SimpleNamespace(entry_id=f"virtual-{group}", created_at=created, data=data,
            title=title or f"EcoFLow STREAM System {group}", unique_id=f"stream-system:{group}")
        self.h.entries.append(entry)
        return entry

    def system(self, entry):
        system = self.ns["get_system"](self.hass, entry.data["system_id"])
        system.entry = entry
        return system

    def entity(self, system, object_id, offset=0x020C, domain="sensor"):
        entity = self.h.old_entity(system.entry.entry_id, offset, domain=domain)
        self.h.entities.pop(entity.entity_id)
        entity.entity_id = f"{domain}.{object_id}"
        entity.unique_id = f"{system.key}-{offset:04X}"
        self.h.entities[entity.entity_id] = entity
        return entity

    def test_first_second_third_names_and_numbers_persist(self):
        entries = [self.entry(group) for group in (588177409, 900000, 1234)]
        for entry, number, name in zip(entries, (1, 2, 3),
                ("EcoFLow STREAM System", "EcoFLow STREAM System (2)", "EcoFLow STREAM System (3)")):
            system = self.system(entry)
            self.ns["async_migrate_system_entities"](self.hass, system)
            self.assertEqual(entry.data["system_number"], number)
            self.assertEqual(entry.title, name)
            self.assertEqual(system.device_info["name"], name)
            self.assertEqual(system.group, entry.data["system_id"])

    def test_restart_and_reverse_loading_keep_existing_numbers(self):
        first = self.entry(588177409, 1)
        second = self.entry(800000, 2)
        self.hass.data = {}
        self.h.entries.reverse()
        for entry, expected in ((second, 2), (first, 1)):
            system = self.system(entry)
            self.assertEqual(system.number, expected)
        self.assertEqual(self.system(second).device_info["name"], "EcoFLow STREAM System (2)")

    def test_existing_unnumbered_entries_use_creation_order(self):
        later = self.entry(10, created="2026-11-17")
        older = self.entry(20, created="2026-11-16")
        self.assertEqual(self.system(later).number, 2)
        self.assertEqual(self.system(older).number, 1)

    def test_deleting_other_system_does_not_renumber_survivor(self):
        first, second = self.entry(10, 1), self.entry(20, 2)
        self.h.entries.remove(first)
        self.hass.data = {}
        self.assertEqual(self.system(second).number, 2)

    def test_default_ids_lose_group_and_second_system_gets_two(self):
        first, second = self.system(self.entry(588177409)), self.system(self.entry(900000))
        a = self.entity(first, "ecoflow_stream_system_588177409_s2_system_battery_power")
        b = self.entity(second, "ecoflow_stream_system_900000_s2_system_battery_power")
        before = [(e.unique_id, e.name, e.options) for e in (a, b)]
        for system in (first, second):
            self.ns["async_migrate_system_entities"](self.hass, system)
        self.assertEqual(a.entity_id, "sensor.ecoflow_stream_system_s2_system_battery_power")
        self.assertEqual(b.entity_id, "sensor.ecoflow_stream_system_2_s2_system_battery_power")
        self.assertEqual(before, [(e.unique_id, e.name, e.options) for e in (a, b)])
        self.assertIs(self.h.entities[a.entity_id], a)
        self.assertIs(self.h.entities[b.entity_id], b)
        # Running the same migration again changes nothing.
        ids = (a.entity_id, b.entity_id)
        for system in (first, second):
            self.ns["async_migrate_system_entities"](self.hass, system)
        self.assertEqual(ids, (a.entity_id, b.entity_id))

    def test_all_entity_domains_and_custom_ids_are_preserved_appropriately(self):
        system = self.system(self.entry(588177409))
        entities = [self.entity(system, f"ecoflow_stream_system_588177409_control_{domain}", domain=domain)
                    for domain in self.ns["PLATFORMS"]]
        custom = self.entity(system, "my_house_soc", offset=0x020E)
        self.ns["async_migrate_system_entities"](self.hass, system)
        for entity in entities:
            self.assertNotIn("588177409", entity.entity_id)
        self.assertEqual(custom.entity_id, "sensor.my_house_soc")

    def test_entity_id_collision_never_overwrites_an_existing_entity(self):
        system = self.system(self.entry(588177409))
        entity = self.entity(system, "ecoflow_stream_system_588177409_s2_system_battery_power")
        target = "sensor.ecoflow_stream_system_s2_system_battery_power"
        foreign = SimpleNamespace(entity_id=target, platform="other", config_entry_id="other")
        self.h.entities[target] = foreign
        with self.assertLogs(level="WARNING"):
            self.ns["async_migrate_system_entities"](self.hass, system)
        self.assertEqual(entity.entity_id, "sensor.ecoflow_stream_system_588177409_s2_system_battery_power")
        self.assertIs(self.h.entities[target], foreign)

    async def test_parallel_discovery_reserves_distinct_ordinals_before_entries_exist(self):
        harness = identity.IdentityTests()
        harness.setUp()
        first, second = harness.flow(), harness.flow()
        second.hass = first.hass
        a = await first.async_step_integration_discovery({"system_id": "588177409"})
        b = await second.async_step_integration_discovery({"system_id": "900000"})
        self.assertEqual(a["title"], "EcoFLow STREAM System")
        self.assertEqual(b["title"], "EcoFLow STREAM System (2)")
        self.assertEqual(a["data"]["system_number"], 1)
        self.assertEqual(b["data"]["system_number"], 2)
        self.assertNotEqual(a["unique_id"], b["unique_id"])

    def test_user_config_entry_title_is_not_replaced(self):
        entry = self.entry(588177409, title="EcoFLow STREAM System Upstairs")
        self.ns["async_migrate_system_entities"](self.hass, self.system(entry))
        self.assertEqual(entry.title, "EcoFLow STREAM System Upstairs")

    def test_invalid_duplicate_numbers_are_repaired_without_changing_groups(self):
        first, second, third = self.entry(10, 1), self.entry(20, 1), self.entry(30, -1)
        numbers = [self.system(entry).number for entry in (first, second, third)]
        self.assertEqual(numbers, [1, 2, 3])
        self.assertEqual([e.data["system_id"] for e in (first, second, third)], ["10", "20", "30"])
