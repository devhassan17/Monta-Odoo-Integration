# -*- coding: utf-8 -*-
import logging
import re
from datetime import datetime, timedelta

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

PULL_MIN_GAP_SECONDS = 60  # throttle repeated pulls

# -------- Dummy ETA policy (used when Monta returns no ETA / "Unknown") --------
# Options:
#   ("relative_days", 30)  -> now() + 30 days
#   ("fixed", "2099-01-01 00:00:00") -> fixed far future
DUMMY_ETA_POLICY = ("fixed", "2099-01-01 00:00:00")


def _normalize_monta_dt(value):
    if not value:
        return False
    if isinstance(value, datetime):
        return fields.Datetime.to_string(value)

    s = str(value).strip()
    if not s:
        return False

    if s.endswith('Z'):
        s = s[:-1] + '+00:00'

    try:
        dt = datetime.fromisoformat(s)
        return fields.Datetime.to_string(dt.replace(tzinfo=None))
    except Exception:
        pass

    s2 = s.replace('T', ' ')
    s2 = re.sub(r'([+-]\d{2}:?\d{2})$', '', s2)
    s2 = s2.split('.')[0]
    try:
        dt = datetime.strptime(s2, '%Y-%m-%d %H:%M:%S')
        return fields.Datetime.to_string(dt)
    except Exception:
        return False


def _pick_dummy_eta():
    kind, val = DUMMY_ETA_POLICY
    if kind == "fixed":
        return _normalize_monta_dt(val)
    return fields.Datetime.to_string(fields.Datetime.now() + timedelta(days=int(val or 30)))


def _extract_eta_payload(payload: dict):
    candidates = [
        ("DeliveryDate", payload.get("DeliveryDate")),
        ("EstimatedDeliveryFrom", payload.get("EstimatedDeliveryFrom")),
        ("EstimatedDeliveryTo", payload.get("EstimatedDeliveryTo")),
        ("LatestDeliveryDate", payload.get("LatestDeliveryDate")),
        ("PlannedShipmentDate", payload.get("PlannedShipmentDate")),
    ]

    for source, raw in candidates:
        if raw is None:
            continue
        if isinstance(raw, str) and raw.strip().lower() in ("unknown", "-", "n/a", "na", "none"):
            return (None, source, "Unknown")
        raw_s = str(raw).strip()
        if raw_s:
            return (raw_s, source, None)

    return (None, None, "Unknown")


class SaleOrderInbound(models.Model):
    _inherit = 'sale.order'

    monta_remote_status = fields.Char(copy=False, index=True)
    monta_tracking_number = fields.Char(copy=False, index=True)
    monta_tracking_url = fields.Char(copy=False)
    monta_carrier = fields.Char(copy=False)
    monta_delivered_at = fields.Datetime(copy=False)
    monta_last_pull = fields.Datetime(copy=False)

    def action_monta_pull_now(self, channel=None):
        from ..services.monta_inbound import MontaInbound
        svc = MontaInbound(self.env)

        for order in self:
            try:
                webshop_id = order.monta_order_id or order.name
                if not webshop_id:
                    _logger.info("[Monta Pull] Skip %s: no webshop id/name", order.display_name)
                    continue

                if order.monta_last_pull:
                    delta = fields.Datetime.now() - order.monta_last_pull
                    if delta.total_seconds() < PULL_MIN_GAP_SECONDS:
                        _logger.info("[Monta Pull] Throttled for %s (last %.0fs ago)",
                                     order.name, delta.total_seconds())
                        continue

                status, body = svc.fetch_order(order, webshop_id, channel=channel)
                order.write({'monta_last_pull': fields.Datetime.now()})

                if not (200 <= int(status or 0) < 300):
                    continue

                payload = body.get('Order', body) if isinstance(body, dict) else {}
                changes, summary = svc.apply_to_sale_order(order, payload)

                eta_raw, eta_source, eta_text = _extract_eta_payload(payload)

                if eta_raw:
                    eta_norm = _normalize_monta_dt(eta_raw)
                    if eta_norm:
                        if (order.commitment_date or False) != eta_norm:
                            changes['commitment_date'] = eta_norm
                        order._create_monta_log(
                            {'eta': {'source': eta_source, 'raw': eta_raw, 'normalized': eta_norm, 'policy': 'real'}},
                            level='info', tag='Monta ETA',
                            console_summary='[Monta ETA] real ETA applied to commitment_date'
                        )
                    else:
                        dummy = _pick_dummy_eta()
                        if (order.commitment_date or False) != dummy:
                            changes['commitment_date'] = dummy
                        order._create_monta_log(
                            {'eta': {'source': eta_source, 'raw': eta_raw, 'normalized': False,
                                     'policy': 'dummy_on_parse_error', 'dummy_applied': dummy}},
                            level='info', tag='Monta ETA',
                            console_summary='[Monta ETA] parse failed → dummy commitment_date'
                        )
                else:
                    dummy = _pick_dummy_eta()
                    if (order.commitment_date or False) != dummy:
                        changes['commitment_date'] = dummy
                    order._create_monta_log(
                        {'eta': {'source': eta_source, 'text': eta_text or 'Unknown',
                                 'policy': 'dummy_on_unknown', 'dummy_applied': dummy}},
                        level='info', tag='Monta ETA',
                        console_summary='[Monta ETA] unknown → dummy commitment_date'
                    )

                if changes:
                    if 'monta_delivered_at' in changes:
                        changes['monta_delivered_at'] = _normalize_monta_dt(changes['monta_delivered_at'])
                    order.write(changes)
                    try:
                        order.message_post(body="<b>Monta inbound update</b><br/><pre>%s</pre>" % summary)
                    except Exception:
                        pass
                    order._create_monta_log(
                        {'apply_changes': changes}, level='info', tag='Monta Pull',
                        console_summary='[Monta Pull] updated fields'
                    )
                else:
                    order._create_monta_log(
                        {'note': 'No applicable changes from Monta response'},
                        level='info', tag='Monta Pull',
                        console_summary='[Monta Pull] no changes'
                    )

            except Exception as e:
                _logger.error("[Monta Pull] Failure for %s: %s", order.name, e, exc_info=True)
        return True

    @api.model
    def cron_monta_pull_open_orders(self, batch_size=30):
        dom = [
            ('state', 'in', ('sale', 'done')),
            ('state', '!=', 'cancel'),
            '|', ('monta_order_id', '!=', False), ('name', '!=', False),
        ]
        orders = self.search(dom, limit=batch_size, order='write_date desc')
        _logger.info("[Monta Pull] Cron scanning %s order(s)", len(orders))

        pulled = 0
        for so in orders:
            try:
                so.action_monta_pull_now()
                pulled += 1
            except Exception as e:
                _logger.error("[Monta Pull] Cron failed for %s: %s", so.name, e, exc_info=True)

        _logger.info("[Monta Pull] Cron finished. Pulled: %s order(s).", pulled)
        return True
