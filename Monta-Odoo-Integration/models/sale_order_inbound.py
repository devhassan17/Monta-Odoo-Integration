# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from odoo import fields, models

from ..services.monta_client import MontaClient
from ..services.monta_status_normalizer import MontaStatusNormalizer

_logger = logging.getLogger(__name__)

DUMMY_ETA_STR = "2099-01-01 00:00:00"  # always UTC (server-naive)


def _norm_iso_dt(value) -> Optional[str]:
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
        try:
            s2 = s.replace('T', ' ').split('.')[0]
            dt = datetime.strptime(s2[:19], '%Y-%m-%d %H:%M:%S')
            return fields.Datetime.to_string(dt)
        except Exception:
            return None


def _pretty(dt_str: Optional[str]) -> str:
    if not dt_str:
        return ''
    try:
        y, m, d = int(dt_str[0:4]), int(dt_str[5:7]), int(dt_str[8:10])
        hh, mm, ss = int(dt_str[11:13]), int(dt_str[14:16]), int(dt_str[17:19])
        return f"{d:02d}/{m:02d}/{y:04d} {hh:02d}:{mm:02d}:{ss:02d}"
    except Exception:
        return dt_str or ''


class SaleOrderInbound(models.Model):
    _inherit = 'sale.order'

    # ---------------- ETA extraction ----------------
    def _monta__eta_from_body(self, body: Dict[str, Any]) -> Tuple[str, Dict[str, Any], bool]:
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
        for k in ordered_keys:
            v = raw.get(k)
            if isinstance(v, str) and v.strip().lower() == 'unknown':
                continue
            norm = _norm_iso_dt(v)
            if norm:
                return norm, raw, False
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
        Only touch default fields:
          - commitment_date (EDD)
          - monta_needs_sync (existing boolean in your model)
        """
        vals: Dict[str, Any] = {}
        eta_str, _eta_raw, _eta_dummy = self._monta__eta_from_body(body)
        vals['commitment_date'] = eta_str
        vals['monta_needs_sync'] = True
        return vals

    # ---------------- Pull + apply + STORE STATUS (separate model) ----------------
    def action_monta_pull_now(self, channel: Optional[str] = None) -> bool:
        for order in self:
            try:
                webshop_id = order.monta_order_id or order.name
                if not webshop_id:
                    continue

                path = f"/order/{webshop_id}"
                if channel:
                    path = f"{path}?channel={channel}"

                # Step: Request Sent To Monta
                order._create_monta_log(
                    {'edd_auto': {'step': 'Request Sent To Monta', 'path': path}},
                    level='info', tag='Monta EDD', console_summary='[EDD] Request Sent To Monta'
                )

                client = MontaClient(order.env)
                status, body = client.request(
                    order, "GET", path, payload=None, headers={"Accept": "application/json"}
                )

                # Log API pull
                try:
                    order._create_monta_log(
                        {'pull': {'status': status, 'path': path, 'body_excerpt': (body if isinstance(body, dict) else {})}},
                        level='info' if (200 <= (status or 0) < 300) else 'error',
                        tag='Monta Pull', console_summary=f"[Monta Pull] GET {path} -> {status}",
                    )
                except Exception:
                    pass

                if 200 <= (status or 0) < 300 and isinstance(body, dict):
                    # --- ETA ---
                    eta_str, eta_raw, eta_dummy = self._monta__eta_from_body(body)
                    vals = self._monta__vals_from_order_body(body)
                    vals['commitment_date'] = eta_str

                    order._create_monta_log(
                        {'edd_auto': {'step': 'Date Get', 'eta_raw': eta_raw, 'chosen': eta_str, 'used_dummy_2099': bool(eta_dummy)}},
                        level='info', tag='Monta EDD', console_summary=f"[EDD] Date Get: {eta_str}"
                    )

                    # Only write changed values
                    changes: Dict[str, Any] = {}
                    for k, v in vals.items():
                        if k in order._fields and (order[k] or False) != (v or False):
                            changes[k] = v

                    if changes:
                        before = order.commitment_date
                        order.write(changes)
                        order._create_monta_log(
                            {'edd_auto': {
                                'step': 'Date is added to Commitment date',
                                'from': before, 'to': order.commitment_date, 'pretty': _pretty(order.commitment_date)}},
                            level='info', tag='Monta EDD',
                            console_summary=f"[EDD] Added to Commitment date: {order.commitment_date}"
                        )
                    else:
                        order._create_monta_log(
                            {'edd_auto': {'step': 'Date is added to Commitment date', 'note': 'no change'}},
                            level='info', tag='Monta EDD', console_summary='[EDD] Commitment date unchanged'
                        )

                    # --- STATUS (SEPARATE MODEL) ---
                    stat_raw, delivered = self._monta__status_and_delivered(body)
                    stat_norm = MontaStatusNormalizer.normalize(stat_raw)
                    self.env['monta.order.status'].sudo().create({
                        'sale_order_id': order.id,
                        'status_raw': (stat_raw or '').strip(),
                        'status_normalized': stat_norm,
                        'delivered_at': delivered or False,
                        'notes': json.dumps({'channel': channel or '', 'path': path}, ensure_ascii=False),
                    })
                    order._create_monta_log(
                        {'status_store': {'raw': stat_raw, 'normalized': stat_norm, 'delivered_at': delivered}},
                        level='info', tag='Monta Status', console_summary=f"[Status] {stat_norm} ({stat_raw})"
                    )

                    # Step: Date is showing (ready for UI)
                    order._create_monta_log(
                        {'edd_auto': {
                            'step': 'Date is showing',
                            'value': order.commitment_date,
                            'pretty': _pretty(order.commitment_date),
                            'order_url_hint': f"/odoo/sales/{order.id}",
                        }},
                        level='info', tag='Monta EDD', console_summary='[EDD] Date is showing'
                    )
                else:
                    order._create_monta_log(
                        {'edd_auto': {'step': 'Date Get', 'error': f"HTTP {status}"}},
                        level='error', tag='Monta EDD', console_summary=f"[EDD] Date Get failed: {status}"
                    )

            except Exception as e:
                _logger.error("[Monta Pull] Failure for %s: %s", order.name, e, exc_info=True)
        return True
