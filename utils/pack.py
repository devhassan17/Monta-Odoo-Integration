# -*- coding: utf-8 -*-
from typing import List, Tuple
import logging

_logger = logging.getLogger(__name__)

def find_phantom_bom_for_variant(env, variant, company_id):
    Bom = env['mrp.bom']
    bom = False
    try:
        bom = Bom._bom_find(product=variant, company_id=company_id)
    except TypeError:
        bom = False
    except Exception as e:
        _logger.debug("[Monta] _bom_find failed for %s: %s", getattr(variant, 'display_name', variant.id), e)
    if bom and getattr(bom, 'type', None) == 'phantom':
        return bom
    domain = [
        ('product_tmpl_id', '=', variant.product_tmpl_id.id),
        ('type', '=', 'phantom'),
        '|', ('product_id', '=', variant.id), ('product_id', '=', False),
        '|', ('company_id', '=', company_id), ('company_id', '=', False),
    ]
    return Bom.search(domain, order='product_id desc', limit=1)

def explode_variant_components(env, variant, qty=1.0, company_id=None) -> Tuple[List[tuple], object]:
    comps: List[tuple] = []
    bom = find_phantom_bom_for_variant(env, variant, company_id or env.company.id)
    if not bom or getattr(bom, 'type', None) != 'phantom':
        return comps, bom or False
    try:
        bom_lines, _ops = bom.explode(variant, qty, picking_type=False)
        for line, data in bom_lines:
            cprod = line.product_id
            cqty = data.get('qty', 0.0)
            if cprod and cqty:
                comps.append((cprod, cqty))
    except Exception as e:
        _logger.error("[Monta Pack] explode failed for %s: %s", getattr(variant, 'display_name', variant.id), e)
    if not comps:
        for bl in bom.bom_line_ids:
            cprod = bl.product_id
            cqty = (bl.product_qty or 0.0) * (qty or 1.0)
            if cprod and cqty:
                comps.append((cprod, cqty))
    return comps, bom

def get_pack_components_from_bom(env, company_id, product, qty) -> List[tuple]:
    components: List[tuple] = []
    try:
        comps, bom = explode_variant_components(env, product, qty=qty, company_id=company_id)
        if comps:
            return comps
        if bom and getattr(bom, 'type', None) != 'phantom':
            return components
    except Exception as e:
        _logger.error("[Monta] Variant explode error for %s: %s", getattr(product, 'display_name', product.id), e)
    return components
