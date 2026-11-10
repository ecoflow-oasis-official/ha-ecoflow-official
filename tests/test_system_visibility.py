"""Physical STREAM entries expose D points for every device role."""

import ast
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_device_identity import COMPONENT, load_methods


class SystemVisibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ns = load_methods("const.py", {})
        ns.update(logging=logging)
        load_methods("visibility.py", ns)
        self.ns = ns
        self.prepare = ns["async_prepare_system_registers"]
        self.regs = [
            {"offset": 0x1001, "region": "D1", "permission": "R", "name": "Master"},
            {"offset": 1, "region": "S1", "permission": "R", "name": "System"},
            {"offset": 2, "region": "D3", "permission": "RW", "name": "Pack"},
        ]
        self.coord = SimpleNamespace(
            product="STREAM 5000", key="entry", entry=SimpleNamespace(entry_id="entry", data={}),
            registers=list(self.regs), data={}, _data_cache={},
            link=SimpleNamespace(read_one=AsyncMock(return_value=0)),
            _partitions=[list(self.regs)],
        )

    async def test_every_role_keeps_only_device_entities_without_altering_polling(self):
        for role in (0, 1, None):
            self.coord.registers = list(self.regs)
            self.coord.link.read_one.return_value = role
            await self.prepare(None, self.coord)
            self.assertEqual([r["region"] for r in self.coord.registers], ["D1", "D3"])
            self.assertEqual(self.coord._partitions, [self.regs])

    async def test_live_preflight_overrides_cached_role(self):
        self.coord.data = {0x1001: 1}
        self.coord.entry.data = {"system_master_flag": 1}
        await self.prepare(None, self.coord)
        self.assertEqual(self.coord._stream_role, 0)
        self.assertEqual(self.coord.data[0x1001], 0)
        self.assertEqual(self.coord._stream_system_values, {})

    async def test_failed_preflight_keeps_role_from_actual_first_poll(self):
        self.coord.data = {0x1001: 0}
        self.coord.link.read_one.side_effect = OSError("offline")
        await self.prepare(None, self.coord)
        self.assertEqual(self.coord._stream_role, 0)

    async def test_persisted_old_master_is_not_used_without_live_confirmation(self):
        self.coord.entry.data = {"system_master_flag": 1}
        self.coord.link.read_one.side_effect = OSError("offline")
        await self.prepare(None, self.coord)
        self.assertIsNone(self.coord._stream_role)
        self.assertNotIn(0x1001, self.coord.data)

    async def test_powerocean_is_unchanged(self):
        self.coord.product = "PowerOcean"
        await self.prepare(None, self.coord)
        self.assertEqual(self.coord.registers, self.regs)
        self.coord.link.read_one.assert_not_awaited()

    async def test_all_five_platforms_create_only_device_points(self):
        await self.prepare(None, self.coord)
        hass = SimpleNamespace(data={self.ns["DOMAIN"]: {"entry": self.coord}})
        for platform in ("sensor", "number", "select", "switch", "button"):
            tree = ast.parse((COMPONENT / f"{platform}.py").read_text(encoding="utf-8"))
            node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                        and n.name == "async_setup_entry")
            def entity(coordinator, reg, *args):
                self.assertTrue(reg["region"].startswith("D"))
                return reg
            ns = dict(self.ns)
            ns.update({name: entity for name in (
                "EcoFlowSensor", "EcoFlowNumber", "EcoFlowSelect", "EcoFlowSwitch", "EcoFlowButton")})
            ns.update(_writable_numbers=lambda regs: regs,
                      SELECTS={r["offset"]: {0: "Off"} for r in self.regs},
                      SWITCHES={r["offset"]: (1, 0) for r in self.regs},
                      BUTTON_WRITES={r["name"]: 1 for r in self.regs})
            exec(compile(ast.Module(body=[node], type_ignores=[]), f"{platform}.py", "exec"), ns)
            added = Mock()
            await ns["async_setup_entry"](hass, self.coord.entry, added)
            self.assertTrue(added.call_args.args[0])
