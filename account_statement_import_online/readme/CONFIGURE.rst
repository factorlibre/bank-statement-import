To configure online bank statements provider:

#. Go to *Invoicing > Configuration > Online Bank Statement Providers*
#. Create a provider and configure provider-specific settings.

If you want to allow empty bank statements to be created every time the
information is pulled, you can check the option "Allow empty statements"
at the provider configuration level.

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

* **Cost.** With N days of overlap each run asks for N+1 periods instead of
  one, so the requests it makes grow in that proportion. Contrast it with the
  usage limits of the service before settling on a value.
* **How late the service publishes.** The value only has to cover that delay.
  Beyond it, every run pays for periods that had nothing left to bring.
* **Locked accounting periods.** With a company lock date, lines recovered into
  a locked period are still created, with their date moved to the first open
  one: Odoo does not reject them, it shifts them, and nothing says so. A
  journal lock date -- from the module that adds one per journal -- behaves the
  opposite way: the line is not created at all and the window fails with an
  error. Check the lock dates before settling on a value, and before any bulk
  recovery of old periods.
* **Providers that run their own pull.** A provider overriding ``_pull``, or
  one that disables the automatic adjustment of its schedule, does not
  necessarily behave as described here, and is worth looking at on its own
  before an overlap is configured for it.

Finding lines in a period the overlap went back for is reported on the
provider's record, and so is any statement balance that the re-import
overwrites.

The overlap is also what recovers a period a run failed to obtain. A scheduled
run that gives up after `MAX_CONSECUTIVE_FAILURES` resumes at the start of that
failure streak, so it does not claim windows it never requested -- but that cap
counts failures within a single run and the count restarts on the next one. A
run holding fewer windows than the cap, which is what a frequent schedule
looks like, never reaches it: it advances its marker like a successful one, and
the periods it failed to obtain are only requested again if the overlap reaches
back to them. Leaving it at 0 leaves them with nothing to recover them.
