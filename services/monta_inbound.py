# -*- coding: utf-8 -*-
import json
import logging
from typing import Dict, Tuple, Any, Optional
from .monta_client import MontaClient  # reuse your existing client

_logger = logging.getLogger(__name__)


class MontaInbound:
    """
    Small service layer for GET /order/{webshoporderid} and mapping → sale.order fields.
    Designed to be tolerant to response shape differences across accounts.
    """

    def __init__(self, env):
        self.env = env

    # -------- http --------
    def fetch_order(self, order, webshop_id: str) -> Tuple[int, Dict[str, Any]]:
        """Call Monta GET /order/{webshoporderid} and persist request/response logs."""
        path = f"/order/{webshop_id}"
        client = MontaClient(self.env)
        # will auto-log request/response via MontaClient and sale log
        status, body = client.request(order, "GET", path, payload=None, headers={"Accept": "application/json"})
        # also put a concise service-level log
        order._create_monta_log({'pull': {'status': status, 'webshop_id': webshop_id, 'body_excerpt': (body if isinstance(body, dict) else {})}},
                                level='info' if (200 <= (status or 0) < 300) else 'error',
                                tag='Monta Pull',
                                console_summary=f"[Monta Pull] GET {path} -> {status}")
        return status, body or {}

    # -------- mapping helpers --------
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

    def _extract_tracking(self, payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Try multiple common shapes:

        - payload["TrackTraceUrl"]
        - payload["Carrier"]["Name"] or ["CarrierName"]
        - payload["Shipments"][0].TrackingNumber / TrackTraceUrl / CarrierName
        - payload["Shipment"]["TrackAndTrace"]...
        """
        # top-level
        url = self._safe_get(payload, 'TrackTraceUrl') or self._safe_get(payload, 'TrackAndTraceUrl')
        carrier = self._safe_get(payload, 'Carrier', 'Name') or self._safe_get(payload, 'CarrierName')

        number = self._safe_get(payload, 'TrackingNumber') \
                 or self._safe_get(payload, 'TrackAndTrace', 'Number')

        # common array form
        ships = payload.get('Shipments') or payload.get('ShipmentList') or []
        if isinstance(ships, list) and ships:
            first = ships[0] or {}
            url = self._first_nonempty(url,
                                       first.get('TrackTraceUrl'),
                                       self._safe_get(first, 'TrackAndTrace', 'Url'))
            number = self._first_nonempty(number,
                                          first.get('TrackingNumber'),
                                          self._safe_get(first, 'TrackAndTrace', 'Number'))
            carrier = self._first_nonempty(carrier,
                                           first.get('CarrierName'),
                                           self._safe_get(first, 'Carrier', 'Name'))

        return number, url, carrier

    def _extract_status_and_dates(self, payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """
        Pull a readable status and a delivered timestamp if present.
        Tries: Status/State/OrderState/DeliveredAt/ShipmentDate/CompletedAt.
        """
        status = (
            self._safe_get(payload, 'Status') or
            self._safe_get(payload, 'State') or
            self._safe_get(payload, 'OrderState') or
            self._safe_get(payload, 'OrderStatus')
        )
        delivered = (
            self._safe_get(payload, 'DeliveredAt') or
            self._safe_get(payload, 'Delivery', 'DeliveredAt') or
            self._safe_get(payload, 'CompletedAt') or
            self._safe_get(payload, 'ShipmentDate')
        )
        return status, delivered

    # -------- application --------
    def apply_to_sale_order(self, order, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """
        Return (changes, human_summary). Only returns changes for fields actually different.
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

        # compute only diffs
        changes = {}
        for k, v in proposed.items():
            if (order[k] or False) != (v or False):
                changes[k] = v

        # readable summary for chatter/log
        summary = json.dumps({
            'status': status,
            'delivered_at': delivered_at,
            'tracking_number': number,
            'tracking_url': url,
            'carrier': carrier,
            'diff_keys': list(changes.keys()),
        }, indent=2, ensure_ascii=False, default=str)

        _logger.info("[Monta Pull] Mapping summary for %s -> keys changed: %s", order.name, list(changes.keys()))
        return changes, summary
