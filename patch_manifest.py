import re

with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/__manifest__.py', 'r') as f:
    content = f.read()

# Add website_sale_delivery to depends if not present
if '"website_sale_delivery"' not in content and "'website_sale_delivery'" not in content:
    content = re.sub(
        r'("website_sale",\s*|(?<="website_sale",\n)\s*)("delivery",\s*)',
        r'\1\2\n        "website_sale_delivery",\n',
        content
    )
    with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/__manifest__.py', 'w') as f:
        f.write(content)
