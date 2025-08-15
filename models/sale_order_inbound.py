# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

PULL_MIN_GAP_SECONDS = 60  # throttle repeated pulls


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
        'channel' can be passed (string) to disambiguate on Monta side.
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
                    # Detailed logs already saved by service
                    continue

                # Monta’s sample wraps payload as {"Order": {...}}
                payload = body.get('Order', body) if isinstance(body, dict) else {}
                changes, summary = svc.apply_to_sale_order(order, payload)

                if changes:
                    order.write(changes)
                    try:
                        order.message_post(
                            body="<b>Monta inbound update</b><br/><pre>%s</pre>" % summary
                        )
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

    # ---------- Cron without XML ----------
    @api.model
    def cron_monta_pull_open_orders(self, batch_size=30):
        """
        Pull a small batch of open/confirmed orders periodically.
        Use Odoo Scheduled Actions UI to call: model.cron_monta_pull_open_orders()
        (no XML needed; you can create the cron from UI).
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
