# -*- coding: utf-8 -*-
import json
import re
import logging
from collections import defaultdict

from odoo import models, fields
from odoo.exceptions import ValidationError

from ..services.monta_client import MontaClient
from ..utils.address import split_street
from ..utils.sku import resolve_sku
from ..utils.pack import expand_to_leaf_components, is_pack_like, get_pack_components

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    monta_order_id = fields.Char(copy=False, index=True)
    monta_sync_state = fields.Selection([
        ("draft", "Draft"),
        ("sent", "Sent"),
        ("updated", "Updated"),
        ("cancelled", "Cancelled"),
        ("error", "Error"),
    ], default="draft", copy=False)
    monta_last_push = fields.Datetime(copy=False)
    monta_needs_sync = fields.Boolean(default=False, copy=False)
    monta_retry_count = fields.Integer(default=0, copy=False)

    def _split_street(self, street, street2=""):
        return split_street(street, street2)

    def _should_push_now(self, min_gap_seconds=2):
        if not self.monta_last_push:
            return True
        delta = fields.Datetime.now() - self.monta_last_push
        try:
            return delta.total_seconds() >= min_gap_seconds
        except Exception:
            return True

    def _monta_config(self):
        return self.env["monta.config"].sudo().get_for_company(self.company_id)

    def _is_company_allowed(self):
        cfg = self._monta_config()
        if not cfg:
            _logger.warning("[Monta Guard] Company not allowed or config missing for %s", self.company_id.display_name)
            return False
        return True

    def _is_allowed_instance(self):
        """
        Allowed when:
        - allowed_base_urls empty (no blocking), or
        - current web.base.url matches one of comma-separated URLs in config.
        """
        cfg = self._monta_config()
        if not cfg:
            return False

        ICP = self.env["ir.config_parameter"].sudo()
        web_url = (ICP.get_param("web.base.url") or "").strip().rstrip("/") + "/"
        allowed_conf = (cfg.allowed_base_urls or "").strip()
        if not allowed_conf:
            return True

        allowed_list = [u.strip().rstrip("/") + "/" for u in allowed_conf.split(",") if u.strip()]
        ok = (web_url.lower() in [a.lower() for a in allowed_list])
        if not ok:
            _logger.warning("[Monta Guard] Blocked. web.base.url=%s allowed_list=%s", web_url, allowed_list)
            self._create_monta_log(
                {"guard": {"web_base_url": web_url, "allowed_list": allowed_list, "blocked": True}},
                level="info", tag="Monta Guard", console_summary="[Monta Guard] blocked by instance URL"
            )
        return ok

    def _create_monta_log(self, payload, level="info", tag="Monta API", console_summary=None):
        self.ensure_one()
        valid_level = "info" if level == "warning" else level
        self.env["monta.sale.log"].sudo().create({
            "sale_order_id": self.id,
            "log_data": json.dumps(payload, indent=2, default=str),
            "level": valid_level,
            "name": f"{tag} {self.name} - {valid_level}",
        })
        (_logger.info if valid_level == "info" else _logger.error)(f"[{tag}] {console_summary or self.name}")

    def _prepare_monta_lines(self):
        from math import isfinite
        sku_qty = defaultdict(float)
        missing = []

        for l in self.order_line:
            p = l.product_id
            if not p:
                continue
            qty = float(l.product_uom_qty or 0.0)
            if qty <= 0:
                continue

            leaves = expand_to_leaf_components(self.env, self.company_id.id, p, qty)
            if not leaves:
                missing.append(f"'{p.display_name}' has no resolvable components.")
                continue

            for comp, q in leaves:
                sku, _ = resolve_sku(comp, env=self.env, allow_synthetic=False)
                if not sku:
                    missing.append(f"Component '{comp.display_name}' is missing a real SKU.")
                    continue
                try:
                    qv = float(q or 0.0)
                    if not isfinite(qv):
                        qv = 0.0
                except Exception:
                    qv = 0.0
                sku_qty[sku] += qv

        if missing:
            self._create_monta_log({"missing_skus": missing}, level="error", tag="Monta SKU check",
                                   console_summary=f"[Monta SKU check] {len(missing)} missing")
            raise ValidationError("Cannot push to Monta:\n- " + "\n- ".join(missing))

        lines = [{"Sku": sku, "OrderedQuantity": int(q)} for sku, q in sku_qty.items() if int(q) > 0]
        if not lines:
            raise ValidationError("Order lines expanded to empty/zero quantities.")
        return lines

    def _prepare_monta_order_payload(self):
        self.ensure_one()
        cfg = self._monta_config()
        if not cfg:
            raise ValidationError("Monta Configuration missing or company not allowed.")

        partner = self.partner_id
        street, house_number, house_suffix = self._split_street(partner.street or "", partner.street2 or "")
        lines = self._prepare_monta_lines()
        invoice_id_digits = re.sub(r"\D", "", self.name or "")
        webshop_factuur_id = int(invoice_id_digits) if invoice_id_digits else 9999

        payload = {
            "WebshopOrderId": self.name,
            "Reference": self.client_order_ref or "",
            "ConsumerDetails": {
                "DeliveryAddress": {
                    "Company": partner.company_name or partner.name or "",
                    "FirstName": partner.name.split(" ")[0] if partner.name else "",
                    "LastName": " ".join((partner.name or "").split(" ")[1:]) if len((partner.name or "").split(" ")) > 1 else "",
                    "Street": street,
                    "HouseNumber": house_number or "1",
                    "HouseNumberAddition": house_suffix or "",
                    "PostalCode": partner.zip or "0000AA",
                    "City": partner.city or "TestCity",
                    "CountryCode": partner.country_id.code if partner.country_id else "NL",
                    "PhoneNumber": partner.phone or "0000000000",
                    "EmailAddress": partner.email or "test@example.com",
                },
                "InvoiceAddress": {
                    "Company": partner.company_name or partner.name or "",
                    "FirstName": partner.name.split(" ")[0] if partner.name else "",
                    "LastName": " ".join((partner.name or "").split(" ")[1:]) if len((partner.name or "").split(" ")) > 1 else "",
                    "Street": street,
                    "HouseNumber": house_number or "1",
                    "HouseNumberAddition": house_suffix or "",
                    "PostalCode": partner.zip or "0000AA",
                    "City": partner.city or "TestCity",
                    "CountryCode": partner.country_id.code if partner.country_id else "NL",
                    "PhoneNumber": partner.phone or "0000000000",
                    "EmailAddress": partner.email or "test@example.com",
                },
            },
            "Lines": lines,
            "Invoice": {
                "PaymentMethodDescription": "Odoo Order",
                "AmountInclTax": float(self.amount_total or 0.0),
                "TotalTax": float(sum((line.price_tax or 0.0) for line in self.order_line)),
                "WebshopFactuurID": webshop_factuur_id,
                "Currency": self.currency_id.name or "EUR",
            },
        }

        if (cfg.origin or "").strip():
            payload["Origin"] = cfg.origin.strip()

        return payload

    def _monta_request(self, method, path, payload=None, headers=None):
        if not self._is_company_allowed():
            return 0, {"note": "Blocked: company not allowed in Monta Configuration"}
        if not self._is_allowed_instance():
            return 0, {"note": "Blocked: instance URL guard"}
        client = MontaClient(self.env, company=self.company_id)
        return client.request(self, method, path, payload=payload, headers=headers)

    def _monta_create(self):
        self.ensure_one()
        status, body = self._monta_request("POST", "/order", self._prepare_monta_order_payload())
        if 200 <= status < 300:
            self.write({
                "monta_order_id": self.name,
                "monta_sync_state": "sent",
                "monta_last_push": fields.Datetime.now(),
                "monta_needs_sync": False,
                "monta_retry_count": 0,
            })
        else:
            if self.monta_retry_count < 1:
                self.write({
                    "monta_sync_state": "error",
                    "monta_needs_sync": True,
                    "monta_retry_count": self.monta_retry_count + 1,
                })
            else:
                self.write({
                    "monta_sync_state": "error",
                    "monta_needs_sync": False,
                    "monta_retry_count": self.monta_retry_count,
                })
        return status, body

    def _monta_delete(self, note="Cancelled from Odoo"):
        self.ensure_one()
        webshop_id = self.monta_order_id or self.name
        headers = {"Content-Type": "application/json-patch+json", "Accept": "application/json"}
        return self._monta_request("DELETE", f"/order/{webshop_id}", {"Note": note}, headers=headers)

    def action_confirm(self):
        res = super().action_confirm()
        for order in self:
            if order._is_company_allowed():
                order._monta_create()
        return res

    def write(self, vals):
        tracked_fields = {"partner_id", "order_line", "client_order_ref", "validity_date", "commitment_date"}
        if any(f in vals for f in tracked_fields):
            vals.setdefault("monta_needs_sync", True)

        res = super().write(vals)

        for order in self.filtered(lambda o: o.state in ("sale", "done") and o.monta_needs_sync and o.state != "cancel"):
            if not order._is_company_allowed():
                continue
            if order._should_push_now():
                order._monta_create()

        return res

    def action_cancel(self):
        res = super().action_cancel()
        for order in self:
            if order._is_company_allowed():
                order._monta_delete(note="Cancelled")
        return res
