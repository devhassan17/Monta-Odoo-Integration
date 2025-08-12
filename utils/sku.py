# -*- coding: utf-8 -*-
def resolve_sku(product):
    """Return (sku, source) using monta_sku → default_code → supplier code → barcode."""
    sku = getattr(product, 'monta_sku', False)
    if sku:
        return sku.strip(), 'monta_sku'
    if product.default_code:
        return product.default_code.strip(), 'default_code'
    seller = product.seller_ids[:1]
    if seller and seller.product_code:
        return seller.product_code.strip(), 'supplier_code'
    if product.barcode:
        return product.barcode.strip(), 'barcode'
    return '', 'missing'
