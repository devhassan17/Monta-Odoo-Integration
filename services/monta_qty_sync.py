# -*- coding: utf-8 -*-
import logging
from odoo import api, SUPERUSER_ID
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class MontaQtySync:
    """
    Pull StockAvailable & MinimumStock from Monta and push into Odoo:
      - Set absolute on-hand qty at the main warehouse Stock location
      - Set low stock threshold (available_threshold or orderpoint.min_qty)
    """
    def __init__(self, env):
        self.env = env.sudo()
        self.icp = self.env['ir.config_parameter'].sudo()

        # Config
        self.base_url = (self.icp.get_param('monta.base_url') or 'https://api-v6.monta.nl').rstrip('/')
        self.user = self.icp.get_param('monta.username') or ''
        self.pwd = self.icp.get_param('monta.password') or ''
        # Channel can be set in System Parameters; defaults to Moyee_Odoo for you
        self.channel = self.icp.get_param('monta.channel') or 'Moyee_Odoo'
        self.timeout = int(self.icp.get_param('monta.timeout') or 20)

        try:
            import requests
            from requests.auth import HTTPBasicAuth
            self._requests = requests
            self._auth = HTTPBasicAuth(self.user, self.pwd)
        except Exception as e:
            raise UserError(f"Python 'requests' not available: {e}")

    # ---------- HTTP ----------
    def _get_product_stock(self, sku):
        """
        GET /product/{sku}/stock?channel=...
        Returns (stock_available: float|None, min_stock: float|None)
        """
        url = f"{self.base_url}/product/{sku}/stock"
        try:
            r = self._requests.get(
                url,
                params={'channel': self.channel},
                auth=self._auth,
                headers={'Accept': 'application/json'},
                timeout=self.timeout,
            )
        except Exception as e:
            _logger.error("Monta HTTP error for %s: %s", sku, e)
            return None, None

        if not r.ok:
            _logger.warning("Monta %s -> HTTP %s body=%s", url, r.status_code, (r.text or '')[:200])
            return None, None

        try:
            data = r.json()
        except Exception:
            _logger.warning("Non-JSON from %s: %s", url, (r.text or '')[:200])
            return None, None

        rec = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        stock = rec.get('Stock') or {}
        def num(v):
            try:
                return float(v) if v is not None else None
            except Exception:
                return None
        stock_available = num(stock.get('StockAvailable'))
        min_stock = num(rec.get('MinimumStock'))
        return stock_available, min_stock

    # ---------- Odoo helpers ----------
    def _get_main_wh_stock_location(self, company):
        """Pick the company’s first warehouse lot_stock_id as the place to adjust."""
        WH = self.env['stock.warehouse'].sudo().search([('company_id', '=', company.id)], limit=1, order='id')
        if not WH:
            raise UserError(f"No warehouse for company {company.name}")
        return WH.lot_stock_id

    def _available_in_location(self, product, location):
        """Compute available (not reserved) quantity for a product at a location."""
        Quant = self.env['stock.quant'].sudo()
        quants = Quant.search([('product_id', '=', product.id), ('location_id', '=', location.id)])
        avail = 0.0
        for q in quants:
            avail += (q.quantity - q.reserved_quantity)
        return avail

    def _set_absolute_qty(self, product, location, target_qty):
        """
        Set absolute available qty at a location by moving the delta using stock.quant helper.
        """
        if target_qty is None:
            return
        Quant = self.env['stock.quant'].sudo()
        current = self._available_in_location(product, location)
        delta = float(target_qty) - current
        if abs(delta) < 1e-6:
            return
        Quant._update_available_quantity(product, location, delta)
        _logger.info("Adjusted %s at %s by %+s (to %s)", product.display_name, location.display_name, delta, target_qty)

    def _apply_low_stock_threshold(self, tmpl, minimum):
        """
        If website is installed, set available_threshold on template.
        Otherwise create/update a Reordering Rule for main WH with min_qty.
        """
        if minimum is None:
            return
        minimum = float(minimum)

        # Prefer website low stock threshold if field exists (no extra modules required by us)
        if 'available_threshold' in tmpl._fields:
            if (tmpl.available_threshold or 0.0) != minimum:
                tmpl.available_threshold = minimum
                _logger.info("Set available_threshold=%s on template %s", minimum, tmpl.display_name)
            return

        # Fallback to reordering rule
        company = tmpl.company_id or self.env.company
        location = self._get_main_wh_stock_location(company)
        wh = self.env['stock.warehouse'].sudo().search([('lot_stock_id', '=', location.id)], limit=1)
        if not wh:
            return
        Orderpoint = self.env['stock.warehouse.orderpoint'].sudo()
        op = Orderpoint.search([('product_id', 'in', tmpl.product_variant_ids.ids), ('warehouse_id', '=', wh.id)], limit=1)
        vals = {
            'product_id': tmpl.product_variant_id.id,
            'warehouse_id': wh.id,
            'location_id': location.id,
            'company_id': company.id,
            'product_min_qty': minimum,
            'product_max_qty': max(minimum, minimum),  # keep same unless you want a buffer
        }
        if op:
            op.write({'product_min_qty': minimum})
            _logger.info("Updated orderpoint min_qty=%s for %s (WH %s)", minimum, tmpl.display_name, wh.display_name)
        else:
            Orderpoint.create(vals)
            _logger.info("Created orderpoint min_qty=%s for %s (WH %s)", minimum, tmpl.display_name, wh.display_name)

    # ---------- Main run ----------
    def run(self, limit=None):
        """
        Iterate products with a Monta key (monta_sku or default_code),
        fetch stock from Monta, adjust qty, and apply low stock threshold.
        """
        Product = self.env['product.product'].sudo()

        # Choose mapping field priority: monta_sku (if you have it), else default_code
        domain = ['|', ('monta_sku', '!=', False), ('default_code', '!=', False)]
        products = Product.search(domain, limit=limit) if limit else Product.search(domain)
        _logger.info("MontaQtySync: processing %s products", len(products))

        for p in products:
            sku = (p.monta_sku or p.default_code or '').strip()
            if not sku:
                continue
            stock_available, min_stock = self._get_product_stock(sku)
            if stock_available is None and min_stock is None:
                continue

            tmpl = p.product_tmpl_id
            company = tmpl.company_id or self.env.company
            loc = self._get_main_wh_stock_location(company)

            # 1) Set absolute on-hand qty at main Stock location
            if stock_available is not None:
                self._set_absolute_qty(p, loc, stock_available)

            # 2) Low stock threshold mapping
            self._apply_low_stock_threshold(tmpl, min_stock)

        self.env.cr.commit()
        return True
