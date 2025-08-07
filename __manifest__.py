{
    'name': 'Monta-Odoo-Integration',
    'version': '1.0',
    'summary': 'Integrates Odoo v18 with Monta WMS',
    'author': 'Ali Raza Jamil',
    'category': 'Inventory',
    'depends': ['sale_management', 'stock'],
    'data': [
        'security/ir.model.access.csv',
        'views/monta_config_view.xml',
    ],
    'installable': True,
    'application': True,
}
