# -*- coding: utf-8 -*-
import logging
import math
from urllib.parse import quote

from odoo import api, SUPERUSER_ID
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class MontaQtySync:
    """
    Pull StockAvailable & MinimumStock from Monta and push into Odoo:

      • For normal items:
          - Set absolute available qty at the company's main WH 'Stock' location
      • For KIT/PACK items (phantom BoM):
          - DO NOT update kit quants
          - Compute & log the theoretical number of packs available from component stock
      • Low-stock threshold:
          - If website installed -> product.template.available_threshold
          - Else -> create/update stock.warehouse.orderpoint.product_min_qty

    NO custom fields are created.
    """

    def __init__(self, env_like):
        # Build a robust sudo Environment regardless of what we get.
        base_env = None
        if hasattr(env_like, "env") and hasattr(env_like.env, "cr"):  # recordset
            base_env = env_like.env
        elif hasattr(env_like, "cr") and hasattr(env_like, "context"):  # Environment
            base_env = env_like
        else:
            raise UserError("MontaQtySync: invalid environment object passed to service.")

        self.env = api.Environment(base_env.cr, SUPERUSER_ID, dict(base_env.context or {}))
        self.icp = self.env["ir.config_parameter"].sudo()

        # Config
        self.base_url = (self.icp.get_param("monta.base_url") or "https://api-v6.monta.nl").rstrip("/")
        self.user = self.icp.get_param("monta.username") or ""
        self.pwd = self.icp.get_param("monta.password") or ""
        self.channel = self.icp.get_param("monta.channel") or "Moyee_Odoo"
        self.timeout = int(self.icp.get_param("monta.timeout") or 20)

        # Requests
        try:
            import requests
            from requests.auth import HTTPBasicAuth  # noqa: F401
        except Exception as e:
            raise UserError(f"Python 'requests' library is required: {e}")

        self._requests = requests
        self._auth = requests.auth.HTTPBasicAuth(self.user, self.pwd)

    # -------------------- HTTP --------------------

    def _get_product_stock(self, raw_sku):
        """
        GET /product/{sku}/stock?channel=...
        Returns (stock_available: float|None, min_stock: float|None, normalized_sku: str|None)
        """
        # Encode SKU per Monta docs (slashes, spaces, etc.)
        sku = quote(str(raw_sku or ""), safe="")  # encodes '/' -> %2F, ' ' -> %20, ...
        url = f"{self.base_url}/product/{sku}/stock"
        try:
            r = self._requests.get(
                url,
                params={"channel": self.channel},
                auth=self._auth,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except Exception as e:
            _logger.error("Monta HTTP error for %s: %s", raw_sku, e)
            return None, None, None

        if not r.ok:
            _logger.warning("Monta %s -> HTTP %s body=%s", url, r.status_code, (r.text or "")[:200])
            return None, None, None

        try:
            data = r.json()
        except Exception:
            _logger.warning("Non-JSON from %s: %s", url, (r.text or "")[:200])
            return None, None, None

        rec = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        stock = rec.get("Stock") or {}

        def num(v):
            try:
                return float(v) if v is not None else None
            except Exception:
                return None

        stock_available = num(stock.get("StockAvailable"))
        min_stock = num(rec.get("MinimumStock"))
        normalized_sku = (rec.get("Sku") or raw_sku or "").strip()
        return stock_available, min_stock, normalized_sku

    # -------------------- Odoo helpers --------------------

    def _get_main_wh_stock_location(self, company):
        """Use the first warehouse of the company; adjust at its lot_stock_id."""
        WH = self.env["stock.warehouse"].sudo().search([("company_id", "=", company.id)], limit=1, order="id")
        if not WH:
            raise UserError(f"No warehouse found for company '{company.name}'")
        return WH.lot_stock_id

    def _available_in_location(self, product, location):
        """Available = quantity - reserved across quants for that location."""
        Quant = self.env["stock.quant"].sudo()
        quants = Quant.search([("product_id", "=", product.id), ("location_id", "=", location.id)])
        avail = 0.0
        for q in quants:
            avail += (q.quantity - q.reserved_quantity)
        return avail

    def _set_absolute_qty(self, product, location, target_qty):
        """
        Set absolute available qty at a location by moving the delta
        using stock.quant helper (no picking docs, mirrors ground truth).
        """
        if target_qty is None:
            return
        Quant = self.env["stock.quant"].sudo()
        current = self._available_in_location(product, location)
        delta = float(target_qty) - current
        if abs(delta) < 1e-6:
            return
        # This will raise on kits; we catch earlier, but keep try/except as safety.
        try:
            Quant._update_available_quantity(product, location, delta)
        except Exception as e:
            _logger.warning("Skipping direct qty update for %s (reason: %s)", product.display_name, e)
            return
        _logger.info(
            "Adjusted [%s] %s at %s by %+s (to %s)",
            product.default_code or "", product.display_name, location.display_name, delta, target_qty
        )

    # ---- KIT detection & available packs computation ----

    def _is_kit(self, product_variant):
        """
        Detects a KIT (phantom BoM) for either the variant or its template.
        """
        tmpl = product_variant.product_tmpl_id
        # Variant-specific BoM or template BoM, but type = 'phantom'
        boms = self.env["mrp.bom"].sudo().search([
            "|",
            ("product_id", "=", product_variant.id),
            ("product_tmpl_id", "=", tmpl.id),
        ])
        return any(b.type == "phantom" for b in boms)

    def _available_packs_from_components(self, product_variant, location):
        """
        For a phantom BoM, compute how many full packs could be made now
        from component availability at 'location'.
        """
        tmpl = product_variant.product_tmpl_id
        # Prefer variant BoM if present, else template BoM; only phantom
        Bom = self.env["mrp.bom"].sudo()
        bom = Bom.search([("product_id", "=", product_variant.id), ("type", "=", "phantom")], limit=1)
        if not bom:
            bom = Bom.search([("product_tmpl_id", "=", tmpl.id), ("type", "=", "phantom")], limit=1)
        if not bom:
            return None  # not a kit

        # For each line, compute how many packs possible from that component
        # considering quantities & units of measure.
        packs_candidates = []
        for line in bom.bom_line_ids:
            comp = line.product_id
            # available of component at location, in component's own UoM
            avail_comp = self._available_in_location(comp, location)  # in comp.uom_id
            # convert available to the line's UoM
            avail_in_line_uom = comp.uom_id._compute_quantity(avail_comp, line.product_uom_id or comp.uom_id)

            req_per_pack_in_line_uom = line.product_qty  # already in line.product_uom_id
            if not req_per_pack_in_line_uom or req_per_pack_in_line_uom <= 0:
                continue

            packs_from_this_component = math.floor(avail_in_line_uom / req_per_pack_in_line_uom)
            packs_candidates.append(packs_from_this_component)

        if not packs_candidates:
            return 0
        return int(min(packs_candidates))

    def _apply_low_stock_threshold(self, tmpl, minimum):
        """
        If website is installed, set available_threshold on template.
        Otherwise create/update a Reordering Rule (orderpoint) with product_min_qty.
        """
        if minimum is None:
            return
        minimum = float(minimum)

        # Prefer Website 'available_threshold' if present on this DB
        if "available_threshold" in tmpl._fields:
            if (tmpl.available_threshold or 0.0) != minimum:
                tmpl.available_threshold = minimum
                _logger.info("Set available_threshold=%s on %s", minimum, tmpl.display_name)
            return

        # Fallback: Reordering Rule
        company = tmpl.company_id or self.env.company
        location = self._get_main_wh_stock_location(company)
        wh = self.env["stock.warehouse"].sudo().search([("lot_stock_id", "=", location.id)], limit=1)
        if not wh:
            return
        Orderpoint = self.env["stock.warehouse.orderpoint"].sudo()
        op = Orderpoint.search(
            [("product_id", "in", tmpl.product_variant_ids.ids), ("warehouse_id", "=", wh.id)],
            limit=1,
        )
        vals = {
            "product_id": tmpl.product_variant_id.id,
            "warehouse_id": wh.id,
            "location_id": location.id,
            "company_id": company.id,
            "product_min_qty": minimum,
            "product_max_qty": max(minimum, minimum),
        }
        if op:
            op.write({"product_min_qty": minimum})
            _logger.info("Updated orderpoint min_qty=%s for %s (WH %s)", minimum, tmpl.display_name, wh.display_name)
        else:
            Orderpoint.create(vals)
            _logger.info("Created orderpoint min_qty=%s for %s (WH %s)", minimum, tmpl.display_name, wh.display_name)

    # -------------------- Main --------------------

    def run(self, limit=None):
        """
        Iterate variants that have a Monta key (monta_sku OR default_code),
        fetch from Monta, set qty (skip kits), apply low-stock threshold,
        and log computed available packs for kits.
        """
        Product = self.env["product.product"].sudo()
        domain = ["|", ("monta_sku", "!=", False), ("default_code", "!=", False)]
        products = Product.search(domain, limit=limit) if limit else Product.search(domain)
        _logger.info("MontaQtySync: processing %s products", len(products))

        for p in products:
            raw_sku = (p.monta_sku or p.default_code or "").strip()
            if not raw_sku:
                continue

            stock_available, min_stock, normalized_sku = self._get_product_stock(raw_sku)
            # If Monta doesn't know it (404 → None, None, None), just skip.
            if stock_available is None and min_stock is None and not normalized_sku:
                continue

            tmpl = p.product_tmpl_id.sudo()
            company = tmpl.company_id or self.env.company
            loc = self._get_main_wh_stock_location(company)

            # 1) Normal item: set absolute qty; KIT item: skip direct quant update
            if self._is_kit(p):
                # Don’t touch quant on kits; compute theoretical packs and log
                packs = self._available_packs_from_components(p, loc)
                _logger.info(
                    "KIT [%s] %s: components allow ~%s pack(s) at %s "
                    "(StockAvailable from Monta=%s, MinStock=%s)",
                    p.default_code or "", p.display_name, packs, loc.display_name,
                    stock_available, min_stock
                )
            else:
                if stock_available is not None:
                    self._set_absolute_qty(p, loc, stock_available)

            # 2) Low stock threshold (MinimumStock) for both normal items and kits
            self._apply_low_stock_threshold(tmpl, min_stock)

        self.env.cr.commit()
        return True
