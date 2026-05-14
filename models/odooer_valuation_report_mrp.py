# -*- coding: utf-8 -*-
from odoo import fields, models


class OdooerValuationReportMrp(models.Model):
    """Extends odooer.valuation.report with MRP (manufacturing order) data."""
    _inherit = 'odooer.valuation.report'

    production_id = fields.Many2one('mrp.production', string='Mfg Order', readonly=True)
    incoming_type = fields.Selection(
        selection_add=[('manufacturing', 'Manufacturing')],
    )

    # ── SQL hook overrides ────────────────────────────────────────────────────

    def _manufacturing_case(self):
        return """
            WHEN sm.production_id IS NOT NULL      THEN 'manufacturing'
            WHEN sm.unbuild_id IS NOT NULL         THEN 'manufacturing'
            WHEN sm.consume_unbuild_id IS NOT NULL THEN 'manufacturing'"""

    def _production_id_sql(self):
        return "sm.production_id,"

    def _mo_name_sql(self):
        return "COALESCE(mo.name, ub.name)"

    def _mrp_joins(self):
        return (
            "LEFT JOIN mrp_production mo ON mo.id = sm.production_id\n"
            "            LEFT JOIN mrp_unbuild ub "
            "ON ub.id = COALESCE(sm.unbuild_id, sm.consume_unbuild_id)"
        )
