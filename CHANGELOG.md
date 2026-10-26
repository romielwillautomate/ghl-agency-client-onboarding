# Changelog

## v1.1 (October 27, 2026): fixes from an adversarial review

A second pass looked for ways to create a second sub-account or user, to write into the wrong sub-account, to leave a run hanging, and for places where the mocks were kinder than HighLevel. Every behavior change below has its own test (91 checks now, all passing).

- **A sub-account that only shared the client's business name could be taken over.** After a lost create answer, the lookup among the newest sub-accounts matched by name alone, and the email lookup fell back to the name too. If the create had made nothing and another client had a business with the same name, the workflow would have added the new client as an admin user of that other client's sub-account and overwritten its custom values. Now only a sub-account with the client's name and email is adopted; a namesake is named in the alert and a person decides.
- **"createSubAccount: true" could create two sub-accounts.** A person's go-ahead let every later create in that run through: if the allowed create lost its answer as well and the lookups could not find it, the run created another one, and after three tries it cleared the "may exist" marker for the next retry. The go-ahead now covers one create, a lost answer always goes back to a lookup, and the marker is only cleared when HighLevel refused the create.
- **A create that kept getting 429 asked a person to look for a sub-account that could not exist.** A 429 means HighLevel did nothing, but after the waits the workflow treated it as a lost answer. Now it stops with the rate limit as the reason, and a plain retry creates the sub-account.
- **The snapshot wait relied on an endpoint HighLevel documents for snapshot pushes.** If "Get Last Snapshot Push" has no record of the load at creation, the workflow did not wait for the snapshot at all and could find every custom value missing. It now reads the custom values (before the calendars) until one of its own appears, within `SETUP_WAIT_SECONDS`, whenever the status could not confirm the load.
- **A returning client did not get the welcome email, and Slack said it did.** The contact still has the `new-client` tag from the first onboarding, so adding it again fires no trigger, and workflow 1 has re-entry off. The workflow now sees the tag in the upsert answer, skips the tag call, and the Slack summary says to send the welcome email and the tasks by hand. The recipe explains it.
- **A lock row that could never be taken made a run loop forever.** With `attempts` emptied by hand (or not a Number column), the lock looked free but the conditional update never matched, so the run waited and looked again without end while Stripe timed out and resent. After `LOCK_WAIT_SECONDS` plus 10 seconds it now answers 500 and raises an alert that says how to fix the row.
- **Smaller:** the sub-account token leaves the run's state when the custom values step ends (it stays in the HTTP node's saved output, as the README says); the status webhook test now sends the Custom Webhook's key and value pairs as a form, as HighLevel's article describes them.

Docs: the setup guide explains with HighLevel's own wording why the agency side needs an OAuth app (`/oauth/locationToken` and `/oauth/installedLocations` accept only an agency OAuth token, custom values only a sub-account token) and cites the developer docs on the Company token an agency install gets. The Agency Pro note now quotes what HighLevel's API article actually says (agency API keys), the Stripe click path follows Workbench ("Create an event destination", "Reveal secret"), the first test payment must use an email that is not a HighLevel user yet, "Allow Duplicate Contact" must stay off for the upsert, and the README is honest that a second client paying during an onboarding waits for Stripe's resend.

## v1 (October 26, 2026)

First build: a paid Stripe checkout for an onboarding plan creates the client's GoHighLevel sub-account from that plan's snapshot, adds the client's admin user, fills the sub-account's custom values and adds the client to the agency's own CRM, where two native GoHighLevel workflows take over (welcome email, team tasks, kickoff reminder). Built in n8n, with an error alert workflow, recipes for the native workflows, a snapshot checklist and an end to end test against strict mocks.

### Stripe

- The Stripe webhook keeps the raw body (n8n's Raw Body option) and checks the `Stripe-Signature` header the way Stripe documents it: HMAC SHA-256 with the endpoint secret over `timestamp.body`, every `v1` signature compared in constant time (two during a secret roll), `v0` ignored, and the timestamp within `STRIPE_TOLERANCE_SECONDS` (300, the default of Stripe's libraries). The event is then parsed from the raw bytes, not from n8n's parsed JSON.
- Answers: 400 for a bad signature (with an alert), 200 at once for events and payments that are not onboardings, 200 for a duplicate, 200 once the event is in the ledger and before any provisioning, 503 while another onboarding holds the lock, 500 when the settings or the ledger are broken (Stripe tries again later). Stripe never waits for GoHighLevel.
- `checkout.session.completed` with `payment_status: unpaid` (delayed payment methods) waits for `checkout.session.async_payment_succeeded`. A thin event without the Checkout Session gets a 200 and an alert that says to switch the destination to snapshot payloads.
- One onboarding per event ID and per Checkout Session, so Stripe's duplicate deliveries and a second event for the same session do nothing.

### Provisioning (n8n)

