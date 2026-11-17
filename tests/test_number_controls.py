"""Regression coverage for number bounds and signed Modbus writes."""

import ast
import logging
import struct
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_device_identity import COMPONENT, load_codec, load_methods


class CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


class NumberEntity:
    pass


class NumberControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = load_methods("const.py", {})
        load_methods("registers_schema.py", self.ns)
        self.ns.update(
            NumberEntity=NumberEntity, CoordinatorEntity=CoordinatorEntity,
            NumberMode=SimpleNamespace(BOX="box", AUTO="auto"),
            unit_device_class=lambda unit, name: (None, unit),
        )
        load_methods("number.py", self.ns)
        self.coordinator = SimpleNamespace(
            key="existing-entry", device_info={}, data={}, async_write=AsyncMock()
        )
        # Load the real codec and synchronous client without optional imports.
        self.codec = load_methods("const.py", {})
        self.codec.update(logging=logging, struct=struct)
        # Skip the module's vendored dependency bootstrap in this unit harness.
        tree = ast.parse((COMPONENT / "modbus_client.py").read_text(encoding="utf-8"))
        tree.body = [node for node in tree.body if isinstance(
            node, (ast.FunctionDef, ast.ClassDef)
        )]
        self.codec.update(WORD_COUNT=load_codec()["WORD_COUNT"])
        exec(compile(tree, "modbus_client.py", "exec"), self.codec)

    def number(self, offset):
        reg = next(r for r in self.ns["REGISTERS"] if r["offset"] == offset)
        return self.ns["EcoFlowNumber"](self.coordinator, reg)

    def test_signed_target_power_accepts_full_int16_range(self):
        for offset in (0x1611, 0x1612, 0x1613, 0x1614):
            number = self.number(offset)
            self.assertEqual(number._attr_native_min_value, -32768)
            self.assertEqual(number._attr_native_max_value, 32767)
            self.assertEqual(number._attr_native_step, 1)
            self.assertEqual(number._attr_mode, "box")

    def test_all_eight_power_limits_have_confirmed_bounds(self):
        for offset in range(0x1600, 0x1610, 2):
            with self.subTest(offset=offset):
                number = self.number(offset)
                self.assertEqual(number._attr_native_min_value, 0)
                self.assertEqual(number._attr_native_max_value, 100000)
                self.assertEqual(number._attr_native_step, 1)
                self.assertEqual(number._attr_mode, "box")

    async def test_power_limits_accept_both_boundaries_as_uint32(self):
        for offset in range(0x1600, 0x1610, 2):
            for value in (0, 100000):
                with self.subTest(offset=offset, value=value):
                    await self.number(offset).async_set_native_value(value)
                    self.coordinator.async_write.assert_awaited_with(offset, "UINT32", value, 0)
                    words = self.codec["encode_value"]("UINT32", value)
                    self.assertEqual(self.codec["decode_value"]("UINT32", words), value)

    async def test_invalid_power_limits_never_reach_modbus(self):
        for offset in range(0x1600, 0x1610, 2):
            for value in (-1, 100001, 100000.5, 4294967295, 0.5,
                          float("nan"), float("inf"), -float("inf")):
                with self.subTest(offset=offset, value=value), self.assertRaises(ValueError):
                    await self.number(offset).async_set_native_value(value)
        self.coordinator.async_write.assert_not_awaited()

    async def test_existing_percentage_and_signed_bounds_are_enforced(self):
        for offset, value in ((0x0217, 101), (0x1611, -32769), (0x1611, 32768)):
            with self.subTest(offset=offset, value=value), self.assertRaises(ValueError):
                await self.number(offset).async_set_native_value(value)
        self.coordinator.async_write.assert_not_awaited()

    def test_percentage_limits_are_preserved(self):
        number = self.number(0x0217)
        self.assertEqual((number._attr_native_min_value,
                          number._attr_native_max_value), (0, 100))
        self.assertEqual(number._attr_mode, "auto")

    def test_all_writable_numbers_have_valid_numeric_limits(self):
        for reg in self.ns["_writable_numbers"](self.ns["REGISTERS"]):
            minimum, maximum = self.ns["_bounds"](reg)
            with self.subTest(offset=reg["offset"]):
                self.assertIsInstance(minimum, (int, float))
                self.assertIsInstance(maximum, (int, float))
                self.assertLess(minimum, maximum)

    async def test_int16_number_reaches_single_register_write_and_decodes(self):
        client = object.__new__(self.codec["ModbusClient"])
        client.address_offset = 40001
        client.unit_id = 1
        client._lock = threading.Lock()
        client._client = Mock(connected=True)
        client._client.write_register.return_value.isError.return_value = False
        writes = []

        async def write(offset, dtype, value, length):
            client.write_value(offset, dtype, value, length)
            writes.append(client._client.write_register.call_args.args[1])

        self.coordinator.async_write.side_effect = write
        number = self.number(0x1611)
        for value, expected_word in ((-32768, 0x8000), (-1000, 0xFC18),
                                     (0, 0), (1000, 1000), (32767, 0x7FFF)):
            await number.async_set_native_value(float(value))
            self.assertEqual(writes[-1], expected_word)
            client._client.write_register.assert_called_with(
                40001 + 0x1611, expected_word, slave=1
            )
            self.assertEqual(self.codec["decode_value"]("INT16", [writes[-1]]), value)

    async def test_unsigned_power_limit_preserves_integer_value(self):
        await self.number(0x1602).async_set_native_value(2500.0)
        self.coordinator.async_write.assert_awaited_once_with(0x1602, "UINT32", 2500, 0)


if __name__ == "__main__":
    unittest.main()
