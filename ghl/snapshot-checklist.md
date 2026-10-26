# Snapshot checklist (for the agency owner)

The n8n workflow creates each new client's sub-account from a snapshot, then fills in the client's details. That only works if the snapshot has the pieces the workflow looks for. This page lists them, plus what your own agency sub-account and your HighLevel agency need. Tick each box before you switch the Stripe webhook on.

You need one snapshot per plan you sell. In this example Brightline Growth Agency sells a **Launch** plan and a **Scale** plan, so it keeps two snapshots, and `PLAN_SNAPSHOTS` in n8n says which one goes with which plan.

## 1. In every client snapshot

### Custom values (Settings > Custom Values in the sub-account you build the snapshot from)

The workflow finds these by **name** and fills them with what the client typed at checkout. Upper and lower case and extra spaces do not matter, but the words do. The values you leave in the snapshot can be placeholders: the workflow overwrites them.

- [ ] `Business Name`: the business name from the checkout
- [ ] `Business Phone`: the phone number from the checkout (in the +1 format Stripe collects)
- [ ] `Business Email`: the client's email
- [ ] `Business Address`: street address (line 1 and line 2)
- [ ] `Business City`
- [ ] `Business State`
- [ ] `Business Postal Code`
- [ ] `Business Country`: the two letter code, for example `US`
- [ ] `Business Website`: from the optional website field at checkout (left alone when the client did not give one)
- [ ] `Owner First Name`
- [ ] `Owner Name`: first and last name
- [ ] `Booking Link`: the link of the client's own booking calendar (see Calendars below)

Use these custom values in the snapshot's emails, funnels, websites, forms and workflows (for example `{{custom_values.business_name}}`, `{{custom_values.booking_link}}`), so everything shows the client's details as soon as the workflow has filled them.

What happens when one is missing: the workflow fills the others, carries on with the rest of the onboarding, and the Slack summary and the alert name the missing value with the value to set by hand. Once you add it to that sub-account, a retry fills it.

The workflow waits for the snapshot to load these values before it fills them. It asks HighLevel's snapshot status first; when that has no record of the load (HighLevel documents it for snapshot pushes), it reads the custom values until at least one of the names above is there, up to `SETUP_WAIT_SECONDS`. If the status has no record and the snapshot has none of these names, that whole wait passes before the alert, so check the names before you go live.

Other custom values in the snapshot (a review link, brand colors and so on) are left exactly as the snapshot set them.

If your snapshots already use other names (for example `Company Name` instead of `Business Name`), you do not have to rename them: set `CUSTOM_VALUE_NAMES` in n8n, for example `{"businessName": "Company Name"}`. An empty name switches a value off: `{"businessWebsite": ""}`.

Keep in mind: a custom value's merge field key comes from its first name. Renaming it later does not change the key, so templates keep working, but the workflow matches the current name.

### Calendars

- [ ] A calendar named exactly `Discovery Call` (or whatever you set in `CLIENT_BOOKING_CALENDAR`). The workflow reads its ID and writes its booking link into `Booking Link`, in the form `https://api.leadconnectorhq.com/widget/booking/<calendar ID>`.
- [ ] If your agency uses its own branded API domain, set `GHL_BOOKING_BASE_URL` to that domain's booking path, so links show your domain. **Not confirmed:** HighLevel's help center does not spell out the link format; the one above is the calendar's permanent link as HighLevel's own WordPress plugin and live calendars use it. Open the calendar's **Share** button once and compare.
- [ ] After a sub-account is created, check the calendar's team members and availability, and assign the client's user if the calendar should book with them. Besides your agency's own users, the only user the workflow adds is the client's admin user (the "snapshot review" task reminds the account manager to check this).

### Pipelines and the rest

The workflow does not touch anything else in the client's sub-account, so build the snapshot the way you deliver your service. The usual set:

- [ ] Pipelines for the client's own leads (for example `New Leads > Contacted > Booked > Won`)
- [ ] Workflows (missed call text back, review requests, appointment reminders)
- [ ] Forms, surveys, funnels and websites (they load as drafts in the new sub-account)
- [ ] Email and text templates, tags, custom fields, trigger links

### What a snapshot does not carry

HighLevel's snapshot article lists these as **not** included: contacts, appointments, conversations, reputation data, Stripe connections, integrations and third party connections, assigned Voice AI phone numbers, private dashboards. **Not confirmed** in that list, but not expected either: users, phone numbers in general, A2P 10DLC registration and domains. That is why the onboarding workflow creates the admin user itself and the native workflow creates the A2P and the domain tasks.

