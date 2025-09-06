# -*- coding: utf-8 -*-
import logging
from odoo import models, fields

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    # Tracking flags for inbound forecast sync (optional but useful)
    monta_if_needs_sync = fields.Boolean(default=False, copy=False)
    monta_if_last_push  = fields.Datetime(copy=False)
    x_monta_inboundforecast_uid = fields.Char(copy=False)  # if Monta returns a UID

    def _monta_if_should_push_now(self, min_gap_seconds=2):
        """Simple debounce to avoid rapid double-push on mass-edits."""
        if not self.monta_if_last_push:
            return True
        try:
            delta = fields.Datetime.now() - self.monta_if_last_push
            return (delta.total_seconds() or 0) >= min_gap_seconds
        except Exception:
            return True

    # ------------ Manual / programmatic trigger ------------
    def action_monta_push_inbound_forecast(self):
        svc = self.env["monta.inbound.forecast.service"]
        for po in self:
            try:
                _logger.info("[Monta IF] Start push for PO %s", po.name)
                ok = svc.send_for_po(po)  # idempotent create/update + line upserts
                if ok:
                    po.write({
                        "monta_if_needs_sync": False,
                        "monta_if_last_push": fields.Datetime.now(),
                    })
                _logger.info("[Monta IF] Done push for PO %s (ok=%s)", po.name, ok)
            except Exception as e:
                _logger.error("[Monta IF] Failed for %s: %s", po.name, e, exc_info=True)
        return True

    # ------------ Confirm ------------
    def button_confirm(self):
        res = super().button_confirm()
        try:
            # push on confirm
            self.action_monta_push_inbound_forecast()
        except Exception as e:
            _logger.error("[Monta IF] Auto push after confirm failed: %s", e, exc_info=True)
        return res

    # ------------ Update ------------
    def write(self, vals):
        # fields that should trigger re-sync if PO already confirmed (or done)
        tracked = {
            "partner_id", "order_line", "date_planned",
            "dest_address_id", "picking_type_id",
        }
        if tracked.intersection(vals.keys()):
            vals.setdefault("monta_if_needs_sync", True)
        res = super().write(vals)

        # only sync confirmed/done POs, not RFQ
        for po in self.filtered(lambda p: p.state in ("purchase", "done") and p.monta_if_needs_sync):
            try:
                if po._monta_if_should_push_now():
                    po.action_monta_push_inbound_forecast()
            except Exception as e:
                _logger.error("[Monta IF] write-trigger push failed for %s: %s", po.name, e, exc_info=True)
        return res

    # ------------ Cancel ------------
    def button_cancel(self):
        svc = self.env["monta.inbound.forecast.service"]
        for po in self:
            try:
                svc.cancel_for_po(po)  # DELETE group in Monta
            except Exception as e:
                _logger.error("[Monta IF] Cancel sync failed for %s: %s", po.name, e, exc_info=True)
        return super().button_cancel()

    # ------------ Delete ------------
    def unlink(self):
        svc = self.env["monta.inbound.forecast.service"]
        for po in self:
            try:
                svc.cancel_for_po(po, note="Deleted from Odoo (unlink)")
            except Exception as e:
                _logger.error("[Monta IF] Delete sync failed for %s: %s", po.name, e, exc_info=True)
        return super().unlink()
