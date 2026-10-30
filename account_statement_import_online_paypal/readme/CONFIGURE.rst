You will need a *Client ID* and *Secret* from PayPal to communicate with the PayPal API. To obtain your PayPal API
*Client ID* and *Secret*:

#. Open `PayPal Developer <https://developer.paypal.com/dashboard/>`_.
#. Login with your *PayPal for Business* account (upgrade your personal account to
   a Business Account, if required).
#. Go to *Apps & Credentials* and switch to *Live*.
#. Under *REST API apps*, click *Create App* to begin creating a new application.
#. Enter a descriptive name for your app (e.g. *Odoo-Statements*) and click *Create App*.
#. Copy the *Client ID* and *Secret* to use during provider configuration (instructions below).
#. Under *Features*, uncheck all optional features except *Transaction Search*.
#. Click *Save Changes*.

To configure PayPal as an online bank statement provider, you will need to create a Bank Account & Journal that
corresponds to your *PayPal for Business* account, and then configure the *PayPal.com* provider with the *Client ID*
and *Secret* obtained above:

#. Go to *Invoicing > Configuration > Banks > Add a Bank Account*.
#. In the *Account Number* field, enter some descriptive text for the account, such as the email address or *PayPal
   Merchant ID* of your *PayPal for Business* account. NOTE: This *Account Number* is not used in the authentication
   with PayPal's API, and is only used to distinguish this PayPal Bank Account/Journal from others you may have
   configured.
#. Fill in the other fields for the Bank Account if desired, and then click *Create*.
#. Go to *Invoicing > Configuration > Accounting > Journals*.
#. Open and *Edit* the Journal corresponding to the PayPal bank account (this Journal was created automatically
   when you created the Bank Account above.
#. Set *Bank Feeds* to *Online (OCA)*.
#. Select *PayPal.com* as the provider in the *Online Bank Statements (OCA)* section.
#. *Save* the Journal.
#. To configure provider-specific settings, click on the provider to open it and click *Edit*.
#. Fill in your desired *Configuration* and *Scheduled Pull* settings.
#. Leave the *API base* field empty, and fill in the *Client ID* and *Secret* from your PayPal
   Developer account.
#. Click *Save*.

NOTE: For development and testing purposes, you can create Sandbox credentials associated with your *PayPal
for Business* account. When configuring the provider-specific settings, enter the following in the *API base* field:
https://api.sandbox.paypal.com

Re-checking previous days
~~~~~~~~~~~~~~~~~~~~~~~~~

PayPal makes a movement available well after it happened, so the day a
scheduled run closes is routinely still incomplete, and the difference is lost
unless the day is requested again. This is the service the *Re-check previous
days* field of the provider exists for.

The field is 0 out of the box, here as everywhere else, so it has to be set
deliberately: the value belongs to the installation, which is the only place
where the schedule, the timezone and the accounting lock dates are known. Three
days covered every delay observed in the instances measured, and each extra day
costs one more period per run.

Two things worth knowing before settling on a value, both explained in the base
module: the requests grow with the overlap, and a period that has been closed
for accounting has its recovered lines dated into the first open one instead of
being refused.
