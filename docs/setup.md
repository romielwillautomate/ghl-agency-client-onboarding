# Setup: Stripe, GoHighLevel, Slack and n8n

You need self-hosted n8n 2.x (it has data tables) reachable over HTTPS, because Stripe and HighLevel call it. You also need a HighLevel agency on the **Agency Pro** plan, the snapshots you onboard clients with, a Stripe account and a Slack channel. Plan about an hour and a half, most of it for the HighLevel app and the two native workflows.

About the plan: HighLevel's API reference for creating a sub-account (`POST /locations/`) says "This feature is only available on Agency Pro ($497) plan.", and its help center article on API access says Agency Pro's "Advanced API access" unlocks agency API keys, where Starter and Unlimited only get location API keys. On a lower plan the first step (the sub-account create) is refused, nothing is created, and the alert names the plan.

Try it end to end in Stripe's test mode first, with a snapshot you do not mind seeing sub-accounts made from. Every test onboarding creates a real sub-account and a real user in your agency, so delete those afterwards.

## 1. HighLevel agency app (the agency credential)

The agency side (creating sub-accounts and users, reading snapshot status, getting a token for a new sub-account) needs an agency level OAuth token. Why not an agency Private Integration token, which is simpler to make? HighLevel's API reference (the OpenAPI files on GitHub) sorts the endpoints this workflow uses into three groups:

| Endpoints | Token HighLevel accepts |
|---|---|
| `POST /locations/`, `GET /locations/search`, `POST /users/`, `GET /users/search`, the snapshot status | "the Access Token generated with user type as Agency (OR) Private Integration Token of Agency" |
| `POST /oauth/locationToken`, `GET /oauth/installedLocations` | "the Access Token generated with user type as Agency" only, so no Private Integration token |
| The new sub-account's custom values and calendars | A sub-account token (the scope list marks them "Sub-Account" access) |

So an agency Private Integration token could create the sub-account and the user, but it could not get a token for the new sub-account, and the custom values would stay empty. (A sub-account Private Integration token would have to be made by hand in every new sub-account, which defeats the purpose.) A private Marketplace app solves it: when an agency admin installs a Sub-account app for the agency, HighLevel's developer docs say "The Access Token generated here will be of type `Company` (Agency-level)", and that token may call `/oauth/locationToken` for each sub-account the app is installed on.

1. Sign in at [marketplace.gohighlevel.com](https://marketplace.gohighlevel.com) with your agency login and create an app (**My Apps > Create App**). Type **Private** (not listed in the Marketplace, live at once).
2. Distribution: target user **Sub-account**, who can install **Agency Only**, bulk installation on (HighLevel sets new apps to bulk installation and it cannot be turned off).
3. Scopes:

   | Scope | Used for |
   |---|---|
   | `locations.readonly` | Looking up sub-accounts before creating one |
   | `locations.write` | Creating the sub-account |
   | `snapshots.readonly` | Snapshot load status of the new sub-account, and listing your snapshots |
   | `users.readonly` | Looking up the client's user before creating one |
   | `users.write` | Creating the admin user |
   | `oauth.readonly` | Checking that the app is installed on the new sub-account |
   | `oauth.write` | Getting a token for the new sub-account (`POST /oauth/locationToken`) |
   | `locations/customValues.readonly` | Reading the new sub-account's custom values |
   | `locations/customValues.write` | Filling them |
   | `calendars.readonly` | Finding the booking calendar for the `Booking Link` value |

4. Under **Auth**, add the redirect URL n8n shows in the next step (`https://YOUR-N8N-DOMAIN/rest/oauth2-credential/callback`). Copy the **Client ID**, the **Client Secret** and the app's ID (`GHL_APP_ID`; it is in the app's address bar and settings in the developer portal).
5. In n8n: **Credentials > Create credential > HighLevel OAuth2 API**. Name it `HighLevel agency (OAuth2)`. Paste the client ID and secret, put the scopes from the table in **Scope** (separated by spaces) and press **Connect**. On HighLevel's page, choose your **agency** and, when it asks which sub-accounts, **all sub-accounts**, so new sub-accounts get the app as well.

