* Only transactions for the previous three years are retrieved, historical data
  can be imported manually, see ``account_bank_statement_import_paypal``. See
  `PayPal Help Center article <https://www.paypal.com/us/smarthelp/article/why-can't-i-access-transaction-history-greater-than-3-years-ts2241>`_
  for details.
* `PayPal Transaction Info <https://developer.paypal.com/docs/api/transaction-search/v1/#definition-transaction_info>`_
  defines extra fields like ``tip_amount``, ``shipping_amount``, etc. that
  could be useful to be decomposed from a single transaction.
* There's a known issue with PayPal API that on every Monday for couple of
  hours after UTC midnight it returns ``INVALID_REQUEST`` incorrectly: their
  servers have not inflated the data yet. PayPal tech support confirmed this
  behaviour in case #06650320 (private).

  The same answer turns up on any weekday, not only on Monday, for a provider
  whose statement day is cut in its own timezone rather than in UTC: the
  scheduled run then asks for a window that has only just started. The error is
  therefore tolerated for any window starting within the last few hours, and
  that day is imported as empty and completed by a later run, once PayPal has
  consolidated it.

  It is tolerated only that close to the present. A window starting further
  back should have been consolidated long ago, so the error is raised and the
  run reports it instead of closing the day at zero: "there were no
  transactions" and "the transactions could not be read" must not look alike in
  a statement.
