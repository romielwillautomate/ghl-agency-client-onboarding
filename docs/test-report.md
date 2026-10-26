# Test report

**Date:** October 27, 2026 (rerun after a review; first build October 26, 2026)
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 91 of 91 checks pass across 29 scenarios

## How it was tested

No real Stripe account, HighLevel agency or Slack workspace was used. The test script:

1. starts strict, stateful mocks of the HighLevel API v2 (the agency, the client sub-accounts it creates, and the agency's own sub-account) and of a Slack incoming webhook on the test machine, and plays Stripe itself: it builds `checkout.session.completed` and `checkout.session.async_payment_succeeded` events around full Checkout Session objects, serializes them the way Stripe sends them (pretty printed UTF-8; one client's business name has accented letters), signs them (`t=<timestamp>,v1=<HMAC SHA-256 of "timestamp.body">,v0=<fake>`) and posts them,
2. creates a temporary n8n data table for the ledger, imports and activates a temporary copy of the error alert workflow, then a temporary copy of the onboarding workflow that reports to it. In the copy, the HighLevel host points at the mock, settings are test values (short waits: `RETRY_BASE_SECONDS=0.5`, `SETUP_POLL_SECONDS=1`, `SETUP_WAIT_SECONDS=6`, `LOCK_WAIT_SECONDS=3`), the stuck run check runs every minute instead of every 15, and n8n's HighLevel OAuth2 credential is replaced by a fixed bearer token that the mock checks (the mock cannot run HighLevel's OAuth flow),
3. pays checkouts for 21 fictional clients and plays what can go wrong (see the results), continues the unfinished ones through the Retry webhook, and calls the status webhook the way the native GoHighLevel workflows do (Custom Webhook POST with the secret header and form-style key and value pairs, and the standard Webhook action with `customData`),
4. changes ledger rows by hand where a test needs a state that only a crash, a race or a person leaves behind (a run lock held by another run, a lock that ran out, a lock row whose `attempts` was emptied, and an onboarding left "in progress" for 2 hours as if n8n had restarted in the middle), and changes the mock between runs where a person would act (adding a missing custom value, installing the app, giving a user access, restoring a scope),
5. checks every request the workflow made, the mock's state (one sub-account and one user per client), the ledger rows, the Slack text, the HTTP answers Stripe got and how fast, the node each run ended on, and that every failed run raised exactly one alert, matched by the execution link in the alert,
6. imports a copy with broken settings, then deletes every temporary workflow and the data table, also after a crash.

Temporary workflows are named "TEST ghl-agency-client-onboarding (temporary)" and the table "TEST ghl-agency-client-onboarding ledger (temporary)". After the run, no TEST workflow and no test table were left in n8n.

### The mocks

**HighLevel API v2** (base `services.leadconnectorhq.com`) keeps state: sub-accounts (with their snapshot, custom values and calendars), users, snapshots, app installs, and in the agency's own sub-account contacts, opportunities, notes and tags. Like the real API it:

- requires the `Version` header each endpoint documents (`2021-04-15` for calendars, `2021-07-28` for everything else) and the right token for each endpoint: the agency OAuth token for sub-accounts, snapshots, users and the OAuth endpoints; a sub-account token (from `/oauth/locationToken`) for that sub-account's custom values and calendars only; the agency sub-account's Private Integration token for contacts and opportunities,
- refuses body fields and query parameters that are not in HighLevel's OpenAPI files (422 `property X should not exist`), missing required fields (422), a country that is not a 2 letter code, a `prospectInfo` without first name, last name and email, an unknown snapshot, a user `type` or `role` outside the documented values, a password that breaks HighLevel's rules (12 characters, upper and lower case, a number, a special character), a user email that exists already, a second opportunity for the same contact and pipeline, a stage of another pipeline, and a phone that is not in E.164 form,
- answers the documented shapes: the sub-account create with the location itself (200, no wrapper), the user create with 201, `{customValues: [...]}`, `{calendars: [...]}`, `{new, contact, traceId}` for the upsert, `{opportunities, meta, aggregations}` for the search, `{note}` and `{tags}` with 201, `{data: {id, locationId, status, completed, pending}}` for the snapshot status, `{locations: [{_id, isInstalled, ...}], count, installToFutureLocations}` for installed locations, and the token fields of `/oauth/locationToken` (form encoded request),
- answers errors in HighLevel's shapes: 400 `{statusCode, message}`, 401 `{"statusCode": 401, "message": "Invalid JWT"}` for a bad token and `"The token is not authorized for this scope."` for a missing scope (a 401, as HighLevel answers it), 403 `"The token does not have access to this location."`, 422 with `message` as a list,
- loads a snapshot in the background: the first two status calls answer `processing` with `custom_values` pending, and until then the sub-account has no custom values or calendars; it installs the agency app on a new sub-account after one check. For one client the status endpoint has no record of the load (404) and the snapshot finishes after the third look at the custom values,
- can be switched to: a slow sub-account create, a create that saves the sub-account but answers 504, one that saves it under another name and answers 502, a 502 without saving, a 429 with `Retry-After: 1` on the create (three times, nothing saved), a missing `locations.write` scope, 503 on the user create, a 429 with `Retry-After: 1` on one custom value, a note that is saved but answered with 504, and an app that is never installed,
- holds, besides the new clients, an older sub-account of another client whose business has the same name as a new one, a HighLevel user from an earlier project, and a returning client's contact that still has the `new-client` tag.

