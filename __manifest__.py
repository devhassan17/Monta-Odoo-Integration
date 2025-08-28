# -*- coding: utf-8 -*-
{
    "name": "Monta-Odoo Integration",
    "version": "1.1.3",  # bumped to ensure reload
    "author": "Ali Hassan Mudasar",
    "category": "Sales",
    "summary": "Bi-directional Monta ↔ Odoo integration (orders out + inbound, no XML)",
    "website": "",
    "license": "LGPL-3",
    "depends": ["sale_management", "mrp"],
    "data": [
        # keep your existing ACLs only; no new XML files
        "security/ir.model.access.csv",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
