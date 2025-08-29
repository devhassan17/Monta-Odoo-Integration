{
    'name': 'Monta-Odoo Integration',
    'version': '1.0.0',
    'author': 'Ali Hassan (Monta)',
    'category': 'Sales',
    'summary': 'Step 1: Log order creation for Monta integration',
    'depends': ['sale_management', "mrp"],
    'data': [
        'security/ir.model.access.csv',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
    
}
