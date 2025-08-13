# -*- coding: utf-8 -*-
from typing import Tuple
from odoo.api import Environment

def resolve_sku(product, env: Environment = None, allow_synthetic: bool = False) -> Tuple[str, str]:
    """
    STRICT resolver: never returns a synthetic SKU unless allow_synthetic=True is explicitly passed
    (we never pass True in this integration).
    Order:
      1) product.monta_sku
      2) product.default_code
      3) first supplierinfo product_code
      4) product.barcode
      5) product.template.default_code
      -> else: ('', 'missing')
    """
    # 1
    sku = getattr(product, 'monta_sku', False)
    if sku and sku.strip():
        return sku.strip(), 'monta_sku'

    # 2
    dcode = getattr(product, 'default_code', False)
    if dcode and dcode.strip():
        return dcode.strip(), 'default_code'

    # 3
    seller_rs = getattr(product, 'seller_ids', False)
    seller = seller_rs[:1] if seller_rs else False
    if seller and getattr(seller, 'product_code', False):
        code = (seller.product_code or '').strip()
        if code:
            return code, 'supplier_code'

    # 4
    barcode = getattr(product, 'barcode', False)
    if barcode and barcode.strip():
        return barcode.strip(), 'barcode'

    # 5
    tmpl = getattr(product, 'product_tmpl_id', False)
    if tmpl and getattr(tmpl, 'default_code', False):
        tcode = (tmpl.default_code or '').strip()
        if tcode:
            return tcode, 'template_default_code'

    # NEVER synthesize by default
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
