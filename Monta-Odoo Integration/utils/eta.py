# -*- coding: utf-8 -*-
import re
from datetime import datetime
from typing import Optional, Tuple

ISO_LIKE = re.compile(
    r'^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?)?([+-]\d{2}:?\d{2}|Z)?$',
    re.IGNORECASE
)

def normalize_iso_dt_to_naive_str(value: str) -> Optional[str]:
    """
    Parse many ISO-8601-ish strings into 'YYYY-MM-DD HH:MM:SS' (naive).
    Returns None if parsing fails.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        # try to strip tz and fractional seconds
        s2 = s.replace('T', ' ')
        s2 = re.sub(r'([+-]\d{2}:?\d{2})$', '', s2)  # remove trailing TZ if any
        s2 = s2.split('.')[0]  # drop fractional secs
        try:
            dt = datetime.strptime(s2, '%Y-%m-%d %H:%M:%S')
            return dt.strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return None

def pick_eta_from_payload(payload: dict) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (eta_dt_str, eta_text).
    - eta_dt_str: normalized 'YYYY-MM-DD HH:MM:SS' if a date/time was found
    - eta_text:   a human text such as 'Unknown' if present
    We search common ETA keys on the order and first shipment.
    """
    def get(d, *ks):
        cur = d or {}
        for k in ks:
            if isinstance(cur, dict) and k in cur:
                cur = cur.get(k)
            else:
                return None
        return cur

    # common keys seen across tenants
    candidates = [
        'ExpectedDelivery', 'ExpectedDeliveryDate',
        'EstimatedDelivery', 'EstimatedDeliveryDate',
        'PromisedDeliveryDate', 'ETA'
    ]

    # 1) top-level
    raw = None
    for k in candidates:
        v = get(payload, k)
        if v:
            raw = v
            break

    # 2) shipment-level (first)
    if not raw:
        ships = payload.get('Shipments') or payload.get('ShipmentList') or []
        if isinstance(ships, list) and ships:
            first = ships[0] or {}
            for k in candidates:
                v = first.get(k) or get(first, 'TrackAndTrace', k)
                if v:
                    raw = v
                    break

    if not raw:
        return None, None

    s = str(raw).strip()
    # If looks like a datetime, parse it
    if ISO_LIKE.match(s):
        dt_norm = normalize_iso_dt_to_naive_str(s)
        return (dt_norm, None) if dt_norm else (None, s)
    # Otherwise, treat as human text
    return None, s
