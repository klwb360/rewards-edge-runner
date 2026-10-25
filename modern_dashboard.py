"""Read only daily-set fields from JSON carried by Next.js React Flight scripts."""

from __future__ import annotations

from datetime import datetime
import json
import re


_PUSH = re.compile(r'\bself\s*\.\s*__next_f\s*\.\s*push\s*\(\s*')
_RECORD = re.compile(r'^[0-9a-fA-F]+:(.*)$')


def _reject_constant(value):
    raise ValueError(f'Non-JSON constant: {value}')


def _date_key(value) -> str | None:
    if not isinstance(value, str):
        return None
    for pattern in ('%m/%d/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, pattern).strftime('%m/%d/%Y')
        except ValueError:
            continue
    return None


def _item_lists(value, field):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            rows = item.get(field)
            if isinstance(rows, list):
                yield rows
            pending.extend(reversed([child for key, child in item.items()
                                     if key != field]))
        elif isinstance(item, list):
            pending.extend(reversed(item))


def _normalize_item(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    offer_id = item.get('offerId')
    if not isinstance(offer_id, str) or not offer_id.strip():
        return None
    title = item.get('title')
    destination = item.get('destination')
    completed = item.get('isCompleted')
    return {
        'offerId': offer_id,
        'title': title if isinstance(title, str) and title else offer_id,
        'destinationUrl': destination if isinstance(destination, str) else '',
        'complete': completed if type(completed) is bool else None,
        'promotionType': 'urlreward',
        '__source': 'modern',
        '__locked': item.get('isLocked') is True or item.get('isUnlocked') is False,
    }


def decode_flight_records(scripts: list[str], *, strict: bool = False) -> list:
    """Decode data records only; never evaluate the surrounding JavaScript.

    React's non-JSON control records are ignored. Strict callers can distinguish
    malformed data from a supported model which simply lacks an optional feature.
    """
    decoder = json.JSONDecoder(parse_constant=_reject_constant)
    chunks = []
    for script in scripts:
        if not isinstance(script, str):
            continue
        for match in _PUSH.finditer(script):
            try:
                argument, end = decoder.raw_decode(script, match.end())
            except (ValueError, RecursionError):
                if strict:
                    raise ValueError('Malformed Flight push argument') from None
                continue
            if not script[end:].lstrip().startswith(')'):
                if strict:
                    raise ValueError('Flight push argument is not standalone JSON')
                continue
            if (isinstance(argument, list) and len(argument) == 2
                    and type(argument[0]) is int and argument[0] == 1
                    and isinstance(argument[1], str)):
                chunks.append(argument[1])
            elif strict and isinstance(argument, list) and argument and argument[0] == 1:
                raise ValueError('Malformed Flight data frame')

    values = []
    for line in ''.join(chunks).splitlines():
        record = _RECORD.fullmatch(line)
        if record is None:
            continue
        payload = record.group(1)
        # Module, preload, text, and other typed Flight control records are not JSON.
        if payload and payload[0].isupper():
            continue
        try:
            values.append(json.loads(payload, parse_constant=_reject_constant))
        except (ValueError, RecursionError):
            if strict:
                raise ValueError('Malformed Flight JSON record') from None
    return values


def parse_modern_dashboard(scripts: list[str]) -> dict | None:
    """Read daily-set and earn cards, copying no account data or activity hashes."""

    found = False
    found_activities = False
    promotions = {}
    activities = []
    for value in decode_flight_records(scripts):
        for daily in _item_lists(value, 'dailySetItems'):
            found = True
            for item in daily:
                if not isinstance(item, dict):
                    continue
                day = _date_key(item.get('date'))
                normalized = _normalize_item(item)
                if day is None or normalized is None:
                    continue
                promotions.setdefault(day, []).append(normalized)
        for cards in _item_lists(value, 'activityCards'):
            found_activities = True
            for item in cards:
                normalized = _normalize_item(item)
                if normalized is None:
                    continue
                points = item.get('points')
                if type(points) is not int or points <= 0 or item.get('isPromotional') is True:
                    continue
                activities.append(normalized | {'__page': 'earn', 'points': points})
    if not found and not found_activities:
        return None
    result = {'dailySetPromotions': promotions}
    if found_activities:
        result['activityPromotions'] = activities
    return result