**Stripe** is played by the test: events in the shape of the Event object (`id`, `object`, `api_version` `2026-09-30.endive`, `created`, `data.object`, `livemode`, `pending_webhooks`, `request`, `type`) around Checkout Session objects with `customer_details` (name, email, phone, address), `custom_fields` (text fields `businessname` and `website`), `metadata.onboarding_plan`, `payment_status`, `amount_total`, `currency`, `payment_intent` and `customer`. The signatures are made with Python's `hmac` as Stripe documents it, independent of the workflow's own code.

**Slack** accepts a JSON body with a non empty `text` (and only the documented fields), answers `ok`, and can be made to answer 500 for one client.

### Checked against the official docs

| Call | Endpoint | What was checked | Source |
|---|---|---|---|
| Stripe signature | `Stripe-Signature` header | `t=` timestamp, one `v1` per active secret (two while a secret is rolled), `v0` only on test events and ignored, signed text `timestamp + "." + raw body`, HMAC SHA-256 with the endpoint secret, constant time comparison, default tolerance 5 minutes, raw body unmodified, 400 on failure in Stripe's examples | docs.stripe.com/webhooks ("Verify manually"); hex digest and the full `whsec_` string as key as in stripe-node's `Webhooks.ts` and `NodeCryptoProvider.ts` |
| Stripe delivery | | Return 2xx quickly before complex logic; redirects, 4xx, 5xx and timeouts are failures; live mode retries for up to 3 days, test mode three times over a few hours; duplicates happen and should be logged by event ID; no order guarantee; Dashboard resend for 15 days, CLI for 30 | docs.stripe.com/webhooks, /events/how-events-work, /cli/events/resend |
| Stripe Event | Event object | `id`, `object`, `api_version`, `created`, `data.object`, `livemode`, `pending_webhooks`, `request`, `type` | docs.stripe.com/api/events/object |
| Checkout Session | Checkout Session object | `payment_status` (`paid`, `unpaid`, `no_payment_required`), `status`, `mode`, `customer_details` (now with `business_name` and `individual_name`), `custom_fields` (up to 3, alphanumeric keys, text value at `text.value`), `metadata`, `amount_total`, `currency`; Payment Link metadata is copied to its sessions; line items are not in webhook payloads | docs.stripe.com/api/checkout/sessions/object, /api/payment-link/create, /metadata, /expand |
| Fulfillment | | Fulfill on `checkout.session.completed` and `checkout.session.async_payment_succeeded`, check `payment_status`, make fulfillment safe to run more than once, even at the same time | docs.stripe.com/checkout/fulfillment |
| Create sub-account | `POST /locations/` | Agency token, `locations.write`, required `name` and `companyId`, `snapshotId` at the top level, `prospectInfo {firstName, lastName, email}`, 2 letter `country`, answer 200 with the location (no `snapshotId` echoed); "This feature is only available on Agency Pro ($497) plan." | HighLevel OpenAPI `apps/locations.json`, marketplace.gohighlevel.com/docs (Create Sub-Account) |
| Search sub-accounts | `GET /locations/search` | `companyId`, `skip`, `limit`, `order` (asc or desc), `email`; answer `{locations: [...]}` without a count | `apps/locations.json` |
| Get sub-account | `GET /locations/{id}` | Agency or sub-account token, answer `{location: {..., companyId}}` | `apps/locations.json` |
| Snapshot status | `GET /snapshots/snapshot-status/{snapshotId}/location/{locationId}?companyId=` | Answer `{data: {id, locationId, status, completed, pending}}`; the example shows `processing` with `custom_values` pending; the endpoint is titled "Get Last Snapshot Push" | `apps/snapshots.json`, marketplace docs (Get latest snapshot push), help center article 155000008264 (load history is kept apart from push history) |
| App installed | `GET /oauth/installedLocations` | Agency OAuth token (not a Private Integration token), `oauth.readonly`, `companyId` and `appId` required, `locationId` and `isInstalled` filters, answer with `_id` (not `id`) | `apps/oauth.json`, GitHub issue 226 in highlevel-api-docs (HighLevel staff) |
| Sub-account token | `POST /oauth/locationToken` | Form encoded `companyId` and `locationId`, `Agency-Access-Only` ("Use the Access Token generated with user type as Agency", so not a Private Integration token, while `Agency-Access` endpoints also take "Private Integration Token of Agency"), `oauth.write`, answer `access_token`, `token_type`, `expires_in`, `locationId` (the marketplace page also lists a `refresh_token`) | `apps/oauth.json` (security schemes), marketplace docs (Get Location Access Token) |
| The agency token | OAuth app with target user Sub-account, installed by the agency | "Only Agency Admin/Owner can install the app" (Agency Only); an agency install gives a token "of type `Company` (Agency-level)" with `isBulkInstallation: true`, exchanged for sub-account tokens with `/oauth/locationToken`; custom values are "Sub-Account" access in the scope list | marketplace docs (Target User: Sub-Account), `apps/oauth.json` (`user_type`, `installToFutureLocations`), `docs/oauth/Scopes.md` |
| Users | `GET /users/search`, `POST /users/` | `companyId` required in the search, answer `{users, count}` with `roles.locationIds`; the create requires `companyId`, `firstName`, `lastName`, `email`, `password`, `type` (`account`), `role` (`admin`), `locationIds`; password rules; answer 201 | `apps/users.json`, marketplace docs (Create User) |
| Custom values | `GET` and `PUT /locations/{id}/customValues[/{id}]` | Sub-account token, `{customValues: [{id, name, fieldKey, value, locationId}]}`, PUT body `{name, value}` (both required), answer `{customValue}` | `apps/locations.json`, `docs/oauth/Scopes.md` |
| Calendars | `GET /calendars/?locationId=` | `Version: 2021-04-15`, sub-account token, answer `{calendars: [{id, name, ...}]}`; the API gives no booking URL | `apps/calendars.json` |
| Contacts | `POST /contacts/upsert`, `GET` and `POST /contacts/{id}/notes`, `POST /contacts/{id}/tags` | Upsert fields (only `locationId` required; `tags` would overwrite all tags, so the workflow never sends them there), the upsert follows the "Allow Duplicate Contact" setting, answer `{new, contact, traceId}` with the contact's `tags`; note `{body}` (201, `{note}`); tags `{tags: [...]}` (201) | `apps/contacts.json`, help center article 48001181714 |
| Opportunities | `GET /opportunities/search`, `POST /opportunities/`, `PUT /opportunities/{id}` | snake_case query (`location_id`, `contact_id`, `pipeline_id`), `status` values; the create requires `pipelineId`, `locationId`, `name`, `status`, `contactId` and answers 201 `{opportunity}`; the update answers 200 | `apps/opportunities.json` |
| HighLevel limits, plan and errors | | 100 requests per 10 seconds and 200,000 per day per app and resource; Agency Pro's "Advanced API access" unlocks agency API keys (lower plans: location API keys only); 429 from the official SDK; error shapes from `common/common-schemas.json` and, for the 401 texts, from HighLevel's GitHub issues | marketplace docs (Rate limits), help.gohighlevel.com article 48001060529, `highlevel-api-sdk` |
| Slack | Incoming webhook POST | JSON with `text`, `ok` on success, `invalid_payload` and `no_text` errors | Slack "Sending messages using incoming webhooks" |
| Native workflow calls | Custom Webhook action | The POST event sends "Body (Key and Value) pairs for a form-style payload"; only the CUSTOM event has a raw body editor | help center article 155000003305 |

