# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

# Use your existing resolver (basic auth, already configured by you)
from ..services.monta_status_resolver import MontaStatusResolver

_logger = logging.getLogger(__name__)


class MontaOrderStatus(models.Model):
    _name = "monta.order.status"
    _description = "Monta Order Status snapshot"
    _order = "last_sync desc, id desc"

    # minimal keys you asked to track + usable in dashboards
    order_name    = fields.Char(required=True, index=True)
    sale_order_id = fields.Many2one("sale.order", ondelete="cascade", index=True)
    status        = fields.Char()
    status_code   = fields.Char()
    source        = fields.Char()              # 'orders' / 'shipments' / 'orderevents?...'
    track_trace   = fields.Char()
    delivery_date = fields.Date()
    last_sync     = fields.Datetime(index=True, default=fields.Datetime.now)

    _sql_constraints = [
        ("monta_order_name_unique", "unique(order_name)", "Monta order name must be unique."),
    ]

    # --------------------------- helpers ---------------------------

    def _resolve_from_monta(self, order_ref):
        """
        Use your MontaStatusResolver to get (status, meta)
        meta can contain: source, code, track_trace, delivery_date
        """
        try:
            resolver = MontaStatusResolver(self.env)
            status, meta = resolver.resolve(order_ref)
            return status, (meta or {})
        except Exception as e:
            _logger.warning("[Monta] Resolve failed for %s: %s", order_ref, e)
            return None, {}

    @api.model
    def _vals_from_resolution(self, so, status, meta):
        """Map resolver output to our fields."""
        delivery = meta.get("delivery_date") or False
        # meta['delivery_date'] may be date or iso datetime string; let Odoo cast if valid
        return {
            "order_name": so.name,
            "sale_order_id": so.id,
            "status": status or "",
            "status_code": str(meta.get("code") or "") if meta.get("code") not in (None, "") else "",
            "source": str(meta.get("source") or "orders"),
            "track_trace": meta.get("track_trace") or False,
            "delivery_date": delivery or False,
            "last_sync": fields.Datetime.now(),
        }

    # keep public so hourly mirror cron can call it
    def _mirror_to_sale(self, rec):
        """Mirror to sale.order if those fields exist (safe writes)."""
        so = rec.sale_order_id
        if not so:
            return
        can = so._fields
        vals = {}
        if "monta_order_id" in can:
            vals["monta_order_id"] = rec.id
        if "monta_status" in can:
            vals["monta_status"] = rec.status or False
        if "monta_status_code" in can:
            vals["monta_status_code"] = rec.status_code or False
        if "monta_status_source" in can:
            vals["monta_status_source"] = rec.source or "orders"
        if "monta_track_trace" in can:
            vals["monta_track_trace"] = rec.track_trace or False
        if "monta_last_sync" in can:
            vals["monta_last_sync"] = rec.last_sync
        if "x_monta_delivery_date" in can:
            vals["x_monta_delivery_date"] = rec.delivery_date or False
        if vals:
            so.sudo().write(vals)

    # --------------------------- public API ---------------------------

    @api.model
    def _resolve_and_upsert(self, so):
        """Resolve status, upsert snapshot, mirror to sale.order."""
        if not so or not so.name:
            return False

        status, meta = self._resolve_from_monta(so.name)
        if not status and not meta:
            _logger.info("[Monta] No status/meta returned for %s", so.name)
            return False

        vals = self._vals_from_resolution(so, status, meta)

        rec = self.search([("order_name", "=", so.name)], limit=1)
        if rec:
            rec.sudo().write(vals)
            _logger.info("[Monta] Updated status row for %s", so.name)
        else:
            rec = self.sudo().create(vals)
            _logger.info("[Monta] Created status row for %s", so.name)

        self._mirror_to_sale(rec)
        return rec

    @api.model
    def cron_monta_sync_status(self, batch_limit=300):
        """
        Runs via cron every 30 min:
          - Pick SOs that look like Monta orders (BC% or S%)
          - Resolve+Upsert
          - Mirror to Sale Order dashboard fields
        """
        SO = self.env["sale.order"].sudo()
        dom = ["|", ("name", "=like", "BC%"), ("name", "=like", "S%")]
        orders = SO.search(dom, limit=batch_limit, order="id")
        processed = 0
        for so in orders:
            try:
                if self._resolve_and_upsert(so):
                    processed += 1
            except Exception as e:
                _logger.exception("[Monta] Failed syncing %s: %s", so.name, e)
        _logger.info("[Monta] Cron finished, processed %s orders", processed)
        return True
