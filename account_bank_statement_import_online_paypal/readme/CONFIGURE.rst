To configure online bank statements provider:

#. Go to *Invoicing > Configuration > Bank Accounts*
#. Open bank account to configure and edit it
#. Set *Bank Feeds* to *Online*
#. Select *PayPal.com* as online bank statements provider in
   *Online Bank Statements (OCA)* section
#. Save the bank account
#. Click on provider and configure provider-specific settings.

or, alternatively:

#. Go to *Invoicing > Overview*
#. Open settings of the corresponding journal account
#. Switch to *Bank Account* tab
#. Set *Bank Feeds* to *Online*
#. Select *PayPal.com* as online bank statements provider in
   *Online Bank Statements (OCA)* section
#. Save the bank account
#. Click on provider and configure provider-specific settings.

To obtain *Client ID* and *Secret*:

#. Open `PayPal Developer <https://developer.paypal.com/developer/applications/>`_
#. Go to *My Apps & Credentials* and switch to *Live*
#. Under *REST API apps*, click *Create App* to create new application (e.g. *Odoo*)
#. Copy *Client ID* and *Secret* to use during provider configuration
#. Under *Live App Settings*, uncheck all features except *Transaction Search*
#. Click Save

Re-checking previous days
~~~~~~~~~~~~~~~~~~~~~~~~~

PayPal makes a movement available well after it happened, so the day a
scheduled run closes is routinely still incomplete, and the difference is lost
unless the day is requested again. This is the service the *Re-check previous
days* field of the provider exists for.

The field is 0 out of the box, here as everywhere else, so it has to be set
deliberately: the value belongs to the installation, which is the only place
where the schedule, the timezone and the accounting lock dates are known.
Three days covered every delay observed in the instances measured.

Mind the cost before raising it, because PayPal is usually configured as one
provider per currency and all of them are pulled in the same run: each period
costs three or four requests, so the requests per run grow with the overlap
and with the number of providers at the same time. The base module explains
that and the rest of what to weigh.
