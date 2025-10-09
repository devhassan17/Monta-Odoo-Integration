# Monta-Odoo-Integration/services/monta_qty_sync.py

import logging
import math
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

from odoo import _
from odoo.tools import float_is_zero

_logger = logging.getLogger(__name__)

STOCK_PATH_SUFFIX = "/stock"  # GET /product/{sku}/stock


@dataclass
class MontaStock:
    available: float
    minimum: float


class MontaQtySync:
    """
    Pulls Monta stock and writes to Odoo without custom fields:
      • For non-kits, set on-hand to Monta StockAvailable at the main stock location
      • Always set product.template.available_threshold = Monta MinimumStock
      • For kits/phantom packs, never write kit stock; compute feasible packs from components
    """

    def __init__(self, env):
        # IMPORTANT: do NOT call env.sudo() here; safe for server-action/cron context
        self.env = env
        ICP = env["ir.config_parameter"].sudo()

        # Helper to read either the "new" keys you use, or the legacy ones.
        def _param(*names, default=None):
            for n in names:
                v = ICP.get_param(n)
                if v and str(v).strip():
                    return str(v).strip()
            return default

        # Accept both schemes:
        #   new: monta.base_url / monta.username / monta.password / monta.channel / monta.timeout
        #   old: monta.api.base_url / monta.api.user / monta.api.password / monta.api.channel / monta.api.timeout
        self.base = (_param("monta.base_url", "monta.api.base_url", default="https://api-v6.monta.nl") or "").rstrip("/")
        self.user = _param("monta.username", "monta.api.user", default="") or ""
        self.pwd = _param("monta.password", "monta.api.password", default="") or ""
        self.channel = _param("monta.channel", "monta.api.channel", default="MoyeeCoffe_odoo") or ""
        try:
            self.timeout = int(_param("monta.timeout", "monta.api.timeout", default="20"))
        except Exception:
            self.timeout = 20

        if not self.user or not self.pwd:
            _logger.warning(
                "MontaQtySync: Missing Basic Auth credentials "
                "(set monta.username/monta.password or monta.api.user/monta.api.password)."
            )
        else:
            _logger.info(
                "MontaQtySync: using base=%s channel=%s timeout=%s (user=****, pwd=****)",
                self.base, self.channel, self.timeout
            )

    # -------- HTTP --------
    def _get_product_stock(self, sku: str) -> Optional[MontaStock]:
        # Monta allows slashes if encoded as %2F; urllib.parse.quote handles this.
        safe_sku = urllib.parse.quote(sku, safe="")
        url = f"{self.base}/product/{safe_sku}{STOCK_PATH_SUFFIX}"
        params = {"channel": self.channel} if self.channel else {}

        try:
            r = requests.get(
                url,
                params=params,
                auth=HTTPBasicAuth(self.user, self.pwd),
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except Exception as e:
            _logger.warning("Monta GET %s failed: %s", url, e)
            return None

        if r.status_code == 404:
            _logger.warning("Monta %s -> HTTP 404 body=%r", url, (r.text or "")[:200])
            return None
        if not r.ok:
            _logger.warning("Monta %s -> HTTP %s body=%r", url, r.status_code, (r.text or "")[:200])
            return None

        try:
            data = r.json() or {}
        except Exception:
            _logger.warning("Monta %s -> non-JSON body=%r", url, (r.text or "")[:200])
            return None

        stock_available = None
        minimum_stock = None
        if isinstance(data, dict):
            # Some tenants return flattened fields; some under Stock{}
            if data.get("MinimumStock") is not None:
                try:
                    minimum_stock = float(data["MinimumStock"])
                except Exception:
                    minimum_stock = None

            if data.get("StockAvailable") is not None:
                try:
                    stock_available = float(data["StockAvailable"])
                except Exception:
                    stock_available = None

            stock_node = data.get("Stock")
            if isinstance(stock_node, dict) and stock_node.get("StockAvailable") is not None:
                try:
                    stock_available = float(stock_node["StockAvailable"])
                except Exception:
                    pass

        if stock_available is None and minimum_stock is None:
            return None

        return MontaStock(float(stock_available or 0.0), float(minimum_stock or 0.0))

    # -------- Odoo helpers --------
    def _company_main_stock_location(self, company):
        StockLocation = self.env["stock.location"]
        return StockLocation.search(
            [("usage", "=", "internal"), ("company_id", "=", company.id)],
            order="complete_name asc",
            limit=1,
        )

    def _set_template_threshold(self, template, minimum: float):
        try:
            template.with_context(tracking_disable=True).write({"available_threshold": minimum})
            _logger.info("Set available_threshold=%s on %s", minimum, template.display_name)
        except Exception as e:
            _logger.warning("Failed to set available_threshold on %s: %s", template.display_name, e)

    def _is_kit(self, product) -> bool:
        MrpBom = self.env["mrp.bom"]
        bom = MrpBom.search([("product_id", "=", product.id)], limit=1)
        if not bom:
            bom = MrpBom.search([("product_tmpl_id", "=", product.product_tmpl_id.id), ("product_id", "=", False)], limit=1)
        return bool(bom and bom.type == "phantom")

    def _kit_max_packs_from_components(self, product, wh_location, monta_avail: float) -> Tuple[float, str]:
        MrpBom = self.env["mrp.bom"]
        Quant = self.env["stock.quant"]

        bom = MrpBom.search([("product_id", "=", product.id)], limit=1)
        if not bom:
            bom = MrpBom.search([("product_tmpl_id", "=", product.product_tmpl_id.id), ("product_id", "=", False)], limit=1)
        if not bom or bom.type != "phantom":
            return 0.0, "no phantom BoM"

        possible = math.inf
        for line in bom.bom_line_ids:
            comp = line.product_id
            need = line.product_qty or 0.0
            if float_is_zero(need, precision_rounding=comp.uom_id.rounding):
                continue
            quants = Quant.search([("product_id", "=", comp.id), ("location_id", "child_of", wh_location.id)])
            onhand = sum(q.quantity for q in quants)
            if need > 0:
                possible = min(possible, onhand / need)

        if math.isinf(possible):
            possible = 0.0
        # never promise more packs than Monta component availability suggests
        capped = min(possible, max(0.0, monta_avail))
        return max(0.0, float(capped)), f"components allow ~{int(max(0, math.floor(capped)))} pack(s)"

    def _set_absolute_onhand(self, product, target_qty: float, wh_location) -> Optional[str]:
        # Never adjust kits directly (phantom BoM / pack)
        if self._is_kit(product):
            return "is kit (phantom) – skip direct qty change"
        if target_qty < 0:
            return "negative target – skip direct qty change"

        try:
            now_qty = product.with_context(location=wh_location.id).qty_available
        except Exception:
            now_qty = 0.0

        delta = target_qty - now_qty
        if float_is_zero(delta, precision_rounding=product.uom_id.rounding):
            return None

        try:
            wiz = self.env["stock.change.product.qty"].create({
                "product_id": product.id,
                "new_quantity": target_qty,
                "location_id": wh_location.id,
            })
            wiz.change_product_qty()
            _logger.info(
                "Adjusted [%s] %s at %s by %+s (to %s)",
                product.default_code or product.display_name,
                product.display_name,
                wh_location.complete_name,
                delta,
                target_qty,
            )
            return None
        except Exception as e:
            _logger.warning(
                "Skipping direct qty update for [%s] %s (reason: %s)",
                product.default_code or product.display_name,
                product.display_name,
                e,
            )
            return str(e)

    # -------- main --------
    def run(self, limit=None):
        Product = self.env["product.product"]
        company = self.env.company
        wh_loc = self._company_main_stock_location(company)
        if not wh_loc:
            _logger.warning("No internal stock location found for company %s; aborting.", company.name)
            return

        # include items having either monta_sku or default_code
        domain = [
            ("active", "=", True),
            ("type", "in", ["product", "consu"]),
            "|", ("monta_sku", "!=", False),
                 ("default_code", "!=", False),
        ]
        products = Product.search(domain, limit=limit)
        _logger.info("MontaQtySync: processing %s products", len(products))

        for prod in products:
            # packs may have no SKU: we use component SKUs via kit check; for non-kits use monta_sku/default_code
            sku = (prod.monta_sku or prod.default_code or "").strip()
            if not sku:
                continue

            ms = self._get_product_stock(sku)
            if not ms:
                continue

            # Always update website threshold from Monta MinimumStock
            self._set_template_threshold(prod.product_tmpl_id, ms.minimum)

            # Non-kits: set on-hand absolutely; Kits: compute feasibility only (no write)
            reason = self._set_absolute_onhand(prod, ms.available, wh_loc)
            if reason and ("kit" in reason or "phantom" in reason):
                packs, desc = self._kit_max_packs_from_components(prod, wh_loc, ms.available)
                _logger.info(
                    "KIT [%s] %s: %s at %s (StockAvailable from Monta=%s, MinStock=%s)",
                    prod.default_code or prod.display_name,
                    prod.display_name,
                    desc,
                    wh_loc.complete_name,
                    ms.available,
                    ms.minimum,
                )
