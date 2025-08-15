# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

PULL_MIN_GAP_SECONDS = 60  # avoid spamming Monta if button is clicked repeatedly


class SaleOrderInbound(models.Model):
    _inherit = 'sale.order'

    # ---- inbound-only technical fields ----
    monta_remote_status = fields.Char(copy=False, index=True)
    monta_tracking_number = fields.Char(copy=False, index=True)
    monta_tracking_url = fields.Char(copy=False)
    monta_carrier = fields.Char(copy=False)
    monta_delivered_at = fields.Datetime(copy=False)
    monta_last_pull = fields.Datetime(copy=False)

    # -------- public actions --------
    def action_monta_pull_now(self):
        """Manual button to pull a single order from Monta."""
        for so in self:
            try:
                so._monta_pull_one()
            except Exception as e:
                _logger.error("[Monta Pull] Manual pull failed for %s: %s", so.name, e, exc_info=True)
        return True

    # -------- scheduler entry point --------
    @api.model
    def cron_monta_pull_open_orders(self, batch_size=30):
        """Cron: pull recent/open orders from Monta and update Odoo."""
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
                # skip if pulled very recently
                if so.monta_last_pull:
                    delta = fields.Datetime.now() - so.monta_last_pull
                    if delta.total_seconds() < PULL_MIN_GAP_SECONDS:
                        continue
                so._monta_pull_one()
                pulled += 1
            except Exception as e:
                _logger.error("[Monta Pull] Cron pull failed for %s: %s", so.name, e, exc_info=True)

        _logger.info("[Monta Pull] Cron finished. Pulled: %s order(s).", pulled)
        return True

    # -------- core logic --------
    def _monta_pull_one(self):
        """Fetch Monta order by webshoporderid and update local fields."""
        self.ensure_one()
        webshop_id = self.monta_order_id or self.name
        if not webshop_id:
            return False

        from ..services.monta_inbound import MontaInbound

        svc = MontaInbound(self.env)
        status_code, body = svc.fetch_order(self, webshop_id)
        # always stamp last pull (even on non-200) for visibility
        self.write({'monta_last_pull': fields.Datetime.now()})

        if not (200 <= int(status_code or 0) < 300):
            # error is already fully logged by the service
            return False

        changes, summary = svc.apply_to_sale_order(self, body)
        if changes:
            self.write(changes)
            # chatter message
            try:
                txt = "<b>Monta inbound update</b><br/><pre>%s</pre>" % summary
                self.message_post(body=txt)
            except Exception:
                pass

            self._create_monta_log({'apply_changes': changes, 'raw_summary': summary},
                                   level='info', tag='Monta Pull',
                                   console_summary='[Monta Pull] updated fields')

        else:
            self._create_monta_log({'note': 'No applicable changes from Monta response'},
                                   level='info', tag='Monta Pull',
                                   console_summary='[Monta Pull] no changes')

        return True
