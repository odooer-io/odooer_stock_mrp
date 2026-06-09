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
            res['line_ids'] = []
            return res

        # ── Steps 2–3: aggregate FIFO links by (MO, incoming move) via SQL ─
        self.env.cr.execute("""
            SELECT
                sm_out.raw_material_production_id AS mo_id,
                fl.incoming_move_id,
                fl.product_id,
                SUM(fl.quantity) AS consumed_qty
            FROM odooer_fifo_link fl
            JOIN stock_move sm_out ON sm_out.id = fl.outgoing_move_id
            WHERE fl.incoming_move_id = ANY(%(in_ids)s)
              AND sm_out.raw_material_production_id IS NOT NULL
            GROUP BY sm_out.raw_material_production_id,
                     fl.incoming_move_id, fl.product_id
        """, {'in_ids': incoming_move_ids})
        rows = self.env.cr.dictfetchall()

        # ── Step 4: compute proportional cost per row, group by MO ──────────
        incoming_moves = self.env['stock.move'].browse(incoming_move_ids)
        in_qty_map = {m.id: m.quantity for m in incoming_moves}

        # Unit landed cost per incoming move
        in_unit_cost_map = {
            mid: incoming_cost_map.get(mid, 0.0) / max(in_qty_map.get(mid, 0.0), 1e-9)
            for mid in incoming_move_ids
        }

        mo_data = {}  # key: (mo_id, product_id)
        for row in rows:
            mo_id = row['mo_id']
            in_id = row['incoming_move_id']
            consumed = row['consumed_qty']
            unit_cost = in_unit_cost_map.get(in_id, 0.0)
            cost = unit_cost * consumed

            key = (mo_id, row['product_id'])
            if key not in mo_data:
                mo_data[key] = {
                    'production_id': mo_id,
                    'product_id': row['product_id'],
                    'consumed_qty': 0.0,
                    'landed_cost_amount': 0.0,
                }
            mo_data[key]['consumed_qty'] += consumed
            mo_data[key]['landed_cost_amount'] += cost

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

        if tools.float_is_zero(total_amount, precision_digits=2) or total_amount < 0:
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
