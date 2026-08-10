import re

with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/__manifest__.py', 'r') as f:
    content = f.read()

# Remove website_sale_delivery
content = re.sub(
    r'\n\s*"website_sale_delivery",',
    '',
    content
)

with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/__manifest__.py', 'w') as f:
    f.write(content)
