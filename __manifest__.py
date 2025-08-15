# -*- coding: utf-8 -*-
{
    "name": "Monta-Odoo Integration",
    "version": "1.1.1",
    "author": "Ali Hassan ",
    "category": "Sales",
    "summary": "Bi-directional Monta ↔ Odoo integration (no XML views)",
    "website": "",
    "license": "LGPL-3",
    "depends": [
        "sale_management",
        "mrp",
    ],
    "data": [
        # keep your existing ACLs only
        "security/ir.model.access.csv",
    ],
    # no 'views' or 'data' items at all
    "installable": True,
    "application": False,
    "auto_install": False,
}
