# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
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


class OdooerMrpLandedCostWizard(models.TransientModel):
    _name = 'odooer.mrp.landed.cost.wizard'
    _description = 'Distribute Landed Cost to MOs'

    landed_cost_id = fields.Many2one(
        'stock.landed.cost', string='Landed Cost',
        required=True, readonly=True)
    line_ids = fields.One2many(
        'odooer.mrp.landed.cost.wizard.line', 'wizard_id',
        string='Manufacturing Orders')

    # ── Line population ───────────────────────────────────────────────────────

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

        # ── Step 1: total additional_landed_cost per incoming move ──────────
        incoming_cost_map = {}
        for adj_line in landed_cost.valuation_adjustment_lines:
            mid = adj_line.move_id.id
            if not mid:
                continue
            incoming_cost_map[mid] = (incoming_cost_map.get(mid, 0.0)
                                      + adj_line.additional_landed_cost)

        incoming_move_ids = list(incoming_cost_map.keys())
        if not incoming_move_ids:
            res['line_ids'] = []
            return res

        # ── Step 2: find FIFO links where the consumer is an MO component ──
        links = self.env['odooer.fifo.link'].search([
            ('incoming_move_id', 'in', incoming_move_ids),
            ('outgoing_move_id.raw_material_production_id', '!=', False),
        ])

        # ── Step 3: group by MO, compute proportional cost ──────────────────
        mo_data = {}  # {mo_id: {production_id, product_id, consumed_qty, amount}}
        for link in links:
            mo = link.outgoing_move_id.raw_material_production_id
            incoming = link.incoming_move_id
            total_incoming_cost = incoming_cost_map.get(incoming.id, 0.0)
            total_incoming_qty = incoming.quantity

            if total_incoming_qty > 0:
                link_cost = total_incoming_cost * (link.quantity / total_incoming_qty)
            else:
                link_cost = 0.0

            mid = mo.id
            if mid not in mo_data:
                mo_data[mid] = {
                    'production_id': mo.id,
                    'product_id': link.product_id.id,
                    'consumed_qty': 0.0,
                    'landed_cost_amount': 0.0,
                }
            mo_data[mid]['consumed_qty'] += link.quantity
            mo_data[mid]['landed_cost_amount'] += link_cost

        # ── Step 4: build wizard line values ────────────────────────────────
        line_vals = []
        for data in mo_data.values():
            line_vals.append([0, 0, {
                'production_id': data['production_id'],
                'product_id': data['product_id'],
                'consumed_qty': data['consumed_qty'],
                'landed_cost_amount': data['landed_cost_amount'],
                'selected': True,
            }])

        res['line_ids'] = line_vals
        return res

    # ── Create second landed cost ──────────────────────────────────────────

    def action_create_mo_landed_cost(self):
        self.ensure_one()
        selected_lines = self.line_ids.filtered('selected')
        if not selected_lines:
            raise UserError(_(
                'Please select at least one manufacturing order to apply the '
                'landed cost to.'
            ))

        original_lc = self.landed_cost_id
        if not original_lc.cost_lines:
            raise UserError(_(
                'The original landed cost has no cost lines. Cannot create '
                'a manufacturing landed cost without a service product.'
            ))

        original_cost_line = original_lc.cost_lines[0]
        mo_ids = selected_lines.mapped('production_id')
        total_amount = sum(selected_lines.mapped('landed_cost_amount'))

        if self.env['res.currency'].compare_amounts(total_amount, 0) <= 0:
            raise UserError(_(
                'The total landed cost amount to distribute is zero or '
                'negative. Nothing to create.'
            ))

        # Create the second landed cost targeting MO finished products
        new_lc = self.env['stock.landed.cost'].create({
            'target_model': 'manufacturing',
            'mrp_production_ids': [(6, 0, mo_ids.ids)],
            'cost_lines': [(0, 0, {
                'product_id': original_cost_line.product_id.id,
                'price_unit': total_amount,
                'split_method': original_cost_line.split_method,
                'account_id': original_cost_line.account_id.id,
            })],
        })

        return {
            'type': 'ir.actions.act_window',
            'name': _('Manufacturing Landed Cost'),
            'res_model': 'stock.landed.cost',
            'res_id': new_lc.id,
            'view_mode': 'form',
            'target': 'current',
        }
