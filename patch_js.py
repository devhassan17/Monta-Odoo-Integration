import re

with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/static/src/js/monta_pickup.js', 'r') as f:
    content = f.read()

# 1. Remove the code that closes the pickup box when clicking a delivery speed radio
content = re.sub(
    r"// Close pickup box if switching to standard delivery[\s\S]*?}\n\s*}",
    "",
    content
)

# 2. Update togglePickup event listener to handle unchecking and not affect speed cards
new_toggle_logic = """
            if (togglePickup.checked) {
                if (box) box.classList.add('show');
                autoDetectUserAddress();
                if (resultsDiv && resultsDiv.children.length === 0) {
                    await performSearch();
                }
            } else {
                if (box) box.classList.remove('show');
                try {
                    await rpc('/shop/monta/select_pickup_point', { shipper_code: false });
                } catch (e) {
                    console.error('Failed to clear pickup point:', e);
                }
            }
"""

content = re.sub(
    r"if \(\!togglePickup\.checked\) return;\s*// Sync visual active state[\s\S]*?await performSearch\(\);\s*}",
    new_toggle_logic.strip(),
    content
)

with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/static/src/js/monta_pickup.js', 'w') as f:
    f.write(content)
