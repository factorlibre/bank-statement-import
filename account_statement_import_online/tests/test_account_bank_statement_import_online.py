# Copyright 2019-2020 Brainbean Apps (https://brainbeanapps.com)
# Copyright 2019-2020 Dataplug (https://dataplug.io)
# Copyright 2022-2023 Therp BV (https://therp.nl)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import logging
from datetime import date, datetime
from unittest import mock
from urllib.error import HTTPError

from dateutil.relativedelta import relativedelta
from odoo_test_helper import FakeModelLoader
from psycopg2 import IntegrityError

from odoo import _, fields
from odoo.tests import common, tagged
from odoo.tools import mute_logger

from odoo.addons.account_statement_import_online.models import (
    online_bank_statement_provider as provider_model,
)

MAX_CONSECUTIVE_FAILURES = provider_model.MAX_CONSECUTIVE_FAILURES

_logger = logging.getLogger(__name__)

mock_obtain_statement_data = (
    "odoo.addons.account_statement_import_online.tests."
    + "online_bank_statement_provider_dummy.OnlineBankStatementProviderDummy."
    + "_obtain_statement_data"
)


@tagged("post_install", "-at_install")
class TestAccountBankAccountStatementImportOnline(common.TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not cls.env.company.chart_template_id:
            # Load a CoA if there's none in current company
            coa = cls.env.ref("l10n_generic_coa.configurable_chart_template", False)
            if not coa:
                # Load the first available CoA
                coa = cls.env["account.chart.template"].search(
                    [("visible", "=", True)], limit=1
                )
            coa.try_loading(company=cls.env.company, install_demo=False)
        # Load fake model
        cls.loader = FakeModelLoader(cls.env, cls.__module__)
        cls.loader.backup_registry()
        cls.addClassCleanup(cls.loader.restore_registry)
        from .online_bank_statement_provider_dummy import (
            OnlineBankStatementProviderDummy,
        )

        cls.loader.update_registry((OnlineBankStatementProviderDummy,))

        cls.now = fields.Datetime.now()
        cls.AccountAccount = cls.env["account.account"]
        cls.AccountJournal = cls.env["account.journal"]
        cls.OnlineBankStatementProvider = cls.env["online.bank.statement.provider"]
        cls.OnlineBankStatementPullWizard = cls.env["online.bank.statement.pull.wizard"]
        cls.AccountBankStatement = cls.env["account.bank.statement"]
        cls.AccountBankStatementLine = cls.env["account.bank.statement.line"]

        cls.journal = cls.AccountJournal.create(
            {
                "name": "Bank",
                "type": "bank",
                "code": "BANK",
                "bank_statements_source": "online",
            }
        )
        cls.provider = cls.OnlineBankStatementProvider.create(
            {
                "name": "Dummy Provider",
                "service": "dummy",
                "journal_id": cls.journal.id,
                "statement_creation_mode": "daily",
            }
        )

    def test_pull_mode_daily(self):
        self.provider.statement_creation_mode = "daily"
        self.provider.with_context(step={"hours": 2})._pull(
            self.now - relativedelta(days=1),
            self.now,
        )
        self._getExpectedStatements(2)

    def test_pull_mode_weekly(self):
        self.provider.statement_creation_mode = "weekly"
        self.provider.with_context(step={"hours": 8})._pull(
            self.now - relativedelta(weeks=1),
            self.now,
        )
        self._getExpectedStatements(2)

    def test_pull_mode_monthly(self):
        self.provider.statement_creation_mode = "monthly"
        self.provider.with_context(step={"hours": 8})._pull(
            self.now - relativedelta(months=1),
            self.now,
        )
        self._getExpectedStatements(2)

    def test_pull_scheduled(self):
        self.provider.next_run = self.now - relativedelta(days=15)
        self._getExpectedStatements(0)
        self.provider.with_context(step={"hours": 8})._scheduled_pull()
        self._getExpectedStatements(1)
        self.assertEqual(
            self.provider.next_run, self.now + self.provider._get_next_run_period()
        )

    def test_pull_skip_duplicates_by_unique_import_id(self):
        self.provider.statement_creation_mode = "weekly"
        # Get for two weeks of data.
        self.provider.with_context(
            step={"hours": 8},
            override_date_since=self.now - relativedelta(weeks=2),
            override_date_until=self.now,
        )._pull(
            self.now - relativedelta(weeks=2),
            self.now,
        )
        expected_count = 14 * (24 / 8)
        self._getExpectedLines(expected_count)
        # Get two weeks, but one overlapping with previous.
        self.provider.with_context(
            step={"hours": 8},
            override_date_since=self.now - relativedelta(weeks=3),
            override_date_until=self.now - relativedelta(weeks=1),
        )._pull(
            self.now - relativedelta(weeks=3),
            self.now - relativedelta(weeks=1),
        )
        expected_count = 21 * (24 / 8)
        self._getExpectedLines(expected_count)
        # Get another day, but within statements already retrieved.
        self.provider.with_context(
            step={"hours": 8},
            override_date_since=self.now - relativedelta(weeks=1),
            override_date_until=self.now,
        )._pull(
            self.now - relativedelta(weeks=1),
            self.now,
        )
        self._getExpectedLines(expected_count)

    def test_interval_type_minutes(self):
        self.provider.interval_type = "minutes"
        self.provider._compute_update_schedule()

    def test_interval_type_hours(self):
        self.provider.interval_type = "hours"
        self.provider._compute_update_schedule()

    def test_interval_type_days(self):
        self.provider.interval_type = "days"
        self.provider._compute_update_schedule()

    def test_interval_type_weeks(self):
        self.provider.interval_type = "weeks"
        self.provider._compute_update_schedule()

    def test_pull_no_crash(self):
        self.provider.statement_creation_mode = "weekly"
        self.provider.with_context(crash=True, scheduled=True)._pull(
            self.now - relativedelta(hours=1),
            self.now,
        )
        self._getExpectedStatements(0)

    def test_pull_crash(self):
        self.provider.statement_creation_mode = "weekly"
        with self.assertRaisesRegex(Exception, "Expected"):
            self.provider.with_context(crash=True)._pull(
                self.now - relativedelta(hours=1),
                self.now,
            )

    def test_pull_httperror(self):
        self.provider.statement_creation_mode = "weekly"
        with self.assertRaises(HTTPError):
            self.provider.with_context(
                crash=True,
                exception=HTTPError(None, 500, "Error", None, None),
            )._pull(
                self.now - relativedelta(hours=1),
                self.now,
            )

    def test_pull_no_balance(self):
        self.provider.with_context(
            step={"hours": 2},
            balance_start=0,
            amount=100.0,
            balance=False,
        )._pull(
            self.now - relativedelta(days=1),
            self.now,
        )
        statements = self._getExpectedStatements(2)
        self.assertFalse(statements[0].balance_start)
        self.assertTrue(statements[0].balance_end)
        self.assertTrue(statements[1].balance_start)

    def test_wizard(self):
        vals = {
            "date_since": self.now - relativedelta(hours=1),
            "date_until": self.now,
        }
        wizard = self.OnlineBankStatementPullWizard.with_context(
            active_model=self.provider._name, active_id=self.provider.id
        ).create(vals)
        wizard.action_pull()
        self._getExpectedStatements(1)

    def test_wizard_on_journal(self):
        vals = {
            "date_since": self.now - relativedelta(hours=1),
            "date_until": self.now,
        }
        wizard = self.OnlineBankStatementPullWizard.with_context(
            active_model=self.journal._name, active_id=self.journal.id
        ).create(vals)
        wizard.action_pull()
        self._getExpectedStatements(1)

    def test_pull_statement_partially(self):
        self.provider.statement_creation_mode = "monthly"
        provider_context = {
            "step": {"hours": 24},
            "override_date_since": datetime(2020, 1, 1),
            "amount": 1.0,
            "balance_start": 0,
        }
        # Should create statement for first 30 days of january.
        self.provider.with_context(
            **provider_context,
            override_date_until=datetime(2020, 1, 31),
        )._pull(
            datetime(2020, 1, 1),
            datetime(2020, 1, 31),
        )
        statements = self._getExpectedStatements(1)
        self.assertEqual(statements[0].balance_start, 0.0)
        self.assertEqual(statements[0].balance_end_real, 30.0)
        # Should create statement for first 14 days of february,
        # and add one line to statement for january.
        self.provider.with_context(
            **provider_context,
            override_date_until=datetime(2020, 2, 15),
        )._pull(
            datetime(2020, 1, 1),
            datetime(2020, 2, 29),
        )
        statements = self._getExpectedStatements(2)
        self.assertEqual(statements[0].balance_start, 0.0)
        self.assertEqual(statements[0].balance_end_real, 31.0)
        self.assertEqual(statements[1].balance_start, 31.0)
        self.assertEqual(statements[1].balance_end_real, 45.0)
        # Getting data for rest of februari should not create new statement.
        self.provider.with_context(
            **provider_context,
            override_date_until=datetime(2020, 2, 29),
        )._pull(
            datetime(2020, 1, 1),
            datetime(2020, 2, 29),
        )
        statements = self._getExpectedStatements(2)
        self.assertEqual(statements[0].balance_start, 0.0)
        self.assertEqual(statements[0].balance_end_real, 31.0)
        self.assertEqual(statements[1].balance_start, 31.0)
        self.assertEqual(statements[1].balance_end_real, 59.0)

    def test_tz_utc(self):
        self.provider.tz = "UTC"
        self.provider.with_context(
            step={"hours": 1},
            override_date_since=datetime(2020, 4, 17, 22, 0),
            override_date_until=datetime(2020, 4, 18, 2, 0),
            tz="UTC",
        )._pull(
            datetime(2020, 4, 17, 22, 0),
            datetime(2020, 4, 18, 2, 0),
        )
        statements = self._getExpectedStatements(2)
        lines = statements.mapped("line_ids").sorted(key=lambda r: r.id)
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].date, date(2020, 4, 17))
        self.assertEqual(lines[1].date, date(2020, 4, 17))
        self.assertEqual(lines[2].date, date(2020, 4, 18))
        self.assertEqual(lines[3].date, date(2020, 4, 18))

    def test_tz_non_utc(self):
        """Test situation where the provider is west of Greenwich.

        In this case, when it is 22:00 according to the provider, it is
        00:00 the next day according to GMT/UTZ.
        """
        self.provider.tz = "Etc/GMT-2"
        self.provider.with_context(
            step={"hours": 1},
            override_date_since=datetime(2020, 4, 17, 22, 0),
            override_date_until=datetime(2020, 4, 18, 2, 0),
            tz="UTC",
        )._pull(
            datetime(2020, 4, 17, 22, 0),
            datetime(2020, 4, 18, 2, 0),
        )
        statements = self._getExpectedStatements(2)
        lines = statements.mapped("line_ids").sorted(key=lambda r: r.id)
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].date, date(2020, 4, 18))
        self.assertEqual(lines[1].date, date(2020, 4, 18))
        self.assertEqual(lines[2].date, date(2020, 4, 18))
        self.assertEqual(lines[3].date, date(2020, 4, 18))

    def test_other_tz_to_utc(self):
        """Test the situation where we are tot the west of the provider.

        Provider will be GMT/UTC, we will be two hours to the west.
        When we pull data from 22:00 on the 17th of april, for
        the provider this will be from 00:00 on the 18th.

        We will translate the provider times back to our time.
        """
        self.provider.with_context(
            step={"hours": 1},
            tz="Etc/GMT-2",
            override_date_since=datetime(2020, 4, 18, 0, 0),
            override_date_until=datetime(2020, 4, 18, 4, 0),
        )._pull(
            datetime(2020, 4, 17, 22, 0),
            datetime(2020, 4, 18, 2, 0),
        )
        statements = self._getExpectedStatements(2)
        lines = statements.mapped("line_ids").sorted(key=lambda r: r.id)
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].date, date(2020, 4, 17))
        self.assertEqual(lines[1].date, date(2020, 4, 17))
        self.assertEqual(lines[2].date, date(2020, 4, 18))
        self.assertEqual(lines[3].date, date(2020, 4, 18))

    def test_timestamp_date_only_date(self):
        self.provider.with_context(step={"hours": 1}, timestamp_mode="date")._pull(
            datetime(2020, 4, 18, 0, 0),
            datetime(2020, 4, 18, 4, 0),
        )
        statements = self._getExpectedStatements(1)
        lines = statements.line_ids
        self.assertEqual(len(lines), 24)
        for line in lines:
            self.assertEqual(line.date, date(2020, 4, 18))

    def test_timestamp_date_only_str(self):
        self.provider.with_context(
            step={"hours": 1},
            override_date_since=datetime(2020, 4, 18, 0, 0),
            override_date_until=datetime(2020, 4, 18, 4, 0),
            timestamp_mode="str",
        )._pull(
            datetime(2020, 4, 18, 0, 0),
            datetime(2020, 4, 18, 4, 0),
        )
        statements = self._getExpectedStatements(1)
        lines = statements.line_ids
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].date, date(2020, 4, 18))
        self.assertEqual(lines[1].date, date(2020, 4, 18))
        self.assertEqual(lines[2].date, date(2020, 4, 18))
        self.assertEqual(lines[3].date, date(2020, 4, 18))

    def _get_statement_line_data(self, statement_date):
        return [
            {
                "payment_ref": "payment",
                "amount": 100,
                "date": statement_date,
                "unique_import_id": str(statement_date),
                "partner_name": "John Doe",
                "account_number": "XX00 0000 0000 0000",
            }
        ], {}

    def test_dont_create_empty_statements(self):
        """Test the default behavior of not creating empty bank
        statements ('Allow empty statements' field is uncheck at the
        provider level.).
        """
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.side_effect = [
                self._get_statement_line_data(date(2021, 8, 10)),
                ([], {}),  # August 8th, doesn't have statement
                ([], {}),  # August 9th, doesn't have statement
                self._get_statement_line_data(date(2021, 8, 13)),
            ]
            self.provider._pull(datetime(2021, 8, 10), datetime(2021, 8, 14))
        statements = self._getExpectedStatements(2)
        self.assertEqual(statements[0].balance_start, 0)
        self.assertEqual(statements[0].balance_end, 100)
        self.assertEqual(len(statements[0].line_ids), 1)
        self.assertEqual(statements[1].balance_start, 100)
        self.assertEqual(statements[1].balance_end, 200)
        self.assertEqual(len(statements[1].line_ids), 1)

    def test_dont_create_statement(self):
        self.provider.statement_creation_mode = "monthly"
        self.provider.create_statement = False
        date_since = datetime(2024, 12, 1)
        date_until = datetime(2024, 12, 31, 23, 59, 59)
        self.provider.with_context(step={"days": 1})._pull(date_since, date_until)
        self._getExpectedStatements(0)
        self._getExpectedLines(31)

    def test_pull_window_failure_does_not_discard_siblings(self):
        """A failed window must not take the remaining ones down with it.

        The marker advances at the end of the run either way, so a window that
        is skipped is never requested again.
        """
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.side_effect = [
                Exception("Expected"),  # August 10th fails
                self._get_statement_line_data(date(2021, 8, 11)),
                self._get_statement_line_data(date(2021, 8, 12)),
            ]
            self.provider.with_context(scheduled=True)._pull(
                datetime(2021, 8, 10), datetime(2021, 8, 13)
            )
        # The two windows after the failure were still processed.
        self._getExpectedStatements(2)
        self.assertEqual(mock_data.call_count, 3)

    def test_pull_gives_up_after_consecutive_failures(self):
        """A broken provider is asked a bounded number of times.

        Expired credentials fail on every single window; there is no point in
        asking the remote service once per window to find that out.
        """
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.side_effect = Exception("Expected")
            self.provider.with_context(scheduled=True)._pull(
                datetime(2021, 8, 1), datetime(2021, 8, 31)
            )
        self.assertEqual(mock_data.call_count, MAX_CONSECUTIVE_FAILURES)
        self._getExpectedStatements(0)

    def test_pull_gives_up_without_claiming_untried_windows(self):
        """Giving up must not mark as pulled what was never requested.

        The run stops after the cap, but the range it was asked for reaches
        much further; if the marker jumped to the end anyway, every window
        after the third would be silently skipped for good.
        """
        since = datetime(2021, 8, 1)
        self.provider.next_run = datetime(2021, 8, 31)
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.side_effect = Exception("Expected")
            self.provider.with_context(scheduled=True)._pull(
                since, datetime(2021, 8, 31)
            )
        # It resumes where the failures started, not where the range ended.
        self.assertEqual(
            self.provider.last_successful_run,
            self.provider._get_statement_date_since(since),
        )

    def test_pull_resumes_at_the_start_of_the_failure_streak(self):
        """Every window of the streak is retried, not just the last one.

        Reaching the cap means three windows failed; resuming at the third
        would leave the first two behind.
        """
        since = datetime(2021, 8, 1)
        self.provider.next_run = datetime(2021, 8, 31)
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.side_effect = [
                self._get_statement_line_data(date(2021, 8, 1)),
                Exception("Expected"),
                Exception("Expected"),
                Exception("Expected"),
            ]
            self.provider.with_context(scheduled=True)._pull(
                since, datetime(2021, 8, 31)
            )
        # The first window succeeded, so the streak starts on the second one.
        self.assertEqual(
            self.provider.last_successful_run,
            self.provider._get_statement_date_since(since) + relativedelta(days=1),
        )

    def test_pull_without_giving_up_marks_the_whole_range(self):
        """A run that covered its range keeps claiming all of it.

        Freezing the marker on any failure was rejected on purpose: a provider
        whose API answers "no data yet" would never move on.
        """
        self.provider.next_run = datetime(2021, 8, 4)
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.side_effect = [
                self._get_statement_line_data(date(2021, 8, 1)),
                Exception("Expected"),
                self._get_statement_line_data(date(2021, 8, 3)),
            ]
            self.provider.with_context(scheduled=True)._pull(
                datetime(2021, 8, 1), datetime(2021, 8, 4)
            )
        self.assertEqual(self.provider.last_successful_run, datetime(2021, 8, 4))

    def test_pull_survives_a_real_database_error(self):
        """The savepoint has to hold against what actually aborts a cursor.

        A Python exception leaves the transaction usable, so every other test
        here would pass without a savepoint at all. A database error does not:
        without it the cursor stays aborted and even writing the failure to the
        provider raises, taking the whole run down.
        """

        def boom(*args, **kwargs):
            self.env.cr.execute("SELECT 1 / 0")

        with mock.patch(mock_obtain_statement_data) as mock_data, mock.patch.object(
            type(self.provider), "_create_or_update_statement", side_effect=boom
        ):
            mock_data.return_value = self._line_data(date(2021, 8, 10), ["a"])
            with mute_logger("odoo.sql_db"):
                self.provider.with_context(scheduled=True)._pull(
                    datetime(2021, 8, 10), datetime(2021, 8, 11)
                )
        # The run reached its end and could still write to the provider.
        self.assertTrue(
            self.provider.message_ids.filtered(
                lambda m: m.subject == "Issue with Online Bank Statement Provider"
            )
        )

    def test_pull_survives_a_database_error_while_obtaining(self):
        """Obtaining the data writes to the database too, and can fail there.

        A provider refreshing an access token persists it inside
        ``_obtain_statement_data``. Protecting only the statement write leaves
        that one bare: the cursor stays aborted, and what dies is not this
        window but the run of every provider, with the marker left behind.
        """

        def boom(*args, **kwargs):
            self.env.cr.execute("SELECT 1 / 0")

        self.provider.next_run = datetime(2021, 8, 11)
        with mock.patch(mock_obtain_statement_data, side_effect=boom):
            with mute_logger("odoo.sql_db"):
                self.provider.with_context(scheduled=True)._pull(
                    datetime(2021, 8, 10), datetime(2021, 8, 11)
                )
        # Both of the things that come after the loop still happened: the run
        # reported its failure, and it moved the schedule on. An isolated
        # failure is meant to leave the marker alone -- only the overlap brings
        # that window back -- so what this pins is that the code past the loop
        # ran at all instead of dying on an aborted cursor.
        self.assertTrue(
            self.provider.message_ids.filtered(
                lambda m: m.subject == "Issue with Online Bank Statement Provider"
            )
        )
        self.assertEqual(self.provider.last_successful_run, datetime(2021, 8, 11))

    def test_obtaining_writes_survive_a_later_failure_of_the_window(self):
        """The guard against fixing the above by widening the savepoint.

        A token refreshed while obtaining the data has already been paid for.
        Rolling it back with the statement would make the next run buy it
        again, so the two halves cannot share one savepoint -- which is the
        shape a careless fix of the aborted cursor would arrive at.
        """
        refreshed = "refreshed-token"

        def refresh_then_hand_over_data(*args, **kwargs):
            self.provider.passphrase = refreshed
            return self._line_data(date(2021, 8, 10), ["a"])

        def boom(*args, **kwargs):
            self.env.cr.execute("SELECT 1 / 0")

        with mock.patch(
            mock_obtain_statement_data, side_effect=refresh_then_hand_over_data
        ), mock.patch.object(
            type(self.provider), "_create_or_update_statement", side_effect=boom
        ):
            with mute_logger("odoo.sql_db"):
                self.provider.with_context(scheduled=True)._pull(
                    datetime(2021, 8, 10), datetime(2021, 8, 11)
                )
        # Read it off the table: the point is that the value reached the
        # database and outlived the rollback, not that it sits in the cache.
        self.env.cr.execute(
            "SELECT passphrase FROM online_bank_statement_provider WHERE id = %s",
            (self.provider.id,),
        )
        self.assertEqual(self.env.cr.fetchone()[0], refreshed)

    def test_pull_counts_failures_in_a_row_only(self):
        """A window going well clears the count, it does not just pause it.

        Otherwise scattered failures over a long range would add up to the cap
        and stop a run that is mostly working.
        """
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.side_effect = [
                Exception("Expected"),
                self._get_statement_line_data(date(2021, 8, 2)),
                Exception("Expected"),
                self._get_statement_line_data(date(2021, 8, 4)),
                Exception("Expected"),
                self._get_statement_line_data(date(2021, 8, 6)),
            ]
            self.provider.with_context(scheduled=True)._pull(
                datetime(2021, 8, 1), datetime(2021, 8, 7)
            )
        # Three failures, never two in a row: the run covered its whole range.
        self.assertEqual(mock_data.call_count, 6)

    def test_pull_reports_its_failures_once(self):
        """Several failed windows leave one message, not one each.

        Continuing after a failure means a broken provider fails on every
        window of the run; a message per window buries the record and makes it
        tempting to give up earlier than is good for the data.
        """
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.side_effect = Exception("Expected")
            self.provider.with_context(scheduled=True)._pull(
                datetime(2021, 8, 1), datetime(2021, 8, 31)
            )
        messages = self.provider.message_ids.filtered(
            lambda m: m.subject == "Issue with Online Bank Statement Provider"
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("3 period(s)", messages.body)

    def test_negative_overlap_is_refused(self):
        """A typo must not silently stop the provider from pulling anything.

        A negative value would push the window start past its end, so the loop
        would not run once: no request, no error, and the marker moving on.
        """
        with self.assertRaises(IntegrityError):
            with mute_logger("odoo.sql_db"):
                self.provider.overlap_days = -1
                self.provider.flush_recordset()

    def test_pull_statement_creation_error_does_not_block_schedule(self):
        """An error while creating the statement must not jam the cron.

        Before, it escaped the handler and aborted the run before the marker
        advanced, so the next run retried the very same window and failed again.
        """
        self.provider.next_run = self.now
        with mock.patch(mock_obtain_statement_data) as mock_data, mock.patch.object(
            type(self.provider),
            "_create_or_update_statement",
            side_effect=Exception("Cannot create"),
        ):
            mock_data.side_effect = [
                self._get_statement_line_data(date(2021, 8, 10)),
                self._get_statement_line_data(date(2021, 8, 11)),
            ]
            self.provider.with_context(scheduled=True)._pull(
                datetime(2021, 8, 10), datetime(2021, 8, 12)
            )
        self.assertEqual(self.provider.last_successful_run, self.now)

    def test_scheduled_pull_overlap_requests_previous_days(self):
        """Overlap adds exactly one extra window per configured day."""
        self.OnlineBankStatementProvider.search([("id", "!=", self.provider.id)]).write(
            {"active": False}
        )
        self.provider.next_run = self.now
        self.provider.last_successful_run = self.now - relativedelta(days=1)
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = ([], {})
            self.provider._scheduled_pull()
        without_overlap = mock_data.call_count

        self.provider.next_run = self.now
        self.provider.last_successful_run = self.now - relativedelta(days=1)
        self.provider.overlap_days = 2
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = ([], {})
            self.provider._scheduled_pull()
        self.assertEqual(mock_data.call_count, without_overlap + 2)

    def test_the_marker_does_not_walk_backwards_run_after_run(self):
        """A broken provider must not widen its pending range for ever.

        Giving up resumes at the start of the failure streak, and with an
        overlap that start is already the shifted one, so the next run shifts it
        again -- and the next, and the next. The range grows for as long as the
        provider stays broken, and the bill arrives when it recovers: the
        catch-up is then attempted in a single run.
        """
        self.OnlineBankStatementProvider.search([("id", "!=", self.provider.id)]).write(
            {"active": False}
        )
        start = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        # A daily period leaves next_run alone between runs.
        self.provider.interval_type = "days"
        self.provider.interval_number = 1
        self.provider.next_run = start
        self.provider.last_successful_run = start
        self.provider.overlap_days = 3
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.side_effect = Exception("Expected")
            self.provider._scheduled_pull()
            after_one = self.provider.last_successful_run
            self.provider._scheduled_pull()
        # It holds where the run's own range started, both times: the overlap is
        # re-derived from the marker, so those days are covered again anyway.
        self.assertEqual(after_one, start)
        self.assertEqual(self.provider.last_successful_run, start)

    def test_scheduled_pull_marks_the_recheck_before_shifting_the_range(self):
        """Production has to switch the detector on, and in the right order.

        The mark is where the run's own range starts, so it has to be taken
        before the overlap moves that start back. Taken after, every window
        would look due and the report would never fire again -- with the whole
        suite still green, since every other test sets the context by hand and
        the one that does come through here returns no data at all.
        """
        self.OnlineBankStatementProvider.search([("id", "!=", self.provider.id)]).write(
            {"active": False}
        )
        today = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today - relativedelta(days=1)
        recovered = today - relativedelta(days=2)
        # A daily period leaves next_run alone, so the windows land on days.
        self.provider.interval_type = "days"
        self.provider.interval_number = 1
        # That day has to have been closed before, or there is no recovery to
        # report: the run that creates the statement is not one.
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._line_data(recovered.date(), ["a"])
            self.provider._pull(recovered, recovered + relativedelta(days=1))
        self.assertFalse(self._late_line_messages())

        self.provider.next_run = today
        self.provider.last_successful_run = yesterday
        self.provider.overlap_days = 1
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.side_effect = [
                self._line_data(recovered.date(), ["a", "late"]),
                self._line_data(yesterday.date(), ["b"]),
            ]
            self.provider._scheduled_pull()
        # Reported for the day the overlap went back for, and not for the one
        # the run was due to cover anyway.
        messages = self._late_line_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn(self.provider.make_statement_name(recovered), messages.body)

    def _line_data(self, statement_date, suffixes):
        return [
            {
                "payment_ref": "payment",
                "amount": 100,
                "date": statement_date,
                "unique_import_id": "%s-%s" % (statement_date, suffix),
                "partner_name": "John Doe",
                "account_number": "XX00 0000 0000 0000",
            }
            for suffix in suffixes
        ], {}

    def _late_line_messages(self):
        return self.provider.message_ids.filtered(
            lambda m: m.subject == "Late lines recovered"
        )

    def test_pull_recovers_late_lines_and_says_so(self):
        """A day closed incomplete gets completed, once, and leaves a trace.

        Requesting a closed day again is how the overlap recovers what the
        provider published late; doing it repeatedly must not duplicate.
        """
        day = date(2021, 8, 10)
        since = datetime(2021, 8, 10)
        until = datetime(2021, 8, 11)
        recheck = self.provider.with_context(statement_recheck_before=until)
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._line_data(day, ["a"])
            recheck._pull(since, until)
        statement = self._getExpectedStatements(1)
        self.assertEqual(len(statement.line_ids), 1)
        # This pass is the one that created the statement, so the day had not
        # been closed and there is nothing to call a recovery.
        self.assertFalse(self._late_line_messages())

        # The late line shows up on a day already closed.
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._line_data(day, ["a", "b"])
            recheck._pull(since, until)
        self.assertEqual(len(statement.line_ids), 2)
        self.assertEqual(len(self._late_line_messages()), 1)

        # Asking again brings nothing new, and says nothing either.
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._line_data(day, ["a", "b"])
            recheck._pull(since, until)
        self.assertEqual(len(statement.line_ids), 2)
        self.assertEqual(len(self._late_line_messages()), 1)

    def _balance_rewrite_messages(self):
        return self.provider.message_ids.filtered(
            lambda m: m.subject == "Statement balances rewritten"
        )

    def _balanced_line_data(self, statement_date, suffixes, balance_end):
        """Line data that also carries balances, as a real provider does."""
        lines, values = self._line_data(statement_date, suffixes)
        values.update({"balance_start": 0, "balance_end_real": balance_end})
        return lines, values

    def test_rewriting_the_balances_of_a_closed_day_leaves_a_trace(self):
        """A balance adjusted by hand must not vanish without a record.

        The rewrite itself is right: the day had been closed incomplete, so the
        figures the provider reports now are the good ones and the adjustment
        was compensating for what was missing. What is not acceptable is doing
        it silently, since afterwards there is no way to know what the statement
        used to say.
        """
        day = date(2021, 8, 10)
        since = datetime(2021, 8, 10)
        until = datetime(2021, 8, 11)
        recheck = self.provider.with_context(statement_recheck_before=until)
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._balanced_line_data(day, ["a"], 100)
            recheck._pull(since, until)
        statement = self._getExpectedStatements(1)
        self.assertFalse(self._balance_rewrite_messages())

        # Someone squares the day by hand, and then the missing line arrives.
        statement.balance_end_real = 500
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._balanced_line_data(day, ["a", "b"], 200)
            recheck._pull(since, until)
        messages = self._balance_rewrite_messages()
        self.assertEqual(len(messages), 1)
        # The trace carries both figures, or it is not a trace.
        self.assertIn("500", messages.body)
        self.assertIn("200", messages.body)

    def test_unchanged_balances_are_rewritten_without_a_word(self):
        """Re-checking a period that had nothing left to bring says nothing.

        The overlap re-requests the same closed periods on every run, so a
        message on every write would fire each time and bury the one case worth
        reading.
        """
        day = date(2021, 8, 10)
        since = datetime(2021, 8, 10)
        until = datetime(2021, 8, 11)
        recheck = self.provider.with_context(statement_recheck_before=until)
        for _run in range(2):
            with mock.patch(mock_obtain_statement_data) as mock_data:
                mock_data.return_value = self._balanced_line_data(day, ["a"], 100)
                recheck._pull(since, until)
        self._getExpectedStatements(1)
        self.assertFalse(self._balance_rewrite_messages())

    def test_growing_balances_of_the_current_day_say_nothing(self):
        """Building the day in progress is not a rewrite worth reporting.

        An hourly provider with daily statements re-writes the same statement on
        every run, and its balances grow each time as the day fills up. If that
        counted, the message would fire every hour -- the same noise that the
        recovery report had to be cured of.
        """
        day = date(2021, 8, 10)
        since = datetime(2021, 8, 10)
        until = datetime(2021, 8, 11)
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._balanced_line_data(day, ["a"], 100)
            self.provider._pull(since, until)
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._balanced_line_data(day, ["a", "b"], 200)
            self.provider._pull(since, until)
        self.assertFalse(self._balance_rewrite_messages())

    def test_pull_does_not_cry_recovery_while_building_the_current_period(self):
        """Polling more often than the statement period is not a recovery.

        With an hourly provider and daily statements every run re-requests the
        day being built, so the statement is already there from the second run
        on. Reporting that as a late recovery would fire every hour and drown
        the signal the message exists to give.
        """
        day = date(2021, 8, 10)
        since = datetime(2021, 8, 10)
        until = datetime(2021, 8, 11)
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._line_data(day, ["a"])
            self.provider._pull(since, until)
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._line_data(day, ["a", "b"])
            self.provider._pull(since, until)
        statement = self._getExpectedStatements(1)
        self.assertEqual(len(statement.line_ids), 2)
        self.assertFalse(self._late_line_messages())

    def test_late_lines_survive_without_a_statement_and_go_unreported(self):
        """A provider that creates no statement recovers, and says nothing.

        The lines are what matters, and they used to be lost: the report named
        the statement, an empty recordset came back, and naming it raised inside
        the savepoint that had just created the recovered lines, so the rollback
        threw away exactly what the overlap had gone back for -- and a scheduled
        run swallowed it, counting the window as one more failure.

        Nothing is reported by this path on purpose. Whether the period had been
        imported before is a fact read off the statement, and here there is
        none: a report would be asserting something nobody checked.
        """
        day = date(2021, 8, 10)
        since = datetime(2021, 8, 10)
        until = datetime(2021, 8, 11)
        self.provider.create_statement = False
        recheck = self.provider.with_context(
            scheduled=True, statement_recheck_before=until
        )
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._line_data(day, ["a"])
            recheck._pull(since, until)
        self._getExpectedStatements(0)
        self._getExpectedLines(1)

        # The late line shows up on a period already imported.
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._line_data(day, ["a", "b"])
            recheck._pull(since, until)
        # The recovered line is there, which is the point, and nothing was
        # claimed about a statement that does not exist.
        self._getExpectedLines(2)
        self.assertFalse(self._late_line_messages())
        self.assertFalse(self._balance_rewrite_messages())

    def test_a_statement_of_the_same_name_is_not_a_rewrite_either(self):
        """With `create_statement` off, finding one does not mean touching it.

        Nothing is looked up or written down that path, so a statement carrying
        the name of the period -- left by a spell with the flag on, or by an
        import following the same convention -- must not be reported as
        rewritten by a run that never went near it.
        """
        day = date(2021, 8, 10)
        since = datetime(2021, 8, 10)
        until = datetime(2021, 8, 11)
        # The period gets its statement while the flag is on, which is one of
        # the ways a homonymous one comes to exist.
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._balanced_line_data(day, ["a"], 100)
            self.provider._pull(since, until)
        self._getExpectedStatements(1)

        self.provider.create_statement = False
        recheck = self.provider.with_context(
            scheduled=True, statement_recheck_before=until
        )
        with mock.patch(mock_obtain_statement_data) as mock_data:
            mock_data.return_value = self._balanced_line_data(day, ["a", "b"], 200)
            recheck._pull(since, until)
        self.assertFalse(self._balance_rewrite_messages())
        self.assertFalse(self._late_line_messages())

    @mute_logger("odoo.models.unlink")
    def test_unlink_provider(self):
        """Unlink provider should clear fields on journal."""
        self.provider.unlink()
        self.assertEqual(self.journal.bank_statements_source, "undefined")
        self.assertEqual(self.journal.online_bank_statement_provider, False)
        self.assertEqual(self.journal.online_bank_statement_provider_id.id, False)

    def _getExpectedStatements(self, expected_length):
        """Check for length of statement recordset, with helpfull logging."""
        statements = self.AccountBankStatement.search(
            [("journal_id", "=", self.journal.id)], order="date asc"
        )
        actual_length = len(statements)
        # If length not expected, log information about statements.
        if actual_length != expected_length:
            if actual_length == 0:
                _logger.warning(
                    _("No statements found in journal"),
                )
            else:
                _logger.warning(
                    _("Names and dates for statements found: %(statements)s"),
                    dict(
                        statements=", ".join(
                            ["%s - %s" % (stmt.name, stmt.date) for stmt in statements]
                        )
                    ),
                )
        # Now do the normal assert.
        self.assertEqual(len(statements), expected_length)
        # If we got expected number, return them.
        return statements

    def _getExpectedLines(self, expected_length):
        """Check number of lines created."""
        lines = self.AccountBankStatementLine.search(
            [("journal_id", "=", self.journal.id)]
        )
        self.assertEqual(len(lines), expected_length)
        # If we got expected number, return them.
        return lines
