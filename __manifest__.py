# -*- coding: utf-8 -*-
{
    "name": "Monta-Odoo Integration",
    "version": "1.1.0",  # bumped because we added new inbound features
    "author": "Ali Hassan Mudassar",
    "category": "Sales",
    "summary": "Bi-directional Monta ↔ Odoo integration (orders out + inbound tracking/status)",
    "license": "LGPL-3",
    "website": "",
    "depends": [
        "sale_management",
        "mrp",
    ],
    "data": [
        # security
        "security/ir.model.access.csv",

        # views (NEW)
        "views/sale_order_inbound_views.xml",

        # cron/scheduled actions (NEW)
        "data/ir_cron_monta_inbound.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
