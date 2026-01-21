# -*- coding: utf-8 -*-
import logging

from .monta_client import MontaClient

_logger = logging.getLogger(__name__)


class MontaStockPull:
    """
    Pull current stock per SKU from Monta and mirror it into Odoo:
      - Writes x_monta_last_stock on product.template
      - Applies min-stock/sold-out policy
    """

    def __init__(self, env):
        self.env = env

    def _endpoint(self):
        # Adjust for your tenant, e.g. /stock?channel=X or /inventory
        return "/stock"

    def _get_log_order(self):
        """
        MontaClient currently expects an order record with _create_monta_log().
        Create a safe dummy sale.order if possible; otherwise return None.
        """
        SaleOrder = self.env["sale.order"].sudo()
        try:
            partner = self.env["res.partner"].sudo().search([], limit=1)
            if not partner:
                return None
            return SaleOrder.create({"partner_id": partner.id})
        except Exception:
            return None

    def pull_and_apply(self, limit=None):
        Product = self.env["product.product"].sudo()
        Template = self.env["product.template"].sudo()

        client = MontaClient(self.env)

        log_order = self._get_log_order()
        if not log_order:
            _logger.warning("[Monta Stock] Could not create dummy sale.order for logging; request may fail if client requires it.")

        status, body = client.request(log_order, "GET", self._endpoint(), payload=None)
        if log_order:
            try:
                log_order.unlink()
            except Exception:
                pass

        if not (200 <= (status or 0) < 300):
            _logger.error("[Monta Stock] GET failed: %s %s", status, body)
            return 0

        # Expecting body like: [{"Sku":"ABC","OnHand":12}, ...] or {"Items":[...]}
        if isinstance(body, list):
            rows = body
        elif isinstance(body, dict):
            rows = body.get("Items") or body.get("items") or []
        else:
            rows = []

        if not rows:
            _logger.info("[Monta Stock] No stock rows returned.")
            return 0

        sku_to_qty = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            sku = r.get("Sku") or r.get("SKU") or r.get("ProductCode")
            qty = r.get("OnHand") or r.get("Available") or r.get("Quantity")
            if not sku or qty is None:
                continue
            try:
                sku_to_qty[str(sku).strip()] = float(qty)
            except Exception:
                continue

        if not sku_to_qty:
            _logger.info("[Monta Stock] No valid SKU quantities parsed.")
            return 0

        keys = list(sku_to_qty.keys())
        domain = ["|", ("monta_sku", "in", keys), ("default_code", "in", keys)]
        prods = Product.search(domain, limit=limit)

        tmpl_ids = set()
        updated = 0

        for p in prods:
            sku = (p.monta_sku or p.default_code or "").strip()
            qty = sku_to_qty.get(sku)
            if qty is None:
                continue
            p.product_tmpl_id.write({"x_monta_last_stock": qty})
            tmpl_ids.add(p.product_tmpl_id.id)
            updated += 1

        if tmpl_ids and hasattr(Template, "_apply_soldout_policy"):
            Template.browse(list(tmpl_ids))._apply_soldout_policy()

        _logger.info("[Monta Stock] Updated %s product templates.", len(tmpl_ids))
        return len(tmpl_ids)