### What the official docs leave open

The mock follows these assumptions; each one needs a look on a live account.

| Point | What the docs say | What the workflow and the mock do |
|---|---|---|
| Plan for agency OAuth apps and `/oauth/locationToken` | Creating sub-accounts is Agency Pro only ("$497"); the API article ties agency API keys to Agency Pro's "Advanced API access". Nothing says whether a lower plan can install an agency OAuth app | Documented as an Agency Pro requirement |
| n8n's HighLevel OAuth2 credential at agency level | HighLevel's token request has an optional `user_type` (Company or Location); n8n's credential does not send it. The developer docs say an agency install of a Sub-account app gives a Company token | Assumed to give that Company token; a mock cannot test it |
| The app on sub-accounts created through the API | Agencies can install apps "for future sub-accounts" with "all sub-accounts"; nothing says this covers sub-accounts made through the API, or how fast | The workflow checks `installedLocations` and waits; if the app never shows up, it says so and carries on |
| `/oauth/locationToken` for a sub-account without the app | Not documented | The mock answers 403 `The token does not have access to this location.`; the workflow only asks after the install check passed |
| Snapshot status values | Only `processing` is documented; the example even has `completed` and `pending` as strings instead of lists | Any other status counts as finished, and lists and strings are both read |
| Snapshot status for a sub-account created with a snapshot | The endpoint is documented as the last snapshot push; the help center keeps load history apart from push history. Nothing says whether a load at creation shows up | When it gives no status, the workflow reads the custom values until one of its own appears (tested with a 404) |
| The `email` filter of `GET /locations/search`, and the location's email | No description; the create has no `email` field, only `prospectInfo.email` | The mock matches the location's email exactly (ignoring case) and sets it from `prospectInfo.email`. The workflow adopts a sub-account only when name and email both match, also among the newest 100; one that only shares the name goes to a person. If HighLevel leaves the location's email empty, a lost create answer always ends with a person picking the sub-account by ID |
| A user create with an email that exists | Not documented | The mock answers 400; the workflow looks the user up first, so it never sends one |
| The 429 body and `Retry-After` from HighLevel | 429 is confirmed by the SDK; the body and headers are not documented | The workflow waits for `Retry-After` when present, else `GHL_RATE_LIMIT_WAIT_SECONDS` |
| `timezone` on the sub-account create | Free text, the example is `US/Central` | The workflow sends an IANA name such as `America/Chicago` |
| The booking link of a calendar | The API returns no URL; the help center does not give the format | `https://api.leadconnectorhq.com/widget/booking/<calendar ID>`, as HighLevel's WordPress plugin and live calendars use it; `GHL_BOOKING_BASE_URL` changes it |
| Whether HighLevel sends its own invite when a user is created through the API | Not documented | The welcome email tells the client to use "Forgot password" |
| Whether the upsert answer lists the contact's existing tags | The schema of the answer's `contact` has `tags` | A returning client's `new-client` tag is read from it; if it were missing there, the tag call would go out and the native workflow would still not start (re-entry off) |
| A thin Stripe event for a v1 resource | Stripe made thin events available for v1 resources; their exact type names were not in the pages read | Treated as a `v2.core.event` with a `v1.` prefix on the type; answered 200 with an alert to switch to snapshot payloads |
| Stripe's webhook timeout | No number is documented; Checkout waits up to 10 seconds before redirecting when it waits for the webhook | Stripe gets its 200 right after the ledger write (under half a second in every test run); the lock wait is capped at 9 seconds |

