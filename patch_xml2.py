import re

with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/website_sale_templates.xml', 'r') as f:
    content = f.read()

# 1. Update xpath to include payment_method fallback
content = re.sub(
    r'<xpath expr="[^"]*" position="before">',
    '<xpath expr="//*[@id=\'delivery_method\'] | //*[@id=\'o_payment_delivery_methods\'] | //*[@id=\'payment_method\']" position="before">',
    content
)

with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/views/website_sale_templates.xml', 'w') as f:
    f.write(content)
