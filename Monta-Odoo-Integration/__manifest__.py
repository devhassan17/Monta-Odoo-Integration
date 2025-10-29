{
    "name": "Monta-Odoo Integration",
    "version": "1.0.0",
    "summary": "Integrate Odoo with Monta WMS — orders, EDD, inbound forecasts and stock sync.",
    "description": """
Monta-Odoo Integration
======================
This module synchronizes Sales Orders, Inbound Forecasts, Expected Delivery Dates (EDD),
and stock quantities between Odoo and Monta WMS via Monta's API.

Main features
-------------
- Push Sales Orders to Monta and link Monta order refs to Odoo sale orders.
- Pull Monta order status updates and map Monta status to Odoo.
- Import Inbound Forecasts / Expected Delivery Dates into Odoo records.
- Sync quantities and product SKUs with Monta stock.
- Optional SO buttons and automation to control synchronization.

Important: This module communicates with the Monta API. Please see the app page and
privacy policy for details about what data is transmitted (order numbers, addresses,
product SKUs, quantities, and shipment metadata). Obtain explicit consent from
your end-users for any data transfer to Monta.
""",
    "author": "Atomixweb2",
    "website": "https://fairchain.org/monta-plugin-documentation/",
    "category": "Warehouse",
    "license": "LGPL-3",
    'images': ['static/description/banner.png'],
    "depends": [
        "sale_management",
        "mrp",
        "purchase",
        # add any other dependencies if needed, e.g. "stock", "stock_account"
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/monta_order_status_rules.xml",
        # views
        "views/monta_order_status_views.xml",
        "views/sale_order_monta_sync_button.xml",
        # other data files (examples)
        # "data/monta_status_mapping_data.xml",
        # "data/monta_cron_jobs.xml",
    ],
    "demo": [
        # add demo data files if you have any
    ],
    "price":199.99,
    "currency":"USD",
    "installable": True,
    "application": True,
    "auto_install": False,
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    # Optional: add support email (recommended)
    "support": "programmer.alihassan@gmail.com",
    # Optional: live demo url
    # "live_test_url": "https://demo.yoursite.com/",
}
