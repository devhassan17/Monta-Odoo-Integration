# -*- coding: utf-8 -*-
import json
import logging

from odoo import models, fields

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model,):
    # Python-inherit the mixin by importing the class and adding it as a parent
    #   from .monta_api import MontaAPIMixin   <-- import below
    _inherit = 'sale.order'


# Import after class definition to avoid circular imports in some loaders
from .monta_api import MontaAPIMixin


class SaleOrder(SaleOrder, MontaAPIMixin):
    # No _inherit here (already extended above); this class just adds behavior
    # Track Monta state (simple flags)
    monta_sent = fields.Boolean(string="Sent to Monta", default=False)
    monta_cancelled = fields.Boolean(string="Cancelled on Monta", default=False)

    # ---------------------------
    # Logging helper
    # ---------------------------
    def _create_monta_log(self, payload, level="info"):
        self.ensure_one()
        vals = {
            "sale_order_id": self.id,
            "log_data": json.dumps(payload, indent=2, default=str),
            "level": level,
            "name": f"Monta {self.name} - {level}",
        }
        self.env["monta.sale.log"].sudo().create(vals)
        if level == "info":
            _logger.info(vals["log_data"])
        else:
            _logger.error(vals["log_data"])

    # ---------------------------
    # Background job targets (no decorator needed)
    # ---------------------------
    def job_send_to_monta(self, payload):
        """Queued: create order on Monta."""
        self.ensure_one()
        response = self._send_to_monta(payload)
        self._create_monta_log(response, level="info" if "error" not in response else "error")
        if "error" not in response:
            self.monta_sent = True
        return response

    def job_delete_on_monta(self, note):
        """Queued: delete order on Monta (DELETE /order/{id})."""
        self.ensure_one()
        resp = self._delete_monta_order(self.name, note=note or "Cancelled")
        self._create_monta_log(resp, level="info" if "error" not in resp else "error")
        if "error" not in resp:
            self.monta_cancelled = True
        return resp

    # ---------------------------
    # Confirm → enqueue create
    # ---------------------------
    def action_confirm(self):
        res = super(SaleOrder, self).action_confirm()
        for order in self:
            partner = order.partner_id

            _logger.info("✅ Order Confirmed:")
            _logger.info(f"📄 Order: {order.name}")
            _logger.info(f"👤 Customer: {partner.name}")
            _logger.info(f"✉️ Email: {partner.email}")
            _logger.info(f"💰 Total: {order.amount_total}")
            _logger.info(
                f"🛍️ Order Lines: {[(l.product_id.name, l.product_uom_qty) for l in order.order_line]}"
            )

            payload = order._prepare_monta_order_payload()
            order._create_monta_log(payload, level="info")

            # Try async via queue_job; fallback to sync
            if order.env["ir.module.module"]._is_installed("queue_job"):
                order.with_delay(
                    priority=10,
                    max_retries=5,
                    retry_pattern={1: 10, 2: 60, 3: 300},
                    channel="root.monta",
                ).job_send_to_monta(payload)
            else:
                _logger.warning("queue_job not installed; sending to Monta synchronously.")
                resp = order._send_to_monta(payload)
                order._create_monta_log(resp, level="info" if "error" not in resp else "error")
                if "error" not in resp:
                    order.monta_sent = True

        return res

    # ---------------------------
    # Cancel → enqueue delete on Monta
    # ---------------------------
    def action_cancel(self):
        res = super(SaleOrder, self).action_cancel()
        for order in self:
            if not order.monta_sent:
                _logger.info(f"Skipping Monta delete for {order.name}: not sent yet.")
                continue

            note = self.env["ir.config_parameter"].sudo().get_param(
                "monta_api.delete_note", default="Cancelled"
            )

            if order.env["ir.module.module"]._is_installed("queue_job"):
                order.with_delay(
                    priority=5,
                    max_retries=5,
                    retry_pattern={1: 10, 2: 60, 3: 300},
                    channel="root.monta",
                ).job_delete_on_monta(note)
            else:
                _logger.warning("queue_job not installed; deleting on Monta synchronously.")
                resp = order._delete_monta_order(order.name, note=note)
                order._create_monta_log(resp, level="info" if "error" not in resp else "error")
                if "error" not in resp:
                    order.monta_cancelled = True

        return res