n8n refreshes the access token (it lasts about a day) and stores HighLevel's new refresh token each time (HighLevel issues a new one on every refresh and the old one stops working).

**Not confirmed against a live account:** HighLevel's docs say an agency install of a Sub-account app gives a Company token, but nobody has checked that n8n's built-in HighLevel credential (which does not send HighLevel's optional `user_type` field) receives that Company token, or that HighLevel installs the app on sub-accounts created through the API. The workflow checks the second point before it asks for a sub-account token; if the app is missing, the run says so and finishes everything else. If the first point fails, HighLevel refuses the agency calls with 401 and the alert names the agency credential.

## 2. Your agency sub-account

This is the sub-account where your own clients are contacts. Follow section 2 of [ghl/snapshot-checklist.md](../ghl/snapshot-checklist.md) (pipeline, kickoff calendar, custom values, tags), then:

1. **Private Integration token.** **Settings > Private Integrations > Create new integration** with the scopes `contacts.readonly` (reading notes), `contacts.write` (contact, note, tag), `opportunities.readonly` and `opportunities.write`. Copy the token (it starts with `pit-`) into `GHL_HOME_TOKEN`.
2. **IDs.** `GHL_HOME_LOCATION_ID` is in **Settings > Business Profile**. The pipeline and stage IDs come from one call with that token:

   ```bash
   curl "https://services.leadconnectorhq.com/opportunities/pipelines?locationId=YOUR_LOCATION_ID" \
     -H "Authorization: Bearer pit-your-token" -H "Version: 2021-07-28"
   ```

   Use the pipeline's `id` for `GHL_ONBOARDING_PIPELINE_ID` and the id of its `Onboarding` stage for `GHL_ONBOARDING_STAGE_ID`.
3. **Your agency's company ID** (`GHL_COMPANY_ID`) is in the same sub-account's details:

   ```bash
   curl "https://services.leadconnectorhq.com/locations/YOUR_LOCATION_ID" \
     -H "Authorization: Bearer pit-your-token" -H "Version: 2021-07-28"
   ```

   Copy `location.companyId`. (If your token lacks `locations.readonly` for this one call, add it for the setup and remove it afterwards.)
4. **Duplicate contacts off.** **Settings > Business Profile > Contact Deduplication Preferences**: leave **Allow Duplicate Contact** off. HighLevel's API reference says the contact upsert follows this setting; with duplicates allowed, a returning client would get a second contact.
5. Build the two native workflows from [ghl/native-workflows.md](../ghl/native-workflows.md). Their Custom Webhook actions post to `https://YOUR-N8N-DOMAIN/webhook/ghl-onboarding-status` with the header `X-Onboarding-Secret` set to your `GHL_STATUS_SECRET`.

## 3. Snapshots

Check every client snapshot against section 1 of [ghl/snapshot-checklist.md](../ghl/snapshot-checklist.md): the twelve custom values by name, and a calendar named like `CLIENT_BOOKING_CALENDAR`.

To find the snapshot IDs, make a temporary n8n workflow with one **HTTP Request** node: method GET, URL `https://services.leadconnectorhq.com/snapshots/`, authentication **Predefined credential type > HighLevel OAuth2 API** (your agency credential), query parameter `companyId` = your company ID, header `Version` = `2021-07-28`. Run it once; the answer lists `snapshots` with `id`, `name` and `type`. Put them in `PLAN_SNAPSHOTS`:

```
PLAN_SNAPSHOTS={"launch": {"snapshotId": "SNAPSHOT_ID_1", "label": "Launch"}, "scale": {"snapshotId": "SNAPSHOT_ID_2", "label": "Scale"}}
```

The keys (`launch`, `scale`) are what Stripe sends in the plan metadata. The label is what Slack, the note and the opportunity name show.

## 4. Stripe

1. **The plan in the payment.** For each onboarding plan, create a Payment Link (or your own Checkout Session) and add the metadata `onboarding_plan` = `launch` (or `scale`). Stripe copies a Payment Link's metadata onto every Checkout Session it creates. Payments without that metadata are left alone, so other products can share the webhook.
2. **What Checkout collects.** Turn on phone number collection and require the billing address. Add a custom **text** field with the key `businessname` (label "Business name"), and optionally a text field with the key `website`. Checkout allows up to 3 custom fields; keys are letters and digits. (Checkout's own business name collection works too; the custom field wins when both are there.)
3. **The webhook endpoint.** In Workbench open the **Webhooks** tab and click **Create an event destination**. Choose **Your account**, payload **Snapshot** (Stripe suggests Thin, but a thin event has no Checkout Session in it and the workflow alerts if it gets one) and the API version, then the events `checkout.session.completed` and `checkout.session.async_payment_succeeded`. Click **Continue**, choose **Webhook endpoint**, click **Continue** and enter the endpoint URL `https://YOUR-N8N-DOMAIN/webhook/ghl-onboarding-stripe`. On the destination's page, click **Reveal secret** and copy the signing secret (`whsec_...`) into `STRIPE_WEBHOOK_SECRET`.
4. Delayed payment methods (ACH and bank transfers) first send `checkout.session.completed` with `payment_status: unpaid`. The workflow answers it with 200 and waits for `checkout.session.async_payment_succeeded`.

When you roll the signing secret, Stripe signs with both secrets for up to 24 hours. The workflow accepts any `v1` signature in the header, so put the new secret in n8n during that window.

## 5. Slack

Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) (**From scratch**), switch on **Incoming Webhooks**, **Add New Webhook to Workspace** for your onboarding channel and copy the URL into `SLACK_WEBHOOK_URL`. The error alert workflow posts to the same URL.

## 6. n8n

1. **The ledger table.** **Overview > Data tables > Create data table** (any name, for example `onboarding_ledger`) with these columns, all of type String except `attempts` (Number):

   `key`, `sessionId`, `status`, `plan`, `business`, `email`, `locationId`, `userId`, `contactId`, `opportunityId`, `steps`, `attempts`, `note`, `data`, `kickoff`, `changedAt`

   Copy the table's ID from its address bar (the part after `/datatables/`) into `LEDGER_TABLE_ID`. Do not name a column `updatedAt`: n8n has a system column with that name. The workflow adds one row with the key `__run_lock__` (the run lock); leave it there.
2. **Environment.** Copy [.env.example](../.env.example) into your n8n environment, fill it in and restart n8n. The nodes read the settings with `$env`, so self-hosted n8n needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`. (On n8n Cloud, replace `$env.` with `$vars.` and create the same names under Variables.)
3. Import `workflow/error-alert-slack.json` and **activate it** (n8n 2.x only runs an active error workflow).
4. Import `workflow/ghl-agency-client-onboarding.json`. Open each node with a credential warning and pick `HighLevel agency (OAuth2)`: GHL: Find Sub-Account, GHL: List Sub-Accounts, GHL: Get Sub-Account, GHL: Create Sub-Account, GHL: Snapshot Status, GHL: App Installed?, GHL: Find User, GHL: Create User, GHL: Location Token.
5. In the workflow settings, choose the error alert workflow as its **Error Workflow**, then activate it.

The workflow listens on three production URLs:

| URL | Called by |
|---|---|
| `https://YOUR-N8N-DOMAIN/webhook/ghl-onboarding-stripe` | Stripe |
| `https://YOUR-N8N-DOMAIN/webhook/ghl-onboarding-retry` | You, to finish an onboarding that stopped |
| `https://YOUR-N8N-DOMAIN/webhook/ghl-onboarding-status` | The native GoHighLevel workflows |

## 7. First run

1. In Stripe test mode, open your Launch plan's Payment Link and pay with the test card `4242 4242 4242 4242`, a business name like `Test Client Co` and an email address of yours that is **not** a HighLevel user yet (not your own agency login): the workflow adds that email as a user of the new sub-account, and an email that already belongs to a HighLevel user stops the run with a request to give that user access.
2. Within a minute or two Slack shows "New client onboarded" with links. Check in HighLevel: the new sub-account (from the Launch snapshot) with its custom values filled, the admin user under the sub-account's team, and in your agency sub-account the contact with the opportunity in `Onboarding`, the note and the `new-client` tag.
3. The welcome email arrives with the kickoff booking link and your account manager has four tasks. Book the kickoff from the email: Slack shows "Kickoff call booked".
4. In Stripe, open the event and press **Resend**: the workflow answers 200 with `duplicate: true` and creates nothing.

## Finishing an onboarding that stopped

When a step keeps failing, the run stops and the Slack alert lists what exists (with IDs) and what is still to do. Fix the cause, then:

```bash
curl -X POST "https://YOUR-N8N-DOMAIN/webhook/ghl-onboarding-retry" \
  -H "X-Run-Secret: YOUR_RETRY_SECRET" -H "Content-Type: application/json" \
  -d '{"eventId": "evt_..."}'
```

It answers `{"started": true, "continuesWith": "<the first open step>"}` and runs from there, skipping every step the ledger has as done. Resending the event from the Stripe Dashboard (possible for 15 days) does the same.

One case asks for your decision: when HighLevel never answered a sub-account create, the workflow looks for the sub-account by the client's email and among your newest sub-accounts. It only uses one with the client's name **and** email. If it finds nothing for sure, it does not create another one by itself, because the first create may still have worked; and a sub-account that only shares the name is named in the alert but never used, because it may belong to another client with the same business name. Look in **Agency view > Sub-Accounts**, then either send `{"eventId": "evt_...", "subAccountId": "<its ID>"}` to use the one you found, or `{"eventId": "evt_...", "createSubAccount": true}` to create it. "Create it" covers one create: if that create loses its answer as well, the workflow asks you again.

## Troubleshooting

Open the workflow's **Executions** list in n8n.

| You see | Likely cause |
|---|---|
| Stripe shows 400 for every delivery, run ended at "Stop: Bad Signature" | `STRIPE_WEBHOOK_SECRET` is not this endpoint's signing secret (test and live endpoints have different ones), or a proxy changes the body before n8n |
| "outside the 300 second tolerance" | The n8n server's clock is off, or a delivery was replayed |
| Stripe shows 503 now and then | Two onboardings at the same moment: the second waited `LOCK_WAIT_SECONDS` for the first and Stripe sends it again later. Nothing to do |
| Run ended at "Stop: Bad Settings", Stripe shows 500 | The alert lists every problem. Stripe keeps trying the event for 3 days, so fixing the settings is enough |
| "needs the locations.write scope" ... "Agency Pro plan" | The app lacks the scope, or the agency is not on Agency Pro. Nothing was created; send the retry after the fix |
| "the agency app is not installed on the new sub-account" | The app was not installed on all sub-accounts. Install it on that sub-account, then send the retry: it fills the custom values |
| "the snapshot was still loading" | Large snapshots take longer than `SETUP_WAIT_SECONDS`. Raise it, then send the retry |
| "no custom value named ..." | The snapshot lacks that custom value. Add it to the sub-account (and the snapshot), then send the retry |
| "A HighLevel user with the email ... already exists" | The client already has a HighLevel user (another sub-account, an old project, or a test with your own login). Give that user access to the new sub-account, then send the retry |
| "Sub-accounts with that name exist ... cannot tell whether one of them is this client's" | A create lost its answer and another sub-account has the same business name. Check it in Agency view, then retry with its `subAccountId` (if it is this client's) or with `"createSubAccount": true` |
| "still answered 429 (rate limit)" on the sub-account create | HighLevel's rate limit held for every try. Nothing was created; send the retry a little later |
| Slack says "tag new-client was already on the contact" | A returning client: workflow 1 does not run twice for a contact. Send the welcome email and create the tasks by hand |
| Run ended at "Stop: Ledger Unavailable" with "the run lock ... could not be taken" | The `__run_lock__` row was changed by hand or the `attempts` column is not a Number column. Set the row to `attempts` 0 and `note` `free`; Stripe resends the refused events by itself |
| Run ended at "Stop: Needs Review" | The checkout had an unknown plan or no business name. Fix `PLAN_SNAPSHOTS` (or the data in the ledger row), then send the retry |
| The welcome email did not go out | Check workflow 1's enrollment history in HighLevel. See "Not confirmed" about the Contact Tag trigger in [ghl/native-workflows.md](../ghl/native-workflows.md) |
| A run failed but Slack stayed quiet | The error alert workflow is not active, is not chosen as the Error Workflow, or `SLACK_WEBHOOK_URL` is wrong |
