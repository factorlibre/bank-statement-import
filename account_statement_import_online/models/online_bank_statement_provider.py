# Copyright 2019-2020 Brainbean Apps (https://brainbeanapps.com)
# Copyright 2019-2020 Dataplug (https://dataplug.io)
# Copyright 2022-2023 Therp BV (https://therp.nl)
# Copyright 2014 Tecnativa - Pedro M. Baeza
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import logging
from datetime import datetime
from decimal import Decimal
from html import escape

from dateutil.relativedelta import MO, relativedelta
from pytz import timezone, utc

from odoo import _, api, fields, models

from odoo.addons.base.models.res_partner import _tz_get

_logger = logging.getLogger(__name__)

# A scheduled run keeps going after a failed window, since the window is lost
# for good otherwise. This bounds the damage when the provider is broken rather
# than the window: expired credentials would fail on every window and there is
# no point in asking the remote service once per window to find that out.
MAX_CONSECUTIVE_FAILURES = 3


class OnlineBankStatementProvider(models.Model):
    _name = "online.bank.statement.provider"
    _inherit = ["mail.thread"]
    _description = "Online Bank Statement Provider"

    company_id = fields.Many2one(related="journal_id.company_id", store=True)
    active = fields.Boolean(default=True)
    create_statement = fields.Boolean(
        default=True,
        help="Create statements for the downloaded transactions automatically or not.",
    )
    name = fields.Char(compute="_compute_name", store=True)
    journal_id = fields.Many2one(
        comodel_name="account.journal",
        required=True,
        ondelete="cascade",
        domain=[("type", "=", "bank")],
    )
    currency_id = fields.Many2one(related="journal_id.currency_id")
    account_number = fields.Char(
        related="journal_id.bank_account_id.sanitized_acc_number"
    )
    tz = fields.Selection(
        selection=_tz_get,
        string="Timezone",
        default=lambda self: self.env.context.get("tz"),
        help=(
            "Timezone to convert transaction timestamps to prior being"
            " saved into a statement."
        ),
    )
    service = fields.Selection(
        selection=lambda self: self._selection_service(),
        required=True,
    )
    interval_type = fields.Selection(
        selection=[
            ("minutes", "Minute(s)"),
            ("hours", "Hour(s)"),
            ("days", "Day(s)"),
            ("weeks", "Week(s)"),
        ],
        default="hours",
        required=True,
    )
    interval_number = fields.Integer(
        string="Scheduled update interval",
        default=1,
        required=True,
    )
    update_schedule = fields.Char(
        compute="_compute_update_schedule",
    )
    last_successful_run = fields.Datetime(string="Last successful pull")
    next_run = fields.Datetime(
        string="Next scheduled pull",
        default=fields.Datetime.now,
        required=True,
    )
    overlap_days = fields.Integer(
        string="Re-check previous days",
        default=0,
        help="Number of days already pulled that every scheduled run requests "
        "again.\n\n"
        "A day is closed with whatever the provider has published by the time "
        "the run happens, and is never requested again: anything published "
        "afterwards is lost. Re-checking the previous days recovers it, and "
        "costs one extra request per day and run.\n\n"
        "Leave it at 0 for providers that publish without delay.",
    )
    statement_creation_mode = fields.Selection(
        selection=[
            ("daily", "Day"),
            ("weekly", "Week"),
            ("monthly", "Month"),
        ],
        default="daily",
        string="Transactions interval to obtain",
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
            "journal_id_uniq",
            "UNIQUE(journal_id)",
            "Only one online banking statement provider per journal!",
        ),
        (
            "valid_interval_number",
            "CHECK(interval_number > 0)",
            "Scheduled update interval must be greater than zero!",
        ),
        (
            "valid_overlap_days",
            "CHECK(overlap_days >= 0)",
            "Re-check previous days must not be negative!",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        """Set provider_id on journal after creation."""
        records = super().create(vals_list)
        records._update_journals()
        return records

    def write(self, vals):
        """Set provider_id on journal after creation."""
        result = super().write(vals)

        if "journal_id" in vals or "service" in vals:
            self._update_journals()

        return result

    def _update_journals(self):
        """Update journal with this provider.

        This is for compatibility reasons.
        """
        for this in self:
            this.journal_id.write(
                {
                    "online_bank_statement_provider_id": this.id,
                    "online_bank_statement_provider": this.service,
                    "bank_statements_source": "online",
                }
            )

    def unlink(self):
        """Reset journals."""
        journals = self.mapped("journal_id")
        if journals:
            vals = {
                "bank_statements_source": "undefined",
                "online_bank_statement_provider": False,
                "online_bank_statement_provider_id": False,
            }
            journals.write(vals)
        result = super().unlink()
        return result

    @api.model
    def _get_available_services(self):
        """Hook for extension"""
        return []

    @api.model
    def _selection_service(self):
        return self._get_available_services() + [("dummy", "Dummy")]

    @api.model
    def values_service(self):
        return self._get_available_services()

    @api.depends("service", "journal_id.name")
    def _compute_name(self):
        """We can have multiple providers/journals for the same service."""
        for provider in self:
            provider.name = " ".join([provider.journal_id.name, provider.service])

    @api.depends("active", "interval_type", "interval_number")
    def _compute_update_schedule(self):
        for provider in self:
            if not provider.active:
                provider.update_schedule = _("Inactive")
                continue

            provider.update_schedule = _("%(number)s %(type)s") % {
                "number": provider.interval_number,
                "type": list(
                    filter(
                        lambda x: x[0] == provider.interval_type,
                        self._fields["interval_type"].selection,
                    )
                )[0][1],
            }

    def _pull(self, date_since, date_until):
        """Pull data for all providers within requested period."""
        is_scheduled = self.env.context.get("scheduled")
        debug = self.env.context.get("account_statement_online_import_debug")
        debug_data = []
        for provider in self:
            statement_date_since = provider._get_statement_date_since(date_since)
            consecutive_failures = 0
            failure_streak_since = None
            gave_up = False
            failures = []
            while statement_date_since < date_until:
                # Note that statement_date_until is exclusive, while date_until is
                # inclusive. So if we have daily statements date_until might
                # be 2020-01-31, while statement_date_until is 2020-02-01.
                statement_date_until = (
                    statement_date_since + provider._get_statement_date_step()
                )
                try:
                    # Two savepoints in a row, not one around the whole window,
                    # because the two halves need opposite things.
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
                    # it. Note the release is where the deferred UPDATE of that
                    # write actually reaches the database, so it has to happen
                    # inside this block for a failure to be contained.
                    with provider.env.cr.savepoint():
                        data = provider._obtain_statement_data(
                            statement_date_since, statement_date_until
                        )
                    if debug:
                        debug_data += data
                    else:
                        # And writing the statement gets its own, so a database
                        # error there does not leave the cursor aborted either.
                        with provider.env.cr.savepoint():
                            provider._create_or_update_statement(
                                data, statement_date_since, statement_date_until
                            )
                except BaseException as exception:
                    if not is_scheduled:
                        raise
                    provider._log_provider_exception(
                        exception,
                        statement_date_since,
                        statement_date_until,
                        post=False,
                    )
                    failures.append(
                        (statement_date_since, statement_date_until, exception)
                    )
                    if not consecutive_failures:
                        failure_streak_since = statement_date_since
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        _logger.warning(
                            'Online Bank Statement Provider "%s" failed %d times '
                            "in a row; giving up on this run, resuming from %s.",
                            provider.name,
                            consecutive_failures,
                            failure_streak_since,
                        )
                        gave_up = True
                        break  # Continue with next provider.
                else:
                    consecutive_failures = 0
                    failure_streak_since = None
                # An isolated failure moves on: the window is left behind and
                # only the overlap can bring it back. Giving up is different --
                # see below.
                statement_date_since = statement_date_until
            provider._post_run_failures(failures)
            if is_scheduled:
                # When the run gave up there are windows it never even
                # requested, so the marker must not claim them: it resumes at
                # the start of the failure streak instead. Freezing it on every
                # failure was rejected on purpose -- a provider whose API
                # answers "no data yet" would stay stuck -- but a run that gave
                # up is already the degraded case, and at most one streak is
                # retried per run.
                provider._schedule_next_run(failure_streak_since if gave_up else None)
        return debug_data

    def _log_late_lines(self, period_name, count):
        """Both log and post that a closed period still had lines to get.

        Worth surfacing: the overlap only reaches back over periods this run
        was not due to cover, so finding anything there means that period had
        been closed incomplete. A provider recovering lines every single run is
        running too close to the moment its data gets published.

        Named after the period and not after the statement on purpose: with
        `create_statement` off there is no statement to name, and the recovery
        is worth reporting just the same.
        """
        self.ensure_one()
        if not count:
            return
        _logger.info(
            'Online Bank Statement provider "%s" recovered %d line(s) for the '
            "already closed period %s.",
            self.name,
            count,
            period_name,
        )
        self.message_post(
            body=_(
                "Recovered {count} line(s) for {period}, which had already "
                "been imported: it was closed incomplete."
            ).format(count=count, period=escape(period_name)),
            subject=_("Late lines recovered"),
        )

    def _log_provider_exception(
        self, exception, statement_date_since, statement_date_until, post=True
    ):
        """Both log error, and post a message on the provider record.

        `post` is what a caller reporting several windows at once turns off, to
        say it once instead of filling the record with one message per window.
        """
        self.ensure_one()
        _logger.warning(
            _(
                'Online Bank Statement provider "%(name)s" failed to'
                " obtain statement data since %(since)s until %(until)s"
            ),
            dict(
                name=self.name,
                since=statement_date_since,
                until=statement_date_until,
            ),
            exc_info=True,
        )
        if not post:
            return
        self.message_post(
            body=_(
                "Failed to obtain statement data for period "
                "since {since} until {until}: {exception}. See server logs for "
                "more details."
            ).format(
                since=statement_date_since,
                until=statement_date_until,
                exception=escape(str(exception)) or _("N/A"),
            ),
            subject=_("Issue with Online Bank Statement self"),
        )

    def _post_run_failures(self, failures):
        """Say once what went wrong in this run, not once per window.

        Continuing after a failure means a broken provider can fail on several
        windows of the same run; one message per window buries the record under
        noise and makes it tempting to give up earlier than is good for the
        data.
        """
        self.ensure_one()
        if not failures:
            return
        self.message_post(
            body=_(
                "Failed to obtain statement data for {count} period(s) in this "
                "run. See server logs for more details.<br/>{detail}"
            ).format(
                count=len(failures),
                detail="<br/>".join(
                    _("Since {since} until {until}: {exception}").format(
                        since=since,
                        until=until,
                        exception=escape(str(exception)) or _("N/A"),
                    )
                    for since, until, exception in failures
                ),
            ),
            subject=_("Issue with Online Bank Statement Provider"),
        )

    def _create_or_update_statement(
        self, data, statement_date_since, statement_date_until
    ):
        """Create or update bank statement with the data retrieved from provider.

        We can not use statement.date as a unique key within the statements
        of a journal, because this is now a computed field based on the last date in
        the statement lines.

        However we can still ensure unique and predictable names, so we wil use that
        to find existing statements.
        """
        self.ensure_one()
        if not data:
            data = ([], {})
        unfiltered_lines, statement_values = data
        if not unfiltered_lines:
            unfiltered_lines = []
        if not statement_values:
            statement_values = {}
        statement_values["name"] = self.make_statement_name(statement_date_since)
        filtered_lines = self._get_statement_filtered_lines(
            unfiltered_lines,
            statement_values,
            statement_date_since,
            statement_date_until,
        )
        if not filtered_lines:
            return self.env["account.bank.statement"]
        if filtered_lines:
            statement_values.update(
                {"line_ids": [[0, False, line] for line in filtered_lines]}
            )
        self._update_statement_balances(statement_values)
        # Only worth reporting for a period this run was not due to cover:
        # finding lines there means it had been closed incomplete. Reported by
        # period name rather than off `statement`, which is empty when the
        # provider does not create statements.
        # Read before the write, and only where there is something to find and
        # to report: an ordinary run does not pay for the search, and with
        # `create_statement` off nothing is looked up or rewritten at all, so
        # neither report would have a fact behind it.
        balances_before = (
            self._statement_balances(statement_values["name"])
            if self._is_recheck_window(statement_date_until) and self.create_statement
            else {}
        )
        statement = self._statement_create_or_write(statement_values)
        if balances_before:
            # Both facts hold here, and both are needed: a statement of this
            # period was already there, so it had been closed, and this run was
            # not due to cover it, so what turned up had been published late.
            self._log_late_lines(statement_values["name"], len(filtered_lines))
            self._log_balance_rewrite(
                statement_values["name"], balances_before, statement_values
            )
        return statement

    def _statement_balances(self, statement_name):
        """The balances a statement of this period holds, if there is one."""
        self.ensure_one()
        statement = self.env["account.bank.statement"].search(
            [("journal_id", "=", self.journal_id.id), ("name", "=", statement_name)],
            limit=1,
        )
        if not statement:
            return {}
        return {
            "balance_start": statement.balance_start,
            "balance_end_real": statement.balance_end_real,
        }

    def _is_recheck_window(self, statement_date_until):
        """Is this window one the overlap went back for, not a due one?

        The existence of the statement says nothing on its own: a provider
        polled more often than its statement period builds the current one over
        several runs, and would look like a recovery on every single run.
        """
        self.ensure_one()
        recheck_before = self.env.context.get("statement_recheck_before")
        return bool(recheck_before) and statement_date_until <= recheck_before

    def make_statement_name(self, statement_date_since):
        """Make name for statement using date and journal name."""
        self.ensure_one()
        return "%s/%s" % (
            self.journal_id.code,
            statement_date_since.strftime("%Y-%m-%d"),
        )

    def _statement_create_or_write(self, statement_values):
        """Final creation of statement if new, else write."""
        AccountBankStatement = self.env["account.bank.statement"]
        is_scheduled = self.env.context.get("scheduled")
        if not self.create_statement:
            return self._online_create_statement_lines(statement_values)
        if is_scheduled:
            AccountBankStatement = AccountBankStatement.with_context(
                tracking_disable=True,
            )
        statement_name = statement_values["name"]
        statement = AccountBankStatement.search(
            [
                ("journal_id", "=", self.journal_id.id),
                ("name", "=", statement_name),
            ],
            limit=1,
        )
        if not statement:
            statement_values["journal_id"] = self.journal_id.id
            statement = AccountBankStatement.with_context(
                journal_id=self.journal_id.id,
            ).create(statement_values)
        else:
            statement.write(statement_values)
        return statement

    def _log_balance_rewrite(self, period_name, balances_before, statement_values):
        """Leave a trace of the balances that a re-import overwrites.

        Re-requesting a closed period is how the overlap completes it, and the
        figures the provider reports now are the right ones: a balance keyed in
        by hand meanwhile was compensating for what was missing. So the rewrite
        is allowed -- what it must not be is silent, because that adjustment
        disappears with no record of what it said.

        Reported only for a period the overlap went back for. The period a run
        is building fills up over several passes, so its balances move on every
        one of them, and reporting that would fire on each run of an hourly
        provider and bury the case worth reading.

        Declared limit: a manual pull over closed periods rewrites their
        balances without a trace, exactly as it did before. What this covers is
        the automatic path, where nobody chose the period.
        """
        self.ensure_one()
        if not balances_before:
            return
        currency = self.currency_id or self.company_id.currency_id
        rewritten = []
        for field_name, label in (
            ("balance_start", _("starting balance")),
            ("balance_end_real", _("ending balance")),
        ):
            if field_name not in statement_values:
                continue
            before = balances_before[field_name]
            after = statement_values[field_name]
            if currency.is_zero(after - before):
                continue
            rewritten.append(
                _("{label} from {before} to {after}").format(
                    label=label, before=before, after=after
                )
            )
        if not rewritten:
            return
        _logger.info(
            'Online Bank Statement provider "%s" rewrote the balances of the '
            "already closed period %s: %s.",
            self.name,
            period_name,
            ", ".join(rewritten),
        )
        self.message_post(
            body=_(
                "Re-importing {period} rewrote its {detail}. The period had "
                "been closed incomplete, so what the provider reports is what "
                "is kept."
            ).format(
                period=escape(period_name),
                detail=escape(", ".join(rewritten)),
            ),
            subject=_("Statement balances rewritten"),
        )

    def _online_create_statement_lines(self, statement_values):
        AccountBankStatementLine = self.env["account.bank.statement.line"]
        is_scheduled = self.env.context.get("scheduled")
        if is_scheduled:
            AccountBankStatementLine = AccountBankStatementLine.with_context(
                tracking_disable=True,
            )
        lines = [line[2] for line in statement_values.get("line_ids", [])]
        AccountBankStatementLine.create(lines)
        return self.env["account.bank.statement"]  # Return empty statement

    def _get_statement_filtered_lines(
        self,
        unfiltered_lines,
        statement_values,
        statement_date_since,
        statement_date_until,
    ):
        """Get lines from line data, but only for the right date."""
        AccountBankStatementLine = self.env["account.bank.statement.line"]
        provider_tz = timezone(self.tz) if self.tz else utc
        journal = self.journal_id
        speeddict = journal._statement_line_import_speeddict()
        filtered_lines = []
        lines_before_since = 0
        lines_after_until = 0
        lines_not_unique = 0
        for line_values in unfiltered_lines:
            date = line_values["date"]
            if not isinstance(date, datetime):
                date = fields.Datetime.from_string(date)
            if date.tzinfo is None:
                date = date.replace(tzinfo=utc)
            date = date.astimezone(utc).replace(tzinfo=None)
            if date < statement_date_since:
                if "balance_start" in statement_values:
                    statement_values["balance_start"] = Decimal(
                        statement_values["balance_start"]
                    ) + Decimal(line_values["amount"])
                lines_before_since += 1
                continue
            elif date >= statement_date_until:
                if "balance_end_real" in statement_values:
                    statement_values["balance_end_real"] = Decimal(
                        statement_values["balance_end_real"]
                    ) - Decimal(line_values["amount"])
                lines_after_until += 1
                continue
            date = date.replace(tzinfo=utc)
            date = date.astimezone(provider_tz).replace(tzinfo=None)
            line_values["date"] = date
            journal._statement_line_import_update_unique_import_id(
                line_values, self.account_number
            )
            unique_import_id = line_values.get("unique_import_id")
            if unique_import_id:
                if AccountBankStatementLine.sudo().search(
                    [("unique_import_id", "=", unique_import_id)], limit=1
                ):
                    lines_not_unique += 1
                    continue
            if not line_values.get("payment_ref"):
                line_values["payment_ref"] = line_values.get("ref")
            line_values["journal_id"] = self.journal_id.id
            journal._statement_line_import_update_hook(line_values, speeddict)
            filtered_lines.append(line_values)
        if unfiltered_lines:
            if len(unfiltered_lines) == len(filtered_lines):
                _logger.debug(_("All lines passed filtering"))
            else:
                _logger.debug(
                    _(
                        "Of %(lines_provided)s lines provided"
                        ", %(before)s where before %(since)s"
                        ", %(after)s where on or after %(until)s"
                        "and %(duplicate)s where not unique."
                    ),
                    dict(
                        lines_provided=len(unfiltered_lines),
                        before=lines_before_since,
                        since=statement_date_since,
                        after=lines_after_until,
                        until=statement_date_until,
                        duplicate=lines_not_unique,
                    ),
                )
        return filtered_lines

    def _update_statement_balances(self, statement_values):
        """Update statement balance_ start/end/end_real."""
        AccountBankStatement = self.env["account.bank.statement"]
        if "balance_start" in statement_values:
            statement_values["balance_start"] = float(statement_values["balance_start"])
        else:
            # Take balance_end of previous statement as start of this one.
            previous_statement = AccountBankStatement.search(
                [
                    ("journal_id", "=", self.journal_id.id),
                    ("name", "<", statement_values["name"]),
                ],
                limit=1,
            )
            if previous_statement and previous_statement.balance_end:
                statement_values["balance_start"] = previous_statement.balance_end
        if "balance_end_real" in statement_values:
            statement_values["balance_end_real"] = float(
                statement_values["balance_end_real"]
            )

    def _schedule_next_run(self, resume_from=None):
        """Move the schedule on, and record how far the data actually got.

        `resume_from` is where the next run has to pick the data up again when
        this one gave up before covering its whole range. Left empty, the run
        covered everything it was asked for.

        It never goes back beyond where this run's own range started, because
        the overlap is derived from the marker again on every run: a marker left
        inside the overlapped days would be moved back once more on the next
        run, and again after that, so a provider that stays broken would widen
        its pending range without bound. Nothing is lost by holding it -- the
        overlap re-covers those days from the marker anyway -- and what is
        avoided is asking for months in one go the moment the provider recovers.
        """
        self.ensure_one()
        own_range_start = self.env.context.get("statement_recheck_before")
        if resume_from and own_range_start and resume_from < own_range_start:
            resume_from = own_range_start
        self.last_successful_run = resume_from or self.next_run
        self.next_run += self._get_next_run_period()

    def _get_statement_date_since(self, date):
        self.ensure_one()
        date = date.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.statement_creation_mode == "daily":
            return date
        elif self.statement_creation_mode == "weekly":
            return date + relativedelta(weekday=MO(-1))
        elif self.statement_creation_mode == "monthly":
            return date.replace(day=1)

    def _get_statement_date_step(self):
        self.ensure_one()
        if self.statement_creation_mode == "daily":
            return relativedelta(days=1, hour=0, minute=0, second=0, microsecond=0)
        elif self.statement_creation_mode == "weekly":
            return relativedelta(
                weeks=1,
                weekday=MO,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        elif self.statement_creation_mode == "monthly":
            return relativedelta(
                months=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

    def _get_next_run_period(self):
        self.ensure_one()
        if self.interval_type == "minutes":
            return relativedelta(minutes=self.interval_number)
        elif self.interval_type == "hours":
            return relativedelta(hours=self.interval_number)
        elif self.interval_type == "days":
            return relativedelta(days=self.interval_number)
        elif self.interval_type == "weeks":
            return relativedelta(weeks=self.interval_number)

    @api.model
    def _scheduled_pull(self):
        _logger.info(_("Scheduled pull of online bank statements..."))
        providers = self.search(
            [("active", "=", True), ("next_run", "<=", fields.Datetime.now())]
        )
        if providers:
            _logger.info(
                _("Pulling online bank statements of: %(provider_names)s"),
                dict(provider_names=", ".join(providers.mapped("journal_id.name"))),
            )
            for provider in providers.with_context(
                scheduled=True, tracking_disable=True
            ):
                provider._adjust_schedule()
                date_since = (
                    (provider.last_successful_run)
                    if provider.last_successful_run
                    else (provider.next_run - provider._get_next_run_period())
                )
                date_until = provider.next_run
                if provider.overlap_days:
                    # Days already closed are requested again so that whatever
                    # the provider published late still finds its way in. The
                    # run keeps a note of where its own range started, so that
                    # anything recovered before it can be told apart from the
                    # ordinary build-up of the current period.
                    provider = provider.with_context(
                        statement_recheck_before=date_since
                    )
                    date_since -= relativedelta(days=provider.overlap_days)
                provider._pull(date_since, date_until)
        _logger.info(_("Scheduled pull of online bank statements complete."))

    def _adjust_schedule(self):
        """Make sure next_run is current.

        Current means adding one more period would put if after the
        current moment. This will be done at the end of the run.
        The net effect of this method and the adjustment after the run
        will be for the next_run to be in the future.
        """
        self.ensure_one()
        delta = self._get_next_run_period()
        now = datetime.now()
        next_run = self.next_run + delta

        if next_run < now:
            current = next_run
            while next_run < now:
                current = next_run
                next_run += delta

            self.next_run = current

    def _obtain_statement_data(self, date_since, date_until):
        """Hook for extension"""
        # Check tests/online_bank_statement_provider_dummy.py for reference
        self.ensure_one()
        return []

    def action_online_bank_statements_pull_wizard(self):
        self.ensure_one()
        WIZARD_MODEL = "online.bank.statement.pull.wizard"
        wizard = self.env[WIZARD_MODEL].create([{"provider_ids": [(6, 0, [self.id])]}])
        return {
            "type": "ir.actions.act_window",
            "res_model": WIZARD_MODEL,
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }
