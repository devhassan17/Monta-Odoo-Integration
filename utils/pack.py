# -*- coding: utf-8 -*-
"""
Pack / kit component expansion helpers.

Primary goal: resolve *variant-specific* components for products that behave as packs.
- Prefer phantom BoM (mrp) on the product variant.
- Fall back to reading OCA product_pack relations if present (handled in sale_order.py).
"""
from typing import List, Tuple
import logging

_logger = logging.getLogger(__name__)


def find_phantom_bom_for_variant(env, variant, company_id):
    """
    Return an mrp.bom for the given variant, preferring phantom type.
    Tries mrp.bom._bom_find when available (Odoo >= 13), with fallback search.
    """
    Bom = env['mrp.bom']
    bom = False
    try:
        # _bom_find respects company and variant; may return template-level BoM
        bom = Bom._bom_find(product=variant, company_id=company_id)
    except TypeError:
        bom = False
    except Exception as e:
        _logger.debug("[Monta] _bom_find failed for %s: %s", getattr(variant, 'display_name', variant.id), e)

    if bom:
        return bom if getattr(bom, 'type', None) == 'phantom' else False

    # Explicit search: template phantom with exact variant match preferred
    domain = [
        ('product_tmpl_id', '=', variant.product_tmpl_id.id),
        ('type', '=', 'phantom'),
        '|', ('product_id', '=', variant.id), ('product_id', '=', False),
        '|', ('company_id', '=', company_id), ('company_id', '=', False),
    ]
    return Bom.search(domain, order='product_id desc', limit=1)


def explode_variant_components(env, variant, qty=1.0, company_id=None) -> Tuple[List[tuple], object]:
    """
    Use phantom BoM to explode components for THIS variant and qty.

    :return: (components[(product, qty)], bom_record or False)
    """
    comps: List[tuple] = []
    bom = find_phantom_bom_for_variant(env, variant, company_id or env.company.id)

    if not bom or getattr(bom, 'type', None) != 'phantom':
        return comps, bom or False

    # Prefer mrp.bom.explode (handles UoM routes, efficiencies, etc.)
    try:
        bom_lines, _ops = bom.explode(variant, qty, picking_type=False)
        for line, data in bom_lines:
            cprod = line.product_id
            cqty = data.get('qty', 0.0)
            if cprod and cqty:
                comps.append((cprod, cqty))
    except Exception as e:
        _logger.error("[Monta Pack] explode failed for %s: %s", getattr(variant, 'display_name', variant.id), e)

    # Fallback: direct bom lines if explode produced nothing
    if not comps:
        for bl in bom.bom_line_ids:
            cprod = bl.product_id
            cqty = (bl.product_qty or 0.0) * (qty or 1.0)
            if cprod and cqty:
                comps.append((cprod, cqty))

    return comps, bom


def get_pack_components_from_bom(env, company_id, product, qty) -> List[tuple]:
    """
    Return components [(product, qty)] for phantom BoM:
    - Variant-first (_bom_find or targeted search)
    - explode() with fallback to direct lines
    """
    components: List[tuple] = []

    # Try full variant explode first
    try:
        comps, bom = explode_variant_components(env, product, qty=qty, company_id=company_id)
        if comps:
            return comps
        # If a non-phantom BoM was returned, we do not treat this as a pack
        if bom and getattr(bom, 'type', None) != 'phantom':
            return components
    except Exception as e:
        _logger.error("[Monta] Variant explode error for %s: %s", getattr(product, 'display_name', product.id), e)

    # If explode didn't work but we still have a phantom BoM, read its lines
    Bom = env['mrp.bom']
    bom = False
    try:
        bom = Bom._bom_find(product=product, company_id=company_id)
    except TypeError:
        bom = False
    except Exception as e:
        _logger.debug("[Monta] _bom_find (2nd pass) failed for %s: %s", getattr(product, 'display_name', product.id), e)

    if not bom:
        bom = Bom.search([
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
            ('type', '=', 'phantom'),
            '|', ('product_id', '=', product.id), ('product_id', '=', False),
            '|', ('company_id', '=', company_id), ('company_id', '=', False),
        ], order='product_id desc', limit=1)

    if not bom or getattr(bom, 'type', None) != 'phantom':
        return components

    # Final fallback to direct lines
    for bl in bom.bom_line_ids:
        comp = bl.product_id
        comp_qty = (bl.product_qty or 0.0) * (qty or 1.0)
        if comp and comp_qty:
            components.append((comp, comp_qty))

    return components
