# -*- coding: utf-8 -*-
import json
import logging
from typing import Dict, Tuple, Any, Optional

from .monta_client import MontaClient  # reuse your existing client

_logger = logging.getLogger(__name__)


class MontaInbound:
    """
    Service for GET /order/{webshoporderid} and mapping response → sale.order fields.
    Robust to minor response shape changes. Includes 'channel' support.
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
            {'pull': {'status': status, 'webshop_id': webshop_id, 'channel': channel,
                      'body_excerpt': (body if isinstance(body, dict) else {})}},
            level='info' if (200 <= (status or 0) < 300) else 'error',
            tag='Monta Pull',
            console_summary=f"[Monta Pull] GET {path} -> {status}"
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
        Support the sample schema and common variants.
        Sample shows: TrackAndTraceLink, TrackAndTraceCode, ShipperDescription.
        """
        url = self._safe_get(payload, 'TrackAndTraceLink') or self._safe_get(payload, 'TrackTraceUrl') or self._safe_get(payload, 'TrackAndTraceUrl')
        number = self._safe_get(payload, 'TrackAndTraceCode') or self._safe_get(payload, 'TrackingNumber')
        carrier = self._safe_get(payload, 'ShipperDescription') or self._safe_get(payload, 'CarrierName') or self._safe_get(payload, 'Carrier', 'Name')

        ships = payload.get('Shipments') or payload.get('ShipmentList') or []
        if isinstance(ships, list) and ships:
            first = ships[0] or {}
            url = self._first_nonempty(url, first.get('TrackAndTraceLink'), first.get('TrackTraceUrl'), self._safe_get(first, 'TrackAndTrace', 'Url'))
            number = self._first_nonempty(number, first.get('TrackAndTraceCode'), first.get('TrackingNumber'), self._safe_get(first, 'TrackAndTrace', 'Number'))
            carrier = self._first_nonempty(carrier, first.get('ShipperDescription'), first.get('CarrierName'), self._safe_get(first, 'Carrier', 'Name'))

        return number, url, carrier

    def _extract_status_and_dates(self, payload: Dict[str, Any]):
        """
        Build a readable status and detect a 'delivered' timestamp if present.
        Supports multiple common fields returned by Monta tenants.
        """
        status = (
            self._safe_get(payload, 'DeliveryStatusDescription') or
            self._safe_get(payload, 'DeliveryStatusCode') or
            self._safe_get(payload, 'Status') or
            self._safe_get(payload, 'State') or
            self._safe_get(payload, 'OrderStatus') or
            self._safe_get(payload, 'ActionCode')
        )
        delivered = (
            self._safe_get(payload, 'DeliveredAt') or
            self._safe_get(payload, 'Delivery', 'DeliveredAt') or
            self._safe_get(payload, 'DeliveryDate') or
            self._safe_get(payload, 'CompletedAt') or
            self._safe_get(payload, 'Shipped') or
            self._safe_get(payload, 'Picked') or
            self._safe_get(payload, 'Received')
        )
        return status, delivered

    # -------- Apply to Odoo --------
    def apply_to_sale_order(self, order, payload: Dict[str, Any]):
        """
        Returns (changes_dict, human_summary_json).
        """
        number, url, carrier = self._extract_tracking(payload)
        status, delivered_at = self._extract_status_and_dates(payload)

        proposed = {
            'monta_tracking_number': number or False,
            'monta_tracking_url': url or False,
            'monta_carrier': carrier or False,
            'monta_remote_status': (status or '').strip() or False,
        }
        if delivered_at:
            proposed['monta_delivered_at'] = delivered_at

        changes = {}
        for k, v in proposed.items():
            if (order[k] or False) != (v or False):
                changes[k] = v

        summary = json.dumps(
            {
                'remote_status': status,
                'delivered_at': delivered_at,
                'tracking_number': number,
                'tracking_url': url,
                'carrier': carrier,
                'diff_keys': list(changes.keys()),
            },
            indent=2, ensure_ascii=False, default=str
        )

        _logger.info("[Monta Pull] %s -> changed keys: %s", order.name, list(changes.keys()))
        return changes, summary