The native GoHighLevel workflows were not tested by this script: they run inside HighLevel. Their recipe in [ghl/native-workflows.md](../ghl/native-workflows.md) marks every option the help center does not confirm. The test does play their calls to the status webhook.

## Results

```
Stripe signature
  PASS  a delivery signed with another secret is refused with 400, nothing is called, the run fails for an alert  (400, ended at Stop: Bad Signature)
  PASS  a call without a Stripe-Signature header is refused with 400  (400, ended at Stop: Bad Signature)
  PASS  a correctly signed delivery 10 minutes old (a replay) is refused: outside the 300 second tolerance  (400, Refused a call to the Stripe webhook: the signed timestamp is 601 seconds old, o)
  PASS  a body changed after signing (one field) is refused: the HMAC covers the raw bytes  (400, ended at Stop: Bad Signature)

Unused Stripe events
  PASS  payment_intent.succeeded gets 200 at once (so Stripe does not send it again) and nothing else happens  (200 {'received': True, 'ignored': 'event type payment_intent.succeeded is not used'})
  PASS  a checkout without onboarding_plan metadata (another product) gets 200 and is left alone  ({'received': True, 'ignored': 'not an onboarding purchase (no onboarding_plan metadata)'})
  PASS  a thin event (no session in it) gets 200, nobody is onboarded, and the alert says to switch the destination to snapshot payloads  (200, ended at Stop: Event Without Session)

Delayed payment
  PASS  checkout.session.completed with payment_status unpaid gets 200 and waits; nothing is created  ({'received': True, 'ignored': 'payment not finished yet; waiting for checkout.session.async_payment_succeeded'})
  PASS  checkout.session.async_payment_succeeded onboards the client: one sub-account, one user, ledger done  (ended at Run Failed?)

Onboarding (Launch plan)
  PASS  Stripe gets 200 {"received": true} as soon as the event is in the ledger, before the sub-account create has finished  (answered in 0.16 s)
  PASS  the calls in order: look up, create the sub-account, look up and create the user, wait for the snapshot and the app, sub-account token, custom values, calendars, custom value updates, contact, opportunity, note, tag, Slack  (findLocation, createLocation, findUser, createUser, snapshotStatus x3, appInstalled x2, locationToken, listCustomValues, listCalendars, updateCustomValue x12, upsertContact, findOpportunity, createOpportunity, findNotes, addNote, addTag, slack)
  PASS  the sub-account is created from the Launch snapshot with the checkout details (name, address, phone, website, timezone, prospect)  ({"name": "Lumi\u00e8re Caf\u00e9", "companyId": "brightlineAgency01", "snapshotId": "snapLaunch01", "timezone": "America/Chicago", "phone": "+15550100141", "address": "1200 Market Street, Suite 4", "c)
  PASS  before creating, the agency sub-accounts are searched by the client email (agency OAuth token, Version 2021-07-28)  ({"companyId": "brightlineAgency01", "email": "jordan.ellis@lumierecafe.example.com", "limit": "100"})
  PASS  the admin user: type account, role admin, only the new sub-account, a random password that meets the rules  ({"companyId": "brightlineAgency01", "firstName": "Jordan", "lastName": "Ellis", "email": "jordan.ellis@lumierecafe.example.com", "type": "account", "role": "admin", "locationIds": ["loc0026"], "phone")
  PASS  the password is never kept: not in the ledger, not in Slack, not in the note
  PASS  it waits for the snapshot (processing twice, then completed) and for the agency app on the new sub-account before asking for its token  ([{"companyId": "brightlineAgency01", "appId": "appOnboarding01", "locationId": "loc0026", "isInstalled": "true"}])
  PASS  the sub-account token: POST /oauth/locationToken, form encoded companyId and locationId
  PASS  calendars are read with the sub-account token and Version 2021-04-15 to find the Discovery Call calendar
  PASS  custom values are matched by name and filled: business name, phone, email, address, city, state, ZIP, website, owner, booking link  ({})
  PASS  values that are already right (Business Country = US) are not written, and custom values the workflow does not own stay as the snapshot set them  (12 PUT calls)
  PASS  GoHighLevel 429: a custom value update that gets 429 waits for Retry-After (1 s) and succeeds on the second try  ([429, 200])
  PASS  the client is added to the agency's own sub-account with GHL_HOME_TOKEN: name, email, phone, company, address, website, source  ({"locationId": "brightlineHome01", "firstName": "Jordan", "lastName": "Ellis", "name": "Jordan Ellis", "email": "jordan.ellis@lumierecafe.example.com", "companyName": "Lumi\u00e8re Caf\u00e9", "source)
  PASS  an opportunity in the Onboarding stage with the amount paid (1500) and a clear name  ([{"id": "opp0045", "name": "Lumi\u00e8re Caf\u00e9: Launch onboarding", "monetaryValue": 1500, "pipelineId": "pipeClients01", "pipelineStageId": "stageOnboarding01", "status": "open", "contactId": "ct)
  PASS  a note with the plan, the amount, the sub-account link, the admin user and the event marker  (Onboarding started from a Stripe checkout [onboarding evt_testacd105877022a4392]
Plan: Launch (USD 1,500.00), Stripe ses)
  PASS  the new-client tag that starts the native workflow is added last, after the opportunity and the note exist
  PASS  Slack summary with the plan, the amount, links to the sub-account and the contact, and what happens next  (:tada: *New client onboarded*: Lumière Café (Launch plan, USD 1,500.00))
  PASS  the ledger row: done, every step recorded with the IDs it created, one attempt  (done)
  PASS  the run took the lock and released it at the end  ({"attempts": 0, "note": "free"})
  PASS  the run ends as a success (no alert)  (ended at Run Failed?)

Duplicate delivery
  PASS  the same event again (Stripe can send one twice) gets 200 with duplicate: true; nothing is called  ({'received': True, 'duplicate': True})
  PASS  another event for a session that is already onboarded is a duplicate too (one onboarding per Checkout Session)  ({'received': True, 'duplicate': True})

Two deliveries at once
  PASS  one onboards, the other waits LOCK_WAIT_SECONDS for the lock and gets 503 (Stripe sends it again later): one sub-account, one user  (codes [200, 503])
  PASS  Stripe's later resend of the refused delivery is answered as a duplicate  ({'received': True, 'duplicate': True})

Run lock
  PASS  a lock row that can never be taken (attempts emptied by hand) ends after about LOCK_WAIT_SECONDS + 10 seconds with 500 and an alert instead of looping  (500 after 13.7 s, ended at Stop: Ledger Unavailable)
  PASS  while another run holds the lock, a delivery waits about LOCK_WAIT_SECONDS, then gets 503; nothing is called or recorded  (503 after 3.2 s)
  PASS  a lock that ran out does not block: Stripe's resend onboards the client  (done)

Partial failure
  PASS  the user create gets 503 three times; after each, the user is looked up again before another create; the run stops there  (findLocation, createLocation, findUser, createUser, findUser, createUser, findUser, createUser)
  PASS  the ledger keeps the finished step and its ID (sub-account) and marks the event failed; nothing in the agency CRM yet  (failed)
  PASS  the alert says exactly what exists (with IDs) and what is left to do by hand, with the values, and how to retry  (Onboarding of Dune &amp; Dawn Yoga &lt;b&gt; (alex.moreno@duneanddawn.example.com, Launch plan, Stripe event evt_testd65b423eeaf634d8a) is not finished. It stop)

Retry
  PASS  the Retry webhook answers 202 with the step it continues from  ({'started': True, 'continuesWith': 'create the admin user'})
  PASS  the retry continues where the run stopped: no second sub-account, the user is created, then the rest; ledger done after 2 attempts  (findUser, createUser, snapshotStatus x3, appInstalled x2, locationToken, listCustomValues, listCalendars, updateCustomValue x10, upsertContact, findOpportunity,)

Lost answer: sub-account
  PASS  the create saved the sub-account but answered 504; the workflow looks it up by email, finds it and goes on: still one sub-account  (findLocation, createLocation, findLocation, findUser, createUser)

Lost answer: sub-account not found
  PASS  a 502 after the create, and neither the email search nor the newest sub-accounts find it: the workflow does not create again and asks a person  (Onboarding of Foxglove Florist (morgan.blake@foxglove.example.com, Launch plan, Stripe event evt_testf7084e9d4d67349ac) is not finished. It )
  PASS  a plain retry still does not create one (it may exist), and asks again  (ended at Stop: Onboarding Not Finished)
  PASS  a retry with the sub-account ID a person found uses it (checked to belong to the agency) and finishes: no second sub-account  (getLocation, findUser, createUser, snapshotStatus x3, appInstalled x2, locationToken, listCustomValues, listCalendars, u)
  PASS  a retry with createSubAccount: true (the person checked) looks once more, then creates it: one sub-account, done  (findLocation, listLocations, createLocation, findUser, createUser, snapshotStatus x3, appInstalled x2, locationToken, li)

Missing scope or plan
  PASS  a 401 "not authorized for this scope" on the create is not retried; the alert names the scope and the Agency Pro plan; nothing exists  (Onboarding of Harborview Pet Clinic (jamie.lee@harborviewpets.example.com, Launch plan, Stripe event evt_testh3f99ab8c53924c09) is not finished. It st)
  PASS  after the scope is added, the retry creates the sub-account without asking a person (the refused create made nothing)  (done)

Snapshot without a custom value
  PASS  the other 11 values are filled, the missing one does not hold up the welcome: contact, opportunity, note and tag still happen
  PASS  Slack is told what to finish, and the run fails with the value to create by hand (with the booking link)  (Onboarding of Ironwood Auto Repair (chris.patel@ironwoodauto.example.com, Scale plan, Stripe event evt_testia7a27dba40744998) is not finished. Open: t)
  PASS  once the custom value exists, the retry only does the custom values: the booking link is filled, nothing else is called, ledger done  (snapshotStatus, appInstalled, locationToken, listCustomValues, listCalendars, updateCustomValue, slack)
  PASS  Slack, which heard about the open item, is told once that the onboarding is finished now  (:white_check_mark: *Onboarding finished*: Ironwood Auto Repair (Scale plan). The items the)

Agency app not on the sub-account
  PASS  the workflow checks for SETUP_WAIT_SECONDS, never asks for a token it cannot get, and goes on with the agency CRM steps  (5 checks)
  PASS  after the app is installed, the retry fills the custom values and finishes

Existing user
  PASS  a HighLevel user with that email exists without access to the new sub-account: no second user, the alert says what to do  (Onboarding of Kestrel Bakery (pat.owner@kestrelbakery.example.com, Launch plan, Stripe event evt_testkee5578bc42db442c) is not finished. It )
  PASS  after a person gives that user access, the retry picks the user up as it is and finishes

Returning client
  PASS  the existing contact is updated (no duplicate) and the old lost opportunity is reopened in the Onboarding stage instead of a second one  ({"pipelineStageId": "stageOnboarding01", "status": "open"})
  PASS  the contact already has the new-client tag from the first onboarding, so the native workflow (re-entry off) cannot start again: no tag call, and Slack says to send the welcome by hand  (To finish: Welcome: the contact already had the tag new-client from an earlier onboarding, so the onboarding w)

Lost answer: note
  PASS  the note was saved but answered 504; the notes are read again, the note is found by its marker and not added twice  (createOpportunity, findNotes, addNote, findNotes, addTag, slack)

Snapshot status not available
  PASS  the snapshot status has no record for the new sub-account (HighLevel documents it for pushes): the custom values are read again until the snapshot has loaded them, then all are filled  (findLocation, createLocation, findUser, createUser, snapshotStatus, appInstalled x2, locationToken, listCustomValues x3, listCalendars, updateCustomVa)

Lost answer: a namesake
  PASS  the create answered 502 and made nothing; another client's older sub-account has the same name: it is not taken, no user or custom value goes into it, a person is asked with its ID  (Onboarding of Sterling Bookkeeping (rowan.tate@sterlingbooks.example.com, Launch plan, Stripe event evt_testsac118f22aab14170) is not finished. It stopped at "c)
  PASS  after a person checked, the retry with createSubAccount: true creates the client's own sub-account; the namesake keeps its custom values and gets no user and no token request  (findLocation, listLocations, createLocation, findUser, createUser, snapshotStatus x3, appInstalled x2, locationToken, li)

Rate limit on the create
  PASS  the create gets 429 three times (waiting for Retry-After each time): nothing was created, so the alert names the rate limit and asks nobody to look for a sub-account  (Onboarding of Tidewater Kayak Tours (noel.rivera@tidewaterkayak.example.com, Launch plan, Stripe event evt_testt6f5dc00dff1f4d85) is not finished. It )
  PASS  a plain retry creates the sub-account without asking a person: one sub-account, done  (findLocation, createLocation, findUser, createUser, snapshotStatus x3, appInstalled x2, locationToken, listCustomValues,)

Lost answer after "create it"
  PASS  createSubAccount: true covers one create: when that create loses its answer too (502, saved where the search cannot find it), nothing is created again and a person is asked  (Onboarding of Upland Pottery Studio (blair.moreau@uplandpottery.example.com, Launch plan, Stripe event evt_testu0cc5baeda26947dd) is not fin)
  PASS  the retry with the ID the person found finishes it: still one sub-account  (done)

Slack down
  PASS  the summary is tried 3 times; everything else is done; the run fails with "tell the team in Slack" as the only open item  (Still to do: tell the team in Slack. To let the workflow fin)
  PASS  the retry only posts the summary; the business name is escaped, so <!here> cannot ping the channel  (['slack'])

Needs a person
  PASS  a paid checkout with an unknown plan and no business name: 200 to Stripe, recorded as needs_review, nothing created, an alert with both problems  (A paid checkout needs a person before onboarding can start (Stripe event evt_testo6c6de3d594b64d99, no business name, le)
  PASS  a retry before the problem is fixed answers 422 with the problems and creates nothing  (422)

Stuck run check
  PASS  a row left in progress for 2 hours (n8n restarted mid-run) is found by the scheduled check, marked failed and reported once  (1 onboarding stopped without finishing and without an alert, most likely because n8n restarted in the middle of the run:)
  PASS  the retry then runs the whole onboarding from the ledger data (the Stripe event is not needed again)  ({'started': True, 'continuesWith': 'create the sub-account from the snapshot'})

Retry webhook
  PASS  a wrong X-Run-Secret is refused with 401, nothing is read, the run fails for an alert  (401)
  PASS  an event ID that is not in the ledger gets 404  (404)
  PASS  a finished onboarding gets 409 and nothing is called  (409)
  PASS  a body without a Stripe event ID gets 400  (400)

Status webhook
  PASS  a call with a wrong X-Onboarding-Secret is refused with 401 and an alert; nothing changes  (401)
  PASS  kickoff booked (Custom Webhook POST: the secret header and form-style key and value pairs): 200, the ledger row gets the kickoff, Slack gets a note  (:calendar: *Kickoff call booked*: Lumière Café for Tuesday, November 3, 2026 10:00 AM.)
  PASS  kickoff not booked, sent by the standard Webhook action (secret and event in customData): 200 and a Slack reminder  (:hourglass_flowing_sand: *Kickoff call not booked yet*: Copperline Electric (dana.ruiz@cop)
  PASS  a contact that is not in the ledger (onboarded by hand) still gets its Slack note, marked as such  (:calendar: *Kickoff call booked*: Walk-in Client Co. (not in the onboarding ledger, so it was probab)
  PASS  a call without a known event is refused with 400 and an alert (the Custom Webhook body is wrong)  (400)

Whole run
  PASS  every client has exactly one sub-account (F and U: the one a person found), one admin user and one opportunity in the pipeline  (23 sub-accounts in the mock)
  PASS  the ledger: done for every onboarded client, needs_review for the unknown plan, no other rows (ignored events leave none)  ({"b04dcd": "done", "2a4392": "done", "284fc6": "done", "114b3e": "done", "634d8a": "done", "734935": "done", "7349ac": "done", "184e06": "done", "924c09": "done", "744998": "done", "0f4b91": "done", "db442c": "done", "b048df": "done", "d349ad": "done", "a24895": "done", "b14170": "done", "1f4d85": "done", "6947dd": "done", "e84473": "done", "b64d99": "needs_review", "2743f3": "done"})
  PASS  no request was refused by the mock as malformed (no 400 or 422 from GoHighLevel)  ([])

Error alert workflow
  PASS  every failed run raised exactly one Slack alert with its execution link, and no other alerts  (25 failed runs, 25 alerts)
  PASS  the alert names the node and carries the whole message, escaped for Slack  (Onboarding of Dune &amp; Dawn Yoga &lt;b&gt; (alex.moreno@duneanddawn.example.com, Launch )

Broken settings
  PASS  a Stripe delivery gets a plain 500 (Stripe sends it again after the fix), nothing is called, the run fails  (500, ended at Stop: Bad Settings)
  PASS  the alert names every problem  (Settings are not valid, so nothing was done. GHL_APP_ID must be the ID of your HighLevel agency app )
  PASS  the Retry webhook answers 500 with the list of problems  ({"started": false, "error": "settings", "problems": ["GHL_APP_ID must be the ID of your HighLevel agency app (Marketplace developer portal > your app)", "STRIPE_WEBHOOK_SECRET must be the endpoint's s)
  PASS  a status call gets a plain 500  (500)
  PASS  each of the three refused calls raised one alert  (3 failed runs)

91/91 checks passed
```

