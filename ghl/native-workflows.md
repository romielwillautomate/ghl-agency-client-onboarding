# Native GoHighLevel workflows (agency sub-account)

The n8n workflow does the provisioning. The client facing part of onboarding runs as two native workflows in the agency's own sub-account (the sub-account where Brightline Growth Agency keeps its own clients as contacts), built by hand in **Automation > Workflows**:

1. **Client onboarding 1: welcome and tasks** starts when n8n adds the `new-client` tag. It sends the welcome email with the kickoff booking link, creates the internal tasks and reminds the team when the kickoff is not booked after 3 days.
2. **Client onboarding 2: kickoff booked** starts when the client books the kickoff call. It tags the contact and tells n8n, which updates the ledger and posts to Slack.

Names, options and field labels below were checked against HighLevel's help center (help.gohighlevel.com) on October 26, 2026. Anything the help center does not state is marked **Not confirmed**: check it in your own account while you build. The article for each part is listed at the end.

## Before you start

Set these up in the agency sub-account first. [snapshot-checklist.md](snapshot-checklist.md) has the full list.

| Item | Value used below |
|---|---|
| Pipeline | `Client Onboarding` with the stages `Onboarding`, `Kickoff Booked`, `Setup In Progress`, `Live`. n8n puts new clients in `Onboarding` (`GHL_ONBOARDING_STAGE_ID`) |
| Calendar | `Client Kickoff Call` (45 minutes), assigned to your account managers |
| Custom values (Settings > Custom Values) | `Agency Name` = Brightline Growth Agency, `Kickoff Booking Link` = the share link of the `Client Kickoff Call` calendar, `Client Login URL` = your white label app address (for example `https://app.brightlinegrowth.example.com`) |
| Tags | `new-client` (n8n adds it), `kickoff-booked` (workflow 2 adds it) |
| User | The account manager who gets the tasks (called "Account Manager" below) |
| Premium actions | Custom Webhook is a premium action. Turn premium workflow features on under **Agency Settings > Company > Workflow - Premium Features**. Each sub-account gets 100 free premium executions, then $0.01 per execution (HighLevel's pricing at the time of writing) |
| n8n | The workflow is active, so `https://YOUR-N8N-DOMAIN/webhook/ghl-onboarding-status` answers, and `GHL_STATUS_SECRET` is set |

Custom value keys come from the name: `Kickoff Booking Link` becomes `{{custom_values.kickoff_booking_link}}`. Renaming a custom value later does not change its key, so the merge fields below keep working.

## Workflow 1: Client onboarding 1: welcome and tasks

**Automation > Workflows > Create Workflow > Start from Scratch.** Name it `Client onboarding 1: welcome and tasks`.

### Triggers

Add both triggers. A contact enters once, whichever comes first.

| # | Trigger | Filters | Why |
|---|---|---|---|
| 1 | **Contact Tag** | **Tag Added** is `new-client` | The normal path: n8n adds the tag as the last step of the provisioning, after the opportunity and the note exist |
| 2 | **Pipeline Stage Changed** | **In Pipeline** is `Client Onboarding`, **Pipeline Stage** is `Onboarding` | For a client your team moves into `Onboarding` by hand (a deal closed by phone, for example) |

**Not confirmed:** the help center lists manual changes, workflow actions, imports and third party tools such as Zapier as sources that fire the Contact Tag trigger, but does not say in so many words that a tag added through API v2 does. Zapier works through the same API, so it is expected to fire. Check the workflow's enrollment history after the first real onboarding (see "Test it" below). If it did not fire, use the Inbound Webhook fallback at the end of this page.

**Not confirmed:** whether **Pipeline Stage Changed** also fires for an opportunity that is created directly in a stage (n8n creates new ones in `Onboarding`). It does not matter here: the tag starts the workflow either way, and re-entry is off.

### Settings tab

| Setting | Value |
|---|---|
| Allow Re-entry | **Off**. A client goes through onboarding once; it also stops the second trigger from starting a second run. See "A returning client" below |
| Stop on Response | Off |
| Timezone | Account Timezone |
| Time Window | Off (the welcome email should go out right away) |
| Sender Details | Your onboarding sender, for example From name `Brightline Growth Agency`, From email `onboarding@brightlinegrowth.example.com` on your verified sending domain |

### A returning client

A client who buys again is the same contact in the agency sub-account, and it still has the `new-client` tag from the first onboarding. Adding a tag the contact already has fires no Contact Tag trigger, and re-entry is off anyway, so workflow 1 does not run a second time. n8n sees the tag on the contact before its tag step, skips that step and says so in the Slack summary ("tag new-client was already on the contact", and under "To finish": "Send the welcome email and create the tasks by hand"). The account manager does that for the client's new plan.

### Actions, in order

**1. Send Email** (Communication). Action name: `Welcome email`.

| Field | Value |
|---|---|
| From Name | `Brightline Growth Agency` (typed out: whether this field takes merge fields is not stated in the help center) |
| From Email | `onboarding@brightlinegrowth.example.com` |
| Subject | `Welcome to {{custom_values.agency_name}}, {{contact.first_name}}` |
| Templates | None (or save the body below as a template) |

Email body:

```
Hi {{contact.first_name}},

Thank you for choosing {{custom_values.agency_name}}. We are setting up the {{contact.company_name}} account right now.

The first step is a 45 minute kickoff call, where we go through your goals, your offer and the access we need from you. Please pick a time that suits you:

{{custom_values.kickoff_booking_link}}

Your login: open {{custom_values.client_login_url}} and choose "Forgot password" with this email address ({{contact.email}}) to set your password.

If none of the times work, just reply to this email.

Talk soon,
The {{custom_values.agency_name}} team
```

The admin user n8n creates has a long random password that nobody knows, so the client sets their own through "Forgot password". **Not confirmed:** whether HighLevel also sends its own invitation email when a user is created through the API; the API reference does not mention one. If it does, drop the login paragraph. **Not confirmed:** the exact merge field for the company name; pick **Contact > Company Name** from the merge field menu, which inserts it for you.

**2. Add Task** (Contact). Action name: `Task: kickoff call`.

| Field | Value |
|---|---|
| Title | `Kickoff call: {{contact.company_name}}` |
| Description | `Run the kickoff call once it is booked. Cover goals, offer, brand and the access we need: domain registrar, Google Business Profile, Facebook page, payment processor.` |
| Assign To | Account Manager |
| Due In | 2 days |

**3. Add Task.** Action name: `Task: A2P 10DLC`.

| Field | Value |
|---|---|
| Title | `A2P 10DLC registration: {{contact.company_name}}` |
| Description | `In the client's sub-account: Settings > Phone System > Trust Center > Start Registration under A2P Messaging (SMS). Register the brand first (a Standard brand needs the client's EIN; a Sole Proprietor brand has no tax ID), then the campaign. Texts to US numbers need this before they are delivered.` |
| Assign To | Account Manager |
| Due In | 3 days |

**4. Add Task.** Action name: `Task: domain and email`.

| Field | Value |
|---|---|
| Title | `Domain and email setup: {{contact.company_name}}` |
| Description | `In the client's sub-account: Settings > Email Services > Dedicated Domain and IP > Add Domain. Use a subdomain (for example mg.clientdomain.com), add the DNS records it shows (2 TXT, 2 MX, 1 CNAME) and verify. Then connect the website domain for funnels and sites.` |
| Assign To | Account Manager |
| Due In | 5 days |

**5. Add Task.** Action name: `Task: snapshot review`.

| Field | Value |
|---|---|
| Title | `Snapshot review: {{contact.company_name}}` |
| Description | `Open the new sub-account and check what the snapshot loaded: custom values (the Slack summary says which were filled), calendars (assign the client's user and set availability), pipelines, workflows, forms. Funnels and websites load as drafts: publish them when the content is ready.` |
| Assign To | Account Manager |
| Due In | 2 days |

**6. Wait** (Internal Tools). Action name: `Wait 3 days`. Option **A set period of time**: 3 days. Leave the Advance Window off.

**7. If / Else** (Internal Tools). Action name: `Kickoff booked?`. Choose **Build My Own**.

| Branch | Condition |
|---|---|
| `Booked` | Contact tags include `kickoff-booked` |
| `None` (the automatic else branch, rename it `Not booked`) | |

**Not confirmed:** the exact wording of the tag condition in the condition picker (the help center shows the field, operator, value pattern but not this field's labels). Pick the contact's tags field and the "includes" (or "has tag") operator.

Leave the `Booked` branch empty: the workflow ends there for that client.

On the `Not booked` branch:

**8. Send Internal Notification** (Internal Tools). Action name: `Tell the account manager`. Type **Email**.

| Field | Value |
|---|---|
| To User Type | Particular Users: Account Manager |
| Subject | `Kickoff not booked yet: {{contact.company_name}}` |
| Message | `{{contact.name}} ({{contact.email}}, {{contact.phone}}) has not booked the kickoff call 3 days after the welcome email. Call them, or send the booking link again: {{custom_values.kickoff_booking_link}}` |

**9. Custom Webhook** (Send Data). Action name: `Tell n8n: kickoff not booked`. n8n posts it to Slack and records it in the ledger.

| Field | Value |
|---|---|
| Event | POST |
| URL | `https://YOUR-N8N-DOMAIN/webhook/ghl-onboarding-status` |
| Authorization | None |
| Headers | `X-Onboarding-Secret` = the value of `GHL_STATUS_SECRET` |
| Body (key and value pairs) | `event` = `kickoff_not_booked`, `contactId` = `{{contact.id}}`, `email` = `{{contact.email}}`, `company` = `{{contact.company_name}}`, `name` = `{{contact.name}}` |

Key and value pairs let HighLevel build the body, so a company name with a quote in it cannot break it. HighLevel's Custom Webhook article describes the POST event's pairs as a "form-style payload" (the raw body editor only comes with the CUSTOM event); the n8n webhook reads form and JSON bodies alike. If you use the CUSTOM event with a raw JSON body instead, set Content-Type to `application/json` and send yourself a test with a double quote in the company name. **Not confirmed:** `{{contact.id}}` is the merge field for the contact ID; pick **Contact > Contact ID** from the merge field menu to be sure.

**10. Send Email** (optional, to the client). Action name: `Kickoff reminder`. Subject `Pick a time for your kickoff call`, a two line body with `{{custom_values.kickoff_booking_link}}`. Leave it out if your account managers prefer to call.

Press **Save**, then switch the workflow from Draft to **Publish** (top right).

## Workflow 2: Client onboarding 2: kickoff booked

Name it `Client onboarding 2: kickoff booked`.

### Trigger

| Trigger | Filters |
|---|---|
| **Customer Booked Appointment** | **In Calendar** is `Client Kickoff Call`. "Who should be enrolled into the workflow?": Contact only |

HighLevel fires this trigger only when the client books through a booking link: "Manually booked appointments will not trigger the workflow." When an account manager books the kickoff for the client by hand, add the `kickoff-booked` tag by hand too, so workflow 1 does not send the reminder.

### Settings tab

| Setting | Value |
|---|---|
| Allow Re-entry | Appointment triggers always allow re-entry (HighLevel's rule), so a client who books again runs it again. That only adds the tag again and posts one more Slack note |

### Actions, in order

**1. Add Contact Tag** (Contact). Tag `kickoff-booked`. Tags are case sensitive, so type it exactly as workflow 1 checks it.

**2. Find Opportunity** and **3. Update Opportunity** (Opportunities, optional). Update Opportunity needs an opportunity in context, which this trigger does not give, so find it first: Find Opportunity in the pipeline `Client Onboarding` for this contact, then Update Opportunity with **Pipeline Stage** `Kickoff Booked`. **Not confirmed:** the Find Opportunity action's own fields (the help center only mentions it in the Update Opportunity article). Skip both steps if you prefer to move the card by hand.

**4. Custom Webhook** (Send Data). Action name: `Tell n8n: kickoff booked`.

| Field | Value |
|---|---|
| Event | POST |
| URL | `https://YOUR-N8N-DOMAIN/webhook/ghl-onboarding-status` |
| Authorization | None |
| Headers | `X-Onboarding-Secret` = the value of `GHL_STATUS_SECRET` |
| Body (key and value pairs) | `event` = `kickoff_booked`, `contactId` = `{{contact.id}}`, `email` = `{{contact.email}}`, `company` = `{{contact.company_name}}`, `name` = `{{contact.name}}`, `appointmentStart` = `{{appointment.start_time}}` |

**5. Send Internal Notification** (optional). Type **Notification** (in-app) to the Account Manager: title `Kickoff booked`, message `{{contact.company_name}} booked the kickoff for {{appointment.start_time}}`.

Save and **Publish**.

## What n8n does with the status calls

| `event` | n8n |
|---|---|
| `kickoff_booked` | Finds the client's ledger row by `contactId` (or `email`), writes `booked for <time>` into its `kickoff` column, posts `:calendar: Kickoff call booked: <business> for <time>.` to Slack |
| `kickoff_not_booked` | Writes `not booked, reminder sent <date>` and posts `:hourglass_flowing_sand: Kickoff call not booked yet: <business> (<email>)` to Slack |

A call with a wrong or missing `X-Onboarding-Secret` gets 401 and raises an alert; a call without a known `event` or a `contactId` gets 400 and an alert. A contact that is not in the ledger (a client onboarded by hand) still gets its Slack note, marked as such.

**Free alternative to Custom Webhook.** The standard **Webhook** action is not a premium action but has no header field, only Custom Data. Point it at the same URL and add the Custom Data items `secret` = your `GHL_STATUS_SECRET` and `event` = `kickoff_booked` (or `kickoff_not_booked`). Its payload carries the contact fields at the top level (`contact_id`, `email`, and so on), and n8n accepts the secret and the event from `customData`. **Not confirmed:** the help center does not show where Custom Data sits in the payload; n8n expects a `customData` object, as the reference GoHighLevel repos do. Check the first call in n8n's executions.

## Test it

1. In the agency sub-account, create a test contact with your own email and the company name `Test Client Co`.
2. Add the tag `new-client` by hand. Workflow 1 should send the welcome email at once and create the four tasks. Open the workflow's **Enrollment History** to see the contact moving through the steps.
3. Book the kickoff from the link in the email. Workflow 2 should add `kickoff-booked`, and n8n should post "Kickoff call booked" in Slack (the n8n execution list shows the call).
4. To see the reminder path without waiting 3 days, change the Wait to 2 minutes on a copy of workflow 1, enroll another test contact and do not book.
5. After the first real onboarding through Stripe, check that the contact entered workflow 1 through the **Contact Tag** trigger.

## Fallback: start workflow 1 from n8n directly

If tags added through the API turn out not to fire the Contact Tag trigger in your account, add a third trigger to workflow 1: **Inbound Webhook** (a premium trigger). It gives you a URL like `https://services.leadconnectorhq.com/hooks/<locationId>/webhook-trigger/<id>`. It needs an email or phone in the payload to find the contact. The n8n workflow does not call it today; adding an HTTP Request node after **GHL: Add Tag** that posts `{"email": "<client email>"}` to that URL is a small change. Keep re-entry off so the tag and the webhook cannot start it twice.

## Sources (help.gohighlevel.com, checked October 26, 2026)

| Topic | Article |
|---|---|
| Contact Tag trigger | 155000002482, 48001213546 |
| Pipeline Stage Changed trigger | 155000002493 |
| Opportunity Created trigger | 155000003499 |
| Customer Booked Appointment trigger | 155000002675 |
| Appointment Status trigger (the only one documented to fire on API changes, through its "Modified By: API" filter) | 155000002619 |
| Inbound Webhook trigger | 48001237383, 155000001183 |
| Send Email action, merge fields | 155000002472, 48001161575 |
| Custom values and their keys | 155000004705, 155000007763 |
| Add Task action | 155000003375 |
| Wait action (current option names) | 155000002470 |
| If / Else action | 155000002471, 155000004050 |
| Send Internal Notification | 155000003202 |
| Add Contact Tag | 155000003111 |
| Create and Update Opportunity actions | 155000004752, 155000004753 |
| Webhook action | 155000003299 |
| Custom Webhook action, premium pricing | 155000003305, 48001238167, 155000005047, 155000005678 |
| Workflow settings (re-entry, time window, sender) | 48001239875, 155000001254 |
| A2P registration | 155000008140, 155000004539 |
| Dedicated sending domain | 48001226115 |

Article URLs have the form `https://help.gohighlevel.com/support/solutions/articles/<number>`.
