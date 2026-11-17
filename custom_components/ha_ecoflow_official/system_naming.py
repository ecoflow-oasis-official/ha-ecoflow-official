"""Persistent display numbering, independent of firmware grouping identity."""

from .const import CONF_SYSTEM_ID, DEFAULT_SYSTEM_ID, DOMAIN, SYSTEM_ENTRY, SYSTEM_NAME, SYSTEM_NUMBER

_RESERVATIONS = "_stream_system_numbers"


def system_name(number=1):
    return SYSTEM_NAME if number == 1 else f"{SYSTEM_NAME} ({number})"


def _valid_number(value):
    return type(value) is int and value > 0


def async_system_number(hass, group):
    """Number existing entries first and reserve new groups without I/O awaits.

    Keep allocated numbers when a system is offline or another system is
    removed. Reservations prevent simultaneous discovery from taking the
    same number before their config entries have been created.
    """
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get(SYSTEM_ENTRY)]
    entries.sort(key=lambda e: str(getattr(e, "created_at", "")))
    reserved = hass.data.setdefault(DOMAIN, {}).setdefault(_RESERVATIONS, {})
    used = {e.data[SYSTEM_NUMBER] for e in entries if _valid_number(e.data.get(SYSTEM_NUMBER))}
    used.update(reserved.values())
    seen = set()
    for entry in entries:
        entry_group = str(entry.data.get(CONF_SYSTEM_ID) or DEFAULT_SYSTEM_ID)
        number = entry.data.get(SYSTEM_NUMBER)
        if not _valid_number(number) or number in seen:
            number = reserved.get(entry_group)
            if number is None or number in seen:
                number = 1
                while number in used:
                    number += 1
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, SYSTEM_NUMBER: number})
        seen.add(number)
        used.add(number)
        reserved[entry_group] = number
    group = str(group)
    used = seen | set(reserved.values())
    if group not in reserved:
        number = 1
        while number in used:
            number += 1
        reserved[group] = number
    return reserved[group]
