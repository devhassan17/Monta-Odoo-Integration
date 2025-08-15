# -*- coding: utf-8 -*-
{
    "name": "Monta-Odoo Integration",
    "version": "1.1.2",  # bump so Odoo reloads
    "author": "Ali Hassan Hassan",
    "category": "Sales",
    "summary": "Bi-directional Monta ↔ Odoo integration (no XML views or data)",
    "website": "",
    "license": "LGPL-3",
    "depends": ["sale_management", "mrp"],
    "data": [
        "security/ir.model.access.csv",   # keep only this if you already had it
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
