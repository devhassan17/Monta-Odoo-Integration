# -*- coding: utf-8 -*-
from pygments import highlight
from pygments.lexers import JsonLexer
from pygments.formatters import TerminalFormatter
import json

from odoo import models
from ..utils.sku import resolve_sku_strict
from ..utils.pack import expand_to_leaf_components


class ProductTemplate(models.Model):
    _inherit = "product.template"

    def action_monta_log_pack_variant_skus(self, per_pack_qty=1.0, flatten=False):
        self.ensure_one()
        out = {
            "template": {"id": self.id, "name": self.name},
            "qty_per_pack": per_pack_qty,
            "flatten": flatten,
            "variants": [],
        }

        tmpl = self.with_context(active_test=False)
        variants = tmpl.product_variant_ids

        for v in variants:
            v_data = {
                "id": v.id,
                "display_name": v.display_name,
                "attributes": ", ".join(v.product_template_attribute_value_ids.mapped("name")) or "-",
                "components": [],
            }
            if flatten:
                leaves = expand_to_leaf_components(self.env, self.env.company.id, v, per_pack_qty)
                for comp, q in leaves:
                    sku, src = resolve_sku_strict(comp, self.env)
                    v_data["components"].append(
                        {
                            "id": comp.id,
                            "name": comp.display_name,
                            "qty": q,
                            "resolved_sku": sku or "MISSING",
                            "sku_source": src,
                        }
                    )
            else:
                sku, src = resolve_sku_strict(v, self.env)
                v_data["components"].append(
                    {
                        "id": v.id,
                        "name": v.display_name,
                        "qty": per_pack_qty,
                        "resolved_sku": sku or "MISSING",
                        "sku_source": src,
                    }
                )
            out["variants"].append(v_data)

        js = json.dumps(out, indent=2, ensure_ascii=False)
        print(highlight(js, JsonLexer(), TerminalFormatter()))
        return True
