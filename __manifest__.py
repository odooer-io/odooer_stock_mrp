# -*- coding: utf-8 -*-
{
    'name': 'Odooer Stock MRP',
    'summary': 'MRP (Manufacturing) integration for Odooer Stock reports',
    'description': """
Odooer Stock MRP
================
Extends the Odooer Stock inventory valuation report to include manufacturing
order data when the MRP module is installed.

Adds:
- Manufacturing Order column on the valuation report
- Manufacturing incoming type and filter
- Manufacturing source in the value-source breakdown
    """,
    'version': '19.0.1.1.0',
    'category': 'Inventory/Inventory',
    'license': 'LGPL-3',
    'author': 'chitswe',
    'website': 'https://github.com/odooer-io/odooer_stock_mrp',
    'depends': [
        'odooer_stock',
        'mrp',
    ],
    'data': [
        'security/ir.model.access.csv',
        'wizards/odooer_mrp_landed_cost_wizard_views.xml',
        'views/odooer_valuation_report_mrp_views.xml',
        'views/stock_landed_cost_mrp_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
