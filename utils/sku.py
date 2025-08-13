# -*- coding: utf-8 -*-
"""
STRICT SKU resolver (NO synthetic).
Order:
  1) product.monta_sku
  2) product.default_code
  3) first supplierinfo product_code
  4) product.barcode
  5) template.default_code
Else -> ('', 'missing')
"""
from typing import Tuple
from odoo.api import Environment


def resolve_sku(product, env: Environment = None, allow_synthetic: bool = False) -> Tuple[str, str]:
    sku = getattr(product, 'monta_sku', False)
    if sku and sku.strip():
        return sku.strip(), 'monta_sku'

    dcode = getattr(product, 'default_code', False)
    if dcode and dcode.strip():
        return dcode.strip(), 'default_code'

    seller_rs = getattr(product, 'seller_ids', False)
    seller = seller_rs[:1] if seller_rs else False
    if seller and getattr(seller, 'product_code', False):
        code = (seller.product_code or '').strip()
        if code:
            return code, 'supplier_code'

    barcode = getattr(product, 'barcode', False)
    if barcode and barcode.strip():
        return barcode.strip(), 'barcode'

    tmpl = getattr(product, 'product_tmpl_id', False)
    if tmpl and getattr(tmpl, 'default_code', False):
        tcode = (tmpl.default_code or '').strip()
        if tcode:
            return tcode, 'template_default_code'

    return '', 'missing'


def resolve_sku_strict(product, env: Environment = None) -> Tuple[str, str]:
    return resolve_sku(product, env=env, allow_synthetic=False)
