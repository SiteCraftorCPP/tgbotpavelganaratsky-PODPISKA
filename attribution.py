"""Парсинг deep-link ?start=... (латиница, цифры, _, -) и правило разделения через __."""

import re
from typing import Optional, Tuple

# Telegram ограничивает start-parameter; допускаем типичный набор символов
_START_PAYLOAD_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def sanitize_start_payload(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if len(value) > 64:
        return None
    if _START_PAYLOAD_PATTERN.match(value) is None:
        return None
    return value


def parse_utm_payload(payload: str) -> Tuple[str, str, str, str]:
    """
    Двойное подчёркивание __ разделяет части метки.

    Одна часть → всё сохраняем как сырое (source = payload, без разбиения).

    Две части → source и campaign.

    Три и больше → source, medium, campaign (оставшиеся через __ объединены в campaign).
    Возвращает (source, medium, campaign, raw).
    medium пустая строка означает «не задавали» для 2-частной схемы.
    """
    raw = payload.strip()
    if not raw:
        return ("", "", "", "")

    parts = raw.split("__")
    if len(parts) == 1:
        return (parts[0], "", "", raw)
    if len(parts) == 2:
        return (parts[0], "", parts[1], raw)

    tail = "__".join(parts[2:])
    return (parts[0], parts[1], tail, raw)

