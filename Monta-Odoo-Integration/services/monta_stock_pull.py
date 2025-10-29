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

    def pull_and_apply(self, limit=None):
        Product = self.env['product.product']
        Tmpl = self.env['product.template']
        client = MontaClient(self.env)
        proxy_order = self.env['sale.order'].browse()

        status, body = client.request(proxy_order, "GET", self._endpoint(), payload=None)
        if not (200 <= (status or 0) < 300):
            _logger.error("[Monta Stock] GET failed: %s %s", status, body)
            return 0

        # Expecting body like: [{"Sku":"ABC","OnHand":12}, ...]
        rows = body if isinstance(body, list) else body.get('Items') or []
        updated = 0
        sku_to_qty = {}
        for r in rows:
            sku = r.get('Sku') or r.get('SKU') or r.get('ProductCode')
            qty = r.get('OnHand') or r.get('Available') or r.get('Quantity')
            if sku is None or qty is None:
                continue
            sku_to_qty[str(sku)] = float(qty)

        # Map SKUs to templates
        prods = Product.search([('|', ('monta_sku', 'in', list(sku_to_qty.keys())),
                                     ('default_code', 'in', list(sku_to_qty.keys())))])
        by_tmpl = {}
        for p in prods:
            qty = sku_to_qty.get(p.monta_sku or p.default_code)
            if qty is None:
                continue
            t = p.product_tmpl_id
            t.write({'x_monta_last_stock': qty})
            by_tmpl.setdefault(t.id, t)
            updated += 1

        # Apply policies
        Tmpl.browse(list(by_tmpl.keys()))._apply_soldout_policy()
        _logger.info("[Monta Stock] Updated %s product templates.", updated)
        return updated