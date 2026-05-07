# -*- coding: utf-8 -*-
"""
Pack / kit expansion helpers
- Prefer phantom BoM on the variant
- Fall back to OCA product_pack
- Recursively flatten packs until only leaf (non-pack) products remain
"""
from typing import List, Tuple
import logging

_logger = logging.getLogger(__name__)


def _find_phantom_bom_for_variant(env, variant, company_id):
    """Return a phantom mrp.bom for the given variant (or False)."""
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
    # explicit search template-level phantom with variant preferred
    return Bom.search([
        ('product_tmpl_id', '=', variant.product_tmpl_id.id),
        ('type', '=', 'phantom'),
        '|', ('product_id', '=', variant.id), ('product_id', '=', False),
        '|', ('company_id', '=', company_id), ('company_id', '=', False),
    ], order='product_id desc', limit=1)


def _explode_bom(env, variant, qty, company_id) -> List[Tuple[object, float]]:
    """Explode phantom BoM for this variant; avoid self-references; fallback to raw lines."""
    comps: List[Tuple[object, float]] = []
    bom = _find_phantom_bom_for_variant(env, variant, company_id)
    if not bom or getattr(bom, 'type', None) != 'phantom':
        return comps
    try:
        lines, _ops = bom.explode(variant, qty, picking_type=False)
        for bl, data in lines:
            p = bl.product_id
            q = data.get('qty', 0.0)
            if p and q and p.id != variant.id:
                comps.append((p, float(q)))
    except Exception as e:
        _logger.error("[Monta Pack] explode failed for %s: %s", getattr(variant, 'display_name', variant.id), e)
    if not comps:
        for bl in bom.bom_line_ids:
            p = bl.product_id
            q = (bl.product_qty or 0.0) * (qty or 1.0)
            if p and q and p.id != variant.id:
                comps.append((p, float(q)))
    return comps


def _extract_oca_pack_lines(owner):
    for name in ('pack_line_ids', 'pack_lines', 'pack_line_ids_variant'):
        lines = getattr(owner, name, False)
        if lines:
            return list(lines)
    return []


def _oca_components(product, qty) -> List[Tuple[object, float]]:
    comps: List[Tuple[object, float]] = []
    lines = _extract_oca_pack_lines(product.product_tmpl_id) or _extract_oca_pack_lines(product)
    for line in lines:
        c = getattr(line, 'product_id', False) or getattr(line, 'item_id', False)
        q = (getattr(line, 'qty', 0.0) or getattr(line, 'quantity', 0.0) or
             getattr(line, 'product_qty', 0.0) or getattr(line, 'uom_qty', 0.0) or 0.0)
        if c and q:
            comps.append((c, float(q) * float(qty or 1.0)))
    return comps


def is_pack_like(env, product, company_id) -> bool:
    """Heuristic: has OCA pack lines or phantom BoM."""
    if getattr(product.product_tmpl_id, 'pack_line_ids', False) or getattr(product, 'pack_line_ids', False):
        return True
    return bool(_find_phantom_bom_for_variant(env, product, company_id))


def get_pack_components(env, company_id, product, qty) -> List[Tuple[object, float]]:
    """Try phantom BoM first, then OCA product_pack."""
    comps = _explode_bom(env, product, qty, company_id)
    return comps or _oca_components(product, qty)


def expand_to_leaf_components(env, company_id, product, qty, depth=0, seen=None) -> List[Tuple[object, float]]:
    """
    Recursively flatten packs until only non-pack (leaf) products remain.
    We NEVER return the pack itself as a leaf.
    """
    if seen is None:
        seen = set()
    key = (product._name, product.id)
    if key in seen or depth > 8:
        _logger.warning("[Monta Pack] recursion stop for %s", getattr(product, 'display_name', product.id))
        return []
    seen.add(key)

    if not is_pack_like(env, product, company_id):
        return [(product, float(qty or 0.0))]

    leaves: List[Tuple[object, float]] = []
    for c, q in get_pack_components(env, company_id, product, qty):
        if c.id == product.id:
            continue
        leaves.extend(expand_to_leaf_components(env, company_id, c, q, depth + 1, seen))
    return leaves
