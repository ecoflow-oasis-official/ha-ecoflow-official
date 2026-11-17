"""Control Point labels and protocol values remain distinct."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from test_device_identity import COMPONENT, load_methods
from test_number_controls import CoordinatorEntity


class SelectEntity:
    pass


class SelectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = load_methods("const.py", {})
        load_methods("registers_schema.py", self.ns)
        self.ns.update(SelectEntity=SelectEntity, CoordinatorEntity=CoordinatorEntity)
        load_methods("select.py", self.ns)
        self.coordinator = SimpleNamespace(
            key="existing-entry", device_info={}, data={}, async_write=AsyncMock()
        )

    def select(self, offset=0x1610):
        reg = next(r for r in self.ns["REGISTERS"] if r["offset"] == offset)
        return self.ns["EcoFlowSelect"](self.coordinator, reg, self.ns["SELECTS"][offset])

    async def test_every_control_point_decodes_and_writes_the_same_protocol_value(self):
        entity = self.select()
        keys = ["none", "battery_point", "meter_point", "inverter_point", "grid_port_point"]
        self.assertEqual(entity._attr_options, keys)
        self.assertEqual(entity._attr_translation_key, "reg_1610")
        self.assertEqual(entity._attr_unique_id, "existing-entry-1610")
        for value, option in enumerate(keys):
            with self.subTest(option=option):
                self.coordinator.data = {0x1610: value}
                self.assertEqual(entity.current_option, option)
                await entity.async_select_option(option)
                self.coordinator.async_write.assert_awaited_with(0x1610, "UINT16", value)

    async def test_invalid_option_cannot_write_arbitrary_protocol_values(self):
        for option in ("5", "0", "Grid port point", "battery", ""):
            with self.subTest(option=option), self.assertRaises(ValueError):
                await self.select().async_select_option(option)
        self.coordinator.async_write.assert_not_awaited()

    def test_unknown_or_missing_state_is_not_reported_as_a_valid_control_point(self):
        entity = self.select()
        for data in (None, {}, {0x1610: None}, {0x1610: 9}):
            self.coordinator.data = data
            self.assertIsNone(entity.current_option)

    async def test_other_selects_preserve_their_existing_options(self):
        for offset, options in self.ns["SELECTS"].items():
            if offset == 0x1610:
                continue
            entity = self.select(offset)
            self.assertEqual(entity._attr_options, [str(value) for value in options])
            for value in options:
                self.coordinator.data = {offset: value}
                self.assertEqual(entity.current_option, str(value))
                await entity.async_select_option(str(value))
                self.coordinator.async_write.assert_awaited_with(offset, "UINT16", value)

    def test_all_languages_have_readable_labels_for_every_option(self):
        keys = set(self.select()._attr_options)
        for path in [COMPONENT / "strings.json", *sorted((COMPONENT / "translations").glob("*.json"))]:
            with self.subTest(language=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                states = data["entity"]["select"]["reg_1610"]["state"]
                self.assertEqual(set(states), keys)
                self.assertTrue(all(isinstance(value, str) and value.strip()
                                    and not value.isdigit() for value in states.values()))
        english = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
        self.assertEqual(english["entity"]["select"]["reg_1610"]["state"]["grid_port_point"],
                         "Grid port point")
