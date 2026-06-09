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
    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        related='wizard_id.landed_cost_id.company_id.currency_id')


class OdooerMrpLandedCostWizard(models.TransientModel):
    _name = 'odooer.mrp.landed.cost.wizard'
    _description = 'Distribute Landed Cost to MOs'
    _rec_name = 'landed_cost_id'

    landed_cost_id = fields.Many2one(
        'stock.landed.cost', string='Landed Cost',
        required=True, readonly=True)
    mo_count = fields.Integer(string='Manufacturing Orders Found', readonly=True)
    product_count = fields.Integer(string='Raw Material Products', readonly=True)
    total_amount = fields.Float(
        string='Total Amount to Distribute', readonly=True,
        digits='Product Price',
        help="Total proportional landed cost share that should flow to finished products.")
    non_mo_move_count = fields.Integer(
        string='Other Usage (non-MO)', readonly=True,
        help="Number of outgoing moves (deliveries, scraps, etc.) that consumed "
             "from this receipt before LC validation — these cannot be redistributed.")
    undistributable_amount = fields.Float(
        string='Undistributable Amount', readonly=True, digits='Product Price',
        help="LC share attributed to non-MO consumption (deliveries, scraps, etc.) "
             "before validation. This amount stays in the expense account.")
    line_ids = fields.One2many(
        'odooer.mrp.landed.cost.wizard.line', 'wizard_id',
        string='Manufacturing Orders')

    # ── Core aggregation ──────────────────────────────────────────────────────

    def _build_mo_data(self, landed_cost):
        """
        For each (mo_id, product_id): qty consumed by the MO from this LC's
        receipt BEFORE the LC was validated, and the proportional cost.

        Per-unit cost per incoming move:
          SUM(additional_landed_cost) / MAX(quantity) from adjustment lines,
          grouped by move_id.
          - quantity = done qty of the incoming move (same for all cost lines)
          - additional_landed_cost = total LC allocated to this move (all lines)
          This equals the true per-unit LC cost regardless of split method.

        "Consumed before LC validation" filter:
          outgoing_move.date < account_move.create_date  (datetime precision)
          Using the journal entry's create_date gives sub-day accuracy — a move
          on the same calendar day as LC validation but created AFTER it is
          correctly excluded. Non-MO outgoing moves are included in FIFO history
          but excluded from the result — they have no raw_material_production_id.
        """
        grouped = self.env['stock.valuation.adjustment.lines'].read_group(
            [('cost_id', '=', landed_cost.id), ('move_id', '!=', False)],
            ['move_id'],
            ['move_id'],
            lazy=False,
        )
        incoming_move_ids = [g['move_id'][0] for g in grouped if g.get('move_id')]
        if not incoming_move_ids:
            return {}

        self.env.cr.execute("""
            WITH per_unit AS (
                -- Per-unit LC cost per incoming move, summed across all cost lines.
                -- MAX(quantity) = done qty (identical for every cost line on the same move).
                SELECT
                    svl.move_id                                                    AS incoming_move_id,
                    SUM(svl.additional_landed_cost) / NULLIF(MAX(svl.quantity), 0) AS per_unit_cost
                FROM stock_valuation_adjustment_lines svl
                WHERE svl.cost_id = %s
                  AND svl.move_id IS NOT NULL
                GROUP BY svl.move_id
            )
            SELECT
                sm_out.raw_material_production_id         AS mo_id,
                fl.incoming_move_id,
                fl.product_id,
                SUM(fl.quantity)                          AS consumed_qty,
                pu.per_unit_cost,
                COUNT(DISTINCT sm_out.id)                 AS outgoing_move_count
            FROM odooer_fifo_link fl
            JOIN stock_move sm_out ON sm_out.id = fl.outgoing_move_id
            JOIN per_unit pu        ON pu.incoming_move_id = fl.incoming_move_id
            WHERE fl.incoming_move_id = ANY(%s)
              AND sm_out.date < (
                  SELECT am.create_date
                  FROM account_move am
                  WHERE am.id = (SELECT account_move_id FROM stock_landed_cost WHERE id = %s)
              )
            GROUP BY sm_out.raw_material_production_id,
                     fl.incoming_move_id, fl.product_id, pu.per_unit_cost
        """, (landed_cost.id, incoming_move_ids, landed_cost.id))
        rows = self.env.cr.dictfetchall()

        mo_data = {}
        non_mo_move_count = 0
        non_mo_amount = 0.0
        for row in rows:
            consumed = float(row['consumed_qty'])
            if consumed <= 0:
                continue
            per_unit = float(row['per_unit_cost'] or 0.0)
            amount = per_unit * consumed

            if row['mo_id']:
                key = (row['mo_id'], row['product_id'])
                if key not in mo_data:
                    mo_data[key] = {
                        'production_id': row['mo_id'],
                        'product_id': row['product_id'],
                        'consumed_qty': 0.0,
                        'landed_cost_amount': 0.0,
                    }
                mo_data[key]['consumed_qty'] += consumed
                mo_data[key]['landed_cost_amount'] += amount
            else:
                non_mo_move_count += int(row['outgoing_move_count'] or 0)
                non_mo_amount += amount

        return mo_data, non_mo_move_count, non_mo_amount

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

        mo_data, non_mo_move_count, non_mo_amount = self._build_mo_data(landed_cost)
        if mo_data:
            res['mo_count'] = len({d['production_id'] for d in mo_data.values()})
            res['product_count'] = len({d['product_id'] for d in mo_data.values()})
            res['total_amount'] = sum(d['landed_cost_amount'] for d in mo_data.values())
        res['non_mo_move_count'] = non_mo_move_count
        res['undistributable_amount'] = non_mo_amount

        return res

    # ── Review & select (lazy line creation) ─────────────────────────────────

    def action_review_and_select(self):
        """Create transient lines on demand, then open a full-page paginated view."""
        self.ensure_one()
        if not self.line_ids:
            mo_data, _nmo_count, _nmo_amount = self._build_mo_data(self.landed_cost_id)
            if not mo_data:
                raise UserError(_('No manufacturing orders found to distribute costs to.'))
            self.env['odooer.mrp.landed.cost.wizard.line'].create([
                {
                    'wizard_id': self.id,
                    'production_id': d['production_id'],
                    'product_id': d['product_id'],
                    'consumed_qty': d['consumed_qty'],
                    'landed_cost_amount': d['landed_cost_amount'],
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
        mo_data, _nmo_count, _nmo_amount = self._build_mo_data(self.landed_cost_id)
        if not mo_data:
            raise UserError(_('No manufacturing orders found to distribute costs to.'))
        mo_ids = self.env['mrp.production'].browse(
            list({d['production_id'] for d in mo_data.values()})
        )
        total_amount = sum(d['landed_cost_amount'] for d in mo_data.values())
        return self._create_mo_landed_cost(mo_ids, total_amount)

    def action_create_mo_landed_cost(self):
        """Create MO LC for ALL loaded lines (called from the detail form)."""
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_(
                'No manufacturing order lines found. Please reload the wizard.'
            ))
        mo_ids = self.line_ids.mapped('production_id')
        total_amount = sum(self.line_ids.mapped('landed_cost_amount'))
        return self._create_mo_landed_cost(mo_ids, total_amount)