- A step engine runs the onboarding in order: sub-account from the plan's snapshot (`PLAN_SNAPSHOTS`), admin user, custom values, contact in the agency sub-account, opportunity in the Onboarding stage, note, the new client tag (last, because it starts the native workflow), Slack summary.
- Every finished step is saved to the ledger with the IDs it created before the next one starts. Creates only go out after a lookup found nothing (sub-account by email and name, user by email, opportunity by contact and pipeline, note by an event marker), so a retry picks up what an earlier run made.
- Custom values are matched by name, only the twelve the workflow owns are written, values that are already right are skipped, and the booking link comes from the snapshot's booking calendar. A missing custom value or a failure while filling them never holds up the welcome email: the run carries on and the alert lists what to set by hand.
- GoHighLevel calls: 429 waits for Retry-After (or `GHL_RATE_LIMIT_WAIT_SECONDS`) and repeats, 5xx and dropped connections back off exponentially, 401 (bad token or missing scope), 403, 400 and 422 stop with the reason. A failed create goes back to a lookup instead of being sent again.
- A run lock (a ledger row taken with one conditional update) keeps two onboardings from running at once.
- Anything left open fails the run through a Stop and Error node. The alert says what exists (with IDs), what is still to do by hand (with the values), and how to continue with the Retry webhook.

### Native GoHighLevel side

- `ghl/native-workflows.md`: two workflows for the agency sub-account, with every trigger, filter, action, setting, message and the Custom Webhook calls back to n8n. Names and options were checked against HighLevel's help center; what it does not state is marked "Not confirmed".
- `ghl/snapshot-checklist.md`: what each client snapshot, the agency sub-account and the agency itself need.
- A status webhook takes "kickoff booked" and "kickoff not booked" from those workflows (shared secret header, or `customData` from the free Webhook action), records it in the ledger and posts to Slack.

### Design decisions made while building

- **The agency side uses an OAuth app, not a Private Integration token.** HighLevel's API reference allows an agency Private Integration token for creating sub-accounts and users, but the new sub-account's custom values and calendars need a sub-account token, and `POST /oauth/locationToken` only accepts an agency token from an OAuth app (the spec's `Agency-Access-Only` scheme). So the agency calls go through n8n's HighLevel OAuth2 credential, which also keeps HighLevel's rotating refresh token. The agency's own sub-account keeps a simple Private Integration token.
- **The workflow waits for the snapshot and for the app.** HighLevel loads a snapshot in the background (its own example shows custom values still pending), and a sub-account token only exists once the app is installed on the sub-account. The workflow polls the snapshot status and the app's installed locations, up to `SETUP_WAIT_SECONDS`, before it asks for the token.
- **A lost create answer goes to a person.** When HighLevel never answered a sub-account create and no sub-account with that email or name can be found, the workflow does not create another one. The alert asks a person to look, and the retry takes `subAccountId` (use the one found) or `createSubAccount: true`. A create that HighLevel refused (4xx) created nothing, so the next try goes ahead by itself.
- **The ledger is a data table, read by ID.** Rows are updated one at a time, and the lock and the stuck run check use conditional updates.
- **The user's password is random and never stored.** HighLevel requires one on create. It is made by n8n's Crypto node, sent once, and is not in the ledger, the note or Slack; the client sets their own with "Forgot password".

### Found and fixed while testing

- **The ledger column `updatedAt` broke the table.** n8n data tables have system columns named `createdAt` and `updatedAt`; creating the table answered `SQLITE_ERROR: duplicate column name: updatedAt` in the first test run. The column is `changedAt` now, and the setup guide warns about the name.

### Found and fixed in a logic review

A pass over the workflow for gaps the first tests did not cover. The stuck run check and the finish note have their own tests; the other fixes run inside existing scenarios.

- **A run cut off by an n8n restart was silent.** Stripe already had its 200, and an execution that a restart cuts off may never reach the error workflow, so the onboarding could sit "in progress" with nobody told. A scheduled check (every 15 minutes) now finds rows with no progress for `STUCK_CHECK_MINUTES` while no run holds the lock, marks them failed and raises one alert.
- **A retry that finished the open items told nobody.** When the first run had already posted its Slack summary with "something to finish", the channel never heard that it was done. Such a retry now posts a short "Onboarding finished" note.
- **A lock take that errored was reported as "ledger down".** n8n runs a conditional data table update as a select and an update in one transaction, so two runs taking the lock at the same moment could, on SQLite, get a busy error instead of an empty answer. That now counts as "not taken" (wait and look again), so a simultaneous duplicate delivery gets its 503. A ledger failure after the lock was taken now releases the lock.
- **Alerts for a create that kept failing said "after 1 tries".** Creates are retried by the step engine after a lookup, so the count lives there; the alert now shows the real number.
- **A refused create left the "may exist" marker behind.** After a 401 or 422 on the sub-account create, a retry would have asked a person although nothing was created. The marker is now cleared when HighLevel refused the create.
