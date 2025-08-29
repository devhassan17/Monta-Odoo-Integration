# -*- coding: utf-8 -*-
import json
import logging
import re
from datetime import datetime
from typing import Dict, Tuple, Any, Optional

from .monta_client import MontaClient  # reuse your existing client

_logger = logging.getLogger(__name__)


class MontaInbound:
    """
    Service for GET /order/{webshoporderid} and mapping response → sale.order fields.
    - Supports `channel` query param
    - Robust to schema differences across tenants
    - Derives readable status when Monta omits one
    - Stores Expected Delivery into sale.order.commitment_date
    """

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
        Tracking fields differ per tenant. Try several shapes:

        Top-level (sample and variants):
          - TrackAndTraceLink / TrackTraceUrl / TrackAndTraceUrl
          - TrackAndTraceCode / TrackingNumber
          - ShipperDescription / CarrierName / Carrier.Name / ShipperCode

        Array forms:
          - Shipments[0].TrackAndTraceLink / TrackTraceUrl / TrackAndTrace.Url
          - Shipments[0].TrackAndTraceCode / TrackingNumber / TrackAndTrace.Number
          - Shipments[0].ShipperDescription / CarrierName / Carrier.Name
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

        Tries a wide set of fields used by different tenants, then derives
        a friendly status based on available timestamps / flags.
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

    # ---- ETA helpers ----
    _ISO_LIKE = re.compile(
        r'^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?)?([+-]\d{2}:?\d{2}|Z)?$',
        re.IGNORECASE
    )

    def _extract_eta(self, payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """
        Returns (eta_dt_str, eta_text).
        - eta_dt_str: normalized 'YYYY-MM-DD HH:MM:SS' if a date/time was found
        - eta_text:   a human text such as 'Unknown' if present

        We search common ETA keys on the order and then the first shipment.
        """
        def get(d, *ks):
            cur = d or {}
            for k in ks:
                if isinstance(cur, dict) and k in cur:
                    cur = cur.get(k)
                else:
                    return None
            return cur

        candidates = [
            'ExpectedDelivery', 'ExpectedDeliveryDate',
            'EstimatedDelivery', 'EstimatedDeliveryDate',
            'PromisedDeliveryDate', 'ETA'
        ]

        # Top-level keys
        raw = None
        for k in candidates:
            v = get(payload, k)
            if v:
                raw = v
                break

        # Shipment-level (first)
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
        if self._ISO_LIKE.match(s):
            # normalize to 'YYYY-MM-DD HH:MM:SS'
            if s.endswith('Z'):
                s = s[:-1] + '+00:00'
            try:
                dt = datetime.fromisoformat(s)
                return dt.replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S'), None
            except Exception:
                # strip tz/fractions and retry
                s2 = s.replace('T', ' ')
                s2 = re.sub(r'([+-]\d{2}:?\d{2})$', '', s2)
                s2 = s2.split('.')[0]
                try:
                    dt = datetime.strptime(s2, '%Y-%m-%d %H:%M:%S')
                    return dt.strftime('%Y-%m-%d %H:%M:%S'), None
                except Exception:
                    return None, s
        # not a datetime → treat as text
        return None, s

    # -------- Apply to Odoo --------
    def apply_to_sale_order(self, order, payload: Dict[str, Any]):
        """
        Returns (changes_dict, human_summary_json).
        """
        number, url, carrier = self._extract_tracking(payload)
        status, delivered_at = self._extract_status_and_dates(payload)
        eta_dt, eta_text = self._extract_eta(payload)  # NEW

        proposed = {
            'monta_tracking_number': number or False,
            'monta_tracking_url': url or False,
            'monta_carrier': carrier or False,
            'monta_remote_status': (status or '').strip() or False,
        }
        if delivered_at:
            proposed['monta_delivered_at'] = delivered_at
        # Store ETA into Odoo's standard Delivery Date on sale.order
        if eta_dt:
            proposed['commitment_date'] = eta_dt
        elif eta_text:
            # keep a log so you can see e.g. "Unknown" without custom fields
            try:
                order._create_monta_log({'eta_text': eta_text}, level='info', tag='Monta ETA',
                                        console_summary=f"[Monta ETA] {eta_text}")
            except Exception:
                pass

        changes = {}
        for k, v in proposed.items():
            if (order[k] or False) != (v or False):
                changes[k] = v

        summary = json.dumps(
            {
                'remote_status': status,
                'delivered_at': delivered_at,
                'eta_dt->commitment_date': eta_dt,
                'eta_text_logged': eta_text if (eta_text and not eta_dt) else None,
                'tracking_number': number,
                'tracking_url': url,
                'carrier': carrier,
                'diff_keys': list(changes.keys()),
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )

        _logger.info("[Monta Pull] %s -> changed keys: %s", order.name, list(changes.keys()))
        return changes, summary
