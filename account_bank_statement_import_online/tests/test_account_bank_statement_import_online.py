# Copyright 2019-2020 Brainbean Apps (https://brainbeanapps.com)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from datetime import datetime
from dateutil.relativedelta import relativedelta
from psycopg2 import IntegrityError
from urllib.error import HTTPError

from unittest import mock

from odoo.tests import common
from odoo.tools import mute_logger
from odoo import fields

from odoo.addons.account_bank_statement_import_online.models import (
    online_bank_statement_provider as provider_model,
)

MAX_CONSECUTIVE_FAILURES = provider_model.MAX_CONSECUTIVE_FAILURES
_mock_obtain = (
    'odoo.addons.account_bank_statement_import_online.tests'
    '.online_bank_statement_provider_dummy'
    '.OnlineBankStatementProviderDummy._obtain_statement_data'
)


class TestAccountBankAccountStatementImportOnline(common.TransactionCase):

    def setUp(self):
        super(TestAccountBankAccountStatementImportOnline, self).setUp()

        self.now = fields.Datetime.from_string(fields.Datetime.now())
        self.AccountJournal = self.env['account.journal']
        self.OnlineBankStatementProvider = self.env[
            'online.bank.statement.provider'
        ]
        self.OnlineBankStatementPullWizard = self.env[
            'online.bank.statement.pull.wizard'
        ]
        self.AccountBankStatement = self.env['account.bank.statement']
        self.AccountBankStatementLine = self.env['account.bank.statement.line']

    def test_provider_unlink_restricted(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
        })
        journal.bank_statements_source = 'online'
        journal.online_bank_statement_provider = 'dummy'

        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            journal.online_bank_statement_provider_id.unlink()

    def test_cascade_unlink(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
        })
        journal.bank_statements_source = 'online'
        journal.online_bank_statement_provider = 'dummy'

        self.assertTrue(journal.online_bank_statement_provider_id)
        journal.unlink()
        self.assertFalse(self.OnlineBankStatementProvider.search([]))

    def test_source_change_cleanup(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
        })
        journal.bank_statements_source = 'online'
        journal.online_bank_statement_provider = 'dummy'

        self.assertTrue(journal.online_bank_statement_provider_id)

        journal.bank_statements_source = 'undefined'

        self.assertFalse(journal.online_bank_statement_provider_id)
        self.assertFalse(self.OnlineBankStatementProvider.search([]))

    def test_pull_mode_daily(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.statement_creation_mode = 'daily'

        provider.with_context(step={'hours': 2})._pull(
            self.now - relativedelta(days=1),
            self.now,
        )
        self.assertEqual(
            len(self.AccountBankStatement.search(
                [('journal_id', '=', journal.id)]
            )),
            2
        )

    def test_pull_mode_weekly(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.statement_creation_mode = 'weekly'

        provider.with_context(step={'hours': 8})._pull(
            self.now - relativedelta(weeks=1),
            self.now,
        )
        self.assertEqual(
            len(self.AccountBankStatement.search(
                [('journal_id', '=', journal.id)]
            )),
            2
        )

    def test_pull_mode_monthly(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.statement_creation_mode = 'monthly'

        provider.with_context(step={'hours': 8})._pull(
            self.now - relativedelta(months=1),
            self.now,
        )
        self.assertEqual(
            len(self.AccountBankStatement.search(
                [('journal_id', '=', journal.id)]
            )),
            2
        )

    def test_pull_scheduled(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.next_run = (
            self.now - relativedelta(days=15)
        )

        self.assertFalse(self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
        ))

        provider.with_context(step={'hours': 8})._scheduled_pull()

        statement = self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
        )
        self.assertEqual(len(statement), 1)

    def test_pull_skip_duplicates_by_unique_import_id(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.statement_creation_mode = 'weekly'

        provider.with_context(
            step={'hours': 8},
            data_since=self.now - relativedelta(weeks=2),
            data_until=self.now,
        )._pull(
            self.now - relativedelta(weeks=2),
            self.now,
        )
        self.assertEqual(
            len(self.AccountBankStatementLine.search(
                [('journal_id', '=', journal.id)]
            )),
            14 * (24 / 8)
        )

        provider.with_context(
            step={'hours': 8},
            data_since=self.now - relativedelta(weeks=3),
            data_until=self.now - relativedelta(weeks=1),
        )._pull(
            self.now - relativedelta(weeks=3),
            self.now - relativedelta(weeks=1),
        )
        self.assertEqual(
            len(self.AccountBankStatementLine.search(
                [('journal_id', '=', journal.id)]
            )),
            21 * (24 / 8)
        )

        provider.with_context(
            step={'hours': 8},
            data_since=self.now - relativedelta(weeks=1),
            data_until=self.now,
        )._pull(
            self.now - relativedelta(weeks=1),
            self.now,
        )
        self.assertEqual(
            len(self.AccountBankStatementLine.search(
                [('journal_id', '=', journal.id)]
            )),
            21 * (24 / 8)
        )

    def test_interval_type_minutes(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.interval_type = 'minutes'
        provider._compute_update_schedule()

    def test_interval_type_hours(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.interval_type = 'hours'
        provider._compute_update_schedule()

    def test_interval_type_days(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.interval_type = 'days'
        provider._compute_update_schedule()

    def test_interval_type_weeks(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.interval_type = 'weeks'
        provider._compute_update_schedule()

    def _dummy_provider(self, mode='daily'):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })
        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.statement_creation_mode = mode
        return provider

    def _line_data(self, moment, suffixes):
        return [
            {
                'name': 'payment',
                'amount': 100,
                'date': moment,
                'unique_import_id': '%s-%s' % (moment, suffix),
                'partner_name': 'John Doe',
                'account_number': 'XX00 0000 0000 0000',
            }
            for suffix in suffixes
        ], {}

    def test_pull_window_failure_does_not_discard_siblings(self):
        """A failed window must not take the remaining ones down with it."""
        provider = self._dummy_provider()
        moment = self.now - relativedelta(days=2)
        with mock.patch(_mock_obtain) as obtain:
            obtain.side_effect = [
                Exception('Expected'),
                self._line_data(moment + relativedelta(days=1), ['a']),
                self._line_data(moment + relativedelta(days=2), ['b']),
            ]
            provider.with_context(scheduled=True)._pull(
                moment, self.now
            )
        self.assertEqual(obtain.call_count, 3)
        # Asking is not arriving: the call count alone would pass just the same
        # if the two good windows had been rolled back on the way out, which is
        # the failure this test exists to catch.
        self.assertEqual(len(self.AccountBankStatement.search(
            [('journal_id', '=', provider.journal_id.id)]
        )), 2)
        self.assertEqual(len(self.AccountBankStatementLine.search(
            [('journal_id', '=', provider.journal_id.id)]
        )), 2)

    def test_pull_gives_up_after_consecutive_failures(self):
        """A broken provider is asked a bounded number of times."""
        provider = self._dummy_provider()
        with mock.patch(_mock_obtain) as obtain:
            obtain.side_effect = Exception('Expected')
            provider.with_context(scheduled=True)._pull(
                self.now - relativedelta(days=30), self.now
            )
        self.assertEqual(obtain.call_count, MAX_CONSECUTIVE_FAILURES)

    def test_the_marker_does_not_walk_backwards_run_after_run(self):
        """A broken provider must not widen its pending range for ever.

        Giving up resumes at the start of the failure streak, and with an
        overlap that start is already the shifted one, so the next run shifts
        it again -- and the next. The range grows for as long as the
        provider stays broken, and the bill arrives when it recovers: the
        catch-up is then attempted in a single run.
        """
        provider = self._dummy_provider()
        self.OnlineBankStatementProvider.search(
            [('id', '!=', provider.id)]
        ).write({'active': False})
        start = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        # A daily period leaves next_run alone between runs.
        provider.interval_type = 'days'
        provider.interval_number = 1
        provider.next_run = fields.Datetime.to_string(start)
        provider.last_successful_run = fields.Datetime.to_string(start)
        provider.overlap_days = 3
        with mock.patch(_mock_obtain) as obtain:
            obtain.side_effect = Exception('Expected')
            provider._scheduled_pull()
            after_one = fields.Datetime.from_string(
                provider.last_successful_run
            )
            provider._scheduled_pull()
        # It holds where the run's own range started, both times: the overlap
        # is re-derived from the marker, so those days get covered anyway.
        self.assertEqual(after_one, start)
        self.assertEqual(
            fields.Datetime.from_string(provider.last_successful_run), start
        )

    def test_scheduled_pull_overlap_requests_previous_days(self):
        """Overlap adds exactly one extra window per configured day."""
        provider = self._dummy_provider()
        self.OnlineBankStatementProvider.search(
            [('id', '!=', provider.id)]
        ).write({'active': False})

        def windows_requested(overlap):
            provider.next_run = fields.Datetime.to_string(self.now)
            provider.last_successful_run = fields.Datetime.to_string(
                self.now - relativedelta(days=1)
            )
            provider.overlap_days = overlap
            with mock.patch(_mock_obtain) as obtain:
                obtain.return_value = ([], {})
                provider._scheduled_pull()
            return obtain.call_count

        # Each run sets its own starting point, so neither depends on the
        # other having gone first.
        self.assertEqual(windows_requested(2), windows_requested(0) + 2)

    def test_pull_survives_a_real_database_error(self):
        """The savepoint has to hold against what actually aborts a cursor.

        A Python exception leaves the transaction usable, so every other test
        here would pass without a savepoint at all. A database error does not:
        without it the cursor stays aborted and even writing the failure to the
        provider raises, taking the whole run down.
        """
        provider = self._dummy_provider()
        moment = self.now - relativedelta(days=1)

        def boom(*args, **kwargs):
            self.env.cr.execute('SELECT 1 / 0')

        with mock.patch(_mock_obtain) as obtain, \
                mock.patch.object(
                    type(provider), '_get_statement_date', side_effect=boom):
            obtain.return_value = self._line_data(moment, ['a'])
            with mute_logger('odoo.sql_db'):
                provider.with_context(scheduled=True)._pull(moment, self.now)
        # The run reached its end and could still write to the provider.
        self.assertTrue(provider.message_ids.filtered(
            lambda m: m.subject == 'Issue with Online Bank Statement Provider'
        ))

    def test_pull_survives_a_database_error_while_obtaining(self):
        """Obtaining the data writes to the database too, and can fail there.

        A provider refreshing an access token persists it inside
        ``_obtain_statement_data``. Protecting only the statement write leaves
        that one bare: the cursor stays aborted, and what dies is not this
        window but the run of every provider.
        """
        provider = self._dummy_provider()
        moment = self.now - relativedelta(days=1)

        def boom(*args, **kwargs):
            self.env.cr.execute('SELECT 1 / 0')

        with mock.patch(_mock_obtain, side_effect=boom):
            with mute_logger('odoo.sql_db'):
                provider.with_context(scheduled=True)._pull(moment, self.now)
        # The run reached its end and could still write to the provider.
        self.assertTrue(provider.message_ids.filtered(
            lambda m: m.subject == 'Issue with Online Bank Statement Provider'
        ))

    def test_obtaining_writes_survive_a_later_failure_of_the_window(self):
        """The guard against fixing the above by widening the savepoint.

        A token refreshed while obtaining the data has already been paid for.
        Rolling it back with the statement would make the next run buy it
        again, so the two halves cannot share one savepoint -- which is the
        shape a careless fix of the aborted cursor would arrive at.
        """
        provider = self._dummy_provider()
        moment = self.now - relativedelta(days=1)
        refreshed = 'refreshed-token'

        def refresh_then_hand_over_data(*args, **kwargs):
            provider.passphrase = refreshed
            return self._line_data(moment, ['a'])

        def boom(*args, **kwargs):
            self.env.cr.execute('SELECT 1 / 0')

        with mock.patch(_mock_obtain,
                        side_effect=refresh_then_hand_over_data), \
                mock.patch.object(
                    type(provider), '_get_statement_date', side_effect=boom):
            with mute_logger('odoo.sql_db'):
                provider.with_context(scheduled=True)._pull(moment, self.now)
        # Read it off the table: the point is that the value reached the
        # database and outlived the rollback, not that it sits in the cache.
        self.env.cr.execute(
            'SELECT passphrase FROM online_bank_statement_provider'
            ' WHERE id = %s',
            (provider.id,),
        )
        self.assertEqual(self.env.cr.fetchone()[0], refreshed)

    def test_pull_gives_up_without_claiming_untried_windows(self):
        """Giving up must not mark as pulled what was never requested."""
        provider = self._dummy_provider()
        since = datetime(2021, 8, 1)
        provider.next_run = fields.Datetime.to_string(datetime(2021, 8, 31))
        with mock.patch(_mock_obtain) as obtain:
            obtain.side_effect = Exception('Expected')
            provider.with_context(scheduled=True)._pull(
                since, datetime(2021, 8, 31)
            )
        self.assertEqual(
            fields.Datetime.from_string(provider.last_successful_run),
            provider._get_statement_date_since(since),
        )

    def test_pull_without_giving_up_marks_the_whole_range(self):
        """A run that covered its range keeps claiming all of it.

        Freezing the marker on any failure was rejected on purpose: a provider
        whose API answers "no data yet" would never move on.
        """
        provider = self._dummy_provider()
        provider.next_run = fields.Datetime.to_string(datetime(2021, 8, 4))
        with mock.patch(_mock_obtain) as obtain:
            obtain.side_effect = [
                ([], {}),
                Exception('Expected'),
                ([], {}),
            ]
            provider.with_context(scheduled=True)._pull(
                datetime(2021, 8, 1), datetime(2021, 8, 4)
            )
        self.assertEqual(
            fields.Datetime.from_string(provider.last_successful_run),
            datetime(2021, 8, 4),
        )

    def test_pull_reports_its_failures_once(self):
        """Several failed windows leave one message, not one each."""
        provider = self._dummy_provider()
        with mock.patch(_mock_obtain) as obtain:
            obtain.side_effect = Exception('Expected')
            provider.with_context(scheduled=True)._pull(
                datetime(2021, 8, 1), datetime(2021, 8, 31)
            )
        messages = provider.message_ids.filtered(
            lambda m: m.subject == 'Issue with Online Bank Statement Provider'
        )
        self.assertEqual(len(messages), 1)
        self.assertIn('3 period(s)', messages.body)

    def test_pull_does_not_cry_recovery_building_the_current_period(self):
        """Polling more often than the statement period is not a recovery.

        The second pass has to bring a line the first did not, or nothing could
        fire by any logic and the test would pass on its own: the same lines
        twice are deduplicated by their identifier and never reach the
        statement. As it stands the day is closed and it does grow, which is
        what the report has to keep quiet about while the run is only building
        the period it was due to cover.
        """
        provider = self._dummy_provider()
        moment = self.now - relativedelta(days=1)
        for suffixes in (['a'], ['a', 'b']):
            with mock.patch(_mock_obtain) as obtain:
                obtain.return_value = self._line_data(moment, suffixes)
                provider._pull(moment, self.now)
        self.assertEqual(len(self.AccountBankStatementLine.search(
            [('journal_id', '=', provider.journal_id.id)]
        )), 2)
        self.assertFalse(self._late_line_messages(provider))

    def _locked_period_messages(self, provider):
        subject = 'Movements left out of a validated statement'
        return provider.message_ids.filtered(lambda m: m.subject == subject)

    def test_validated_statement_is_not_duplicated(self):
        """A validated day must not get a second statement of its own.

        The search looked only for open statements, so a validated one was not
        found and a second statement was created for the same date. With the
        overlap that stops being a rarity: a client validating its statements
        would collect one duplicate per day recovered.
        """
        provider = self._dummy_provider()
        # A single day of range: otherwise the run also opens the statement of
        # the current day and the counts stop being readable.
        day = (self.now - relativedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        next_day = day + relativedelta(days=1)
        moment = day + relativedelta(hours=12)
        with mock.patch(_mock_obtain) as obtain:
            obtain.return_value = self._line_data(moment, ['a'])
            provider._pull(day, next_day)
        statements = self.AccountBankStatement.search(
            [('journal_id', '=', provider.journal_id.id)]
        )
        self.assertEqual(len(statements), 1)
        statements.write({'state': 'confirm'})

        # A movement of that day turns up after it was validated.
        with mock.patch(_mock_obtain) as obtain:
            obtain.return_value = self._line_data(moment, ['a', 'b'])
            provider.with_context(scheduled=True)._pull(day, next_day)

        # No second statement, and the late movement was not written into the
        # validated one either.
        self.assertEqual(len(self.AccountBankStatement.search(
            [('journal_id', '=', provider.journal_id.id)]
        )), 1)
        self.assertEqual(len(self.AccountBankStatementLine.search(
            [('journal_id', '=', provider.journal_id.id)]
        )), 1)
        # And it was not discarded in silence.
        messages = self._locked_period_messages(provider)
        self.assertEqual(len(messages), 1)
        self.assertIn('1 movement(s)', messages.body)

    def test_validated_statement_with_nothing_new_says_nothing(self):
        """The report clears itself once there is nothing left out.

        Otherwise a validated day would be reported on every run for ever, and
        with the overlap on that is every run of every day.
        """
        provider = self._dummy_provider()
        day = (self.now - relativedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        next_day = day + relativedelta(days=1)
        moment = day + relativedelta(hours=12)
        with mock.patch(_mock_obtain) as obtain:
            obtain.return_value = self._line_data(moment, ['a'])
            provider._pull(day, next_day)
        self.AccountBankStatement.search(
            [('journal_id', '=', provider.journal_id.id)]
        ).write({'state': 'confirm'})

        # Same movements as before: nothing counts as new.
        with mock.patch(_mock_obtain) as obtain:
            obtain.return_value = self._line_data(moment, ['a'])
            provider.with_context(scheduled=True)._pull(day, next_day)
        self.assertFalse(self._locked_period_messages(provider))

    def _late_line_messages(self, provider):
        return provider.message_ids.filtered(
            lambda m: m.subject == 'Late lines recovered'
        )

    def test_pull_recovers_late_lines_and_says_so(self):
        """A day closed incomplete gets completed, once, and leaves a trace.

        Only the negative case above was covered, so nothing checked that the
        report ever fires: the condition guarding it was never true in the
        suite, and it is the visible effect of the whole overlap.
        """
        provider = self._dummy_provider()
        moment = self.now - relativedelta(days=1)
        recheck = provider.with_context(statement_recheck_before=self.now)
        with mock.patch(_mock_obtain) as obtain:
            obtain.return_value = self._line_data(moment, ['a'])
            recheck._pull(moment, self.now)
        # This pass is the one that created the statement, so the day had not
        # been closed and there is nothing to call a recovery.
        self.assertFalse(self._late_line_messages(provider))

        # A line published late turns up on a day already closed.
        with mock.patch(_mock_obtain) as obtain:
            obtain.return_value = self._line_data(moment, ['a', 'b'])
            recheck._pull(moment, self.now)
        self.assertEqual(len(self.AccountBankStatementLine.search(
            [('journal_id', '=', provider.journal_id.id)]
        )), 2)
        self.assertEqual(len(self._late_line_messages(provider)), 1)

        # Asking again brings nothing new, and says nothing either.
        with mock.patch(_mock_obtain) as obtain:
            obtain.return_value = self._line_data(moment, ['a', 'b'])
            recheck._pull(moment, self.now)
        self.assertEqual(len(self._late_line_messages(provider)), 1)

    def test_pull_no_crash(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.statement_creation_mode = 'weekly'

        provider.with_context(
            crash=True,
            scheduled=True,
        )._pull(
            self.now - relativedelta(hours=1),
            self.now,
        )
        self.assertFalse(self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
        ))

    def test_pull_crash(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.statement_creation_mode = 'weekly'

        with self.assertRaises(Exception):
            provider.with_context(
                crash=True,
            )._pull(
                self.now - relativedelta(hours=1),
                self.now,
            )

    def test_pull_httperror(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.statement_creation_mode = 'weekly'

        with self.assertRaises(HTTPError):
            provider.with_context(
                crash=True,
                exception=HTTPError(None, 500, 'Error', None, None),
            )._pull(
                self.now - relativedelta(hours=1),
                self.now,
            )

    def test_pull_no_balance(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.statement_creation_mode = 'daily'

        provider.with_context(
            step={'hours': 2},
            balance_start=0,
            amount=100.0,
            balance=False,
        )._pull(
            self.now - relativedelta(days=1),
            self.now,
        )
        statements = self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
            order='date asc',
        )
        self.assertFalse(statements[0].balance_start)
        self.assertFalse(statements[0].balance_end_real)
        self.assertTrue(statements[0].balance_end)
        self.assertTrue(statements[1].balance_start)
        self.assertFalse(statements[1].balance_end_real)

    def test_wizard(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })
        action = journal.action_online_bank_statements_pull_wizard()
        self.assertTrue(action['context']['default_provider_ids'][0][2])

        wizard = self.OnlineBankStatementPullWizard.with_context(
            action['context']
        ).create({
            'date_since': self.now - relativedelta(hours=1),
            'date_until': self.now,
        })
        self.assertTrue(wizard.provider_ids)

        wizard.action_pull()
        self.assertTrue(self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
        ))

    def test_pull_statement_partially(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.statement_creation_mode = 'monthly'

        provider_context = {
            'step': {'hours': 24},
            'data_since': datetime(2020, 1, 1),
            'amount': 1.0,
            'balance_start': 0,
        }

        provider.with_context(
            **provider_context,
            data_until=datetime(2020, 1, 31),
        )._pull(
            datetime(2020, 1, 1),
            datetime(2020, 1, 31),
        )
        statements = self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
            order='date asc',
        )
        self.assertEqual(len(statements), 1)
        self.assertEqual(statements[0].balance_start, 0.0)
        self.assertEqual(statements[0].balance_end_real, 30.0)

        provider.with_context(
            **provider_context,
            data_until=datetime(2020, 2, 15),
        )._pull(
            datetime(2020, 1, 1),
            datetime(2020, 2, 29),
        )
        statements = self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
            order='date asc',
        )
        self.assertEqual(len(statements), 2)
        self.assertEqual(statements[0].balance_start, 0.0)
        self.assertEqual(statements[0].balance_end_real, 31.0)
        self.assertEqual(statements[1].balance_start, 31.0)
        self.assertEqual(statements[1].balance_end_real, 45.0)

        provider.with_context(
            **provider_context,
            data_until=datetime(2020, 2, 29),
        )._pull(
            datetime(2020, 1, 1),
            datetime(2020, 2, 29),
        )
        statements = self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
            order='date asc',
        )
        self.assertEqual(len(statements), 2)
        self.assertEqual(statements[0].balance_start, 0.0)
        self.assertEqual(statements[0].balance_end_real, 31.0)
        self.assertEqual(statements[1].balance_start, 31.0)
        self.assertEqual(statements[1].balance_end_real, 59.0)

    def test_tz_utc(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.tz = 'UTC'
        provider.with_context(
            step={'hours': 1},
            data_since=datetime(2020, 4, 17, 22, 0),
            data_until=datetime(2020, 4, 18, 2, 0),
            tz='UTC',
        )._pull(
            datetime(2020, 4, 17, 22, 0),
            datetime(2020, 4, 18, 2, 0),
        )

        statement = self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
        )
        self.assertEqual(len(statement), 2)

        lines = statement.mapped('line_ids').sorted()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].date, '2020-04-17')
        self.assertEqual(lines[1].date, '2020-04-17')
        self.assertEqual(lines[2].date, '2020-04-18')
        self.assertEqual(lines[3].date, '2020-04-18')

    def test_tz_non_utc(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.tz = 'Etc/GMT-2'
        provider.with_context(
            step={'hours': 1},
            data_since=datetime(2020, 4, 17, 22, 0),
            data_until=datetime(2020, 4, 18, 2, 0),
            tz='UTC',
        )._pull(
            datetime(2020, 4, 17, 22, 0),
            datetime(2020, 4, 18, 2, 0),
        )

        statement = self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
        )
        self.assertEqual(len(statement), 2)

        lines = statement.mapped('line_ids').sorted()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].date, '2020-04-18')
        self.assertEqual(lines[1].date, '2020-04-18')
        self.assertEqual(lines[2].date, '2020-04-18')
        self.assertEqual(lines[3].date, '2020-04-18')

    def test_other_tz_to_utc(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.with_context(
            step={'hours': 1},
            tz='Etc/GMT-2',
            data_since=datetime(2020, 4, 18, 0, 0),
            data_until=datetime(2020, 4, 18, 4, 0),
        )._pull(
            datetime(2020, 4, 17, 22, 0),
            datetime(2020, 4, 18, 2, 0),
        )

        statement = self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
        )
        self.assertEqual(len(statement), 2)

        lines = statement.mapped('line_ids').sorted()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].date, '2020-04-17')
        self.assertEqual(lines[1].date, '2020-04-17')
        self.assertEqual(lines[2].date, '2020-04-18')
        self.assertEqual(lines[3].date, '2020-04-18')

    def test_timestamp_date_only_date(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.with_context(
            step={'hours': 1},
            timestamp_mode='date',
        )._pull(
            datetime(2020, 4, 18, 0, 0),
            datetime(2020, 4, 18, 4, 0),
        )

        statement = self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
        )
        self.assertEqual(len(statement), 1)

        lines = statement.line_ids
        self.assertEqual(len(lines), 24)
        for line in lines:
            self.assertEqual(line.date, '2020-04-18')

    def test_timestamp_date_only_str(self):
        journal = self.AccountJournal.create({
            'name': 'Bank',
            'type': 'bank',
            'code': 'BANK',
            'bank_statements_source': 'online',
            'online_bank_statement_provider': 'dummy',
        })

        provider = journal.online_bank_statement_provider_id
        provider.active = True
        provider.with_context(
            step={'hours': 1},
            data_since=datetime(2020, 4, 18, 0, 0),
            data_until=datetime(2020, 4, 18, 4, 0),
            timestamp_mode='str',
        )._pull(
            datetime(2020, 4, 18, 0, 0),
            datetime(2020, 4, 18, 4, 0),
        )

        statement = self.AccountBankStatement.search(
            [('journal_id', '=', journal.id)],
        )
        self.assertEqual(len(statement), 1)

        lines = statement.line_ids
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].date, '2020-04-18')
        self.assertEqual(lines[1].date, '2020-04-18')
        self.assertEqual(lines[2].date, '2020-04-18')
        self.assertEqual(lines[3].date, '2020-04-18')
