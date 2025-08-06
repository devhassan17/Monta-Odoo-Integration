{
    "name": "Monta-Odoo-Integration",
    "version": "1.0",
    "summary": "Integrates Odoo with Monta API",
    "author": "Ali Raza Jamil",
    "category": "Warehouse",
    "depends": ["base", "sale", "stock"],
    "data": [
        "security/ir.model.access.csv",
        "models/monta_config.py",  # Ensure model loads first
        "views/monta_config_views.xml",
        "views/monta_order_views.xml"
    ],
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}