# -*- coding: utf-8 -*-
{
    "name": "Monta-Odoo Integration",
    "version": "1.0.1",
    "summary": "Integrate Odoo with Monta WMS — orders, EDD, inbound forecasts and stock sync.",
    "description": """
Monta-Odoo Integration
======================
This module synchronizes Sales Orders, Inbound Forecasts, Expected Delivery Dates (EDD),
and stock quantities between Odoo and Monta WMS via Monta's API.
""",
    "author": "Atomixweb9",
    "website": "https://fairchain.org/monta-plugin-documentation/",
    "category": "Warehouse",
    "license": "LGPL-3",
    "images": ["static/description/banner.png"],
    "depends": [
        "sale_management",
        "account",
        "portal",
        "mrp",
        "purchase",
        "sale_subscription",
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/monta_order_status_rules.xml",
        "views/monta_menu.xml",                 # <-- add this first
        "views/monta_order_status_views.xml",
        "views/sale_order_monta_sync_button.xml",
        "views/monta_config_views.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "support": "programmer.alihassan@gmail.com",
    "price": 199.99,
    "currency": "USD",
}
