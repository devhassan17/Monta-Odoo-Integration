# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from odoo import api, fields, models

from ..services.monta_client import MontaClient  # reuse your existing client

_logger = logging.getLogger(__name__)

DUMMY_ETA_STR = "2099-01-01 00:00:00"  # always UTC (server-naive)


def _norm_iso_dt(value) -> Optional[str]:
    """
    Normalize a Monta datetime into Odoo-safe string '%Y-%m-%d %H:%M:%S' (server-naive).
    Accepts:
      - '2025-08-16T01:49:23.42'
      - '2025-08-16T01:49:23.420123Z'
      - '2025-08-16T01:49:23+02:00'
      - datetime instances
    Returns string or None.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return fields.Datetime.to_string(value)
    s = str(value).strip()
    if not s:
        return None
    # Monta sometimes returns trailing Z = UTC
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
        # store as server-naive
        return fields.Datetime.to_string(dt.replace(tzinfo=None))
    except Exception:
        # fallback: strip fractional seconds and TZ if present
        try:
            s2 = s.replace('T', ' ').split('.')[0]
            dt = datetime.strptime(s2[:19], '%Y-%m-%d %H:%M:%S')
            return fields.Datetime.to_string(dt)
        except Exception:
            return None


class SaleOrderInbound(models.Model):
    _inherit = 'sale.order'

    # ---------------- ETA extraction ----------------
    def _monta__eta_from_body(self, body: Dict[str, Any]) -> Tuple[str, Dict[str, Any], bool]:
        """
        Decide which ETA we use for commitment_date.

        Priority (first non-empty after normalization):
          1) EstimatedDeliveryTo
          2) EstimatedDeliveryFrom
          3) DeliveryDate
          4) LatestDeliveryDate
          5) DeliveryDateRequested
          6) PlannedShipmentDate
          else -> DUMMY_ETA_STR

        Returns: (chosen_odoostr, raw_debug_dict, used_dummy_bool)
        """
        body = body or {}
        raw = {
            'EstimatedDeliveryFrom': body.get('EstimatedDeliveryFrom'),
            'EstimatedDeliveryTo': body.get('EstimatedDeliveryTo'),
            'DeliveryDate': body.get('DeliveryDate'),
            'LatestDeliveryDate': body.get('LatestDeliveryDate'),
            'DeliveryDateRequested': body.get('DeliveryDateRequested'),
            'PlannedShipmentDate': body.get('PlannedShipmentDate'),
            'Blocked': body.get('Blocked'),
            'BlockedMessage': body.get('BlockedMessage'),
            'Comment': body.get('Comment'),
        }

        ordered_keys = (
            'EstimatedDeliveryTo',
            'EstimatedDeliveryFrom',
            'DeliveryDate',
            'LatestDeliveryDate',
            'DeliveryDateRequested',
            'PlannedShipmentDate',
        )

        # Select first valid
        for k in ordered_keys:
            v = raw.get(k)
            if isinstance(v, str) and v.strip().lower() == 'unknown':
                continue
            norm = _norm_iso_dt(v)
            if norm:
                return norm, raw, False

        # Unknown / missing -> dummy
        return DUMMY_ETA_STR, raw, True

    # ---------------- Status & delivered ----------------
    @staticmethod
    def _monta__status_and_delivered(body: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        """
        Build a readable status and detect a likely 'delivered' timestamp.
        """
        body = body or {}
        status = (
            body.get('DeliveryStatusDescription')
            or body.get('DeliveryStatusCode')
            or body.get('Status')
            or body.get('State')
            or body.get('OrderStatus')
            or body.get('ActionCode')
        )

        ts_opts = [
            body.get('DeliveredAt'),
            body.get('Delivery', {}).get('DeliveredAt') if isinstance(body.get('Delivery'), dict) else None,
            body.get('DeliveryDate'),
            body.get('Shipped'),
            body.get('Picked'),
            body.get('Received'),
        ]
        delivered = None
        for t in ts_opts:
            norm = _norm_iso_dt(t)
            if norm:
                delivered = norm
                break

        if not status:
            if delivered:
                status = 'Delivered'
            elif body.get('Shipped'):
                status = 'Shipped'
            elif body.get('Picked'):
                status = 'Picked'
            elif body.get('Received'):
                status = 'Received'
            elif body.get('Backorder'):
                status = 'Backorder'
            else:
                status = 'Processing'

        return status, delivered

    # ---------------- Value mapping ----------------
    def _monta__vals_from_order_body(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compute values to apply to sale.order from Monta GET /order/{id} body.
        We keep it small and explicit.
        """
        vals: Dict[str, Any] = {}

        # Remote status & delivered_at
        status, delivered = self._monta__status_and_delivered(body)
        vals['monta_remote_status'] = (status or '').strip() or False
        if delivered:
            vals['monta_delivered_at'] = delivered

        # ETA -> commitment_date
        eta_str, _eta_raw, _eta_dummy = self._monta__eta_from_body(body)
        vals['commitment_date'] = eta_str

        # Keep sync flag (harmless if you push back to Monta elsewhere)
        vals['monta_needs_sync'] = True
        return vals

    # ---------------- Pull + apply ----------------
    def action_monta_pull_now(self, channel: Optional[str] = None) -> bool:
        """
        Pull latest state for each order from Monta, update commitment_date + status.
        Creates two logs per call:
          - 'Monta Pull': API status + small body excerpt
          - 'Monta ETA' : ETA decision (raw fields considered + chosen result)
        """
        for order in self:
            try:
                webshop_id = order.monta_order_id or order.name
                if not webshop_id:
                    continue

                path = f"/order/{webshop_id}"
                if channel:
                    path = f"{path}?channel={channel}"

                client = MontaClient(order.env)
                status, body = client.request(
                    order,
                    "GET",
                    path,
                    payload=None,
                    headers={"Accept": "application/json"},
                )

                # Log API pull with a trimmed body for visibility
                try:
                    order._create_monta_log(
                        {
                            'pull': {
                                'status': status,
                                'path': path,
                                'body_excerpt': (body if isinstance(body, dict) else {}),
                            }
                        },
                        level='info' if (200 <= (status or 0) < 300) else 'error',
                        tag='Monta Pull',
                        console_summary=f"[Monta Pull] GET {path} -> {status}",
                    )
                except Exception:
                    _logger.info("[Monta Pull] %s -> %s (log save failed but continuing)", path, status)

                if 200 <= (status or 0) < 300 and isinstance(body, dict):
                    # Decide ETA + gather vals
                    eta_str, eta_raw, eta_dummy = self._monta__eta_from_body(body)
                    vals = self._monta__vals_from_order_body(body)
                    # force the chosen ETA we just computed (safety)
                    vals['commitment_date'] = eta_str

                    # Persist ETA decision (human readable)
                    try:
                        order._create_monta_log(
                            {
                                'eta': {
                                    'chosen_for_commitment_date': eta_str,
                                    'used_dummy_2099': bool(eta_dummy),
                                    'raw_fields': eta_raw,
                                }
                            },
                            level='info',
                            tag='Monta ETA',
                            console_summary='[Monta ETA] decision saved',
                        )
                    except Exception:
                        _logger.info("[Monta ETA] log save failed for %s", order.name)

                    # Only write changed values
                    changes: Dict[str, Any] = {}
                    for k, v in vals.items():
                        if (order[k] or False) != (v or False):
                            changes[k] = v

                    if changes:
                        order.write(changes)
                        _logger.info("[Monta Pull] %s -> changed keys: %s", order.name, list(changes.keys()))
                    else:
                        _logger.info("[Monta Pull] %s -> no changes", order.name)
                else:
                    # Non-200 or unexpected body
                    try:
                        order._create_monta_log(
                            {'status': status, 'path': path, 'body': body or {}},
                            level='error',
                            tag='Monta Pull',
                            console_summary='[Monta Pull] non-200 status',
                        )
                    except Exception:
                        pass
            except Exception as e:
                _logger.error("[Monta Pull] Failure for %s: %s", order.name, e, exc_info=True)
        return True
