with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/static/src/css/monta_pickup.css', 'r') as f:
    content = f.read()

checkbox_css = """
/* ── Custom checkbox (pink filled checkmark) ── */
.monta-checkbox {
    appearance: none;
    -webkit-appearance: none;
    width: 20px;
    height: 20px;
    border: 2px solid #adb5bd;
    border-radius: 4px;
    background: #fff;
    cursor: pointer;
    position: relative;
    transition: border-color 0.15s, background-color 0.15s;
    flex-shrink: 0;
}

.monta-checkbox:checked {
    border-color: #E5007D;
    background-color: #E5007D;
}

.monta-checkbox:checked::after {
    content: '';
    position: absolute;
    left: 5px;
    top: 1px;
    width: 6px;
    height: 11px;
    border: solid white;
    border-width: 0 2px 2px 0;
    transform: rotate(45deg);
}
"""

if '.monta-checkbox' not in content:
    content = content.replace('.monta-radio:checked::after {', checkbox_css + '\n.monta-radio:checked::after {')
    with open('/Users/alihassan/Documents/Github/Monta-Odoo-Integration/static/src/css/monta_pickup.css', 'w') as f:
        f.write(content)
