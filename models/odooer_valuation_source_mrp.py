# -*- coding: utf-8 -*-
from odoo import fields, models


class OdooerValuationSourceMrp(models.Model):
    """Extends odooer.valuation.source with the manufacturing order source type."""
    _inherit = 'odooer.valuation.source'

    source_type = fields.Selection(
        selection_add=[('production', 'Manufacturing')],
    )

    def _parts(self):
        parts = super()._parts()
        parts.append("""
            SELECT
                sm.id::bigint * 10 + 9          AS id,
                sm.id                            AS incoming_move_id,
                'production'                     AS source_type,
                mp.name                          AS reference,
                NULL::integer                    AS account_move_id,
                NULL::integer                    AS landed_cost_id,
                sm.value                         AS value,
                comp.currency_id                 AS currency_id,
                sm.date                          AS date
            FROM stock_move  sm
            JOIN mrp_production mp   ON mp.id   = sm.production_id
            JOIN res_company   comp ON comp.id  = sm.company_id
            WHERE sm.is_in = TRUE AND sm.production_id IS NOT NULL
        """)
        return parts
