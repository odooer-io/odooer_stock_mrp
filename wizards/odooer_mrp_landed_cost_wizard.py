# -*- coding: utf-8 -*-
from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError


class OdooerMrpLandedCostWizardLine(models.TransientModel):
    _name = 'odooer.mrp.landed.cost.wizard.line'
    _description = 'MO Landed Cost Distribution Line'

    wizard_id = fields.Many2one(
        'odooer.mrp.landed.cost.wizard', string='Wizard',
        required=True, ondelete='cascade')
    production_id = fields.Many2one(
        'mrp.production', string='Manufacturing Order',
        required=True, readonly=True)
    product_id = fields.Many2one(
        'product.product', string='Raw Material',
        readonly=True)
    consumed_qty = fields.Float(
        string='Consumed Qty', digits='Product Unit of Measure',
        readonly=True)
    landed_cost_amount = fields.Float(
        string='Landed Cost Amount', digits='Product Price',
        readonly=True,
        help="Proportional landed cost for this MO based on consumed quantity.")
    selected = fields.Boolean(string='Apply', default=True)
    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        related='wizard_id.landed_cost_id.company_id.currency_id')


class OdooerMrpLandedCostWizard(models.TransientModel):
    _name = 'odooer.mrp.landed.cost.wizard'
    _description = 'Distribute Landed Cost to MOs'

    landed_cost_id = fields.Many2one(
        'stock.landed.cost', string='Landed Cost',
        required=True, readonly=True)
    mo_count = fields.Integer(string='Manufacturing Orders Found', readonly=True)
    product_count = fields.Integer(string='Raw Material Products', readonly=True)
    total_amount = fields.Float(
        string='Total Amount to Distribute', readonly=True,
        digits='Product Price',
        help="Total proportional landed cost share that should flow to finished products.")
    line_ids = fields.One2many(
        'odooer.mrp.landed.cost.wizard.line', 'wizard_id',
        string='Manufacturing Orders')

    # ── Core aggregation ──────────────────────────────────────────────────────

    def _build_mo_data(self, landed_cost):
        """
        Run the FIFO-link aggregation for landed_cost.
        Returns a dict keyed by (mo_id, product_id) — empty dict if nothing found.
        """
        grouped = self.env['stock.valuation.adjustment.lines'].read_group(
            [('cost_id', '=', landed_cost.id), ('move_id', '!=', False)],
            ['move_id', 'additional_landed_cost:sum'],
            ['move_id'],
            lazy=False,
        )
        incoming_cost_map = {
            g['move_id'][0]: g['additional_landed_cost']
            for g in grouped
            if g.get('move_id')
        }
        incoming_move_ids = list(incoming_cost_map.keys())
        if not incoming_move_ids:
            return {}

        self.env.cr.execute("""
            SELECT
                sm_out.raw_material_production_id AS mo_id,
                fl.incoming_move_id,
                fl.product_id,
                SUM(fl.quantity) AS consumed_qty
            FROM odooer_fifo_link fl
            JOIN stock_move sm_out ON sm_out.id = fl.outgoing_move_id
            WHERE fl.incoming_move_id = ANY(%s)
              AND sm_out.raw_material_production_id IS NOT NULL
            GROUP BY sm_out.raw_material_production_id,
                     fl.incoming_move_id, fl.product_id
        """, (incoming_move_ids,))
        rows = self.env.cr.dictfetchall()

        incoming_moves = self.env['stock.move'].browse(incoming_move_ids)
        in_qty_map = {m.id: m.quantity for m in incoming_moves}
        in_unit_cost_map = {
            mid: incoming_cost_map.get(mid, 0.0) / max(in_qty_map.get(mid, 0.0), 1e-9)
            for mid in incoming_move_ids
        }

        mo_data = {}
        for row in rows:
            mo_id = row['mo_id']
            in_id = row['incoming_move_id']
            consumed = row['consumed_qty']
            key = (mo_id, row['product_id'])
            if key not in mo_data:
                mo_data[key] = {
                    'production_id': mo_id,
                    'product_id': row['product_id'],
                    'consumed_qty': 0.0,
                    'landed_cost_amount': 0.0,
                }
            mo_data[key]['consumed_qty'] += consumed
            mo_data[key]['landed_cost_amount'] += in_unit_cost_map.get(in_id, 0.0) * consumed

        return mo_data

    # ── Summary population (fast open, no transient line creation) ────────────

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        landed_cost_id = self.env.context.get('default_landed_cost_id')
        if not landed_cost_id:
            return res

        landed_cost = self.env['stock.landed.cost'].browse(landed_cost_id)
        if not landed_cost.exists():
            return res

        res['landed_cost_id'] = landed_cost.id

        mo_data = self._build_mo_data(landed_cost)
        if mo_data:
            res['mo_count'] = len({d['production_id'] for d in mo_data.values()})
            res['product_count'] = len({d['product_id'] for d in mo_data.values()})
            res['total_amount'] = sum(d['landed_cost_amount'] for d in mo_data.values())

        return res

    # ── Review & select (lazy line creation) ─────────────────────────────────

    def action_review_and_select(self):
        """Create transient lines on demand, then open a full-page paginated view."""
        self.ensure_one()
        if not self.line_ids:
            mo_data = self._build_mo_data(self.landed_cost_id)
            if not mo_data:
                raise UserError(_('No manufacturing orders found to distribute costs to.'))
            self.env['odooer.mrp.landed.cost.wizard.line'].create([
                {
                    'wizard_id': self.id,
                    'production_id': d['production_id'],
                    'product_id': d['product_id'],
                    'consumed_qty': d['consumed_qty'],
                    'landed_cost_amount': d['landed_cost_amount'],
                    'selected': True,
                }
                for d in mo_data.values()
            ])
        return {
            'type': 'ir.actions.act_window',
            'name': _('Select Manufacturing Orders'),
            'res_model': 'odooer.mrp.landed.cost.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'view_id': self.env.ref(
                'odooer_stock_mrp.odooer_mrp_landed_cost_wizard_detail_form'
            ).id,
            'target': 'current',
        }

    # ── Shared LC creation logic ───────────────────────────────────────────────

    def _create_mo_landed_cost(self, mo_ids, total_amount):
        original_lc = self.landed_cost_id
        if not original_lc.cost_lines:
            raise UserError(_(
                'The original landed cost has no cost lines. Cannot create '
                'a manufacturing landed cost without a service product.'
            ))
        unfinished = mo_ids.filtered(lambda m: m.state != 'done')
        if unfinished:
            raise UserError(_(
                'Some selected manufacturing orders are not done:\n%s\n\n'
                'Only completed MOs have finished product moves that can '
                'receive landed costs.',
            ) % '\n'.join('• ' + name for name in unfinished.mapped('name')))
        if tools.float_is_zero(total_amount, precision_digits=2) or total_amount < 0:
            raise UserError(_(
                'The total landed cost amount to distribute is zero or '
                'negative. Nothing to create.'
            ))
        original_lc_total = sum(original_lc.cost_lines.mapped('price_unit'))
        mo_share_ratio = total_amount / original_lc_total if original_lc_total else 1.0
        cost_lines_vals = [(0, 0, {
            'product_id': cl.product_id.id,
            'price_unit': cl.price_unit * mo_share_ratio,
            'split_method': cl.split_method,
            'account_id': cl.account_id.id,
        }) for cl in original_lc.cost_lines]
        new_lc = self.env['stock.landed.cost'].create({
            'target_model': 'manufacturing',
            'parent_landed_cost_id': original_lc.id,
            'mrp_production_ids': [(6, 0, mo_ids.ids)],
            'cost_lines': cost_lines_vals,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Manufacturing Landed Cost'),
            'res_model': 'stock.landed.cost',
            'res_id': new_lc.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ── Create actions ─────────────────────────────────────────────────────────

    def action_create_all_mo_landed_cost(self):
        """Create MO LC for ALL detected MOs — no selection required."""
        self.ensure_one()
        mo_data = self._build_mo_data(self.landed_cost_id)
        if not mo_data:
            raise UserError(_('No manufacturing orders found to distribute costs to.'))
        mo_ids = self.env['mrp.production'].browse(
            list({d['production_id'] for d in mo_data.values()})
        )
        total_amount = sum(d['landed_cost_amount'] for d in mo_data.values())
        return self._create_mo_landed_cost(mo_ids, total_amount)

    def action_create_mo_landed_cost(self):
        """Create MO LC for the SELECTED lines (called from the detail form)."""
        self.ensure_one()
        selected_lines = self.line_ids.filtered('selected')
        if not selected_lines:
            raise UserError(_(
                'Please select at least one manufacturing order to apply the '
                'landed cost to.'
            ))
        mo_ids = selected_lines.mapped('production_id')
        total_amount = sum(selected_lines.mapped('landed_cost_amount'))
        return self._create_mo_landed_cost(mo_ids, total_amount)
