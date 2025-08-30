{
    'name': 'Monta-Odoo Integration',
    'version': '1.0.0',
    'author': 'Ali Hassan4',
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
    'assets': {
    'web.assets_backend': [
        'Monta-Odoo-Integration/static/src/js/commitment_autofill.js',
    ],
    'web.assets_frontend': [
        'Monta-Odoo-Integration/static/src/js/commitment_autofill.js',
    ],
},
}
