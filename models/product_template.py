# -*- coding: utf-8 -*-
import json
import logging

from odoo import models

from ..utils.pack import expand_to_leaf_components, get_pack_components
from ..utils.sku import resolve_sku

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    def action_monta_log_pack_variant_skus(self, per_pack_qty=1.0, flatten=False):
        """
        For each variant:
          - If flatten=False: log direct components from the pack (phantom BoM first, then OCA pack)
          - If flatten=True:  recursively expand to leaf components (no packs), aggregating only real SKUs
        All SKUs are resolved STRICTLY (no synthetic).

        Adds one monta.sale.log record per template and also writes to server logs.
        """
        env = self.env
        company_id = env.company.id
        Log = env["monta.sale.log"].sudo()

        per_pack_qty = float(per_pack_qty or 0.0)
        mode = "flatten" if flatten else "direct_components"

        for tmpl in self:
            variants = tmpl.product_variant_ids
            _logger.info(
                "[Monta Pack Scan] Template: %s (ID %s) — Variants: %s",
                tmpl.display_name,
                tmpl.id,
                len(variants),
            )

            report = {
                "template": {"id": tmpl.id, "name": tmpl.display_name},
                "mode": mode,
                "variants": [],
            }

            for v in variants:
                v_info = {
                    "variant_id": v.id,
                    "variant_name": v.display_name,
                    "attributes": ", ".join(v.product_template_attribute_value_ids.mapped("name")) or "-",
                    "per_pack_qty": per_pack_qty,
                    "components": [],
                }

                if flatten:
                    items = expand_to_leaf_components(env, company_id, v, per_pack_qty)
                    if not items:
                        _logger.info("[Monta Pack Scan]  Variant: %s | no leaf components found", v.display_name)
                else:
                    items = get_pack_components(env, company_id, v, per_pack_qty)
                    if not items:
                        _logger.info("[Monta Pack Scan]  Variant: %s | no direct components found", v.display_name)

                for comp, q in items or []:
                    sku, src = resolve_sku(comp, env=env, allow_synthetic=False)
                    v_info["components"].append(
                        {
                            "component_id": comp.id,
                            "component_name": comp.display_name,
                            "qty": float(q or 0.0),
                            "sku": sku or "EMPTY",
                            "sku_source": src,
                        }
                    )
                    _logger.info(
                        "[Monta Pack Scan]    -> %s | qty=%s | SKU=%s (%s)",
                        comp.display_name,
                        q,
                        (sku or "EMPTY"),
                        src,
                    )

                report["variants"].append(v_info)

            Log.create(
                {
                    "name": f"Monta Pack Scan - {tmpl.display_name}",
                    "sale_order_id": False,
                    "level": "info",
                    "log_data": json.dumps(report, ensure_ascii=False, indent=2, default=str),
                }
            )

        return True
