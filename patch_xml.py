import re

with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/website_sale_templates.xml', 'r') as f:
    content = f.read()

# 1. Change inherit_id from website_sale.checkout to website_sale.payment
content = content.replace('inherit_id="website_sale.checkout"', 'inherit_id="website_sale.payment"')

# 2. Simplify the xpath to just target delivery_method or o_payment_delivery_methods
content = re.sub(
    r'<xpath expr="[^"]*" position="before">',
    '<xpath expr="//*[@id=\'delivery_method\'] | //*[@id=\'o_payment_delivery_methods\']" position="before">',
    content
)

with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/website_sale_templates.xml', 'w') as f:
    f.write(content)
