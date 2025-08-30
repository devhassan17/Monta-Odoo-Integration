# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime
from typing import Dict, Tuple, Any, Optional

from odoo import fields

from .monta_client import MontaClient  # reuse your existing client

_logger = logging.getLogger(__name__)


def _norm_iso_dt(value) -> Optional[str]:
    """
    Normalize a Monta datetime into Odoo-safe string '%Y-%m-%d %H:%M:%S' (server-tz naive).
    Accepts:
      - '2025-08-16T01:49:23.42'
      - '2025-08-16T01:49:23.420123Z'
      - '2025-08-16T01:49:23+02:00'
    Returns string or None.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return fields.Datetime.to_string(value)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
        return fields.Datetime.to_string(dt.replace(tzinfo=None))
    except Exception:
        # try removing fractional seconds and TZ
        try:
            s2 = s.replace('T', ' ').split('.')[0]
            dt = datetime.strptime(s2[:19], '%Y-%m-%d %H:%M:%S')
            return fields.Datetime.to_string(dt)
        except Exception:
            return None


class MontaInbound:
    """
    Service for GET /order/{webshoporderid} and mapping response → sale.order fields.
    - Supports `channel` query param
    - Robust to schema differences across tenants
    - Derives readable status when Monta omits one
    - Chooses ETA (commitment_date). If unknown → dummy 2099-01-01 00:00:00
    """

    DUMMY_ETA_STR = "2099-01-01 00:00:00"  # requested dummy when ETA is Unknown/missing

    def __init__(self, env):
        self.env = env

    # -------- HTTP --------
    def fetch_order(self, order, webshop_id: str, channel: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        """
        Calls Monta:
          GET /order/{webshoporderid}[?channel=...]
        """
        path = f"/order/{webshop_id}"
        if channel:
            path = f"{path}?channel={channel}"

        client = MontaClient(self.env)
        status, body = client.request(order, "GET", path, payload=None, headers={"Accept": "application/json"})

        order._create_monta_log(
            {
                'pull': {
                    'status': status,
                    'webshop_id': webshop_id,
                    'channel': channel,
                    'body_excerpt': (body if isinstance(body, dict) else {}),
                }
            },
            level='info' if (200 <= (status or 0) < 300) else 'error',
            tag='Monta Pull',
            console_summary=f"[Monta Pull] GET {path} -> {status}",
        )
        return status, body or {}

    # -------- Mapping helpers --------
    @staticmethod
    def _safe_get(d: dict, *keys, default=None):
        cur = d or {}
        for k in keys:
            if isinstance(cur, dict) and k in cur:
                cur = cur.get(k)
            else:
                return default
        return cur

    @staticmethod
    def _first_nonempty(*vals) -> Optional[str]:
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def _extract_tracking(self, payload: Dict[str, Any]):
        """
        Tracking fields differ per tenant. Try several shapes.
        """
        url = (
            self._safe_get(payload, 'TrackAndTraceLink')
            or self._safe_get(payload, 'TrackTraceUrl')
            or self._safe_get(payload, 'TrackAndTraceUrl')
        )
        number = (
            self._safe_get(payload, 'TrackAndTraceCode')
            or self._safe_get(payload, 'TrackingNumber')
        )
        carrier = (
            self._safe_get(payload, 'ShipperDescription')
            or self._safe_get(payload, 'CarrierName')
            or self._safe_get(payload, 'Carrier', 'Name')
            or self._safe_get(payload, 'ShipperCode')
        )

        ships = payload.get('Shipments') or payload.get('ShipmentList') or []
        if isinstance(ships, list) and ships:
            first = ships[0] or {}
            url = self._first_nonempty(
                url,
                first.get('TrackAndTraceLink'),
                first.get('TrackTraceUrl'),
                self._safe_get(first, 'TrackAndTrace', 'Url'),
            )
            number = self._first_nonempty(
                number,
                first.get('TrackAndTraceCode'),
                first.get('TrackingNumber'),
                self._safe_get(first, 'TrackAndTrace', 'Number'),
            )
            carrier = self._first_nonempty(
                carrier,
                first.get('ShipperDescription'),
                first.get('CarrierName'),
                self._safe_get(first, 'Carrier', 'Name'),
                first.get('ShipperCode'),
            )

        return number, url, carrier

    def _extract_status_and_dates(self, payload: Dict[str, Any]):
        """
        Build a readable status and detect a 'delivered' timestamp if present.
        """
        status = (
            self._safe_get(payload, 'DeliveryStatusDescription')
            or self._safe_get(payload, 'DeliveryStatusCode')
            or self._safe_get(payload, 'Status')
            or self._safe_get(payload, 'State')
            or self._safe_get(payload, 'OrderStatus')
            or self._safe_get(payload, 'ActionCode')
        )

        ts = {
            'delivered_at': (
                self._safe_get(payload, 'DeliveredAt')
                or self._safe_get(payload, 'Delivery', 'DeliveredAt')
                or self._safe_get(payload, 'DeliveryDate')
            ),
            'completed_at': self._safe_get(payload, 'CompletedAt'),
            'shipped_at': self._safe_get(payload, 'Shipped'),
            'picked_at': self._safe_get(payload, 'Picked'),
            'received_at': self._safe_get(payload, 'Received'),
        }

        # Derive a human status if Monta didn't provide one
        if not status:
            if ts['delivered_at'] or ts['completed_at']:
                status = 'Delivered'
            elif ts['shipped_at']:
                status = 'Shipped'
            elif ts['picked_at']:
                status = 'Picked'
            elif ts['received_at']:
                status = 'Received'
            elif self._safe_get(payload, 'Backorder'):
                status = 'Backorder'
            else:
                status = 'Processing'

        delivered = (
            ts['delivered_at']
            or ts['completed_at']
            or ts['shipped_at']
            or ts['picked_at']
            or ts['received_at']
        )
        return status, delivered

    def _extract_eta_for_commitment(self, payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any], bool]:
        """
        Choose ETA for Odoo sale.order.commitment_date.
        Returns (chosen_odoostr, raw_debug_dict, used_dummy_bool).

        Priority order (normalize first valid):
          1) EstimatedDeliveryFrom
          2) EstimatedDeliveryTo
          3) DeliveryDate
          4) LatestDeliveryDate
          5) DeliveryDateRequested
          6) PlannedShipmentDate
        If none found -> use DUMMY_ETA_STR ('2099-01-01 00:00:00').
        """
        raw = {
            'EstimatedDeliveryFrom': payload.get('EstimatedDeliveryFrom'),
            'EstimatedDeliveryTo': payload.get('EstimatedDeliveryTo'),
            'DeliveryDate': payload.get('DeliveryDate'),
            'LatestDeliveryDate': payload.get('LatestDeliveryDate'),
            'DeliveryDateRequested': payload.get('DeliveryDateRequested'),
            'PlannedShipmentDate': payload.get('PlannedShipmentDate'),
            'Blocked': payload.get('Blocked'),
            'BlockedMessage': payload.get('BlockedMessage'),
            'Comment': payload.get('Comment'),
        }

        for key in ('EstimatedDeliveryFrom',
                    'EstimatedDeliveryTo',
                    'DeliveryDate',
                    'LatestDeliveryDate',
                    'DeliveryDateRequested',
                    'PlannedShipmentDate'):
            cand = _norm_iso_dt(raw.get(key))
            if cand:
                return cand, raw, False

        # Unknown / missing -> dummy
        return self.DUMMY_ETA_STR, raw, True

    # -------- Apply to Odoo --------
    def apply_to_sale_order(self, order, payload: Dict[str, Any]):
        """
        Returns (changes_dict, human_summary_json).
        Also writes an extra 'Monta ETA' log with decision & raw fields.
        """
        number, url, carrier = self._extract_tracking(payload)
        status, delivered_at = self._extract_status_and_dates(payload)
        eta_odoostr, eta_raw, eta_dummy = self._extract_eta_for_commitment(payload)

        proposed = {
            'monta_tracking_number': number or False,
            'monta_tracking_url': url or False,
            'monta_carrier': carrier or False,
            'monta_remote_status': (status or '').strip() or False,
            'commitment_date': eta_odoostr,  # default delivery date field on sale.order
        }
        if delivered_at:
            proposed['monta_delivered_at'] = _norm_iso_dt(delivered_at)

        changes = {}
        for k, v in proposed.items():
            if (order[k] or False) != (v or False):
                changes[k] = v

        # Nice JSON summary for chatter/log
        summary = json.dumps(
            {
                'remote_status': status,
                'delivered_at': _norm_iso_dt(delivered_at) if delivered_at else None,
                'tracking_number': number,
                'tracking_url': url,
                'carrier': carrier,
                'eta_chosen': eta_odoostr,
                'eta_used_dummy': bool(eta_dummy),
                'diff_keys': list(changes.keys()),
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )

        # Dedicated ETA decision log (very explicit for debugging/visibility)
        try:
            order._create_monta_log(
                {
                    'eta': {
                        'chosen_for_commitment_date': eta_odoostr,
                        'used_dummy_2099': bool(eta_dummy),
                        'raw_fields': eta_raw,
                    }
                },
                level='info',
                tag='Monta ETA',
                console_summary='[Monta ETA] decision saved',
            )
        except Exception:
            pass

        _logger.info("[Monta Pull] %s -> changed keys: %s", order.name, list(changes.keys()))
        return changes, summary
