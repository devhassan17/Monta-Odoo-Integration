{
    "name": "Monta-Odoo-Integration",
    "version": "1.0", 
    "summary": "Integrates Odoo with Monta API",
    "author": "Your Name",
    "category": "Warehouse",
    "depends": ["base", "sale", "stock"],
    "data": [
        "security/ir.model.access.csv",
        "views/monta_config_views.xml", 
        "views/monta_order_views.xml"
    ],
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}