## What the workflow sent

Requests from the Launch plan onboarding (Lumière Café), as the mock received them. Paths are relative to `https://services.leadconnectorhq.com`.

**Look up first** (`GET /locations/search`, agency token, `Version: 2021-07-28`): `{"companyId": "brightlineAgency01", "email": "jordan.ellis@lumierecafe.example.com", "limit": "100"}`

**Create the sub-account** (`POST /locations/`, agency token):

```json
{"name": "Lumière Café", "companyId": "brightlineAgency01", "snapshotId": "snapLaunch01", "timezone": "America/Chicago", "phone": "+15550100141", "address": "1200 Market Street, Suite 4", "city": "Springfield", "state": "IL", "postalCode": "62701", "country": "US", "website": "https://lumierecafe.example.com", "prospectInfo": {"firstName": "Jordan", "lastName": "Ellis", "email": "jordan.ellis@lumierecafe.example.com"}}
```

**Look up and create the admin user** (`GET /users/search` with `{"companyId": "brightlineAgency01", "query": "jordan.ellis@lumierecafe.example.com", "limit": "25"}`, then `POST /users/`):

```json
{"companyId": "brightlineAgency01", "firstName": "Jordan", "lastName": "Ellis", "email": "jordan.ellis@lumierecafe.example.com", "type": "account", "role": "admin", "locationIds": ["loc0026"], "phone": "+15550100141", "password": "(random, 32 characters, never stored)"}
```

