{
    "name": "Monta-Odoo-Integration",
    "version": "1.0",
    "summary": "Integrates Odoo with Monta API for order syncing and shipment tracking",
    "author": "Ali Raza Jamil",
    "category": "Warehouse",
    "depends": ["base", "sale", "stock"],
    "data": [
        "views/monta_config_views.xml",
        "views/monta_order_views.xml",
        "security/ir.model.access.csv"
    ],
    "installable": True,
    "application": True,
}
