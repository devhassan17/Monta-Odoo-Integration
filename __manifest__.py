{
    'name': 'Monta-Odoo-Integration',
    'version': '1.0',
    'summary': 'Two-way integration with Monta API v6',
    'description': """
        Integrates Odoo with Monta for order management, shipment tracking, and inventory synchronization.
        Provides two-way communication between Odoo and Monta.
    """,
    'author': 'Your Company',
    'website': 'https://www.yourcompany.com',
    'category': 'Inventory/Delivery',
    'depends': ['sale_management', 'stock'],
    'data': [
        'security/ir.model.access.csv',
        'views/monta_settings_views.xml',
        'views/sale_order_views.xml',
        'data/ir_cron_data.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}