# -*- coding: utf-8 -*-
import json
import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    monta_pushed = fields.Boolean(string="Pushed to Monta", default=False, copy=False)
    monta_webshop_order_id = fields.Char(string="Monta Webshop Order ID", copy=False, index=True)
    monta_last_push = fields.Datetime(string="Monta Last Push", copy=False)

    monta_status = fields.Char(string="Monta Status", copy=False, index=True)
    monta_status_code = fields.Char(string="Monta Status Code", copy=False)
    monta_track_trace = fields.Char(string="Monta Track & Trace", copy=False)
    monta_delivery_date = fields.Date(string="Monta Delivery Date", copy=False)

    def _is_monta_push_eligible(self):
        """Check if this picking should be pushed to Monta."""
        self.ensure_one()
        if self.picking_type_code != 'outgoing':
            return False
        if not self.sale_id:
            return False
        cfg = self.env["monta.config"].sudo().get_for_company(self.company_id)
        if not cfg:
            return False
        # Optional: check if SO name is BC... to skip
        if self.sale_id.name and self.sale_id.name.startswith("BC"):
            return False
        return True

    def _monta_is_first_delivery(self):
        """Returns True if this is the first (successful or pending) Monta push for the related Sales Order."""
        self.ensure_one()
        if not self.sale_id:
            return False
        # Search for any other picking for the same SO that was already pushed or has a status row with 'Sent'
        others = self.search([
            ("sale_id", "=", self.sale_id.id),
            ("id", "!=", self.id),
            ("monta_pushed", "=", True)
        ])
        if others:
            return False
        
        # Also check monta.order.status for any row linked to this SO that is successfully 'Sent'
        # (This covers cases where a picking was deleted but the order exists in Monta)
        Status = self.env["monta.order.status"].sudo()
        existing = Status.search([
            ("sale_order_id", "=", self.sale_id.id),
            ("status", "in", ["Sent", "sent"])
        ])
        return not bool(existing)

    def _monta_make_webshop_order_id(self, so):
        self.ensure_one()
        if self.monta_webshop_order_id:
            return self.monta_webshop_order_id

        so_name = (so.name or "").replace("/", "-")
        
        # Smart ID: First delivery uses SO name, subsequent use unique ID
        if self._monta_is_first_delivery():
            webshop_order_id = so.name
        else:
            webshop_order_id = f"{so_name}-PICK{self.id}"
            
        self.write({"monta_webshop_order_id": webshop_order_id})
        return webshop_order_id

    def _prepare_monta_lines(self):
        """Build Monta lines from stock moves in the picking."""
        self.ensure_one()
        components = []
        # Use move_ids for better compatibility across Odoo versions/configs
        for m in self.move_ids:
            if m.product_id and m.product_uom_qty > 0:
                components.append((m.product_id, m.product_uom_qty))
        
        if not self.sale_id:
             return []
             
        lines = self.sale_id._prepare_monta_lines_from_components(components)
        
        # Log the specific lines being sent
        product_summary = ", ".join([f"{l['Sku']} (qty {l['Quantity']})" for l in lines])
        _logger.info("[Monta Push] %s: Preparing payload with %s products: %s", self.name, len(lines), product_summary)
        
        return lines

    def _monta_prepare_payload(self, so, webshop_order_id):
        """Reuse existing sale.order payload generator but overwrite lines with picking contents."""
        self.ensure_one()
        payload = so._prepare_monta_order_payload()
        
        # Overwrite lines with what's actually in THIS picking
        payload["OrderLines"] = self._prepare_monta_lines()
        
        payload["WebshopOrderId"] = webshop_order_id
        payload["Reference"] = (self.name or "").strip()
        
        # Recalculate invoice amount if possible? 
        # Actually, if it's a partial shipment, we might still send the total amount?
        # Usually WMS wants the order total for the first shipment.
        # For now, keeping the SO total to avoid complexity.
        
        payload["Invoice"]["WebshopFactuurID"] = int(self.id) if self.id else 9999
        return payload

    def action_push_to_monta(self, sale_order=None):
        """Pushes the picking to Monta."""
        self.ensure_one()
        if not self._is_monta_push_eligible():
            return False

        if not sale_order:
            sale_order = self.sale_id

        webshop_order_id = self._monta_make_webshop_order_id(sale_order)
        is_renewal = not (webshop_order_id == sale_order.name)

        # Idempotency guard
        Status = self.env["monta.order.status"].sudo()
        existing = Status.search(
            [
                ("order_name", "=", webshop_order_id),
                ("status", "in", ["Sent", "sent"]),
            ],
            limit=1,
        )
        if existing and not self.env.context.get("force_send_to_monta"):
            return True

        # Snapshot creation
        kind = "renewal" if is_renewal else "sale"
        if is_renewal:
            Status.upsert_for_renewal(
                sale_order, self, webshop_order_id,
                status="Not sent", status_code=0, source="orders",
                status_raw=json.dumps({"note": "Push initiated"}, ensure_ascii=False)
            )
        else:
            Status.upsert_for_order(
                sale_order,
                status="Not sent", status_code=0, source="orders",
                status_raw=json.dumps({"note": "Push initiated"}, ensure_ascii=False)
            )

        payload = self._monta_prepare_payload(sale_order, webshop_order_id)
        status, body = sale_order._monta_request("POST", "/order", payload)

        if 200 <= status < 300 or self._monta_is_duplicate_exists_error(status, body):
            monta_ref = self._monta_extract_monta_ref(body, webshop_order_id)
            vals_status = {
                "status": "Sent",
                "status_code": status,
                "source": "orders",
                "monta_order_ref": monta_ref,
                "status_raw": json.dumps(body or {}, ensure_ascii=False),
                "last_sync": fields.Datetime.now(),
            }
            if is_renewal:
                Status.upsert_for_renewal(sale_order, self, webshop_order_id, **vals_status)
            else:
                Status.upsert_for_order(sale_order, **vals_status)

            self.write({
                "monta_pushed": True,
                "monta_last_push": fields.Datetime.now(),
            })
            return True

        # Error handling
        vals_err = {
            "status": "Error",
            "status_code": status,
            "source": "orders",
            "status_raw": json.dumps(body or {}, ensure_ascii=False),
            "last_sync": fields.Datetime.now(),
        }
        if is_renewal:
            Status.upsert_for_renewal(sale_order, self, webshop_order_id, **vals_err)
        else:
            Status.upsert_for_order(sale_order, **vals_err)
        return False

    def _monta_is_duplicate_exists_error(self, status, body):
        if status != 400 or not isinstance(body, dict):
            return False
        reasons = body.get("OrderInvalidReasons") or []
        for r in reasons:
            msg = (r or {}).get("Message") or ""
            if "already exists" in msg.lower():
                return True
        return False

    def _monta_extract_monta_ref(self, body, fallback):
        if isinstance(body, dict):
            for k in ("OrderId", "orderId", "Id", "id", "MontaOrderId", "montaOrderId", "OrderNumber", "orderNumber"):
                v = body.get(k)
                if v:
                    return str(v)
        return fallback

    def action_confirm(self):
        res = super(StockPicking, self).action_confirm()
        for picking in self:
            if picking._is_monta_push_eligible() and not picking.monta_pushed:
                picking.action_push_to_monta()
        return res

    def action_cancel(self):
        res = super(StockPicking, self).action_cancel()
        for picking in self:
            if picking.monta_pushed:
                # Attempt to cancel in Monta if pushed
                try:
                    webshop_id = picking.monta_webshop_order_id or picking.name
                    sale_order = picking.sale_id
                    if sale_order:
                        headers = {"Content-Type": "application/json-patch+json", "Accept": "application/json"}
                        sale_order._monta_request("DELETE", f"/order/{webshop_id}", {"Note": "Delivery Cancelled in Odoo"}, headers=headers)
                except Exception:
                    _logger.warning("Failed to cancel delivery %s in Monta", picking.name)
        return res

    def action_send_renewal_to_monta(self, sale_order=None):
        """Deprecated/Legacy method kept for status button compatibility if needed."""
        return self.action_push_to_monta(sale_order=sale_order)
