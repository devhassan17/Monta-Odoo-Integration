# -*- coding: utf-8 -*-
import json, logging
from odoo import models
from ..utils.sku import resolve_sku
from ..utils.pack import explode_variant_components

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def action_monta_log_pack_variant_skus(self, per_pack_qty=1.0):
        """
        Iterate variants, explode phantom BoM, log component products with resolved SKUs.
        """
        Log = self.env['monta.sale.log'].sudo()
        for tmpl in self:
            _logger.info("[Monta Pack Scan] Template: %s (ID %s) — Variants: %s",
                         tmpl.display_name, tmpl.id, len(tmpl.product_variant_ids))
            pack_report = {
                'template': {'id': tmpl.id, 'name': tmpl.display_name},
                'variants': []
            }
            for v in tmpl.product_variant_ids:
                attrs = ", ".join(v.product_template_attribute_value_ids.mapped('name')) or "-"
                vsku, vsrc = resolve_sku(v, env=self.env, allow_synthetic=False)
                v_info = {
                    'variant_id': v.id,
                    'variant_name': v.display_name,
                    'attributes': attrs,
                    'variant_sku': vsku or 'EMPTY',
                    'variant_sku_source': vsrc,
                    'components': [],
                }
                comps, bom = explode_variant_components(self.env, v, qty=per_pack_qty)
                if not bom:
                    _logger.info("[Monta Pack Scan]  Variant: %s | No phantom BoM found.", v.display_name)
                else:
                    owner = bom.product_id.display_name if getattr(bom, 'product_id', False) else "TEMPLATE"
                    _logger.info("[Monta Pack Scan]  Variant: %s | BoM %s (%s)", v.display_name, bom.id, owner)
                for comp, q in comps:
                    csku, csrc = resolve_sku(comp, env=self.env, allow_synthetic=False)
                    v_info['components'].append({
                        'component_id': comp.id,
                        'component_name': comp.display_name,
                        'qty_per_pack': q,
                        'sku': csku or 'EMPTY',
                        'sku_source': csrc,
                    })
                    _logger.info("[Monta Pack Scan]    -> %s | qty=%s | SKU=%s (%s)",
                                 comp.display_name, q, (csku or 'EMPTY'), csrc)
                pack_report['variants'].append(v_info)
            Log.create({
                'name': f"Monta Pack Scan - {tmpl.display_name}",
                'sale_order_id': False,
                'level': 'info',
                'log_data': json.dumps(pack_report, indent=2, default=str),
            })
        return True
