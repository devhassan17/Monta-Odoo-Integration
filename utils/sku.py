# -*- coding: utf-8 -*-
"""
SKU resolution helpers for Monta integration.

Order of resolution:
1) product.monta_sku
2) product.default_code
3) first supplierinfo code
4) product.barcode
5) template.default_code
6) optional synthetic fallback (config-driven)
"""
from typing import Tuple
from odoo.api import Environment

def resolve_sku(product, env: Environment = None, allow_synthetic: bool = True) -> Tuple[str, str]:
    """
    Return (sku, source) with an optional synthetic fallback.
    """
    sku = getattr(product, 'monta_sku', False)
    if sku:
        s = sku.strip()
        if s:
            return s, 'monta_sku'

    dcode = getattr(product, 'default_code', False)
    if dcode:
        s = dcode.strip()
        if s:
            return s, 'default_code'

    seller = getattr(product, 'seller_ids', False)
    seller = seller[:1] if seller else False
    if seller and seller.product_code:
        s = (seller.product_code or '').strip()
        if s:
            return s, 'supplier_code'

    barcode = getattr(product, 'barcode', False)
    if barcode:
        s = barcode.strip()
        if s:
            return s, 'barcode'

    tmpl = getattr(product, 'product_tmpl_id', False)
    if tmpl and getattr(tmpl, 'default_code', False):
        s = (tmpl.default_code or '').strip()
        if s:
            return s, 'template_default_code'

    # LAST RESORT (synthetic)
    if allow_synthetic:
        prefix = 'SYN-'
        if env is not None:
            ICP = env['ir.config_parameter'].sudo()
            allow_syn_cfg = str(ICP.get_param('monta.allow_synthetic_sku', '1')).lower()
            if allow_syn_cfg not in ('1', 'true', 'yes'):
                return '', 'missing'
            prefix = ICP.get_param('monta.synthetic_sku_prefix', prefix) or prefix
        return f"{prefix}{product.id}", 'synthetic'

    return '', 'missing'

def resolve_sku_strict(product, env: Environment = None) -> Tuple[str, str]:
    """Same as resolve_sku but NEVER generates a synthetic SKU."""
    return resolve_sku(product, env=env, allow_synthetic=False)
