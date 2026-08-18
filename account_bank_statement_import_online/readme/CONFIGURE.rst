To configure online bank statements provider:

#. Go to *Invoicing > Configuration > Bank Accounts*
#. Open bank account to configure and edit it
#. Set *Bank Feeds* to *Online*
#. Select online bank statements provider in *Online Bank Statements (OCA)*
   section
#. Save the bank account
#. Click on provider and configure provider-specific settings.

or, alternatively:

#. Go to *Invoicing > Overview*
#. Open settings of the corresponding journal account
#. Switch to *Bank Account* tab
#. Set *Bank Feeds* to *Online*
#. Select online bank statements provider in *Online Bank Statements (OCA)*
   section
#. Save the bank account
#. Click on provider and configure provider-specific settings.

**NOTE**: To access these features, user needs to belong to
*Show Full Accounting Features* group.

Re-checking previous days
~~~~~~~~~~~~~~~~~~~~~~~~~

A period is closed with whatever the provider has published by the time the
scheduled run reaches it, and it is never requested again: whatever the
provider publishes afterwards is lost, and silently, because the run moves its
marker on either way. The *Re-check previous days* field makes every scheduled
run request that many days again, so movements published late still find their
way in.

It is 0 by default, which is what a provider publishing without delay needs,
and it is set per provider. What to weigh before raising it:

* **Cost, which multiplies twice over here.** With N days of overlap each run
  asks for N+1 periods instead of one. And a service is often configured as
  one provider per currency, a dozen or more on a single installation being
  normal, and every one of them is pulled in the same run. For PayPal each
  period costs three or four requests, so fourteen providers with three days
  of overlap turn a run of some fifty requests into one of a couple of
  hundred. Contrast it with the usage limits of the service before settling
  on a value.

* **How late the service publishes.** The value only has to cover that delay.
  Beyond it, every run pays for periods that had nothing left to bring.

* **Locked accounting periods.** Importing does not touch accounting: a
  statement line creates no journal entry by itself, so the overlap reaches a
  locked period without complaining. The lock bites later, when the line is
  reconciled or the statement is validated, and then Odoo rejects the entry
  with an error instead of moving its date. Lines recovered into a locked
  period stay in the statement, unreconciled, until the period is opened; the
  adviser role bypasses the non-adviser lock date only, not the fiscal year
  one. Check the lock dates before settling on a value, and before any bulk
  recovery of old periods.

* **Statements that get validated.** A validated statement cannot be written
  to, so movements published late for such a day stay out. They are not
  discarded quietly: the provider's record reports which days and how many
  movements were left over, once per run, so they can be brought in either by
  reopening the statement and pulling again or by hand. That report stops on
  its own once they are in.

* **Providers that run their own pull.** A provider overriding ``_pull``, or
  one that disables the automatic adjustment of its schedule, does not
  necessarily behave as described here, and is worth looking at on its own
  before an overlap is configured for it.

Finding lines in a period the overlap went back for is reported on the
provider's record as well.
