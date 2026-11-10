"""Separate STREAM device points from the persistent virtual system."""

import logging

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
    coordinator._stream_system_values = {}
    if role is not None:
        coordinator._data_cache[MASTER_FLAG_OFFSET] = role
        coordinator.data = dict(coordinator._data_cache)
    # Retain full polling definitions for role monitoring and system writes.
    coordinator.registers = [r for r in coordinator.registers
                             if not r.get("region", "").startswith("S")]
