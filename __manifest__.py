{
    "name": "Monta-Odoo Integration",
    "version": "1.0.1",
    "author": "Ali Hassan58",
    "category": "Sales",
    "summary": "Monta WMS integration: Orders, EDD, Inbound Forecast",
    "depends": ["sale_management", "mrp", "purchase"],
    "data": [
        "security/ir.model.access.csv",
        "security/monta_order_status_rules.xml",

        # views
        "views/monta_order_status_views.xml",
        # optional SO buttons
        "views/sale_order_monta_sync_button.xml",
    ],
    "installable": True,
    "auto_install": False,
    "application": True,
    "license": "LGPL-3",
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "assets": {
        "web.assets_backend": [
            "Monta-Odoo-Integration/static/src/js/commitment_autofill.js",
        ],
        "web.assets_frontend": [
            "Monta-Odoo-Integration/static/src/js/commitment_autofill.js",
        ],
    },
}
