{
    "name": "Monta-Odoo Integration",
    "version": "1.0.0",
    "author": "Ali Hassan16",
    "category": "Sales",
    "summary": "Monta WMS integration: Orders, EDD, Inbound Forecast",
    "depends": ["sale_management", "mrp", "purchase"],
    "data": [],  # ← no XML
    "installable": True,
    "auto_install": False,
    "application": False,
    "license": "LGPL-3",
    # hooks (pure Python)
    "uninstall_hook": "uninstall_hook",
    # If you don't create anything on install, omit post_init_hook
    # "post_init_hook": "post_init_hook",
    "assets": {
        "web.assets_backend": [
            "Monta-Odoo-Integration/static/src/js/commitment_autofill.js",
        ],
        "web.assets_frontend": [
            "Monta-Odoo-Integration/static/src/js/commitment_autofill.js",
        ],
    },
}
