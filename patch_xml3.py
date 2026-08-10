import re

with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/website_sale_templates.xml', 'r') as f:
    content = f.read()

# 1. Change inherit_id back to website_sale.checkout
content = content.replace('inherit_id="website_sale.payment"', 'inherit_id="website_sale.checkout"')

# 2. Use bulletproof xpath with [1] index
content = re.sub(
    r'<xpath expr="[^"]*" position="before">',
    '<xpath expr="(//*[@id=\'o_delivery_methods\'] | //*[@id=\'delivery_method\'] | //t[@t-call=\'website_sale.checkout_delivery\'] | //t[@t-call=\'website_sale.billing_address_row\'])[1]" position="before">',
    content
)

with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/website_sale_templates.xml', 'w') as f:
    f.write(content)