**Wait for the snapshot and the app** (agency token): `GET /snapshots/snapshot-status/snapLaunch01/location/loc0026` with `{"companyId": "brightlineAgency01"}`, then `GET /oauth/installedLocations` with `{"companyId": "brightlineAgency01", "appId": "appOnboarding01", "locationId": "loc0026", "isInstalled": "true"}`

**Token for the new sub-account** (`POST /oauth/locationToken`, form encoded): `{"companyId": "brightlineAgency01", "locationId": "loc0026"}`

**One custom value** (`PUT /locations/loc0026/customValues/cv0038`, with that sub-account's token):

```json
{"name": "Booking Link", "value": "https://api.leadconnectorhq.com/widget/booking/cal0041"}
```

**The client in the agency sub-account** (`POST /contacts/upsert`, `GHL_HOME_TOKEN`):

```json
{"locationId": "brightlineHome01", "firstName": "Jordan", "lastName": "Ellis", "name": "Jordan Ellis", "email": "jordan.ellis@lumierecafe.example.com", "companyName": "Lumière Café", "source": "Stripe onboarding checkout", "phone": "+15550100141", "address1": "1200 Market Street, Suite 4", "city": "Springfield", "state": "IL", "postalCode": "62701", "country": "US", "website": "https://lumierecafe.example.com"}
```

**The opportunity** (`GET /opportunities/search` with `{"location_id": "brightlineHome01", "contact_id": "ctc0044", "pipeline_id": "pipeClients01", "status": "all", "limit": "20"}` first, then `POST /opportunities/`):

```json
{"pipelineId": "pipeClients01", "locationId": "brightlineHome01", "name": "Lumière Café: Launch onboarding", "pipelineStageId": "stageOnboarding01", "status": "open", "contactId": "ctc0044", "monetaryValue": 1500}
```

**The tag** (`POST /contacts/<id>/tags`, last): `{"tags": ["new-client"]}`

The Slack summary and the alerts are shown in the README, "What people see".

## What the test does not cover

- Real Stripe, HighLevel and Slack accounts. Every request shape follows the official docs, but no request reached a real service. In particular the HighLevel OAuth flow (the agency token, its refresh, the app install on new sub-accounts) and the plan gate are not exercised: the mock takes a fixed token.
- The native GoHighLevel workflows themselves: whether the Contact Tag trigger fires for a tag added through the API, the welcome email, the tasks, the wait and the branches. Their calls to n8n are played by the test.
- Load. The test pays for 21 clients one after another (two at the same moment once). HighLevel's rate limits at volume were not tested beyond a 429 on one custom value and three on one create.
- Real races and crashes. The run lock, a lock left by a crashed run and an onboarding cut off by a restart were tested by putting the ledger into the state they leave, plus two deliveries sent at the same moment; n8n was not stopped in the middle of a run.

## Findings

Issues found and fixed while building and testing are listed in [CHANGELOG.md](../CHANGELOG.md). All names, companies, emails, phone numbers and IDs above are made up.
