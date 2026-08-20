# Copyright 2019-2020 Brainbean Apps (https://brainbeanapps.com)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

from datetime import datetime
from dateutil.relativedelta import relativedelta, MO
from decimal import Decimal
from html import escape
import logging
from pytz import timezone, utc
from sys import exc_info

from odoo import models, fields, api, _
from odoo.addons.base.res.res_bank import sanitize_account_number
from odoo.addons.base.res.res_partner import _tz_get

_logger = logging.getLogger(__name__)

# A scheduled run keeps going after a failed window, since the window is lost
# for good otherwise. This bounds the damage when the provider is broken rather
# than the window: expired credentials would fail on every window and there is
# no point in asking the remote service once per window to find that out.
MAX_CONSECUTIVE_FAILURES = 3


class OnlineBankStatementProvider(models.Model):
    _name = 'online.bank.statement.provider'
    _inherit = ['mail.thread']
    _description = 'Online Bank Statement Provider'

    company_id = fields.Many2one(
        related='journal_id.company_id',
        readonly=True,
        store=True,
    )
    active = fields.Boolean()
    name = fields.Char(
        string='Name',
        compute='_compute_name',
        store=True,
    )
    journal_id = fields.Many2one(
        comodel_name='account.journal',
        required=True,
        readonly=True,
        ondelete='cascade',
        domain=[
            ('type', '=', 'bank'),
        ],
    )
    currency_id = fields.Many2one(
        related='journal_id.currency_id',
        readonly=True,
    )
    account_number = fields.Char(
        related='journal_id.bank_account_id.sanitized_acc_number',
        readonly=True,
    )
    tz = fields.Selection(
        selection=_tz_get,
        string='Timezone',
        default=lambda self: self.env.context.get('tz'),
        help=(
            'Timezone to convert transaction timestamps to prior being'
            ' saved into a statement.'
        ),
    )
    service = fields.Selection(
        selection=lambda self: self._selection_service(),
        required=True,
        readonly=True,
    )
    interval_type = fields.Selection(
        selection=[
            ('minutes', 'Minute(s)'),
            ('hours', 'Hour(s)'),
            ('days', 'Day(s)'),
            ('weeks', 'Week(s)'),
        ],
        default='hours',
        required=True,
    )
    interval_number = fields.Integer(
        string='Scheduled update interval',
        default=1,
        required=True,
    )
    update_schedule = fields.Char(
        string='Update Schedule',
        compute='_compute_update_schedule',
    )
    last_successful_run = fields.Datetime(
        string='Last successful pull',
    )
    next_run = fields.Datetime(
        string='Next scheduled pull',
        default=fields.Datetime.now,
        required=True,
    )
    overlap_days = fields.Integer(
        string='Re-check previous days',
        default=0,
        help='Number of days already pulled that every scheduled run requests'
             ' again.\n\n'
             'A day is closed with whatever the provider has published by the'
             ' time the run happens, and is never requested again: anything'
             ' published afterwards is lost. Re-checking the previous days'
             ' recovers it, and costs one extra request per day and run.\n\n'
             'Leave it at 0 for providers that publish without delay.',
    )
    statement_creation_mode = fields.Selection(
        selection=[
            ('daily', 'Daily statements'),
            ('weekly', 'Weekly statements'),
            ('monthly', 'Monthly statements'),
        ],
        default='daily',
        required=True,
    )
    api_base = fields.Char()
    origin = fields.Char()
    username = fields.Char()
    password = fields.Char()
    key = fields.Binary()
    certificate = fields.Binary()
    passphrase = fields.Char()
    certificate_public_key = fields.Text()
    certificate_private_key = fields.Text()
    certificate_chain = fields.Text()

    _sql_constraints = [
        (
            'journal_id_uniq',
            'UNIQUE(journal_id)',
            'Only one online banking statement provider per journal!'
        ),
        (
            'valid_interval_number',
            'CHECK(interval_number > 0)',
            'Scheduled update interval must be greater than zero!'
        ),
        (
            'valid_overlap_days',
            'CHECK(overlap_days >= 0)',
            'Re-check previous days must not be negative!'
        )
    ]

    @api.model
    def _get_available_services(self):
        """Hook for extension"""
        return []

    @api.model
    def _selection_service(self):
        return self._get_available_services() + [('dummy', 'Dummy')]

    @api.model
    def values_service(self):
        return self._get_available_services()

    @api.multi
    @api.depends('service')
    def _compute_name(self):
        for provider in self:
            provider.name = list(filter(
                lambda x: x[0] == provider.service,
                self._selection_service()
            ))[0][1]

    @api.multi
    @api.depends('active', 'interval_type', 'interval_number')
    def _compute_update_schedule(self):
        for provider in self:
            if not provider.active:
                provider.update_schedule = _('Inactive')
                continue

            provider.update_schedule = _('%(number)s %(type)s') % {
                'number': provider.interval_number,
                'type': list(filter(
                    lambda x: x[0] == provider.interval_type,
                    self._fields['interval_type'].selection
                ))[0][1],
            }

    @api.multi
    def _pull(self, date_since, date_until):
        AccountBankStatement = self.env['account.bank.statement']
        is_scheduled = self.env.context.get('scheduled')
        if is_scheduled:
            AccountBankStatement = AccountBankStatement.with_context(
                tracking_disable=True,
            )
        AccountBankStatementLine = self.env['account.bank.statement.line']
        for provider in self:
            provider_tz = timezone(provider.tz) if provider.tz else utc
            statement_date_since = provider._get_statement_date_since(
                date_since
            )
            consecutive_failures = 0
            failure_streak_since = None
            gave_up = False
            failures = []
            locked_periods = []
            while statement_date_since < date_until:
                statement_date_until = (
                    statement_date_since + provider._get_statement_date_step()
                )
                try:
                    # Two savepoints in a row, not one around the whole
                    # window, because the two halves need opposite things.
                    #
                    # Obtaining the data writes to the database too: a provider
                    # refreshing an access token persists it here. That write
                    # must survive a later failure of this same window, or the
                    # token gets paid for again -- so it cannot share the
                    # statement's savepoint. But it can also fail on its own
                    # (integrity constraint, row lock, serialization failure),
                    # and an aborted cursor would then take down the run of
                    # every provider, so it cannot go unprotected either.
                    #
                    # Releasing this savepoint on the way out keeps its work
                    # while leaving it outside the next one: what the provider
                    # persisted stays, and a rollback below can no longer undo
                    # it.
                    with provider.env.cr.savepoint():
                        data = provider._obtain_statement_data(
                            statement_date_since,
                            statement_date_until
                        )
                    # And writing the statement gets its own, so a database
                    # error there does not leave the cursor aborted either.
                    with provider.env.cr.savepoint():
                        statement_date = provider._get_statement_date(
                            statement_date_since,
                            statement_date_until,
                        )
                        if not data:
                            data = ([], {})
                        lines_data, statement_values = data
                        if not lines_data:
                            lines_data = []
                        if not statement_values:
                            statement_values = {}
                        # Without filtering by state: a validated statement of
                        # this day is still the statement of this day. Looking
                        # only for open ones did not find it and created a
                        # second statement for the same date, which the overlap
                        # would turn from a rarity into one duplicate per day
                        # recovered.
                        statement = AccountBankStatement.search([
                            ('journal_id', '=', provider.journal_id.id),
                            ('date', '=',
                             fields.Date.to_string(statement_date)),
                        ], limit=1)
                        # Whether this period had been closed before, which is
                        # a different fact from whether the overlap went back
                        # for it, and the report needs both.
                        statement_existed = bool(statement)
                        # A validated statement cannot be written to, and must
                        # not be duplicated either. What is left over is
                        # reported after the run instead of being dropped.
                        statement_locked = statement.state == 'confirm'
                        if not statement:
                            statement_values.update({
                                'name': provider._get_statement_name(
                                    statement_date
                                ),
                                'journal_id': provider.journal_id.id,
                                'date': fields.Date.to_string(statement_date),
                            })
                            statement = AccountBankStatement.with_context(
                                journal_id=provider.journal_id.id,
                            ).create(
                                # NOTE: needed, create() alters values
                                statement_values.copy()
                            )
                        filtered_lines = []
                        for line_values in lines_data:
                            date = line_values['date']
                            if not isinstance(date, datetime):
                                if not isinstance(date, str):
                                    date = fields.Datetime.to_string(date)
                                date = fields.Datetime.from_string(date)

                            if date.tzinfo is None:
                                date = date.replace(tzinfo=utc)
                            date = date.astimezone(utc).replace(tzinfo=None)

                            if date < statement_date_since:
                                if 'balance_start' in statement_values:
                                    statement_values['balance_start'] = (
                                        Decimal(
                                            statement_values['balance_start']
                                        ) + Decimal(
                                            line_values['amount']
                                        )
                                    )
                                continue
                            elif date >= statement_date_until:
                                if 'balance_end_real' in statement_values:
                                    statement_values['balance_end_real'] = (
                                        Decimal(
                                            statement_values[
                                                'balance_end_real'
                                            ]
                                        ) - Decimal(
                                            line_values['amount']
                                        )
                                    )
                                continue

                            date = date.replace(tzinfo=utc)
                            date = date.astimezone(
                                provider_tz
                            ).replace(tzinfo=None)
                            line_values['date'] = (
                                fields.Datetime.to_string(date)
                            )

                            unique_import_id = line_values.get(
                                'unique_import_id'
                            )
                            if unique_import_id:
                                unique_import_id = (
                                    provider._generate_unique_import_id(
                                        unique_import_id
                                    )
                                )
                                line_values.update({
                                    'unique_import_id': unique_import_id,
                                })
                                if AccountBankStatementLine.sudo().search(
                                        [('unique_import_id', '=',
                                          unique_import_id)],
                                        limit=1):
                                    continue

                            bank_account_number = line_values.get(
                                'account_number'
                            )
                            if bank_account_number:
                                line_values.update({
                                    'account_number': (
                                        self._sanitize_bank_account_number(
                                            bank_account_number
                                        )
                                    ),
                                })

                            filtered_lines.append(line_values)
                        statement_values.update({
                            'line_ids': [
                                [0, False, line] for line in filtered_lines
                            ],
                        })
                        if 'balance_start' in statement_values:
                            statement_values['balance_start'] = float(
                                statement_values['balance_start']
                            )
                        if 'balance_end_real' in statement_values:
                            statement_values['balance_end_real'] = float(
                                statement_values['balance_end_real']
                            )
                        if statement_locked:
                            # Nothing is written and nothing is thrown away:
                            # what could not go in is reported once the run is
                            # over, so somebody decides whether to reopen the
                            # statement or enter the movements by hand. It
                            # stops being reported on its own once they are in,
                            # because they stop counting as new.
                            if filtered_lines:
                                locked_periods.append(
                                    (statement_date, len(filtered_lines))
                                )
                        else:
                            if (
                                filtered_lines
                                and statement_existed
                                and provider._is_recheck_window(
                                    statement_date_until)
                            ):
                                provider._log_late_lines(
                                    statement, len(filtered_lines)
                                )
                            statement.write(statement_values)
                except Exception:
                    if not is_scheduled:
                        raise
                    _logger.warning(
                        'Online Bank Statement Provider "%s" failed to'
                        ' obtain statement data since %s until %s' % (
                            provider.name,
                            statement_date_since,
                            statement_date_until,
                        ),
                        exc_info=True,
                    )
                    failures.append((
                        statement_date_since,
                        statement_date_until,
                        escape(str(exc_info()[1])) or _('N/A'),
                    ))
                    if not consecutive_failures:
                        failure_streak_since = statement_date_since
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        _logger.warning(
                            'Online Bank Statement Provider "%s" failed %d'
                            ' times in a row; giving up on this run.' % (
                                provider.name,
                                consecutive_failures,
                            )
                        )
                        gave_up = True
                        break
                else:
                    consecutive_failures = 0
                    failure_streak_since = None
                # Always move on: a window left behind is never requested
                # again, since the marker advances at the end either way.
                # An isolated failure moves on: the window is left behind and
                # only the overlap can bring it back. Giving up is different.
                statement_date_since = statement_date_until
            provider._post_run_failures(failures)
            provider._post_run_locked_periods(locked_periods)
            if is_scheduled:
                # When the run gave up there are windows it never even
                # requested, so the marker must not claim them: it resumes at
                # the start of the failure streak instead. Freezing it on every
                # failure was rejected on purpose -- a provider whose API
                # answers "no data yet" would stay stuck -- but a run that gave
                # up is already the degraded case.
                provider._schedule_next_run(
                    failure_streak_since if gave_up else None
                )

    @api.multi
    def _is_recheck_window(self, statement_date_until):
        """Is this window one the overlap went back for, not a due one?

        The existence of the statement says nothing on its own: a provider
        polled more often than its statement period builds the current one over
        several runs, and would look like a recovery on every single run.
        """
        self.ensure_one()
        recheck_before = self.env.context.get('statement_recheck_before')
        return bool(recheck_before) and statement_date_until <= recheck_before

    @api.multi
    def _post_run_failures(self, failures):
        """Say once what went wrong in this run, not once per window.

        Continuing after a failure means a broken provider can fail on several
        windows of the same run; one message per window buries the record under
        noise and makes it tempting to give up earlier than is good for the
        data. The per-window detail stays in the server log.
        """
        self.ensure_one()
        if not failures:
            return
        self.message_post(
            body=_(
                'Failed to obtain statement data for %s period(s) in this '
                'run. See server logs for more details.<br/>%s'
            ) % (
                len(failures),
                '<br/>'.join(
                    _('Since %s until %s: %s') % (since, until, error)
                    for since, until, error in failures
                ),
            ),
            subject=_('Issue with Online Bank Statement Provider'),
        )

    @api.multi
    def _post_run_locked_periods(self, locked_periods):
        """Say what could not go into an already validated statement.

        A validated statement cannot be written to, and creating a second one
        for the same day is not an option either -- that is what used to
        happen, and with the overlap it would mean one duplicate per day
        recovered. So the movements stay out, and saying so is the whole point:
        neither duplicating in silence nor discarding in silence.

        Somebody has to decide whether to reopen the statement or enter them by
        hand. Once they are in they stop counting as new, so this stops being
        reported on its own. That self-extinction rides on
        ``unique_import_id``: a provider whose lines carry none never
        deduplicates, so for it a validated day would be reported on every run
        for ever.

        Once per run and not once per window, for the same reason as the
        failures: with the overlap on, a provider that validates its statements
        daily would hit several validated days in every single run.
        """
        self.ensure_one()
        if not locked_periods:
            return
        _logger.info(
            'Online Bank Statement Provider "%s": %d validated period(s) left'
            ' movements out: %s' % (
                self.name,
                len(locked_periods),
                ', '.join(
                    '%s (%d)' % (day, count) for day, count in locked_periods
                ),
            )
        )
        self.message_post(
            body=_(
                '%s movement(s) could not be imported because the statement of'
                ' their day is already validated, so nothing was written and'
                ' nothing was discarded. Reopen the statement and pull again,'
                ' or enter them by hand.<br/>%s'
            ) % (
                sum(count for _day, count in locked_periods),
                '<br/>'.join(
                    _('%s: %s movement(s)') % (day, count)
                    for day, count in locked_periods
                ),
            ),
            subject=_('Movements left out of a validated statement'),
        )

    @api.multi
    def _log_late_lines(self, statement, count):
        """Both log and post that an already closed day got more lines.

        Worth surfacing: it means the day had been closed incomplete, and a
        provider doing this every day is running too close to the moment its
        data is published.
        """
        self.ensure_one()
        _logger.info(
            'Online Bank Statement Provider "%s" recovered %d line(s) for the'
            ' already closed statement %s.' % (
                self.name,
                count,
                statement.name,
            )
        )
        self.message_post(
            body=_(
                'Recovered %s line(s) for %s, a statement that was already'
                ' closed: that day had been imported incomplete.'
            ) % (count, escape(statement.name)),
            subject=_('Late lines recovered'),
        )

    @api.multi
    def _schedule_next_run(self, resume_from=None):
        """Move the schedule on, and record how far the data actually got.

        `resume_from` is where the next run has to pick the data up again when
        this one gave up before covering its whole range. Left empty, the run
        covered everything it was asked for.

        It never goes back beyond where this run's own range started, because
        the overlap is derived from the marker again on every run: a marker
        left inside the overlapped days would be moved back once more on the
        next run, and again after, so a provider that stays broken would widen
        its pending range without bound. Nothing is lost by holding it -- the
        overlap re-covers those days from the marker anyway -- and what is
        avoided is asking for months in one go the moment the provider
        recovers.
        """
        self.ensure_one()
        own_range_start = self.env.context.get('statement_recheck_before')
        if resume_from and own_range_start:
            # Both are naive datetimes here, same as in _is_recheck_window.
            resume_from = max(resume_from, own_range_start)
        self.last_successful_run = resume_from or self.next_run
        self.next_run = fields.Datetime.from_string(self.next_run) \
            + self._get_next_run_period()

    @api.multi
    def _get_statement_date_since(self, date):
        self.ensure_one()
        date = date.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        if self.statement_creation_mode == 'daily':
            return date
        elif self.statement_creation_mode == 'weekly':
            return date + relativedelta(weekday=MO(-1))
        elif self.statement_creation_mode == 'monthly':
            return date.replace(
                day=1,
            )

    @api.multi
    def _get_statement_date_step(self):
        self.ensure_one()
        if self.statement_creation_mode == 'daily':
            return relativedelta(
                days=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        elif self.statement_creation_mode == 'weekly':
            return relativedelta(
                weeks=1,
                weekday=MO,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        elif self.statement_creation_mode == 'monthly':
            return relativedelta(
                months=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

    @api.multi
    def _get_statement_date(self, date_since, date_until):
        self.ensure_one()
        # NOTE: Statement date is treated by Odoo as start of period. Details
        #  - addons/account/models/account_journal_dashboard.py
        #  - def get_line_graph_datas()
        tz = timezone(self.tz) if self.tz else utc
        date_since = date_since.replace(tzinfo=utc).astimezone(tz)
        return date_since.date()

    @api.multi
    def _get_statement_name(self, statement_date):
        """Hook for extension. Default behaviour: use the journal's sequence."""
        self.ensure_one()
        return self.journal_id.sequence_id.with_context(
            ir_sequence_date=fields.Date.to_string(statement_date),
        ).next_by_id()

    @api.multi
    def _generate_unique_import_id(self, unique_import_id):
        self.ensure_one()
        return (
            self.account_number and self.account_number + '-' or ''
        ) + str(self.journal_id.id) + '-' + unique_import_id

    @api.multi
    def _sanitize_bank_account_number(self, bank_account_number):
        """Hook for extension"""
        self.ensure_one()
        return sanitize_account_number(bank_account_number)

    @api.multi
    def _get_next_run_period(self):
        self.ensure_one()
        if self.interval_type == 'minutes':
            return relativedelta(minutes=self.interval_number)
        elif self.interval_type == 'hours':
            return relativedelta(hours=self.interval_number)
        elif self.interval_type == 'days':
            return relativedelta(days=self.interval_number)
        elif self.interval_type == 'weeks':
            return relativedelta(weeks=self.interval_number)

    @api.model
    def _scheduled_pull(self):
        _logger.info('Scheduled pull of online bank statements...')

        providers = self.search([
            ('active', '=', True),
            ('next_run', '<=', fields.Datetime.now()),
        ])
        if providers:
            _logger.info('Pulling online bank statements of: %s' % ', '.join(
                providers.mapped('journal_id.name')
            ))
            for provider in providers.with_context({'scheduled': True}):
                next_run = fields.Datetime.from_string(provider.next_run)
                date_since = fields.Datetime.from_string(
                    provider.last_successful_run
                ) if provider.last_successful_run else (
                    next_run - provider._get_next_run_period()
                )
                date_until = next_run
                if provider.overlap_days:
                    # Days already closed are requested again so that whatever
                    # the provider published late still gets in. The run keeps
                    # a note of where its own range started, so that anything
                    # recovered before it can be told apart from the ordinary
                    # build-up of the current period.
                    provider = provider.with_context(
                        statement_recheck_before=date_since
                    )
                    date_since -= relativedelta(
                        days=provider.overlap_days
                    )
                provider._pull(date_since, date_until)

        _logger.info('Scheduled pull of online bank statements complete.')

    @api.multi
    def _obtain_statement_data(
        self, date_since, date_until
    ):
        """Hook for extension"""
        # Check tests/online_bank_statement_provider_dummy.py for reference
        self.ensure_one()
        return []
