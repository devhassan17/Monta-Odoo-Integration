# -*- coding: utf-8 -*-
def resolve_sku(product, env=None):
    """
    Return (sku, source) using:
    1) product.monta_sku
    2) product.default_code
    3) first supplierinfo code
    4) product.barcode
    5) template.default_code
    6) synthetic fallback (if enabled via ir.config_parameter)
    """
    sku = getattr(product, 'monta_sku', False)
    if sku:
        return sku.strip(), 'monta_sku'
    if getattr(product, 'default_code', False):
        return product.default_code.strip(), 'default_code'
    seller = product.seller_ids[:1]
    if seller and seller.product_code:
        return seller.product_code.strip(), 'supplier_code'
    if getattr(product, 'barcode', False):
        return product.barcode.strip(), 'barcode'
    tmpl = getattr(product, 'product_tmpl_id', False)
    if tmpl and getattr(tmpl, 'default_code', False):
        return tmpl.default_code.strip(), 'template_default_code'

    # === LAST RESORT (synthetic) ===
    if env is not None:
        ICP = env['ir.config_parameter'].sudo()
        allow_syn = ICP.get_param('monta.allow_synthetic_sku', '1')
        prefix = ICP.get_param('monta.synthetic_sku_prefix', 'SYN-')
        if str(allow_syn).lower() in ('1', 'true', 'yes'):
            return f"{prefix}{product.id}", 'synthetic'
    return '', 'missing'
