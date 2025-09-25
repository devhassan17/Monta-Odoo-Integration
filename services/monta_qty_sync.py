# -*- coding: utf-8 -*-
import json
import logging
import math
from datetime import datetime
from urllib.parse import quote

import requests
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


class MontaQtySync:
    """
    Pulls StockAvailable + MinimumStock from Monta and applies in Odoo:
      • Set absolute available qty at the company's main WH 'Stock' location
      • Set product.template.available_threshold = MinimumStock
      • Never push negatives to Odoo; values are clamped to 0
      • Never update phantom (kit) products directly; compute theoretical packs
    """

    def __init__(self, env):
        # Always run as superuser for stock + settings write
        self.env = env.sudo()
        ICP = self.env["ir.config_parameter"].sudo()

        # Config with sane defaults
        self.base = (ICP.get_param("monta.api.base") or "https://api-v6.monta.nl").rstrip("/")
        self.user = ICP.get_param("monta.api.user") or ""
        self.pwd = ICP.get_param("monta.api.password") or ""
        self.channel = ICP.get_param("monta.api.channel") or "Moyee_Odoo"
        self.timeout = int(ICP.get_param("monta.api.timeout") or 25)

    # ----------------------------- HTTP helpers -----------------------------

    def _encode_sku(self, sku: str) -> str:
        """Encode SKU for path-segment usage (handles spaces, '/', etc.)."""
        return quote(str(sku), safe="")

    def _get_json(self, url, params=None):
        auth = (self.user, self.pwd) if self.user or self.pwd else None
        try:
            r = requests.get(
                url,
                params=params or {},
                auth=auth,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except Exception as e:
            return None, 0, f"HTTP error: {e}"

        if not r.ok:
            body = (r.text or "")[:300]
            return None, r.status_code, body

        try:
            return r.json(), r.status_code, None
        except Exception:
            return None, r.status_code, "Non-JSON body"

    # ---------------------------- Monta parsing -----------------------------

    def _get_product_stock(self, raw_sku):
        """
        Returns tuple: (stock_available: float|None, min_stock: float|None, normalized_sku)
        Calls: GET {base}/product/{sku}/stock?channel={channel}
        """
        sku = (raw_sku or "").strip()
        if not sku:
            return None, None, sku

        url = f"{self.base}/product/{self._encode_sku(sku)}/stock"
        params = {"channel": self.channel}
        data, code, err = self._get_json(url, params)

        if err or not isinstance(data, dict):
            _logger.warning("Monta %s -> HTTP %s body=%r", url, code, err)
            return None, None, sku

        # StockAvailable could be at top-level or nested under "Stock"
        stock_available = None
        candidates = ("StockAvailable", "Available", "FreeToSell", "Stock")
        for c in candidates:
            if c in data and isinstance(data[c], (int, float, str)):
                try:
                    stock_available = float(data[c])
                    break
                except Exception:
                    pass

        if stock_available is None and isinstance(data.get("Stock"), dict):
            for c in ("StockAvailable", "Available", "FreeToSell"):
                if c in data["Stock"] and data["Stock"][c] is not None:
                    try:
                        stock_available = float(data["Stock"][c])
                        break
                    except Exception:
                        pass

        min_stock = None
        if "MinimumStock" in data and data["MinimumStock"] is not None:
            try:
                min_stock = float(data["MinimumStock"])
            except Exception:
                min_stock = None

        return stock_available, min_stock, data.get("Sku") or sku

    # --------------------------- Odoo stock helpers -------------------------

    def _main_stock_location(self, company):
        """
        Return the 'Stock' internal location under the first warehouse of the company.
        Fallback to any internal location of that company.
        """
        Warehouse = self.env["stock.warehouse"].sudo()
        Location = self.env["stock.location"].sudo()

        wh = Warehouse.search([("company_id", "=", company.id)], limit=1)
        if wh:
            # In modern Odoo, warehouse lot_stock_id is the main internal location
            stock_loc = wh.lot_stock_id
            if stock_loc and stock_loc.usage == "internal":
                return stock_loc

        # Fallback: any internal location of company
        loc = Location.search(
            [("company_id", "=", company.id), ("usage", "=", "internal")], limit=1
        )
        return loc or Location.search([("usage", "=", "internal")], limit=1)

    def _available_in_location(self, product, location):
        """Current available (sum of quants) at given location."""
        Quant = self.env["stock.quant"].sudo()
        qty = 0.0
        # performance-friendly: aggregate from quants domain
        quants = Quant.search(
            [("product_id", "=", product.id), ("location_id", "child_of", location.id)]
        )
        for q in quants:
            qty += q.quantity - q.reserved_quantity
        return qty

    def _set_absolute_qty(self, product, location, target_qty):
        """
        Force available quantity at location to target by posting delta with
        stock.quant._update_available_quantity. Clamp negatives to 0.
        """
        if target_qty is None:
            return

        target_qty = max(0.0, float(target_qty))
        Quant = self.env["stock.quant"].sudo()
        current = self._available_in_location(product, location)
        delta = target_qty - current
        # ignore float noise
        if abs(delta) < 1e-6:
            return
        try:
            Quant._update_available_quantity(product, location, delta)
        except Exception as e:
            _logger.warning(
                "Skipping direct qty update for [%s] %s (reason: %s)",
                product.default_code or "",
                product.display_name,
                e,
            )
            return
        _logger.info(
            "Adjusted [%s] %s at %s by %+s (to %s)",
            product.default_code or "",
            product.display_name,
            location.display_name,
            delta,
            target_qty,
        )

    # ------------------------- Kit (phantom BOM) math -----------------------

    def _find_phantom_bom(self, product_tmpl):
        """
        Find a phantom (kit) BoM for the template.
        """
        BoM = self.env["mrp.bom"].sudo()
        return BoM.search(
            [("product_tmpl_id", "=", product_tmpl.id), ("type", "=", "phantom")],
            limit=1,
        )

    def _available_packs_from_components(self, product_variant, location):
        """
        Compute how many kit packs are possible from its components in 'location'.
        Clamp negative component availability to 0. Return int >= 0.
        """
        bom = self._find_phantom_bom(product_variant.product_tmpl_id)
        if not bom:
            return 0

        candidates = []
        for line in bom.bom_line_ids:
            comp = line.product_id  # product.product
            avail_comp = self._available_in_location(comp, location)
            avail_comp = max(0.0, avail_comp)  # clamp

            # convert available into the line uom
            avail_in_line_uom = comp.uom_id._compute_quantity(
                avail_comp, line.product_uom_id or comp.uom_id
            )
            req = float(line.product_qty or 0.0)
            if req <= 0:
                continue
            candidates.append(math.floor(avail_in_line_uom / req))

        return int(max(0, min(candidates))) if candidates else 0

    # ------------------------------- Runner ---------------------------------

    def run(self, limit=None):
        """
        For every product variant having a SKU (default_code):
          • Pull StockAvailable + MinimumStock from Monta
          • If product is NOT a kit (no phantom BoM) -> set absolute physical qty
          • If product IS a kit -> don't touch qty; log packs possible from components
          • Set product.template.available_threshold from Monta MinimumStock
        """
        Product = self.env["product.product"].sudo()
        dom = [("default_code", "!=", False), ("active", "=", True)]
        products = Product.search(dom, limit=limit)
        _logger.info("MontaQtySync: processing %s products", len(products))

        for p in products:
            raw_sku = p.default_code
            stock_available, min_stock, normalized_sku = self._get_product_stock(raw_sku)

            # Apply clamps for physical stock
            if stock_available is not None:
                try:
                    stock_available = max(0.0, float(stock_available))
                except Exception:
                    stock_available = None

            # Update available_threshold at template level if Monta provides MinimumStock
            if min_stock is not None and p.product_tmpl_id.exists():
                try:
                    p.product_tmpl_id.write({"available_threshold": float(min_stock)})
                    _logger.info(
                        "Set available_threshold=%s on %s",
                        float(min_stock),
                        p.product_tmpl_id.display_name,
                    )
                except Exception as e:
                    _logger.warning(
                        "Could not set available_threshold for %s: %s",
                        p.product_tmpl_id.display_name,
                        e,
                    )

            # If we can't read stock, continue
            if stock_available is None:
                continue

            # Choose target location
            location = self._main_stock_location(p.company_id or self.env.company)
            if not location:
                _logger.warning(
                    "No internal Stock location found for company %s; skip %s",
                    (p.company_id or self.env.company).name,
                    p.display_name,
                )
                continue

            # Phantom BoM (kit) -> never update qty directly
            bom = self._find_phantom_bom(p.product_tmpl_id)
            if bom:
                packs_by_components = self._available_packs_from_components(p, location)
                # clamp negative components result to 0
                packs_by_components = max(0, packs_by_components)
                _logger.info(
                    "KIT [%s] %s: components allow ~%s pack(s) at %s "
                    "(StockAvailable from Monta=%s, MinStock=%s)",
                    p.default_code or "",
                    p.display_name,
                    packs_by_components,
                    location.display_name,
                    stock_available,
                    min_stock,
                )
                # Do not touch kit quants
                continue

            # Normal product -> set absolute quantity
            self._set_absolute_qty(p, location, stock_available)
