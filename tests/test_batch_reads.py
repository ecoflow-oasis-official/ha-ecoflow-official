"""Batch planning, decoding, wire requests and compatibility fallback."""
import asyncio
import runpy
from types import SimpleNamespace
import unittest

import test_device_identity as identity
import test_connection_recovery as recovery


CODEC = identity.load_codec()


def reg(offset, dtype="UINT16", permission="R", length=0):
    return {"offset": offset, "dtype": dtype, "permission": permission, "length": length}


def reply(words):
    return SimpleNamespace(isError=lambda: False, registers=words)


MIXED = [reg(0x100, "INT16"), reg(0x101, "UINT32"),
         reg(0x103, "INT32"), reg(0x105, "FLOAT"), reg(0x107, "STRING", length=3)]
VALUES = [-17, 0x12345678, -100000, 12.5, "ES2201"]
WORDS = [word for point, value in zip(MIXED, VALUES)
         for word in CODEC["encode_value"](point["dtype"], value, point["length"])]
EXPECTED = {point["offset"]: value for point, value in zip(MIXED, VALUES)}


class PlanTests(unittest.TestCase):
    def test_only_contiguous_readable_addresses_are_combined(self):
        points = [reg(5), reg(1), reg(0), reg(2, permission="W"), reg(3)]
        blocks = CODEC["build_read_blocks"](points)
        self.assertEqual([[r["offset"] for r in block] for block in blocks], [[0, 1], [3], [5]])
        self.assertEqual([r["offset"] for r in points], [5, 1, 0, 2, 3])

    def test_word_limit_never_splits_a_multiregister_value(self):
        points = [reg(i) for i in range(124)] + [reg(124, "FLOAT"), reg(126)]
        blocks = CODEC["build_read_blocks"](points)
        self.assertEqual([CODEC["block_range"](b) for b in blocks], [(0, 124), (124, 3)])
        exact = CODEC["build_read_blocks"]([reg(i) for i in range(126)])
        self.assertEqual([CODEC["block_range"](b) for b in exact], [(0, 125), (125, 1)])

    def test_mixed_values_decode_from_their_own_slices(self):
        self.assertEqual(CODEC["decode_block"](MIXED, WORDS), EXPECTED)
        with self.assertRaises(ValueError):
            CODEC["decode_block"](MIXED, WORDS[:-1])

    def test_overlap_is_not_merged_and_write_only_input_is_empty(self):
        blocks = CODEC["build_read_blocks"]([reg(0, "UINT32"), reg(1), reg(2)])
        self.assertEqual([CODEC["block_range"](b) for b in blocks], [(0, 2), (1, 2)])
        self.assertEqual(CODEC["build_read_blocks"]([reg(10, permission="W")]), [])

    def test_invalid_oversized_value_is_rejected(self):
        with self.assertRaises(ValueError):
            CODEC["build_read_blocks"]([reg(0, "STRING", length=126)])


class OwnBatchTests(unittest.TestCase):
    def setUp(self):
        harness = recovery.OwnClientRecoveryTests()
        harness.setUp()
        self.client, self.transport = harness.client, harness.transport

    def test_mixed_values_use_one_request_with_configured_address_and_unit(self):
        self.client.address_offset = 0
        self.client.unit_id = 7
        self.transport.read_holding_registers.return_value = reply(WORDS)
        self.assertEqual(self.client.read_all(MIXED), EXPECTED)
        self.transport.read_holding_registers.assert_called_once_with(0x100, 10, slave=7)

    def test_rejected_batch_falls_back_and_is_not_retried_next_poll(self):
        points = [reg(0), reg(1)]
        self.transport.read_holding_registers.side_effect = [
            recovery.ExceptionResponse(3, 2), reply([4]), reply([9]), reply([5]), reply([10])]
        self.assertEqual(self.client.read_all(points), {0: 4, 1: 9})
        self.assertEqual(self.client.read_all(points), {0: 5, 1: 10})
        self.assertEqual([call.args[:2] for call in self.transport.read_holding_registers.call_args_list],
                         [(40001, 2), (40001, 1), (40002, 1), (40001, 1), (40002, 1)])

    def test_short_batch_does_not_publish_partial_or_misaligned_values(self):
        self.transport.read_holding_registers.return_value = reply([123])
        self.assertEqual(self.client.read_all([reg(0), reg(1)]), {0: None, 1: None})
        self.assertEqual(self.transport.read_holding_registers.call_count, 1)
        self.assertFalse(self.client._single_read_blocks)

    def test_timeout_does_not_trigger_single_point_fallback(self):
        self.transport.read_holding_registers.side_effect = OSError("no response")
        self.transport.close.side_effect = lambda: setattr(self.transport, "connected", False)
        self.assertEqual(self.client.read_all([reg(0), reg(1), reg(5)]), {0: None, 1: None})
        self.assertEqual(self.transport.read_holding_registers.call_count, 1)
        self.assertFalse(self.client._single_read_blocks)

    def test_write_only_points_do_not_reach_transport(self):
        self.assertEqual(self.client.read_all([reg(0x25F, permission="W")]), {})
        self.transport.read_holding_registers.assert_not_called()


class SharedBatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        harness = recovery.LinkRecoveryTests()
        harness.setUp()
        self.link = harness.shared_link()
        self.ns = harness.ns

    async def test_mixed_values_use_one_shared_request(self):
        self.link._address_offset = 0
        self.link._unit.read_holding_registers.return_value = WORDS
        self.assertEqual(await self.link.read_all(MIXED), EXPECTED)
        self.link._unit.read_holding_registers.assert_awaited_once_with(0x100, 10)

    async def test_device_exception_fallback_is_remembered(self):
        points = [reg(0), reg(1)]
        self.link._unit.read_holding_registers.side_effect = [
            recovery.DeviceExceptionError(), [4], [9], [5], [10]]
        self.assertEqual(await self.link.read_all(points), {0: 4, 1: 9})
        self.assertEqual(await self.link.read_all(points), {0: 5, 1: 10})
        self.assertEqual([call.args for call in self.link._unit.read_holding_registers.await_args_list],
                         [(40001, 2), (40001, 1), (40002, 1), (40001, 1), (40002, 1)])

    async def test_short_batch_does_not_publish_partial_values(self):
        self.link._unit.read_holding_registers.return_value = [123]
        self.assertEqual(await self.link.read_all([reg(0), reg(1)]), {0: None, 1: None})
        self.assertFalse(self.link._single_read_blocks)
        self.assertEqual(self.link.last_response, 0)

    async def test_timeout_stops_poll_without_fallback_and_releases_lock(self):
        self.link._unit.read_holding_registers.side_effect = TimeoutError()
        self.assertEqual(await self.link.read_all([reg(0), reg(1), reg(5)]), {0: None, 1: None})
        self.assertEqual(self.link._unit.read_holding_registers.await_count, 1)
        self.assertFalse(self.link._single_read_blocks)
        self.assertFalse(self.link._lock.locked())

    async def test_cancellation_is_not_swallowed(self):
        self.link._unit.read_holding_registers.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.link.read_all([reg(0), reg(1)])
        self.assertFalse(self.link._lock.locked())


class PartitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_scan_keeps_blocks_together_and_refreshes_every_point_once(self):
        for prefix, requests in (("ES21", 28), ("ES22", 28), ("SN", 20)):
            with self.subTest(prefix=prefix):
                harness = identity.ProductTests()
                harness.setUp()
                coord = harness.coordinator(serial=prefix + "0019")
                self.assertEqual(len(coord._partitions), 5)
                polled = []
                for _ in range(len(coord._partitions)):
                    await coord._async_update_data()
                    polled.append(coord.link.read_all.await_args.args[0])
                self.assertEqual(sum(len(CODEC["build_read_blocks"](p)) for p in polled), requests)
                offsets = [r["offset"] for p in polled for r in p]
                expected = [r["offset"] for r in coord.registers if r["permission"] != "W"]
                self.assertCountEqual(offsets, expected)
                self.assertEqual(len(offsets), len(set(offsets)))
                self.assertNotIn(0x025F, offsets)

    async def test_actual_schema_is_decoded_identically_over_both_transports(self):
        schema = runpy.run_path(str(identity.COMPONENT / "registers_schema.py"))["REGISTERS"]
        points = [r for r in schema if "STREAM 5000" in r["products"] and r["permission"] != "W"]
        memory, expected = {}, {}
        for index, point in enumerate(points):
            value = ("ES220019" if point["dtype"] == "STRING" else
                     (-index if point["dtype"].startswith("INT") else index))
            expected[point["offset"]] = value
            words = CODEC["encode_value"](point["dtype"], value, point.get("length", 8))
            for i, word in enumerate(words):
                self.assertNotIn(point["offset"] + i, memory)
                memory[point["offset"] + i] = word
        def read(address, count, **kwargs):
            return [memory[address - 40001 + i] for i in range(count)]
        own = recovery.OwnClientRecoveryTests()
        own.setUp()
        own.transport.read_holding_registers.side_effect = lambda *a, **kw: reply(read(*a, **kw))
        self.assertEqual(own.client.read_all(points), expected)
        self.assertEqual(own.transport.read_holding_registers.call_count, 28)
        shared = recovery.LinkRecoveryTests()
        shared.setUp()
        link = shared.shared_link()
        link._unit.read_holding_registers.side_effect = read
        self.assertEqual(await link.read_all(points), expected)
        self.assertEqual(link._unit.read_holding_registers.await_count, 28)
