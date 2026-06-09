# -*- coding: utf-8 -*-
from odoo import fields, models


class StockLandedCost(models.Model):
    _inherit = 'stock.landed.cost'

    parent_landed_cost_id = fields.Many2one(
        'stock.landed.cost', string='Source Landed Cost',
        readonly=True, index=True,
        help="Original raw material landed cost that this MO landed "
             "cost was distributed from.")
