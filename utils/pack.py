# -*- coding: utf-8 -*-
import logging
_logger = logging.getLogger(__name__)

def find_phantom_bom_for_variant(env, variant, company_id):
    Bom = env['mrp.bom']
    bom = False
    try:
        bom = Bom._bom_find(product=variant, company_id=company_id)
    except TypeError:
        bom = False
    if not bom:
        bom = Bom.search([
            ('product_tmpl_id', '=', variant.product_tmpl_id.id),
            ('type', '=', 'phantom'),
            '|', ('product_id', '=', variant.id), ('product_id', '=', False),
            '|', ('company_id', '=', company_id), ('company_id', '=', False),
        ], order='product_id desc', limit=1)
    return bom

def explode_variant_components(env, variant, qty=1.0):
    """Return (components[(product, qty)], bom) for THIS variant using phantom BoM."""
    comps = []
    bom = find_phantom_bom_for_variant(env, variant, env.company.id)
    if not bom or bom.type != 'phantom':
        return comps, bom
    try:
        b_lines, _ops = bom.explode(variant, qty, picking_type=False)
        for bl, data in b_lines:
            cprod = bl.product_id
            cqty = data.get('qty', 0.0)
            if cprod and cqty:
                comps.append((cprod, cqty))
    except Exception as e:
        _logger.error(f"[Monta Pack Scan] explode failed for {variant.display_name}: {e}")
    return comps, bom

def get_pack_components_from_bom(env, company_id, product, qty):
    """Return [(product, qty)] for phantom BoM components, else [] (Odoo 18-safe)."""
    components = []
    Bom = env['mrp.bom']
    bom = False
    try:
        bom = Bom._bom_find(product=product, company_id=company_id)
    except TypeError:
        bom = False
    if not bom:
        bom = Bom.search([
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
            ('type', '=', 'phantom'),
            '|', ('product_id', '=', product.id), ('product_id', '=', False),
            '|', ('company_id', '=', company_id), ('company_id', '=', False),
        ], order='product_id desc', limit=1)
    if bom and bom.type == 'phantom':
        try:
            bom_lines, _ops = bom.explode(product, qty, picking_type=False)
            for line, line_data in bom_lines:
                comp = line.product_id
                comp_qty = line_data.get('qty', 0.0)
                if comp and comp_qty:
                    components.append((comp, comp_qty))
        except Exception as e:
            _logger.error(f"[Monta] BoM explode failed for {product.display_name}: {e}")
    return components
