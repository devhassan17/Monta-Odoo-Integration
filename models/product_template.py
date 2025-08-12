# -*- coding: utf-8 -*-
from odoo import models
import json, logging
from ..utils.sku import resolve_sku
from ..utils.pack import explode_variant_components

_logger = logging.getLogger(__name__)

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def action_monta_log_pack_variant_skus(self, per_pack_qty=1.0):
        """
        For each template in self:
          - Iterate all variants, explode phantom BoM
          - Log component products with resolved SKUs
        """
        Log = self.env['monta.sale.log'].sudo()
        for tmpl in self:
            _logger.info(f"[Monta Pack Scan] Template: {tmpl.display_name} (ID {tmpl.id}) — Variants: {len(tmpl.product_variant_ids)}")
            pack_report = {
                'template': {'id': tmpl.id, 'name': tmpl.display_name},
                'variants': []
            }
            for v in tmpl.product_variant_ids:
                attrs = ", ".join(v.product_template_attribute_value_ids.mapped('name')) or "-"
                vsku, vsrc = resolve_sku(v)
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
                    _logger.info(f"[Monta Pack Scan]  Variant: {v.display_name} | No phantom BoM found.")
                else:
                    owner = bom.product_id.display_name if bom.product_id else "TEMPLATE"
                    _logger.info(f"[Monta Pack Scan]  Variant: {v.display_name} | BoM {bom.id} ({owner})")
                for comp, q in comps:
                    csku, csrc = resolve_sku(comp)
                    v_info['components'].append({
                        'component_id': comp.id,
                        'component_name': comp.display_name,
                        'qty_per_pack': q,
                        'sku': csku or 'EMPTY',
                        'sku_source': csrc,
                    })
                    _logger.info(f"[Monta Pack Scan]    -> {comp.display_name} | qty={q} | SKU={(csku or 'EMPTY')} ({csrc})")
                pack_report['variants'].append(v_info)
            Log.create({
                'name': f"Monta Pack Scan - {tmpl.display_name}",
                'sale_order_id': False,
                'level': 'info',
                'log_data': json.dumps(pack_report, indent=2, default=str),
            })
        return True
