# -*- coding: utf-8 -*-
import logging
import re
from datetime import datetime

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

PULL_MIN_GAP_SECONDS = 60  # throttle repeated pulls


def _normalize_monta_dt(value):
    """
    Accepts ISO-8601 strings like:
      - '2025-08-16T01:49:23.42'
      - '2025-08-16T01:49:23.420123Z'
      - '2025-08-16T01:49:23+02:00'
    Returns an Odoo-safe string ('%Y-%m-%d %H:%M:%S') or False.
    """
    if not value:
        return False
    if isinstance(value, datetime):
        return fields.Datetime.to_string(value)

    s = str(value).strip()
    if not s:
        return False

    # Convert 'Z' to '+00:00' so fromisoformat can parse
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'

    # Try full ISO first
    try:
        dt = datetime.fromisoformat(s)
        # store naive in server tz (Odoo will handle TZ display)
        return fields.Datetime.to_string(dt.replace(tzinfo=None))
    except Exception:
        pass

    # Fallback: strip timezone, drop fractional secs, replace 'T' with space
    s2 = s.replace('T', ' ')
    s2 = re.sub(r'([+-]\d{2}:?\d{2})$', '', s2)  # remove trailing TZ if any
    s2 = s2.split('.')[0]  # drop fractional secs
    try:
        dt = datetime.strptime(s2, '%Y-%m-%d %H:%M:%S')
        return fields.Datetime.to_string(dt)
    except Exception:
        return False


class SaleOrderInbound(models.Model):
    _inherit = 'sale.order'

    # Inbound mirror fields
    monta_remote_status = fields.Char(copy=False, index=True)
    monta_tracking_number = fields.Char(copy=False, index=True)
    monta_tracking_url = fields.Char(copy=False)
    monta_carrier = fields.Char(copy=False)
    monta_delivered_at = fields.Datetime(copy=False)
    monta_last_pull = fields.Datetime(copy=False)

    # ---------- Public API ----------
    def action_monta_pull_now(self, channel=None):
        """
        Pull GET /order/{webshoporderid} for these orders and update fields.
        Optional 'channel' (string) if your Monta has multiple channels.
        """
        from ..services.monta_inbound import MontaInbound
        svc = MontaInbound(self.env)

        for order in self:
            try:
                webshop_id = order.monta_order_id or order.name
                if not webshop_id:
                    _logger.info("[Monta Pull] Skip %s: no webshop id/name", order.display_name)
                    continue

                # throttle if very recent
                if order.monta_last_pull:
                    delta = fields.Datetime.now() - order.monta_last_pull
                    if delta.total_seconds() < PULL_MIN_GAP_SECONDS:
                        _logger.info("[Monta Pull] Throttled for %s (last %.0fs ago)",
                                     order.name, delta.total_seconds())
                        continue

                status, body = svc.fetch_order(order, webshop_id, channel=channel)
                order.write({'monta_last_pull': fields.Datetime.now()})

                if not (200 <= int(status or 0) < 300):
                    # detailed logs already saved by service
                    continue

                # Monta example wraps payload as {"Order": {...}}
                payload = body.get('Order', body) if isinstance(body, dict) else {}
                changes, summary = svc.apply_to_sale_order(order, payload)

                if changes:
                    # normalize datetimes before write
                    if 'monta_delivered_at' in changes:
                        changes['monta_delivered_at'] = _normalize_monta_dt(changes['monta_delivered_at'])

                    order.write(changes)
                    try:
                        order.message_post(body="<b>Monta inbound update</b><br/><pre>%s</pre>" % summary)
                    except Exception:
                        pass

                    order._create_monta_log(
                        {'apply_changes': changes},
                        level='info', tag='Monta Pull',
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

    # ---------- Cron entry (call from Scheduled Action UI; no XML) ----------
    @api.model
    def cron_monta_pull_open_orders(self, batch_size=30):
        """
        Optional: create a Scheduled Action in UI to call:
          model.cron_monta_pull_open_orders()
        """
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