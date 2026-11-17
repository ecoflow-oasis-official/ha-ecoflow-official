"""Separate STREAM device points from the persistent virtual system."""

import logging

from .const import SYSTEM_GROUP_OFFSET

LOGGER = logging.getLogger(__name__)
MASTER_FLAG_OFFSET = 0x1001
STREAM_PRODUCTS = {"STREAM 5000", "STREAM AC 5000"}


def _valid_role(value):
    return type(value) is int and value in (0, 1)


async def async_prepare_system_registers(hass, coordinator):
    """Read the role before setup and expose only D entities on STREAM units."""
    if coordinator.product not in STREAM_PRODUCTS:
        return
    role_reg = next(r for r in coordinator.registers if r["offset"] == MASTER_FLAG_OFFSET)
    role = (coordinator.data or {}).get(MASTER_FLAG_OFFSET)
    try:
        latest = await coordinator.link.read_one(role_reg)
    except Exception:
        LOGGER.debug("Could not read system master flag", exc_info=True)
        latest = None
    if _valid_role(latest):
        role = latest
    if not _valid_role(role):
        role = None
    coordinator._stream_role = role
    group_reg = next((r for r in coordinator.registers if r["offset"] == SYSTEM_GROUP_OFFSET), None)
    group = getattr(coordinator, "_stream_group_id", None)
    if group_reg is not None:
        try:
            group = await coordinator.link.read_one(group_reg)
        except Exception:
            LOGGER.debug("Could not read system group ID", exc_info=True)
    coordinator._stream_group_id = (
        group if type(group) is int and 0 < group <= 0xFFFFFFFF else None)
    if coordinator._stream_group_id is not None:
        coordinator._data_cache[SYSTEM_GROUP_OFFSET] = coordinator._stream_group_id
    coordinator._stream_system_values = {}
    if role is not None:
        coordinator._data_cache[MASTER_FLAG_OFFSET] = role
        coordinator.data = dict(coordinator._data_cache)
    # Retain full polling definitions for role monitoring and system writes.
    coordinator.registers = [r for r in coordinator.registers
                             if not r.get("region", "").startswith("S")]
