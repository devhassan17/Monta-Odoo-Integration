{
    "name": "Monta-Odoo Integration",
    "version": "1.0.0",
    "author": "Ali Hassan34\5",
    "category": "Sales",
    "summary": "Monta WMS integration: Orders, EDD, Inbound Forecast",
    "depends": ["sale_management", "mrp", "purchase"],
    "data": [
        "security/ir.model.access.csv",
        "views/monta_order_status_views.xml",
        ],  # ← no XML
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