**Not confirmed:** whether a snapshot copies custom value **values** or only their names. Feature requests suggest only the names travel. It does not matter here, because the workflow writes the values it owns.

## 2. In your own agency sub-account (where your clients are contacts)

- [ ] A pipeline for onboarding, for example `Client Onboarding`, with a stage for new clients (`Onboarding`). Put the pipeline ID in `GHL_ONBOARDING_PIPELINE_ID` and the stage ID in `GHL_ONBOARDING_STAGE_ID` (one API call, shown in docs/setup.md).
- [ ] A calendar for kickoff calls, for example `Client Kickoff Call`, with your account managers on it.
- [ ] Custom values `Agency Name`, `Kickoff Booking Link` (the share link of the kickoff calendar) and `Client Login URL` (your white label app address). The welcome email uses them.
- [ ] The tags `new-client` and `kickoff-booked` (n8n and workflow 2 add them; creating them first keeps the names consistent).
- [ ] The two native workflows from [native-workflows.md](native-workflows.md), published.
- [ ] A Private Integration token of this sub-account with `contacts.readonly`, `contacts.write`, `opportunities.readonly` and `opportunities.write`, in `GHL_HOME_TOKEN`.
- [ ] **Allow Duplicate Contact** off (Settings > Business Profile > Contact Deduplication Preferences). HighLevel's API reference says the contact upsert follows this setting, so with it off a returning client's contact is updated instead of duplicated.
- [ ] If you keep "Allow Multiple Opportunities per Contact" off (the default), a returning client's old opportunity in the onboarding pipeline is reopened and moved to `Onboarding`. Only a won one is left alone; then a new opportunity is needed, which HighLevel refuses unless that setting is on, and the alert says to add it by hand.

## 3. In your HighLevel agency

- [ ] **The Agency Pro plan.** HighLevel's API reference for creating a sub-account says: "This feature is only available on Agency Pro ($497) plan." Its help center article on API access adds that Agency Pro's "Advanced API access" unlocks agency API keys, where Starter and Unlimited only get location API keys. On Starter or Unlimited, the sub-account create is refused, the run stops at the first step and the alert says so. Nothing else is created in that case.
- [ ] **A private Marketplace app for your agency** (marketplace.gohighlevel.com > My Apps). Private apps are not listed publicly and go live without review. Settings HighLevel's distribution docs point to for this mix of agency and sub-account calls: target user **Sub-account**, who can install **Agency Only**, bulk installation on. Scopes: `locations.readonly`, `locations.write`, `users.readonly`, `users.write`, `snapshots.readonly`, `oauth.readonly`, `oauth.write`, `locations/customValues.readonly`, `locations/customValues.write`, `calendars.readonly`. **Not confirmed:** HighLevel does not document this exact combination for an app that creates sub-accounts; the settings come from its app distribution table.
- [ ] **Install the app on your agency and choose "all sub-accounts"** when asked. HighLevel then installs it on sub-accounts created later, which the workflow needs: it can only get a token for a new sub-account (to fill its custom values) once the app is installed there. The workflow waits for that up to `SETUP_WAIT_SECONDS` (3 minutes by default). **Not confirmed:** that a sub-account created through the API gets the app this way, and how fast. If it does not, the run says so, carries on with everything else, and a retry fills the custom values after you install the app on that sub-account.
- [ ] **The snapshot IDs** in `PLAN_SNAPSHOTS`, for example `{"launch": {"snapshotId": "...", "label": "Launch"}, "scale": {"snapshotId": "...", "label": "Scale"}}`. docs/setup.md shows how to list your snapshots with their IDs.
- [ ] **Stripe:** each onboarding plan's Payment Link (or Checkout Session) carries the metadata `onboarding_plan` = `launch` or `scale`, and Checkout collects the business name in a custom text field with the key `businessname` (plus an optional `website` field), the phone number and the billing address. Stripe copies a Payment Link's metadata onto every Checkout Session it creates.

## 4. When you change a snapshot later

- Refresh the snapshot, then use **Push Update** to send the change to sub-accounts that loaded it. Pushes go only to sub-accounts in your own agency.
- A new custom value added to a snapshot reaches existing sub-accounts only through a push. To fill it for clients who are already onboarded, set it by hand; for clients onboarded from then on, the workflow fills it if its name is in the list above (or in `CUSTOM_VALUE_NAMES`).
- If you add a new plan, create its snapshot, add the plan to `PLAN_SNAPSHOTS` and restart n8n. A paid checkout for a plan that is missing there is held (Stripe still gets its 200) and the alert asks you to add it; then a retry starts it.
