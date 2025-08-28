# -*- coding: utf-8 -*-
import json
import logging
from typing import Dict, Tuple, Any, Optional

from .monta_client import MontaClient  # reuse your existing client
from .monta_status_normalizer import MontaStatusNormalizer  # NEW
from .monta_inbound_batches import MontaInboundBatches      # NEW

_logger = logging.getLogger(__name__)


class MontaInbound:
    """
    Service for GET /order/{webshoporderid} and mapping response → sale.order fields.
    - Supports `channel` query param
    - Robust to schema differences across tenants
    - Derives readable status when Monta omits one
    - NEW: Normalizes status and persists batch/expiry rows
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

        # Prefer a concrete 'delivered' timestamp
        delivered = (
            ts['delivered_at']
            or ts['completed_at']
            or ts['shipped_at']
            or ts['picked_at']
            or ts['received_at']
        )
        return status, delivered

    # -------- Apply to Odoo --------
    def apply_to_sale_order(self, order, payload: Dict[str, Any]):
        """
        Returns (changes_dict, human_summary_json).
        Also (NEW):
          - sets monta_status_normalized via MontaStatusNormalizer
          - writes batch/expiry rows into monta.order.batch.trace
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

        # NEW: normalized status
        normalized = MontaStatusNormalizer.normalize(status)
        if normalized:
            proposed['monta_status_normalized'] = normalized

        changes = {}
        for k, v in proposed.items():
            if (order[k] or False) != (v or False):
                changes[k] = v

        # NEW: persist batches/expiry rows (non-blocking; errors only logged)
        try:
            MontaInboundBatches(self.env).sync_for_order(order, payload if isinstance(payload, dict) else {})
        except Exception as e:
            _logger.error("[Monta Pull] Batch sync failed for %s: %s", order.name, e, exc_info=True)

        summary = json.dumps(
            {
                'remote_status': status,
                'status_normalized': normalized,  # NEW
                'delivered_at': delivered_at,
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
