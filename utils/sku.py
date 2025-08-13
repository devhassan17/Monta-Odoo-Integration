# -*- coding: utf-8 -*-
from typing import Tuple
from odoo.api import Environment

def resolve_sku(product, env: Environment = None, allow_synthetic: bool = False) -> Tuple[str, str]:
    """
    Strict SKU resolver: NO synthetic by default.
    Order: product.monta_sku → default_code → supplierinfo → barcode → template.default_code.
    """
    sku = getattr(product, 'monta_sku', False)
    if sku and sku.strip():
        return sku.strip(), 'monta_sku'

    dcode = getattr(product, 'default_code', False)
    if dcode and dcode.strip():
        return dcode.strip(), 'default_code'

    seller = getattr(product, 'seller_ids', False)
    seller = seller[:1] if seller else False
    if seller and getattr(seller, 'product_code', False):
        code = (seller.product_code or '').strip()
        if code:
            return code, 'supplier_code'

    barcode = getattr(product, 'barcode', False)
    if barcode and barcode.strip():
        return barcode.strip(), 'barcode'

    tmpl = getattr(product, 'product_tmpl_id', False)
    if tmpl and getattr(tmpl, 'default_code', False) and tmpl.default_code.strip():
        return tmpl.default_code.strip(), 'template_default_code'

    # Only if explicitly allowed (we default to False and never pass True)
    if allow_synthetic:
        prefix = 'SYN-'
        if env is not None:
            ICP = env['ir.config_parameter'].sudo()
            if str(ICP.get_param('monta.allow_synthetic_sku', '0')).lower() not in ('1', 'true', 'yes'):
                return '', 'missing'
            prefix = ICP.get_param('monta.synthetic_sku_prefix', prefix) or prefix
        return f"{prefix}{product.id}", 'synthetic'

    return '', 'missing'

def resolve_sku_strict(product, env: Environment = None) -> Tuple[str, str]:
    return resolve_sku(product, env=env, allow_synthetic=False)
