"""End-to-end test for the GoHighLevel agency client onboarding workflow against MOCK APIs.

No real Stripe, GoHighLevel or Slack account is used. The script:
  1. starts strict, stateful mocks of the GoHighLevel API v2 (agency, a new client's sub-account and the agency's own
     sub-account) and of a Slack incoming webhook on this machine, and plays Stripe itself: it builds Checkout Session
     events, signs them like Stripe (HMAC SHA-256 over "timestamp.body", Stripe-Signature header) and posts them,
  2. creates a temporary n8n data table for the ledger, imports and activates a temporary copy of
     workflow/error-alert-slack.json (n8n only runs an active error workflow), then a temporary copy of the onboarding
     workflow that reports to it. In the copy, API hosts point at the mocks, settings are test values, and the n8n
     HighLevel OAuth2 credential is replaced by a fixed bearer token (the mock cannot run HighLevel's OAuth flow),
  3. sends paid checkouts for fictional clients and plays what goes wrong: forged, stale and changed signatures,
     unused events, duplicate and simultaneous deliveries, a held run lock, a user create that keeps failing, a
     sub-account create whose answer is lost, a missing scope, a snapshot without a custom value, an agency app that is
     not installed on the new sub-account, an existing user, a returning client, a lost note answer, Slack down, an
     unknown plan, a snapshot status without a record, another client's sub-account with the same name, a 429 on the
     create, a lock row that cannot be taken; then continues the unfinished ones with the Retry webhook, and calls the
     status webhook like the native GoHighLevel workflows do,
  4. checks every request the workflow made, the mock state (one sub-account and one user per client), the ledger,
     the Slack text, the HTTP answers Stripe got, the node each run ended on, and that every failed run raised exactly
     one error alert linked to it,
  5. imports a copy with broken settings (every entry point must answer 500 and the alert must name the problems),
  6. prints PASS or FAIL per check and deletes every temporary workflow and the data table, also after a crash.
Temporary workflows are named "TEST ghl-agency-client-onboarding (temporary)" and the data table
"TEST ghl-agency-client-onboarding ledger (temporary)". Leftovers with exactly those names from an interrupted run are
deleted at the start. Nothing else in n8n is touched.

Requirements: Python 3.9+, a running n8n 2.x (with data tables) and the public API enabled.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
  VERBOSE      optional; 1 prints each check as soon as it is made
  SAVE_REQUESTS  optional file path; every request the workflow made to the mocks is saved there as JSON
                 (headers are left out)
Usage: python tests/e2e_mock_test.py
"""
import copy, hashlib, hmac, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8808
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / 'workflow' / 'ghl-agency-client-onboarding.json'
SLUG = 'ghl-agency-client-onboarding'
TEMP_NAME = f'TEST {SLUG} (temporary)'
TABLE_NAME = f'TEST {SLUG} ledger (temporary)'

COMPANY, HOME, APP_ID = 'brightlineAgency01', 'brightlineHome01', 'appOnboarding01'
PIPELINE, STAGE_ONBOARDING, STAGE_KICKOFF, STAGE_LIVE = 'pipeClients01', 'stageOnboarding01', 'stageKickoff01', 'stageLive01'
AGENCY_TOKEN, HOME_TOKEN = 'mock-agency-oauth-token', 'pit-mock-home'
STRIPE_SECRET = 'whsec_test_mock_7c1e5a90b3d24f68a1e9c0b7d2f4a6c8'
ENV = {
    'GHL_COMPANY_ID': COMPANY, 'GHL_APP_ID': APP_ID, 'GHL_HOME_LOCATION_ID': HOME, 'GHL_HOME_TOKEN': HOME_TOKEN,
    'GHL_ONBOARDING_PIPELINE_ID': PIPELINE, 'GHL_ONBOARDING_STAGE_ID': STAGE_ONBOARDING, 'NEW_CLIENT_TAG': 'new-client',
    'PLAN_SNAPSHOTS': json.dumps({'launch': {'snapshotId': 'snapLaunch01', 'label': 'Launch'}, 'scale': {'snapshotId': 'snapScale01', 'label': 'Scale'}}),
    'STRIPE_WEBHOOK_SECRET': STRIPE_SECRET, 'STRIPE_TOLERANCE_SECONDS': '300', 'STRIPE_PLAN_METADATA_KEY': 'onboarding_plan',
    'STRIPE_BUSINESS_FIELD_KEY': 'businessname', 'STRIPE_WEBSITE_FIELD_KEY': 'website', 'CLIENT_TIMEZONE': 'America/Chicago',
    'CLIENT_BOOKING_CALENDAR': 'Discovery Call', 'CUSTOM_VALUE_NAMES': '', 'GHL_APP_URL': 'https://app.gohighlevel.com',
    'GHL_BOOKING_BASE_URL': 'https://api.leadconnectorhq.com/widget/booking', 'LEDGER_TABLE_ID': '',
    'RETRY_SECRET': 'test-retry-secret-5d2a9c81e7f04b36', 'GHL_STATUS_SECRET': 'test-status-secret-81b4f0c29d7e3a65',
    'SLACK_WEBHOOK_URL': MOCK + '/slack/hook', 'API_MAX_ATTEMPTS': '3', 'RETRY_BASE_SECONDS': '0.5', 'GHL_RATE_LIMIT_WAIT_SECONDS': '1',
    'LOCK_WAIT_SECONDS': '3', 'SETUP_WAIT_SECONDS': '6', 'SETUP_POLL_SECONDS': '1', 'STUCK_CHECK_MINUTES': '20',
}
HOSTS = {'https://services.leadconnectorhq.com': MOCK + '/ghl'}
LEDGER_COLUMNS = [('key', 'string'), ('sessionId', 'string'), ('status', 'string'), ('plan', 'string'), ('business', 'string'), ('email', 'string'),
                  ('locationId', 'string'), ('userId', 'string'), ('contactId', 'string'), ('opportunityId', 'string'), ('steps', 'string'),
                  ('attempts', 'number'), ('note', 'string'), ('data', 'string'), ('kickoff', 'string'), ('changedAt', 'string')]
LOCK_KEY = '__run_lock__'


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


# ----------------------------------------------------------------------------------------------------------------------
# Mock state
# ----------------------------------------------------------------------------------------------------------------------
LOCK = threading.RLock()
LOG = []            # dict(service, method, path, query, headers, body, status, response, t)
COUNTER = {'n': 0}
FAIL = {
    'agency_scopes_removed': set(),   # scopes the agency app does not have
    'create_delay': {},               # business name -> seconds the sub-account create takes
    'create_lost': set(),             # creates the sub-account, then answers 504 (the answer is lost)
    'create_hidden': set(),           # creates it under another name and email, then answers 502
    'create_502': {},                 # business -> number of 502 answers without creating anything
    'user_503': {},                   # email -> number of 503 answers to the user create
    'cv_429_once': set(),             # custom value names that get one 429
    'note_lost': set(),               # contact IDs whose note is saved but answered with 504
    'slack_fail': set(),              # Slack answers 500 for texts that contain one of these
    'never_install': set(),           # business names whose sub-account never gets the agency app
    'create_429': {},                 # business -> number of 429 answers to the create (nothing created)
    'no_push_record': set(),          # business names whose snapshot status has no record (the load was not a push)
}


def next_id(prefix):
    COUNTER['n'] += 1
    return f'{prefix}{COUNTER["n"]:04d}'


SNAPSHOT_VALUES = [('Business Name', 'Your Business'), ('Business Phone', ''), ('Business Email', ''), ('Business Address', ''),
                   ('Business City', ''), ('Business State', ''), ('Business Postal Code', ''), ('Business Country', 'US'),
                   ('Business Website', ''), ('Owner First Name', ''), ('Owner Name', ''), ('Booking Link', ''),
                   ('Review Link', 'https://g.page/r/your-review-link'), ('Brand Color', '#1E6FD9')]
SNAPSHOTS = {
    'snapLaunch01': {'name': 'Brightline Launch', 'type': 'own', 'values': SNAPSHOT_VALUES, 'calendars': ['Discovery Call', 'Strategy Session']},
    'snapScale01': {'name': 'Brightline Scale', 'type': 'own', 'values': [v for v in SNAPSHOT_VALUES if v[0] != 'Booking Link'], 'calendars': ['Discovery Call']},
}
LOCATIONS = {}      # id -> location (+ private keys starting with _)
USERS = {}
CONTACTS = {}       # agency sub-account contacts
OPPORTUNITIES = {}
NOTES = {}          # contactId -> [note]
TAG_EVENTS = []     # (time, contactId, tags)
COUNTRIES = {'US', 'CA', 'GB', 'AU', 'NZ', 'IE', 'PH', 'DE', 'FR', 'ES', 'IT', 'NL', 'MX', 'BR', 'IN', 'ZA', 'SG', 'AE'}


def slug(name):
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def location_view(l):
    return {k: v for k, v in l.items() if not k.startswith('_')}


def add_location(name, email, company=COMPANY, snapshot=None, created=None, ready_after=0, install_after=0, decoy=False):
    lid = next_id('loc')
    LOCATIONS[lid] = {'id': lid, 'companyId': company, 'name': name, 'email': email, 'phone': '', 'address': '', 'city': '', 'state': '',
                      'country': 'US', 'postalCode': '', 'website': '', 'timezone': 'America/Chicago', 'domain': '', 'settings': {}, 'social': {},
                      '_created': created or time.time(), '_snapshot': snapshot, '_status_polls': 0, '_ready_after': ready_after,
                      '_install_polls': 0, '_install_after': install_after, '_installed': install_after == 0 and name not in FAIL['never_install'],
                      '_values': [], '_calendars': [], '_decoy': decoy}
    if snapshot:
        snap = SNAPSHOTS[snapshot]
        LOCATIONS[lid]['_values'] = [{'id': next_id('cv'), 'name': n, 'fieldKey': '{{ custom_values.' + slug(n) + ' }}', 'value': v, 'locationId': lid}
                                     for n, v in snap['values']]
        LOCATIONS[lid]['_calendars'] = [{'id': next_id('cal'), 'locationId': lid, 'name': n, 'isActive': True, 'slug': slug(n).replace('_', '-'),
                                         'widgetSlug': slug(n).replace('_', '-'), 'calendarType': 'round_robin', 'widgetType': 'default'}
                                        for n in snap['calendars']]
    return lid


def snapshot_ready(l):
    return l['_snapshot'] is None or l['_status_polls'] >= l['_ready_after']


def user_view(u):
    return copy.deepcopy(u)


def add_user(first, last, email, location_ids, company=COMPANY):
    uid = next_id('usr')
    USERS[uid] = {'id': uid, 'name': f'{first} {last}', 'firstName': first, 'lastName': last, 'email': email, 'phone': '', 'extension': '',
                  'permissions': {'contactsEnabled': True}, 'scopes': '', 'roles': {'type': 'account', 'role': 'admin', 'locationIds': list(location_ids),
                  'restrictSubAccount': False}, 'deleted': False, 'lcPhone': {}, 'platformLanguage': 'en_US', '_company': company}
    return uid


def add_contact(first, last, email, phone, company=''):
    cid = next_id('ctc')
    CONTACTS[cid] = {'id': cid, 'locationId': HOME, 'firstName': first, 'lastName': last, 'name': f'{first} {last}', 'email': email, 'phone': phone,
                     'companyName': company, 'tags': [], 'dateAdded': now_iso(), 'source': ''}
    return cid


# ---------------------------------------------------------------------------------------------------------------------
# GoHighLevel API v2 mock. Body and query fields from GoHighLevel's OpenAPI files (highlevel-api-docs, apps/*.json).
# ---------------------------------------------------------------------------------------------------------------------
def e400(message):
    return 400, {'statusCode': 400, 'message': message}


def e401():
    return 401, {'statusCode': 401, 'message': 'Invalid JWT'}


def e401_scope():
    return 401, {'statusCode': 401, 'message': 'The token is not authorized for this scope.'}


def e403():
    return 403, {'statusCode': 403, 'message': 'The token does not have access to this location.'}


def e422(*messages):
    return 422, {'statusCode': 422, 'message': list(messages), 'error': 'Unprocessable Entity'}


def unknown(obj, allowed):
    extra = sorted(set(obj) - set(allowed))
    return e422(*[f'property {k} should not exist' for k in extra]) if extra else None


def missing(obj, required):
    gone = [k for k in required if obj.get(k) in (None, '', [])]
    return e422(*[f'{k} should not be empty' for k in gone]) if gone else None


CREATE_LOCATION_KEYS = {'name', 'phone', 'companyId', 'address', 'city', 'state', 'country', 'postalCode', 'website', 'timezone', 'prospectInfo',
                        'settings', 'social', 'twilio', 'mailgun', 'snapshotId'}
CREATE_USER_KEYS = {'companyId', 'firstName', 'lastName', 'email', 'password', 'phone', 'type', 'role', 'locationIds', 'permissions', 'scopes',
                    'scopesAssignedToOnly', 'profilePhoto', 'twilioPhone', 'platformLanguage'}
UPSERT_KEYS = {'firstName', 'lastName', 'name', 'email', 'locationId', 'gender', 'phone', 'address1', 'city', 'state', 'postalCode', 'website',
               'timezone', 'dnd', 'dndSettings', 'inboundDndSettings', 'tags', 'customFields', 'source', 'dateOfBirth', 'country', 'companyName',
               'assignedTo', 'createNewIfDuplicateAllowed'}
CREATE_OPP_KEYS = {'pipelineId', 'locationId', 'name', 'pipelineStageId', 'status', 'contactId', 'monetaryValue', 'assignedTo', 'customFields'}
UPDATE_OPP_KEYS = {'pipelineId', 'name', 'pipelineStageId', 'status', 'monetaryValue', 'assignedTo', 'customFields'}
OPP_SEARCH_PARAMS = {'location_id', 'pipeline_id', 'pipeline_stage_id', 'contact_id', 'assigned_to', 'status', 'q', 'campaignId', 'id', 'order',
                     'date', 'endDate', 'startAfter', 'startAfterId', 'country', 'page', 'limit', 'getTasks', 'getNotes', 'getCalendarEvents'}
STAGES = {PIPELINE: {STAGE_ONBOARDING, STAGE_KICKOFF, STAGE_LIVE}}
AGENCY_SCOPES = {'locations.readonly', 'locations.write', 'users.readonly', 'users.write', 'oauth.readonly', 'oauth.write', 'snapshots.readonly'}
PASSWORD_RULE = re.compile(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9]).{12,}$')


def agency_ok(headers, scope):
    if headers.get('authorization') != 'Bearer ' + AGENCY_TOKEN:
        return e401()
    if scope not in AGENCY_SCOPES - FAIL['agency_scopes_removed']:
        return e401_scope()
    return None


def location_ok(headers, lid):
    l = LOCATIONS.get(lid)
    if not l:
        return e400('Location not found')
    if headers.get('authorization') != 'Bearer mock-location-token-' + lid:
        return e401() if not str(headers.get('authorization', '')).startswith('Bearer mock-location-token-') else e403()
    return None


def home_ok(headers):
    return None if headers.get('authorization') == 'Bearer ' + HOME_TOKEN else e401()


def ghl(method, path, q, headers, body, form):
    version = '2021-04-15' if path.startswith('/calendars') else '2021-07-28'
    if headers.get('version') != version:
        return e400(f'Version header must be {version} for this endpoint')
    if method in ('POST', 'PUT') and path != '/oauth/locationToken' and not str(headers.get('content-type', '')).startswith('application/json'):
        return e400('Content-Type must be application/json')

    # ---- agency level
    if method == 'GET' and path == '/locations/search':
        err = agency_ok(headers, 'locations.readonly') or unknown(q, {'companyId', 'skip', 'limit', 'order', 'email'})
        if err:
            return err
        if q.get('companyId') != COMPANY:
            return e403()
        if q.get('order', 'asc') not in ('asc', 'desc') or not re.fullmatch(r'\d{1,4}', q.get('limit', '10')) or not re.fullmatch(r'\d{1,6}', q.get('skip', '0')):
            return e422('order must be asc or desc; skip and limit must be numbers')
        rows = [l for l in LOCATIONS.values() if l['companyId'] == COMPANY]
        if q.get('email'):
            # Assumption: an exact, case-insensitive match on the location's email (the docs do not describe the filter)
            rows = [l for l in rows if (l['email'] or '').lower() == q['email'].lower()]
        rows.sort(key=lambda l: l['_created'], reverse=q.get('order') == 'desc')
        skip, limit = int(q.get('skip', '0')), int(q.get('limit', '10'))
        return 200, {'locations': [location_view(l) for l in rows[skip:skip + limit]]}
    if method == 'POST' and path == '/locations/':
        err = agency_ok(headers, 'locations.write') or unknown(body, CREATE_LOCATION_KEYS) or missing(body, ('name', 'companyId'))
        if err:
            return err
        if body['companyId'] != COMPANY:
            return e403()
        if 'country' in body and body['country'] not in COUNTRIES:
            return e422('country must be a valid 2 letter country code')
        pi = body.get('prospectInfo')
        if pi is not None and (not isinstance(pi, dict) or unknown(pi, {'firstName', 'lastName', 'email'}) or missing(pi, ('firstName', 'lastName', 'email'))):
            return e422('prospectInfo.firstName, prospectInfo.lastName and prospectInfo.email should not be empty')
        if body.get('snapshotId') and body['snapshotId'] not in SNAPSHOTS:
            return e400('Snapshot not found')
        name = body['name']
        if FAIL['create_502'].get(name):
            FAIL['create_502'][name] -= 1
            return 502, {'statusCode': 502, 'message': 'Bad Gateway'}
        if FAIL['create_429'].get(name):
            FAIL['create_429'][name] -= 1
            return 429, {'statusCode': 429, 'message': 'Too Many Requests'}, {'Retry-After': '1'}
        time.sleep(FAIL['create_delay'].get(name, 0))
        hidden = name in FAIL['create_hidden']
        lid = add_location(name + ' (copy)' if hidden else name, '' if hidden else (pi or {}).get('email', ''), snapshot=body.get('snapshotId'),
                           ready_after=3, install_after=0 if name in FAIL['never_install'] else 1)
        l = LOCATIONS[lid]
        for k in ('phone', 'address', 'city', 'state', 'country', 'postalCode', 'website', 'timezone'):
            if k in body:
                l[k] = body[k]
        if name in FAIL['never_install']:
            l['_installed'] = False
            l['_install_after'] = 10 ** 6
        if name in FAIL['no_push_record']:
            l['_no_push'] = True
        if hidden:
            FAIL['create_hidden'].discard(name)
            return 502, {'statusCode': 502, 'message': 'Bad Gateway'}
        if name in FAIL['create_lost']:
            FAIL['create_lost'].discard(name)
            return 504, {'statusCode': 504, 'message': 'Gateway Timeout'}
        return 200, location_view(l)          # the create answers 200 with the location itself, no wrapper
    m = re.fullmatch(r'/locations/([^/]+)', path)
    if method == 'GET' and m and m.group(1) != 'search':
        err = agency_ok(headers, 'locations.readonly')
        if err:
            return err
        l = LOCATIONS.get(m.group(1))
        if not l:
            return e400('Location not found')
        if l['companyId'] != COMPANY:
            return e403()
        return 200, {'location': location_view(l)}
    m = re.fullmatch(r'/snapshots/snapshot-status/([^/]+)/location/([^/]+)', path)
    if method == 'GET' and m:
        err = agency_ok(headers, 'snapshots.readonly') or unknown(q, {'companyId'})
        if err:
            return err
        if q.get('companyId') != COMPANY:
            return e422('companyId should not be empty')
        l = LOCATIONS.get(m.group(2))
        if l and l.get('_no_push'):
            # assumption: HighLevel documents this endpoint as the last snapshot PUSH; a load at creation may have no record
            return 404, {'statusCode': 404, 'message': 'No snapshot push found for this location'}
        if not l or l['_snapshot'] != m.group(1):
            return e400('No snapshot load found for this location')
        l['_status_polls'] += 1
        ready = snapshot_ready(l)
        done_assets = ['forms', 'surveys', 'funnels', 'workflows', 'calendars'] + (['custom_fields', 'custom_values', 'tags'] if ready else [])
        return 200, {'data': {'id': 'load-' + l['id'], 'locationId': l['id'], 'status': 'completed' if ready else 'processing',
                              'completed': done_assets, 'pending': [] if ready else ['custom_fields', 'custom_values', 'tags']}}
    if method == 'GET' and path == '/oauth/installedLocations':
        err = agency_ok(headers, 'oauth.readonly') or unknown(q, {'companyId', 'appId', 'skip', 'limit', 'query', 'isInstalled', 'versionId', 'onTrial', 'planId', 'locationId'})
        if err:
            return err
        if q.get('companyId') != COMPANY or q.get('appId') != APP_ID:
            return e422('companyId and appId should not be empty')
        rows = []
        for l in LOCATIONS.values():
            if q.get('locationId') and l['id'] != q['locationId']:
                continue
            if l['id'] == q.get('locationId'):
                l['_install_polls'] += 1
                if l['_install_polls'] > l['_install_after'] and l['name'] not in FAIL['never_install']:
                    l['_installed'] = True
            if q.get('isInstalled') == 'true' and not l['_installed']:
                continue
            rows.append({'_id': l['id'], 'name': l['name'], 'address': l['address'], 'isInstalled': l['_installed'], 'versionId': 'v1',
                         'installedAt': now_iso()})
        return 200, {'locations': rows, 'count': len(rows), 'installToFutureLocations': True}
    if method == 'POST' and path == '/oauth/locationToken':
        err = agency_ok(headers, 'oauth.write')
        if err:
            return err
        if not str(headers.get('content-type', '')).startswith('application/x-www-form-urlencoded'):
            return e400('Content-Type must be application/x-www-form-urlencoded')
        err = unknown(form, {'companyId', 'locationId'}) or missing(form, ('companyId', 'locationId'))
        if err:
            return err
        l = LOCATIONS.get(form['locationId'])
        if form['companyId'] != COMPANY or not l or l['companyId'] != COMPANY:
            return e400('Location not found for this company')
        if not l['_installed']:
            return e403()    # assumption: the docs do not say what the API answers when the app is not installed there
        return 200, {'access_token': 'mock-location-token-' + l['id'], 'token_type': 'Bearer', 'expires_in': 86399,
                     'refresh_token': 'mock-location-refresh-' + l['id'], 'scope': 'locations/customValues.readonly locations/customValues.write calendars.readonly',
                     'locationId': l['id'], 'planId': '', 'userId': 'usrAgencyAdmin01'}
    if method == 'GET' and path == '/users/search':
        err = agency_ok(headers, 'users.readonly') or unknown(q, {'companyId', 'query', 'skip', 'limit', 'locationId', 'type', 'role', 'ids', 'sort', 'sortDirection', 'enabled2waySync'})
        if err:
            return err
        if q.get('companyId') != COMPANY:
            return e422('companyId should not be empty')
        needle = q.get('query', '').lower()
        rows = [u for u in USERS.values() if u['_company'] == COMPANY and (needle in u['email'].lower() or needle in u['name'].lower())]
        return 200, {'users': [{k: v for k, v in user_view(u).items() if not k.startswith('_')} for u in rows[:int(q.get('limit', '25'))]], 'count': len(rows)}
    if method == 'POST' and path == '/users/':
        err = agency_ok(headers, 'users.write') or unknown(body, CREATE_USER_KEYS) or missing(body, ('companyId', 'firstName', 'lastName', 'email', 'password', 'type', 'role', 'locationIds'))
        if err:
            return err
        if body['companyId'] != COMPANY:
            return e403()
        if body['type'] not in ('account', 'agency') or body['role'] not in ('admin', 'user'):
            return e422('type must be account or agency; role must be admin or user')
        if not PASSWORD_RULE.match(body['password']):
            return e422('password must be at least 12 characters with an uppercase letter, a lowercase letter, a number and a special character')
        if not isinstance(body['locationIds'], list) or any(lid not in LOCATIONS or LOCATIONS[lid]['companyId'] != COMPANY for lid in body['locationIds']):
            return e400('locationIds contain a location that does not belong to this agency')
        email = body['email'].lower()
        if FAIL['user_503'].get(email):
            FAIL['user_503'][email] -= 1
            return 503, {'statusCode': 503, 'message': 'Service Unavailable'}
        if any(u['email'].lower() == email for u in USERS.values()):
            return e400('A user with this email already exists')     # assumption: the exact message is not documented
        uid = add_user(body['firstName'], body['lastName'], email, body['locationIds'])
        USERS[uid]['phone'] = body.get('phone', '')
        USERS[uid]['_password'] = body['password']
        return 201, {k: v for k, v in USERS[uid].items() if not k.startswith('_')}

    # ---- the new client's sub-account (token from /oauth/locationToken)
    m = re.fullmatch(r'/locations/([^/]+)/customValues(?:/([^/]+))?', path)
    if m:
        lid = m.group(1)
        err = location_ok(headers, lid)
        if err:
            return err
        l = LOCATIONS[lid]
        if method == 'GET' and not m.group(2) and l.get('_no_push'):
            l['_status_polls'] += 1          # without a status record, the load moves on with every look
        values = l['_values'] if snapshot_ready(l) else []      # the snapshot loads custom values in the background
        if method == 'GET' and not m.group(2):
            return 200, {'customValues': copy.deepcopy(values)}
        if method == 'PUT' and m.group(2):
            err = unknown(body, {'name', 'value'}) or missing(body, ('name',))
            if err:
                return err
            if not isinstance(body.get('value'), str):
                return e422('value must be a string')
            cv = next((v for v in values if v['id'] == m.group(2)), None)
            if not cv:
                return e400('Custom value not found')
            if cv['name'] in FAIL['cv_429_once']:
                FAIL['cv_429_once'].discard(cv['name'])
                return 429, {'statusCode': 429, 'message': 'Too Many Requests'}, {'Retry-After': '1'}
            cv['name'], cv['value'] = body['name'], body['value']
            return 200, {'customValue': copy.deepcopy(cv)}
    if method == 'GET' and path == '/calendars/':
        err = unknown(q, {'locationId', 'groupId', 'showDrafted'}) or missing(q, ('locationId',)) or location_ok(headers, q.get('locationId'))
        if err:
            return err
        l = LOCATIONS[q['locationId']]
        return 200, {'calendars': copy.deepcopy(l['_calendars'] if snapshot_ready(l) else [])}

    # ---- the agency's own sub-account (Private Integration token)
    if path.startswith('/contacts') or path.startswith('/opportunities'):
        err = home_ok(headers)
        if err:
            return err
    if method == 'POST' and path == '/contacts/upsert':
        err = unknown(body, UPSERT_KEYS) or missing(body, ('locationId',))
        if err:
            return err
        if body['locationId'] != HOME:
            return e403()
        if 'phone' in body and not re.fullmatch(r'\+\d{8,15}', body['phone']):
            return e422('phone must be a valid phone number')
        match = next((c for c in CONTACTS.values() if body.get('email') and c['email'].lower() == body['email'].lower()), None) \
            or next((c for c in CONTACTS.values() if body.get('phone') and c['phone'] == body['phone']), None)
        new = match is None
        cid = add_contact(body.get('firstName', ''), body.get('lastName', ''), body.get('email', ''), body.get('phone', '')) if new else match['id']
        c = CONTACTS[cid]
        for k, v in body.items():
            if k not in ('locationId', 'tags'):
                c[k] = v
        if 'tags' in body:
            c['tags'] = list(body['tags'])       # the docs: tags in an upsert overwrite all current tags
        return 200, {'new': new, 'contact': copy.deepcopy(c), 'traceId': 'trace-' + uuid.uuid4().hex[:8]}
    m = re.fullmatch(r'/contacts/([^/]+)/(notes|tags)', path)
    if m:
        c = CONTACTS.get(m.group(1))
        if not c:
            return e400('Contact not found')
        if m.group(2) == 'notes' and method == 'GET':
            return 200, {'notes': copy.deepcopy(NOTES.get(c['id'], []))}
        if m.group(2) == 'notes' and method == 'POST':
            err = unknown(body, {'body', 'userId', 'title', 'color', 'pinned'}) or missing(body, ('body',))
            if err:
                return err
            note = {'id': next_id('note'), 'body': body['body'], 'userId': body.get('userId', ''), 'dateAdded': now_iso(), 'contactId': c['id']}
            NOTES.setdefault(c['id'], []).append(note)
            if c['id'] in FAIL['note_lost']:
                FAIL['note_lost'].discard(c['id'])
                return 504, {'statusCode': 504, 'message': 'Gateway Timeout'}
            return 201, {'note': copy.deepcopy(note)}
        if m.group(2) == 'tags' and method == 'POST':
            err = unknown(body, {'tags'}) or missing(body, ('tags',))
            if err:
                return err
            if not all(isinstance(t, str) and t.strip() for t in body['tags']):
                return e422('tags must be an array of strings')
            for t in body['tags']:
                if t not in c['tags']:
                    c['tags'].append(t)
            TAG_EVENTS.append((time.time(), c['id'], list(body['tags'])))
            return 201, {'tags': list(c['tags'])}
    if method == 'GET' and path == '/opportunities/search':
        err = unknown(q, OPP_SEARCH_PARAMS) or missing(q, ('location_id',))
        if err:
            return err
        if q['location_id'] != HOME:
            return e403()
        if q.get('status', 'all') not in ('open', 'won', 'lost', 'abandoned', 'all') or not 1 <= int(q.get('limit', '20')) <= 100:
            return e422('status must be open, won, lost, abandoned or all; limit must be from 1 to 100')
        rows = [o for o in OPPORTUNITIES.values() if (not q.get('contact_id') or o['contactId'] == q['contact_id'])
                and (not q.get('pipeline_id') or o['pipelineId'] == q['pipeline_id']) and q.get('status', 'all') in ('all', o['status'])]
        return 200, {'opportunities': copy.deepcopy(rows), 'meta': {'total': len(rows), 'currentPage': 1, 'nextPage': None, 'prevPage': None,
                                                                      'nextPageUrl': None, 'startAfterId': None, 'startAfter': None}, 'aggregations': {}}
    if method == 'POST' and path == '/opportunities/':
        err = unknown(body, CREATE_OPP_KEYS) or missing(body, ('pipelineId', 'locationId', 'name', 'status', 'contactId'))
        if err:
            return err
        if body['locationId'] != HOME:
            return e403()
        if body['status'] not in ('open', 'won', 'lost', 'abandoned'):
            return e422('status must be one of open, won, lost, abandoned')
        if body['pipelineId'] not in STAGES or body.get('pipelineStageId') not in STAGES[body['pipelineId']]:
            return e400('Pipeline stage does not belong to the pipeline')
        if body['contactId'] not in CONTACTS:
            return e400('Contact not found')
        if 'monetaryValue' in body and not isinstance(body['monetaryValue'], (int, float)):
            return e422('monetaryValue must be a number')
        # One opportunity per contact per pipeline unless the sub-account allows more
        if any(o['contactId'] == body['contactId'] and o['pipelineId'] == body['pipelineId'] for o in OPPORTUNITIES.values()):
            return e400('Can not create duplicate opportunity for the contact.')
        oid = next_id('opp')
        OPPORTUNITIES[oid] = {'id': oid, 'name': body['name'], 'monetaryValue': body.get('monetaryValue', 0), 'pipelineId': body['pipelineId'],
                              'pipelineStageId': body['pipelineStageId'], 'status': body['status'], 'contactId': body['contactId'], 'locationId': HOME,
                              'source': '', 'createdAt': now_iso(), 'updatedAt': now_iso()}
        return 201, {'opportunity': copy.deepcopy(OPPORTUNITIES[oid])}
    m = re.fullmatch(r'/opportunities/([^/]+)', path)
    if method == 'PUT' and m:
        o = OPPORTUNITIES.get(m.group(1))
        if not o:
            return e400('Opportunity not found')
        err = unknown(body, UPDATE_OPP_KEYS)
        if err:
            return err
        if body.get('pipelineStageId', o['pipelineStageId']) not in STAGES[body.get('pipelineId', o['pipelineId'])]:
            return e400('Pipeline stage does not belong to the pipeline')
        if body.get('status', o['status']) not in ('open', 'won', 'lost', 'abandoned'):
            return e422('status must be one of open, won, lost, abandoned')
        o.update(body)
        o['updatedAt'] = now_iso()
        return 200, {'opportunity': copy.deepcopy(o)}
    return 404, {'statusCode': 404, 'message': f'Cannot {method} {path}'}


# ----------------------------------------------------------------------------------------------------------------------
# Mock HTTP server
# ----------------------------------------------------------------------------------------------------------------------
class Mock(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive, so n8n reuses connections through Docker Desktop's host.docker.internal proxy
    protocol_version = 'HTTP/1.1'
    timeout = 120

    def log_message(self, *args):
        pass

    def _send(self, code, obj, extra_headers=None):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain' if isinstance(obj, str) else 'application/json; charset=utf-8')
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urllib.parse.urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        path = url.path
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        service = 'alert' if path.startswith('/slack/errors') else 'slack' if path.startswith('/slack') else 'ghl' if path.startswith('/ghl/') else 'unknown'
        body, form = None, {}
        if raw and headers.get('content-type', '').startswith('application/x-www-form-urlencoded'):
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode(), keep_blank_values=True).items()}
        elif raw:
            try:
                body = json.loads(raw)
            except ValueError:
                return self._send(400, {'statusCode': 400, 'message': 'Body is not valid JSON'})
        extra = None
        with LOCK:
            if service in ('slack', 'alert'):
                ok = (method == 'POST' and str(headers.get('content-type', '')).startswith('application/json') and isinstance(body, dict)
                      and isinstance(body.get('text'), str) and body['text'].strip() and not set(body) - {'text', 'blocks', 'attachments', 'thread_ts', 'mrkdwn'})
                status, resp = (200, 'ok') if ok else (400, 'invalid_payload' if isinstance(body, dict) and body.get('text') else 'no_text')
                if ok and service == 'slack' and any(s in body['text'] for s in FAIL['slack_fail']):
                    status, resp = 500, 'internal_error'
            elif service == 'ghl':
                gpath = path[len('/ghl'):]
                if body is not None and not isinstance(body, dict):
                    out = e422('body must be a JSON object')
                else:
                    out = ghl(method, gpath, query, headers, body if body is not None else {}, form)
                status, resp = out[0], out[1]
                extra = out[2] if len(out) > 2 else None
            else:
                status, resp = 404, {'error': f'Unknown mock path {path}'}
            LOG.append({'service': service, 'method': method, 'path': path[len('/ghl'):] if service == 'ghl' else path, 'query': query,
                        'headers': headers, 'body': body if body is not None else (form or None), 'status': status, 'response': resp, 't': time.time()})
        return self._send(status, resp, extra)

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')


CONNECTION_ERRORS = []


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        CONNECTION_ERRORS.append(repr(sys.exc_info()[1]))


# ----------------------------------------------------------------------------------------------------------------------
# Stripe: events built and signed like Stripe sends them (all data made up)
# ----------------------------------------------------------------------------------------------------------------------
def session(cs, plan, business, name, email, phone, line1, city, state, postal, website='', payment_status='paid', amount=150000, business_field=True):
    fields = []
    if business_field:
        fields.append({'key': 'businessname', 'label': {'custom': 'Business name', 'type': 'custom'}, 'optional': False, 'type': 'text',
                       'text': {'default_value': None, 'maximum_length': None, 'minimum_length': None, 'value': business}})
    fields.append({'key': 'website', 'label': {'custom': 'Website (optional)', 'type': 'custom'}, 'optional': True, 'type': 'text',
                   'text': {'default_value': None, 'maximum_length': None, 'minimum_length': None, 'value': website or None}})
    created = int(time.time()) - 60
    return {
        'id': cs, 'object': 'checkout.session', 'adaptive_pricing': {'enabled': False}, 'after_expiration': None, 'allow_promotion_codes': False,
        'amount_subtotal': amount, 'amount_total': amount, 'automatic_tax': {'enabled': False, 'liability': None, 'status': None},
        'billing_address_collection': 'required', 'cancel_url': None, 'client_reference_id': None, 'collected_information': {'shipping_details': None},
        'consent': None, 'consent_collection': None, 'created': created, 'currency': 'usd', 'custom_fields': fields,
        'custom_text': {'after_submit': None, 'shipping_address': None, 'submit': None, 'terms_of_service_acceptance': None},
        'customer': 'cus_' + cs[-12:], 'customer_creation': 'always',
        'customer_details': {'address': {'city': city, 'country': 'US', 'line1': line1, 'line2': None, 'postal_code': postal, 'state': state},
                             'business_name': None, 'email': email, 'individual_name': None, 'name': name, 'phone': phone, 'tax_exempt': 'none', 'tax_ids': []},
        'customer_email': None, 'expires_at': created + 86400, 'invoice': None, 'invoice_creation': {'enabled': False}, 'livemode': False,
        'locale': None, 'metadata': {'onboarding_plan': plan} if plan else {}, 'mode': 'payment', 'payment_intent': 'pi_' + cs[-12:],
        'payment_link': 'plink_brightline' + plan if plan else None, 'payment_method_collection': 'if_required', 'payment_method_types': ['card'],
        'payment_status': payment_status, 'phone_number_collection': {'enabled': True}, 'recovered_from': None, 'shipping_address_collection': None,
        'status': 'complete', 'submit_type': None, 'subscription': None, 'success_url': 'https://brightlinegrowth.example.com/welcome',
        'total_details': {'amount_discount': 0, 'amount_shipping': 0, 'amount_tax': 0}, 'ui_mode': 'hosted', 'url': None,
    }


def event(evt, obj, etype='checkout.session.completed'):
    return {'id': evt, 'object': 'event', 'api_version': '2026-09-30.endive', 'created': int(time.time()), 'data': {'object': obj},
            'livemode': False, 'pending_webhooks': 1, 'request': {'id': None, 'idempotency_key': None}, 'type': etype}


def stripe_body(evt):
    return json.dumps(evt, indent=2, ensure_ascii=False).encode('utf-8')      # Stripe sends pretty printed UTF-8 JSON


def stripe_sig(body, ts=None, secrets=(STRIPE_SECRET,)):
    ts = int(time.time()) if ts is None else ts
    sigs = ','.join('v1=' + hmac.new(s.encode(), f'{ts}.'.encode() + body, hashlib.sha256).hexdigest() for s in secrets)
    return f't={ts},{sigs},v0={hashlib.sha256(uuid.uuid4().bytes).hexdigest()}'   # test events also carry a fake v0


# ----------------------------------------------------------------------------------------------------------------------
# n8n helpers
# ----------------------------------------------------------------------------------------------------------------------
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def test_copy(env, suffix=''):
    """The workflow with $env values replaced by test values, the GoHighLevel host pointed at the mock, the n8n
    HighLevel OAuth2 credential replaced by a fixed bearer token the mock checks, and test webhook paths."""
    wf = json.loads(WORKFLOW.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: json.dumps(env.get(m.group(1), '')), value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    paths = {'Stripe Webhook': 'stripe', 'Retry Onboarding': 'retry', 'GHL Status': 'status'}
    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        p = node['parameters']
        if p.get('authentication') == 'predefinedCredentialType':
            assert p.pop('nodeCredentialType') == 'highLevelOAuth2Api'
            p['authentication'] = 'none'
            node.pop('credentials', None)
            p['sendHeaders'] = True
            p.setdefault('headerParameters', {'parameters': []})['parameters'].insert(0, {'name': 'Authorization', 'value': 'Bearer ' + AGENCY_TOKEN})
        if 'webhookId' in node:
            node['webhookId'] = str(uuid.uuid4())
        if node['type'] == 'n8n-nodes-base.webhook':
            p['path'] = f"{SLUG}-e2e-{paths[node['name']]}{suffix}"
        if node['type'] == 'n8n-nodes-base.scheduleTrigger':
            p['rule'] = {'interval': [{'field': 'minutes', 'minutesInterval': 1}]}   # so the test does not wait 15 minutes
    return wf


def import_active(env, suffix, error_wid):
    wf = test_copy(env, suffix)
    settings = {**wf.get('settings', {}), 'errorWorkflow': error_wid}
    wid = api('POST', '/api/v1/workflows', {'name': TEMP_NAME, 'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': settings})['id']
    api('POST', f'/api/v1/workflows/{wid}/activate')
    time.sleep(2)
    return wid


def import_error_workflow():
    wf = json.loads((WORKFLOW.parent / 'error-alert-slack.json').read_text(encoding='utf-8'))
    for node in wf['nodes']:
        node['parameters'] = json.loads(json.dumps(node.get('parameters', {})).replace('$env.SLACK_WEBHOOK_URL', json.dumps(MOCK + '/slack/errors').replace('"', '\\"')))
    wid = api('POST', '/api/v1/workflows', {'name': TEMP_NAME, 'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': wf.get('settings', {})})['id']
    api('POST', f'/api/v1/workflows/{wid}/activate')
    return wid


def remove(wid):
    try:
        api('POST', f'/api/v1/workflows/{wid}/deactivate')
    finally:
        api('DELETE', f'/api/v1/workflows/{wid}')


def remove_leftovers():
    cursor, found = None, []
    while True:
        page = api('GET', '/api/v1/workflows?limit=250' + (f'&cursor={cursor}' if cursor else ''))
        found += [w['id'] for w in page.get('data', []) if w.get('name') == TEMP_NAME]
        cursor = page.get('nextCursor')
        if not cursor:
            break
    for wid in found:
        remove(wid)
    tables = [t['id'] for t in api('GET', '/api/v1/data-tables?limit=250').get('data', []) if t.get('name') == TABLE_NAME]
    for tid in tables:
        api('DELETE', f'/api/v1/data-tables/{tid}')
    return len(found), len(tables)


def create_table():
    return api('POST', '/api/v1/data-tables', {'name': TABLE_NAME, 'columns': [{'name': n, 'type': t} for n, t in LEDGER_COLUMNS]})['id']


def ledger(tid, with_lock=False):
    rows, cursor = [], None
    while True:
        page = api('GET', f'/api/v1/data-tables/{tid}/rows?limit=250' + (f'&cursor={cursor}' if cursor else ''))
        rows += page.get('data', [])
        cursor = page.get('nextCursor')
        if not cursor:
            break
    return {r['key']: r for r in rows if with_lock or r['key'] != LOCK_KEY}


def ledger_update(tid, key, data):
    api('PATCH', f'/api/v1/data-tables/{tid}/rows/update', {'filter': {'type': 'and', 'filters': [{'columnName': 'key', 'condition': 'eq', 'value': key}]},
                                                             'data': data, 'returnData': False})


def steps_of(tid, evt):
    row = ledger(tid).get(evt) or {}
    try:
        return json.loads(row.get('steps') or '{}')
    except ValueError:
        return {}


def alerts_for(ids, timeout=90):
    end = time.time() + timeout
    while True:
        got = [e for e in LOG if e['service'] == 'alert']
        linked = {i: [e for e in got if re.search(rf'/executions/{i}(?!\d)', e['body']['text'])] for i in ids}
        if all(linked.values()) or time.time() > end:
            return linked, got
        time.sleep(1)


class Runner:
    def __init__(self, wid, suffix=''):
        self.wid, self.suffix, self.sent, self.results, self.mark = wid, suffix, 0, [], 0
        self.ids = {}       # name -> execution id of runs that must end as failed

    def executions(self):
        out, cursor = [], None
        while True:
            page = api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250' + (f'&cursor={cursor}' if cursor else ''))
            out += page.get('data', [])
            cursor = page.get('nextCursor')
            if not cursor:
                return out

    def wait(self, count=1, timeout=240):
        end = time.time() + timeout
        while time.time() < end:
            done = [e for e in self.executions() if e.get('mode') == 'webhook' and e.get('stoppedAt')]
            if len(done) >= self.sent:
                return sorted(done, key=lambda e: int(e['id']))[-count:]
            time.sleep(0.4)
        raise TimeoutError('the workflow did not finish in time')

    def send(self, which, data, headers):
        req = urllib.request.Request(f'{N8N}/webhook/{SLUG}-e2e-{which}{self.suffix}', method='POST', data=data, headers=headers)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status, text = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode()
        return status, text, t0, time.time()

    def post(self, which, data, headers):
        """Returns (status, parsed answer, seconds until the answer, time the answer arrived, the finished execution)."""
        self.mark = len(LOG)
        status, text, t0, t1 = self.send(which, data, headers)
        self.sent += 1
        ex = self.wait()[0]
        try:
            answer = json.loads(text)
        except ValueError:
            answer = text
        return status, answer, t1 - t0, t1, ex

    def stripe(self, evt, **sig):
        body = stripe_body(evt)
        return self.post('stripe', body, {'Content-Type': 'application/json; charset=utf-8', 'User-Agent': 'Stripe/1.0 (+https://stripe.com/docs/webhooks)',
                                          'Stripe-Signature': stripe_sig(body, **sig)})

    def retry(self, evt_id, secret=ENV['RETRY_SECRET'], **extra):
        headers = {'Content-Type': 'application/json'}
        if secret is not None:
            headers['X-Run-Secret'] = secret
        return self.post('retry', json.dumps({'eventId': evt_id, **extra}).encode(), headers)

    def status_call(self, payload, secret=ENV['GHL_STATUS_SECRET'], form=False):
        # form=True: the Custom Webhook action's POST event sends its key and value pairs as a form-style payload
        headers = {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json'}
        if secret:
            headers['X-Onboarding-Secret'] = secret
        data = urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
        return self.post('status', data, headers)

    def stopped_at(self, ex):
        data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
        result = data.get('resultData') or {}
        return result.get('lastNodeExecuted'), (result.get('error') or {}).get('message', '')

    def new(self, service=None, method=None, path=None):
        out = []
        for e in LOG[self.mark:]:
            if e['service'] == 'alert' and service != 'alert':
                continue
            if service and e['service'] != service or method and e['method'] != method:
                continue
            if path and not re.fullmatch(path, e['path']):
                continue
            out.append(e)
        return out

    def check(self, scenario, label, ok, detail=''):
        self.results.append((scenario, label, bool(ok), detail))
        if os.environ.get('VERBOSE'):
            print(f"{'PASS' if ok else 'FAIL'}  {scenario}: {label}  ({detail})", flush=True)


def calls(entries):
    """The GoHighLevel calls of a run as short names, in order."""
    names = []
    for e in entries:
        if e['service'] == 'slack':
            names.append('slack')
            continue
        if e['service'] != 'ghl':
            continue
        m, p = e['method'], e['path']
        name = {('GET', '/locations/search'): 'findLocation' if 'email' in e['query'] else 'listLocations', ('POST', '/locations/'): 'createLocation',
                ('GET', '/oauth/installedLocations'): 'appInstalled', ('POST', '/oauth/locationToken'): 'locationToken', ('GET', '/users/search'): 'findUser',
                ('POST', '/users/'): 'createUser', ('GET', '/calendars/'): 'listCalendars', ('POST', '/contacts/upsert'): 'upsertContact',
                ('GET', '/opportunities/search'): 'findOpportunity', ('POST', '/opportunities/'): 'createOpportunity'}.get((m, p))
        if not name:
            name = ('snapshotStatus' if p.startswith('/snapshots/') else 'getLocation' if re.fullmatch(r'/locations/[^/]+', p)
                    else 'listCustomValues' if m == 'GET' and p.endswith('/customValues') else 'updateCustomValue' if '/customValues/' in p
                    else 'moveOpportunity' if p.startswith('/opportunities/') else 'findNotes' if m == 'GET' and p.endswith('/notes')
                    else 'addNote' if p.endswith('/notes') else 'addTag' if p.endswith('/tags') else f'{m} {p}')
        names.append(name)
    return names


def squash(names):
    """findLocation, createLocation, snapshotStatus x3 ... (repeats folded)."""
    out = []
    for n in names:
        if out and out[-1][0] == n:
            out[-1][1] += 1
        else:
            out.append([n, 1])
    return [n if c == 1 else f'{n} x{c}' for n, c in out]


def locations_named(name, decoys=False):
    return [l for l in LOCATIONS.values() if l['name'] == name and (decoys or not l['_decoy'])]


def users_with(email):
    return [u for u in USERS.values() if u['email'].lower() == email.lower()]


def slack_texts(entries, *fragments):
    return [e['body']['text'] for e in entries if e['service'] == 'slack' and e['status'] == 200 and all(f in e['body']['text'] for f in fragments)]


def contact_by_email(email):
    return next((c for c in CONTACTS.values() if c['email'].lower() == email.lower()), None)


# ----------------------------------------------------------------------------------------------------------------------
# The clients (all made up: example.com emails, 555 01xx numbers)
# ----------------------------------------------------------------------------------------------------------------------
def client(key, plan, business, name, email, phone, line1, city, state, postal, website='', **kw):
    cs = f'cs_test_{key}{uuid.uuid4().hex[:20]}'
    return {'evt': f'evt_test{key}{uuid.uuid4().hex[:16]}', 'session': session(cs, plan, business, name, email, phone, line1, city, state, postal, website, **kw),
            'business': business, 'email': email, 'name': name}


A = client('a', 'launch', 'Lumière Café', 'Jordan Ellis', 'jordan.ellis@lumierecafe.example.com', '+15550100141', '1200 Market Street, Suite 4',
           'Springfield', 'IL', '62701', 'lumierecafe.example.com')
Q = client('q', 'launch', 'Quarry Lane Dental', 'Riley Chen', 'riley.chen@quarrylanedental.example.com', '+15550100142', '48 Quarry Lane', 'Madison', 'WI', '53703',
           payment_status='unpaid')
B = client('b', 'launch', 'Birchwood Landscaping', 'Sam Porter', 'sam.porter@birchwood.example.com', '+15550100143', '9 Elm Road', 'Boise', 'ID', '83702')
C = client('c', 'launch', 'Copperline Electric', 'Dana Ruiz', 'dana.ruiz@copperline.example.com', '+15550100144', '310 Wire Street', 'Tulsa', 'OK', '74103')
D = client('d', 'launch', 'Dune & Dawn Yoga <b>', 'Alex Moreno', 'alex.moreno@duneanddawn.example.com', '+15550100145', '77 Shore Drive', 'Santa Cruz', 'CA', '95060')
E = client('e', 'launch', 'Evergreen Roofing', 'Casey Nolan', 'casey.nolan@evergreenroofing.example.com', '+15550100146', '5 Pine Way', 'Eugene', 'OR', '97401')
F = client('f', 'launch', 'Foxglove Florist', 'Morgan Blake', 'morgan.blake@foxglove.example.com', '+15550100147', '18 Bloom Street', 'Savannah', 'GA', '31401')
G = client('g', 'launch', 'Granite Peak Fitness', 'Taylor Brooks', 'taylor.brooks@granitepeak.example.com', '+15550100148', '600 Summit Ave', 'Denver', 'CO', '80202')
H = client('h', 'launch', 'Harborview Pet Clinic', 'Jamie Lee', 'jamie.lee@harborviewpets.example.com', '+15550100149', '2 Dock Street', 'Portland', 'ME', '04101')
I = client('i', 'scale', 'Ironwood Auto Repair', 'Chris Patel', 'chris.patel@ironwoodauto.example.com', '+15550100150', '41 Garage Road', 'Dayton', 'OH', '45402', amount=320000)
J = client('j', 'launch', 'Juniper Home Cleaning', 'Avery Quinn', 'avery.quinn@juniperclean.example.com', '+15550100151', '12 Sparkle Court', 'Austin', 'TX', '78701')
K = client('k', 'launch', 'Kestrel Bakery', 'Pat Owner', 'pat.owner@kestrelbakery.example.com', '+15550100152', '3 Oven Lane', 'Burlington', 'VT', '05401')
L = client('l', 'launch', 'Linden Law Group', 'Robin Hale', 'robin.hale@lindenlaw.example.com', '+15550100153', '100 Court Street', 'Albany', 'NY', '12207')
M = client('m', 'launch', 'Maple Ridge Chiropractic', 'Drew Fischer', 'drew.fischer@mapleridgechiro.example.com', '+15550100154', '27 Spine Road', 'Lansing', 'MI', '48933')
N = client('n', 'launch', 'Northside Gym <!here>', 'Kai Morgan', 'kai.morgan@northsidegym.example.com', '+15550100155', '8 Iron Street', 'Fargo', 'ND', '58102')
P = client('p', 'launch', 'Pinecrest Florals', 'Jesse Park', 'jesse.park@pinecrestflorals.example.com', '+15550100157', '64 Fern Avenue', 'Asheville', 'NC', '28801')
O = client('o', 'enterprise', '', 'Lee Sun', 'lee.sun@example.com', '+15550100156', '1 Unknown Way', 'Reno', 'NV', '89501', business_field=False)
R = client('r', 'launch', 'Redwood Plumbing Co', 'Quinn Harper', 'quinn.harper@redwoodplumbing.example.com', '+15550100158', '19 Valve Street', 'Spokane', 'WA', '99201')
S = client('s', 'launch', 'Sterling Bookkeeping', 'Rowan Tate', 'rowan.tate@sterlingbooks.example.com', '+15550100159', '400 Ledger Lane', 'Boulder', 'CO', '80302')
T = client('t', 'launch', 'Tidewater Kayak Tours', 'Noel Rivera', 'noel.rivera@tidewaterkayak.example.com', '+15550100160', '2 Harbor Road', 'Mystic', 'CT', '06355')
U = client('u', 'launch', 'Upland Pottery Studio', 'Blair Moreau', 'blair.moreau@uplandpottery.example.com', '+15550100161', '88 Kiln Street', 'Santa Fe', 'NM', '87501')


def seed():
    add_location('Harbor Dental Studio', 'office@harbordental.example.com', created=time.time() - 86400 * 30)
    add_location('Northwind Partners (other agency)', 'hello@northwindpartners.example.com', company='otherAgency01')
    add_user('Pat', 'Owner', 'pat.owner@kestrelbakery.example.com', [next(iter(LOCATIONS))])      # a user from an earlier project
    # Robin is a returning client: a contact with an old lost opportunity in the onboarding pipeline, and the new client tag
    # from the first onboarding
    rid = add_contact('Robin', 'Hale', 'robin.hale@lindenlaw.example.com', '+15550100153', 'Linden Law Group')
    CONTACTS[rid]['tags'] = ['new-client']
    OPPORTUNITIES['oppLinden2025'] = {'id': 'oppLinden2025', 'name': 'Linden Law Group: Launch onboarding', 'monetaryValue': 1500, 'pipelineId': PIPELINE,
                                      'pipelineStageId': STAGE_KICKOFF, 'status': 'lost', 'contactId': rid, 'locationId': HOME, 'source': '',
                                      'createdAt': '2025-11-02T15:00:00.000Z', 'updatedAt': '2025-12-01T15:00:00.000Z'}
    FAIL['create_delay'][A['business']] = 1.5


HAPPY = ['findLocation', 'createLocation', 'findUser', 'createUser', 'snapshotStatus x3', 'appInstalled x2', 'locationToken', 'listCustomValues',
         'listCalendars', 'updateCustomValue x12', 'upsertContact', 'findOpportunity', 'createOpportunity', 'findNotes', 'addNote', 'addTag', 'slack']


def expected_values(cl, booking):
    s = cl['session']
    d, a = s['customer_details'], s['customer_details']['address']
    website = next((f['text']['value'] for f in s['custom_fields'] if f['key'] == 'website'), None)
    return {'Business Name': cl['business'], 'Business Phone': d['phone'], 'Business Email': d['email'], 'Business Address': a['line1'],
            'Business City': a['city'], 'Business State': a['state'], 'Business Postal Code': a['postal_code'], 'Business Country': 'US',
            'Business Website': ('https://' + website) if website else '', 'Owner First Name': cl['name'].split()[0], 'Owner Name': cl['name'],
            'Booking Link': booking}


def run_main(r, tid):
    # ------------------------------------------------------------------ refused and ignored Stripe calls
    ev = event('evt_testforged01', copy.deepcopy(A['session']))
    status, ans, secs, _, ex = r.stripe(ev, secrets=('whsec_wrong_secret_0000000000000000',))
    node, error = r.stopped_at(ex)
    r.ids['forged'] = ex['id']
    r.check('Stripe signature', 'a delivery signed with another secret is refused with 400, nothing is called, the run fails for an alert',
            status == 400 and ans == {'received': False, 'error': 'invalid signature'} and not r.new('ghl') and not r.new('slack')
            and node == 'Stop: Bad Signature' and 'no v1 signature' in error, f'{status}, ended at {node}')
    r.mark = len(LOG)
    st, text, _, _ = r.send('stripe', stripe_body(ev), {'Content-Type': 'application/json'})
    r.sent += 1
    ex = r.wait()[0]
    node, error = r.stopped_at(ex)
    r.ids['noheader'] = ex['id']
    r.check('Stripe signature', 'a call without a Stripe-Signature header is refused with 400', st == 400 and node == 'Stop: Bad Signature'
            and 'no Stripe-Signature header' in error and not r.new('ghl'), f'{st}, ended at {node}')
    status, ans, secs, _, ex = r.stripe(ev, ts=int(time.time()) - 600)
    node, error = r.stopped_at(ex)
    r.ids['stale'] = ex['id']
    r.check('Stripe signature', 'a correctly signed delivery 10 minutes old (a replay) is refused: outside the 300 second tolerance',
            status == 400 and node == 'Stop: Bad Signature' and 'outside the 300 second tolerance' in error and not r.new('ghl'), f'{status}, {error[:80]}')
    body = stripe_body(ev)
    header = stripe_sig(body)
    r.mark = len(LOG)
    st, text, _, _ = r.send('stripe', body.replace(b'"livemode": false', b'"livemode": true', 1), {'Content-Type': 'application/json', 'Stripe-Signature': header})
    r.sent += 1
    ex = r.wait()[0]
    node, error = r.stopped_at(ex)
    r.ids['changed'] = ex['id']
    r.check('Stripe signature', 'a body changed after signing (one field) is refused: the HMAC covers the raw bytes', st == 400 and node == 'Stop: Bad Signature'
            and not r.new('ghl'), f'{st}, ended at {node}')

    ev = event('evt_testpi000001', {'id': 'pi_test000001', 'object': 'payment_intent', 'amount': 150000, 'currency': 'usd', 'status': 'succeeded'}, 'payment_intent.succeeded')
    status, ans, secs, _, ex = r.stripe(ev)
    node, _ = r.stopped_at(ex)
    r.check('Unused Stripe events', 'payment_intent.succeeded gets 200 at once (so Stripe does not send it again) and nothing else happens',
            status == 200 and ans == {'received': True, 'ignored': 'event type payment_intent.succeeded is not used'} and not r.new('ghl')
            and not ledger(tid) and ex['status'] == 'success', f'{status} {ans}')
    other = copy.deepcopy(A['session'])
    other['id'], other['metadata'] = 'cs_test_tshirtorder0001', {}
    status, ans, secs, _, ex = r.stripe(event('evt_testtshirt001', other))
    r.check('Unused Stripe events', 'a checkout without onboarding_plan metadata (another product) gets 200 and is left alone',
            status == 200 and 'not an onboarding purchase' in ans.get('ignored', '') and not r.new('ghl') and not ledger(tid) and ex['status'] == 'success', str(ans))
    thin = {'id': 'evt_testthin00001', 'object': 'v2.core.event', 'type': 'v1.checkout.session.completed', 'created': now_iso(), 'livemode': False,
            'related_object': {'id': A['session']['id'], 'type': 'checkout.session', 'url': '/v1/checkout/sessions/' + A['session']['id']}}
    status, ans, secs, _, ex = r.stripe(thin)
    node, error = r.stopped_at(ex)
    r.ids['thin'] = ex['id']
    r.check('Unused Stripe events', 'a thin event (no session in it) gets 200, nobody is onboarded, and the alert says to switch the destination to snapshot payloads',
            status == 200 and node == 'Stop: Event Without Session' and 'payload style to snapshot' in error and not r.new('ghl'), f'{status}, ended at {node}')

    # ------------------------------------------------------------------ a delayed payment: completed (unpaid), then async_payment_succeeded
    status, ans, secs, _, ex = r.stripe(event(Q['evt'], Q['session']))
    r.check('Delayed payment', 'checkout.session.completed with payment_status unpaid gets 200 and waits; nothing is created',
            status == 200 and 'waiting for checkout.session.async_payment_succeeded' in ans.get('ignored', '') and not r.new('ghl') and not ledger(tid), str(ans))
    paid = copy.deepcopy(Q['session'])
    paid['payment_status'] = 'paid'
    Q['evt'] = 'evt_testq' + uuid.uuid4().hex[:16]
    status, ans, secs, _, ex = r.stripe(event(Q['evt'], paid, 'checkout.session.async_payment_succeeded'))
    node, _ = r.stopped_at(ex)
    r.check('Delayed payment', 'checkout.session.async_payment_succeeded onboards the client: one sub-account, one user, ledger done',
            status == 200 and len(locations_named(Q['business'])) == 1 and len(users_with(Q['email'])) == 1
            and ledger(tid)[Q['evt']]['status'] == 'done' and node == 'Run Failed?' and ex['status'] == 'success', f'ended at {node}')

    # ------------------------------------------------------------------ A: the full onboarding
    FAIL['cv_429_once'].add('Business Phone')
    status, ans, secs, answered, ex = r.stripe(event(A['evt'], A['session']))
    node, error = r.stopped_at(ex)
    flow = r.new()
    names = calls(flow)
    create = [e for e in flow if e['service'] == 'ghl' and e['method'] == 'POST' and e['path'] == '/locations/']
    r.check('Onboarding (Launch plan)', 'Stripe gets 200 {"received": true} as soon as the event is in the ledger, before the sub-account create has finished',
            status == 200 and ans == {'received': True} and create and answered < create[0]['t'], f'answered in {secs:.2f} s')
    r.check('Onboarding (Launch plan)', 'the calls in order: look up, create the sub-account, look up and create the user, wait for the snapshot and the app, '
            'sub-account token, custom values, calendars, custom value updates, contact, opportunity, note, tag, Slack',
            squash(names) == HAPPY, ', '.join(squash(names)))
    loc = locations_named(A['business'])
    lid = loc[0]['id'] if loc else ''
    body = create[0]['body'] if create else {}
    r.check('Onboarding (Launch plan)', 'the sub-account is created from the Launch snapshot with the checkout details (name, address, phone, website, timezone, prospect)',
            body == {'name': A['business'], 'companyId': COMPANY, 'snapshotId': 'snapLaunch01', 'timezone': 'America/Chicago', 'phone': '+15550100141',
                     'address': '1200 Market Street, Suite 4', 'city': 'Springfield', 'state': 'IL', 'postalCode': '62701', 'country': 'US',
                     'website': 'https://lumierecafe.example.com', 'prospectInfo': {'firstName': 'Jordan', 'lastName': 'Ellis', 'email': A['email']}}
            and len(loc) == 1 and create[0]['status'] == 200, json.dumps(body, ensure_ascii=True)[:200])
    find = [e for e in flow if e['service'] == 'ghl' and e['path'] == '/locations/search']
    r.check('Onboarding (Launch plan)', 'before creating, the agency sub-accounts are searched by the client email (agency OAuth token, Version 2021-07-28)',
            find and find[0]['query'] == {'companyId': COMPANY, 'email': A['email'], 'limit': '100'} and find[0]['headers'].get('authorization') == 'Bearer ' + AGENCY_TOKEN
            and find[0]['t'] < create[0]['t'], json.dumps(find[0]['query']) if find else '')
    ucall = [e for e in flow if e['service'] == 'ghl' and e['method'] == 'POST' and e['path'] == '/users/']
    ub = dict(ucall[0]['body']) if ucall else {}
    pw = ub.pop('password', '')
    r.check('Onboarding (Launch plan)', 'the admin user: type account, role admin, only the new sub-account, a random password that meets the rules',
            ub == {'companyId': COMPANY, 'firstName': 'Jordan', 'lastName': 'Ellis', 'email': A['email'], 'type': 'account', 'role': 'admin', 'locationIds': [lid],
                   'phone': '+15550100141'} and PASSWORD_RULE.match(pw) and len(users_with(A['email'])) == 1 and ucall[0]['status'] == 201, json.dumps(ub)[:200])
    r.check('Onboarding (Launch plan)', 'the password is never kept: not in the ledger, not in Slack, not in the note',
            pw and pw not in json.dumps(ledger(tid)) and not any(pw in json.dumps(e['body']) for e in flow if e['service'] == 'slack')
            and not any(pw in n['body'] for ns in NOTES.values() for n in ns), '')
    waits = [e for e in flow if e['service'] == 'ghl' and (e['path'].startswith('/snapshots/') or e['path'] == '/oauth/installedLocations')]
    r.check('Onboarding (Launch plan)', 'it waits for the snapshot (processing twice, then completed) and for the agency app on the new sub-account before asking for its token',
            [e['response']['data']['status'] if 'data' in e['response'] else len(e['response']['locations']) for e in waits] == ['processing', 'processing', 'completed', 0, 1]
            and waits[0]['query'] == {'companyId': COMPANY} and waits[3]['query'] == {'companyId': COMPANY, 'appId': APP_ID, 'locationId': lid, 'isInstalled': 'true'},
            json.dumps([e['query'] for e in waits[-1:]]))
    tok = [e for e in flow if e['service'] == 'ghl' and e['path'] == '/oauth/locationToken']
    r.check('Onboarding (Launch plan)', 'the sub-account token: POST /oauth/locationToken, form encoded companyId and locationId',
            tok and tok[0]['body'] == {'companyId': COMPANY, 'locationId': lid} and tok[0]['headers']['content-type'].startswith('application/x-www-form-urlencoded'), '')
    cal_call = [e for e in flow if e['service'] == 'ghl' and e['path'] == '/calendars/']
    cal_id = next((c['id'] for c in loc[0]['_calendars'] if c['name'] == 'Discovery Call'), '') if loc else ''
    r.check('Onboarding (Launch plan)', 'calendars are read with the sub-account token and Version 2021-04-15 to find the Discovery Call calendar',
            cal_call and cal_call[0]['headers'].get('version') == '2021-04-15' and cal_call[0]['headers'].get('authorization') == 'Bearer mock-location-token-' + lid
            and cal_call[0]['query'] == {'locationId': lid}, '')
    booking = 'https://api.leadconnectorhq.com/widget/booking/' + cal_id
    want = expected_values(A, booking)
    have = {v['name']: v['value'] for v in loc[0]['_values']} if loc else {}
    updates = [e for e in flow if e['service'] == 'ghl' and '/customValues/' in e['path']]
    r.check('Onboarding (Launch plan)', 'custom values are matched by name and filled: business name, phone, email, address, city, state, ZIP, website, owner, booking link',
            all(have.get(k) == v for k, v in want.items()), json.dumps({k: have.get(k) for k in want if have.get(k) != want[k]}, ensure_ascii=True)[:300])
    r.check('Onboarding (Launch plan)', 'values that are already right (Business Country = US) are not written, and custom values the workflow does not own stay as the snapshot set them',
            not any(e['body'].get('name') == 'Business Country' for e in updates) and have.get('Review Link') == 'https://g.page/r/your-review-link'
            and have.get('Brand Color') == '#1E6FD9' and all(set(e['body']) == {'name', 'value'} for e in updates), f'{len(updates)} PUT calls')
    phone = [e for e in updates if e['body'].get('name') == 'Business Phone']
    r.check('Onboarding (Launch plan)', 'GoHighLevel 429: a custom value update that gets 429 waits for Retry-After (1 s) and succeeds on the second try',
            [e['status'] for e in phone] == [429, 200] and phone[1]['t'] - phone[0]['t'] >= 0.9, str([e['status'] for e in phone]))
    contact = contact_by_email(A['email'])
    up = [e for e in flow if e['service'] == 'ghl' and e['path'] == '/contacts/upsert']
    r.check('Onboarding (Launch plan)', "the client is added to the agency's own sub-account with GHL_HOME_TOKEN: name, email, phone, company, address, website, source",
            up and up[0]['body'] == {'locationId': HOME, 'firstName': 'Jordan', 'lastName': 'Ellis', 'name': 'Jordan Ellis', 'email': A['email'], 'companyName': A['business'],
                                     'source': 'Stripe onboarding checkout', 'phone': '+15550100141', 'address1': '1200 Market Street, Suite 4', 'city': 'Springfield',
                                     'state': 'IL', 'postalCode': '62701', 'country': 'US', 'website': 'https://lumierecafe.example.com'}
            and up[0]['headers'].get('authorization') == 'Bearer ' + HOME_TOKEN and contact, json.dumps(up[0]['body'], ensure_ascii=True)[:200] if up else '')
    opps = [o for o in OPPORTUNITIES.values() if contact and o['contactId'] == contact['id']]
    r.check('Onboarding (Launch plan)', 'an opportunity in the Onboarding stage with the amount paid (1500) and a clear name',
            len(opps) == 1 and opps[0]['pipelineStageId'] == STAGE_ONBOARDING and opps[0]['status'] == 'open' and opps[0]['monetaryValue'] == 1500
            and opps[0]['name'] == f"{A['business']}: Launch onboarding", json.dumps(opps, ensure_ascii=True)[:200])
    notes = NOTES.get(contact['id'], []) if contact else []
    r.check('Onboarding (Launch plan)', 'a note with the plan, the amount, the sub-account link, the admin user and the event marker',
            len(notes) == 1 and f'[onboarding {A["evt"]}]' in notes[0]['body'] and 'Plan: Launch (USD 1,500.00)' in notes[0]['body']
            and f'https://app.gohighlevel.com/v2/location/{lid}/dashboard' in notes[0]['body'] and 'Custom values: 11 filled' in notes[0]['body'], notes[0]['body'][:120] if notes else '')
    tag_t = [t for t, cid, tags in TAG_EVENTS if contact and cid == contact['id']]
    home_calls = [e for e in flow if e['service'] == 'ghl' and (e['path'].startswith('/contacts') or e['path'].startswith('/opportunities'))]
    r.check('Onboarding (Launch plan)', 'the new-client tag that starts the native workflow is added last, after the opportunity and the note exist',
            contact and contact['tags'] == ['new-client'] and len(tag_t) == 1 and home_calls[-1]['path'].endswith('/tags'), '')
    sl = slack_texts(flow, 'New client onboarded')
    r.check('Onboarding (Launch plan)', 'Slack summary with the plan, the amount, links to the sub-account and the contact, and what happens next',
            len(sl) == 1 and sl[0].startswith(':tada: *New client onboarded*: Lumière Café (Launch plan, USD 1,500.00)')
            and f'<https://app.gohighlevel.com/v2/location/{lid}/dashboard|' in sl[0] and f"/v2/location/{HOME}/contacts/detail/{contact['id'] if contact else ''}|contact>" in sl[0]
            and 'the welcome email and the onboarding tasks start now' in sl[0] and 'Custom values: 11 filled, 1 already right' in sl[0], sl[0].splitlines()[0] if sl else 'no message')
    row = ledger(tid).get(A['evt'], {})
    st = steps_of(tid, A['evt'])
    r.check('Onboarding (Launch plan)', 'the ledger row: done, every step recorded with the IDs it created, one attempt',
            row.get('status') == 'done' and row.get('locationId') == lid and row.get('userId') == users_with(A['email'])[0]['id'] and row.get('contactId') == contact['id']
            and row.get('opportunityId') == opps[0]['id'] and row.get('attempts') == 1 and all(st.get(s, {}).get('done') for s in
            ('subaccount', 'user', 'customValues', 'contact', 'opportunity', 'note', 'tag', 'slack')) and row.get('sessionId') == A['session']['id'], row.get('status'))
    lock = ledger(tid, with_lock=True).get(LOCK_KEY, {})
    r.check('Onboarding (Launch plan)', 'the run took the lock and released it at the end', lock.get('status') == 'lock' and lock.get('attempts') == 0 and lock.get('note') == 'free',
            json.dumps({k: lock.get(k) for k in ('attempts', 'note')}))
    r.check('Onboarding (Launch plan)', 'the run ends as a success (no alert)', node == 'Run Failed?' and ex['status'] == 'success', f'ended at {node}')

    # ------------------------------------------------------------------ duplicates
    status, ans, secs, _, ex = r.stripe(event(A['evt'], A['session']))
    r.check('Duplicate delivery', 'the same event again (Stripe can send one twice) gets 200 with duplicate: true; nothing is called',
            status == 200 and ans == {'received': True, 'duplicate': True} and not r.new('ghl') and not r.new('slack') and ex['status'] == 'success', str(ans))
    status, ans, secs, _, ex = r.stripe(event('evt_testasync' + uuid.uuid4().hex[:12], A['session'], 'checkout.session.async_payment_succeeded'))
    r.check('Duplicate delivery', 'another event for a session that is already onboarded is a duplicate too (one onboarding per Checkout Session)',
            status == 200 and ans == {'received': True, 'duplicate': True} and not r.new('ghl') and len(locations_named(A['business'])) == 1, str(ans))

    # ------------------------------------------------------------------ B: two deliveries of one event at the same moment
    evb = event(B['evt'], B['session'])
    results = []
    body = stripe_body(evb)

    def deliver():
        results.append(r.send('stripe', body, {'Content-Type': 'application/json', 'Stripe-Signature': stripe_sig(body)}))
    r.mark = len(LOG)
    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    r.sent += 2
    r.wait(2)
    codes = sorted(s for s, *_ in results)
    r.check('Two deliveries at once', 'one onboards, the other waits LOCK_WAIT_SECONDS for the lock and gets 503 (Stripe sends it again later): one sub-account, one user',
            codes == [200, 503] and len(locations_named(B['business'])) == 1 and len(users_with(B['email'])) == 1
            and len([e for e in LOG[r.mark:] if e['service'] == 'ghl' and e['method'] == 'POST' and e['path'] == '/locations/']) == 1, f'codes {codes}')
    status, ans, secs, _, ex = r.stripe(evb)
    r.check('Two deliveries at once', "Stripe's later resend of the refused delivery is answered as a duplicate", status == 200 and ans.get('duplicate') is True and not r.new('ghl'), str(ans))

    # ------------------------------------------------------------------ C: a lock row nobody can take, then a lock held by another run
    ledger_update(tid, LOCK_KEY, {'attempts': None, 'note': 'free'})          # "attempts" emptied by hand
    status, ans, secs, _, ex = r.stripe(event(C['evt'], C['session']))
    node, error = r.stopped_at(ex)
    r.ids['lockstuck'] = ex['id']
    r.check('Run lock', 'a lock row that can never be taken (attempts emptied by hand) ends after about LOCK_WAIT_SECONDS + 10 seconds with 500 and an alert instead of looping',
            status == 500 and ans == {'received': False, 'error': 'ledger unavailable'} and 12 <= secs < 45 and node == 'Stop: Ledger Unavailable'
            and 'could not be taken' in error and not r.new('ghl') and C['evt'] not in ledger(tid), f'{status} after {secs:.1f} s, ended at {node}')
    ledger_update(tid, LOCK_KEY, {'attempts': int(time.time() * 1000) + 600000, 'note': '999999'})
    status, ans, secs, _, ex = r.stripe(event(C['evt'], C['session']))
    node, _ = r.stopped_at(ex)
    r.check('Run lock', 'while another run holds the lock, a delivery waits about LOCK_WAIT_SECONDS, then gets 503; nothing is called or recorded',
            status == 503 and secs >= 2.5 and not r.new('ghl') and C['evt'] not in ledger(tid) and node == 'Respond: Busy' and ex['status'] == 'success', f'{status} after {secs:.1f} s')
    ledger_update(tid, LOCK_KEY, {'attempts': int(time.time() * 1000) - 1000, 'note': '999999'})
    status, ans, secs, _, ex = r.stripe(event(C['evt'], C['session']))
    r.check('Run lock', "a lock that ran out does not block: Stripe's resend onboards the client", status == 200 and ledger(tid)[C['evt']]['status'] == 'done'
            and len(locations_named(C['business'])) == 1, ledger(tid)[C['evt']]['status'])

    # ------------------------------------------------------------------ D: the user create keeps failing, then a retry
    FAIL['user_503'][D['email']] = 3
    status, ans, secs, _, ex = r.stripe(event(D['evt'], D['session']))
    node, error = r.stopped_at(ex)
    r.ids['user503'] = ex['id']
    flow = r.new()
    names = squash(calls(flow))
    row = ledger(tid).get(D['evt'], {})
    st = steps_of(tid, D['evt'])
    dl = locations_named(D['business'])
    r.check('Partial failure', 'the user create gets 503 three times; after each, the user is looked up again before another create; the run stops there',
            names == ['findLocation', 'createLocation', 'findUser', 'createUser', 'findUser', 'createUser', 'findUser', 'createUser'] and not users_with(D['email']), ', '.join(names))
    r.check('Partial failure', 'the ledger keeps the finished step and its ID (sub-account) and marks the event failed; nothing in the agency CRM yet',
            row.get('status') == 'failed' and st.get('subaccount', {}).get('done') and st['subaccount']['locationId'] == dl[0]['id'] and not st.get('user', {}).get('done')
            and not contact_by_email(D['email']) and not slack_texts(flow, 'Dune'), row.get('status'))
    r.check('Partial failure', 'the alert says exactly what exists (with IDs) and what is left to do by hand, with the values, and how to retry',
            node == 'Stop: Onboarding Not Finished' and 'stopped at "create the admin user": GoHighLevel did not answer (503) after 3 tries' in error
            and f'Done: sub-account "Dune &amp; Dawn Yoga &lt;b&gt;" (ID {dl[0]["id"]})' in error and 'add Alex Moreno &lt;alex.moreno@duneanddawn.example.com&gt; as an admin user' in error
            and 'Business Phone = +15550100145' in error and 'add the tag new-client to the contact' in error and f'{{"eventId": "{D["evt"]}"}}' in error, error[:160])
    status, ans, secs, _, ex = r.retry(D['evt'])
    flow = r.new()
    node, _ = r.stopped_at(ex)
    row = ledger(tid).get(D['evt'], {})
    r.check('Retry', 'the Retry webhook answers 202 with the step it continues from', status == 202 and ans == {'started': True, 'continuesWith': 'create the admin user'}, str(ans))
    r.check('Retry', 'the retry continues where the run stopped: no second sub-account, the user is created, then the rest; ledger done after 2 attempts',
            not [e for e in flow if e['service'] == 'ghl' and e['method'] == 'POST' and e['path'] == '/locations/'] and calls(flow)[:2] == ['findUser', 'createUser']
            and len(locations_named(D['business'])) == 1 and len(users_with(D['email'])) == 1 and row.get('status') == 'done' and row.get('attempts') == 2
            and node == 'Run Failed?', ', '.join(squash(calls(flow)))[:160])

    # ------------------------------------------------------------------ E: the sub-account create answer is lost
    FAIL['create_lost'].add(E['business'])
    status, ans, secs, _, ex = r.stripe(event(E['evt'], E['session']))
    flow = r.new()
    names = squash(calls(flow))
    r.check('Lost answer: sub-account', 'the create saved the sub-account but answered 504; the workflow looks it up by email, finds it and goes on: still one sub-account',
            names[:4] == ['findLocation', 'createLocation', 'findLocation', 'findUser'] and len(locations_named(E['business'])) == 1
            and ledger(tid)[E['evt']]['status'] == 'done' and steps_of(tid, E['evt'])['subaccount']['adopted'] is True, ', '.join(names[:5]))

    # ------------------------------------------------------------------ F: created under another name (search cannot find it), then adopted by ID
    FAIL['create_hidden'].add(F['business'])
    status, ans, secs, _, ex = r.stripe(event(F['evt'], F['session']))
    node, error = r.stopped_at(ex)
    r.ids['hidden'] = ex['id']
    flow = r.new()
    hidden = locations_named(F['business'] + ' (copy)')
    r.check('Lost answer: sub-account not found', 'a 502 after the create, and neither the email search nor the newest sub-accounts find it: the workflow does not create again and asks a person',
            squash(calls(flow)) == ['findLocation', 'createLocation', 'findLocation', 'listLocations'] and len(hidden) == 1 and not locations_named(F['business'])
            and node == 'Stop: Onboarding Not Finished' and 'its answer never arrived' in error and '"subAccountId"' in error and '"createSubAccount": true' in error, error[:140])
    status, ans, secs, _, ex = r.retry(F['evt'])
    node, error = r.stopped_at(ex)
    r.ids['hidden2'] = ex['id']
    r.check('Lost answer: sub-account not found', 'a plain retry still does not create one (it may exist), and asks again',
            status == 202 and not [e for e in r.new('ghl') if e['method'] == 'POST' and e['path'] == '/locations/'] and node == 'Stop: Onboarding Not Finished', f'ended at {node}')
    status, ans, secs, _, ex = r.retry(F['evt'], subAccountId=hidden[0]['id'] if hidden else 'x')
    flow = r.new()
    r.check('Lost answer: sub-account not found', 'a retry with the sub-account ID a person found uses it (checked to belong to the agency) and finishes: no second sub-account',
            calls(flow)[:2] == ['getLocation', 'findUser'] and ledger(tid)[F['evt']]['status'] == 'done' and ledger(tid)[F['evt']]['locationId'] == hidden[0]['id']
            and not locations_named(F['business']), ', '.join(squash(calls(flow)))[:120])

    # ------------------------------------------------------------------ G: 502 without creating, then the person confirms
    FAIL['create_502'][G['business']] = 1
    status, ans, secs, _, ex = r.stripe(event(G['evt'], G['session']))
    node, error = r.stopped_at(ex)
    r.ids['g502'] = ex['id']
    status, ans, secs, _, ex = r.retry(G['evt'], createSubAccount=True)
    flow = r.new()
    r.check('Lost answer: sub-account not found', 'a retry with createSubAccount: true (the person checked) looks once more, then creates it: one sub-account, done',
            squash(calls(flow))[:4] == ['findLocation', 'listLocations', 'createLocation', 'findUser'] and len(locations_named(G['business'])) == 1
            and ledger(tid)[G['evt']]['status'] == 'done', ', '.join(squash(calls(flow)))[:120])

    # ------------------------------------------------------------------ H: the agency app lacks locations.write
    FAIL['agency_scopes_removed'].add('locations.write')
    status, ans, secs, _, ex = r.stripe(event(H['evt'], H['session']))
    node, error = r.stopped_at(ex)
    r.ids['scope'] = ex['id']
    flow = r.new()
    r.check('Missing scope or plan', 'a 401 "not authorized for this scope" on the create is not retried; the alert names the scope and the Agency Pro plan; nothing exists',
            calls(flow) == ['findLocation', 'createLocation'] and not locations_named(H['business']) and node == 'Stop: Onboarding Not Finished'
            and '(401). The HighLevel agency credential in n8n' in error and 'needs the locations.write scope' in error and 'Agency Pro plan' in error
            and 'Done: nothing yet' in error, error[:150])
    FAIL['agency_scopes_removed'].clear()
    status, ans, secs, _, ex = r.retry(H['evt'])
    r.check('Missing scope or plan', 'after the scope is added, the retry creates the sub-account without asking a person (the refused create made nothing)',
            ledger(tid)[H['evt']]['status'] == 'done' and len(locations_named(H['business'])) == 1, ledger(tid)[H['evt']]['status'])

    # ------------------------------------------------------------------ I: the Scale snapshot has no Booking Link custom value
    status, ans, secs, _, ex = r.stripe(event(I['evt'], I['session']))
    node, error = r.stopped_at(ex)
    r.ids['nobooking'] = ex['id']
    flow = r.new()
    il = locations_named(I['business'])
    cal_i = next((c['id'] for c in il[0]['_calendars'] if c['name'] == 'Discovery Call'), '') if il else ''
    vals = {v['name']: v['value'] for v in il[0]['_values']} if il else {}
    sl = slack_texts(flow, 'Ironwood')
    r.check('Snapshot without a custom value', 'the other 11 values are filled, the missing one does not hold up the welcome: contact, opportunity, note and tag still happen',
            vals.get('Business Name') == I['business'] and 'Booking Link' not in vals and contact_by_email(I['email']) and 'new-client' in contact_by_email(I['email'])['tags'], '')
    r.check('Snapshot without a custom value', 'Slack is told what to finish, and the run fails with the value to create by hand (with the booking link)',
            sl and sl[0].startswith(':warning: *New client onboarded, with something to finish*') and node == 'Stop: Onboarding Not Finished'
            and 'is not finished. Open: the sub-account has no custom value named "Booking Link"' in error and f'Booking Link = https://api.leadconnectorhq.com/widget/booking/{cal_i} (create it, the snapshot does not have it)' in error,
            error[:150])
    il[0]['_values'].append({'id': next_id('cv'), 'name': 'Booking Link', 'fieldKey': '{{ custom_values.booking_link }}', 'value': '', 'locationId': il[0]['id']})
    status, ans, secs, _, ex = r.retry(I['evt'])
    flow = r.new()
    vals = {v['name']: v['value'] for v in il[0]['_values']}
    r.check('Snapshot without a custom value', 'once the custom value exists, the retry only does the custom values: the booking link is filled, nothing else is called, ledger done',
            ans.get('continuesWith') == 'fill the custom values' and vals.get('Booking Link') == f'https://api.leadconnectorhq.com/widget/booking/{cal_i}'
            and not [e for e in flow if e['service'] == 'ghl' and (e['path'].startswith('/contacts') or e['path'].startswith('/opportunities'))]
            and ledger(tid)[I['evt']]['status'] == 'done', ', '.join(squash(calls(flow))))
    sl = [e['body']['text'] for e in r.new('slack')]
    r.check('Snapshot without a custom value', 'Slack, which heard about the open item, is told once that the onboarding is finished now',
            sl == [':white_check_mark: *Onboarding finished*: Ironwood Auto Repair (Scale plan). The items the earlier run left open are done now.'], sl[0][:90] if sl else 'no message')

    # ------------------------------------------------------------------ J: the agency app is never installed on the new sub-account
    FAIL['never_install'].add(J['business'])
    status, ans, secs, _, ex = r.stripe(event(J['evt'], J['session']))
    node, error = r.stopped_at(ex)
    r.ids['noapp'] = ex['id']
    flow = r.new()
    polls = [e for e in flow if e['service'] == 'ghl' and e['path'] == '/oauth/installedLocations']
    r.check('Agency app not on the sub-account', 'the workflow checks for SETUP_WAIT_SECONDS, never asks for a token it cannot get, and goes on with the agency CRM steps',
            len(polls) >= 5 and not [e for e in flow if e['service'] == 'ghl' and e['path'] == '/oauth/locationToken'] and contact_by_email(J['email'])
            and node == 'Stop: Onboarding Not Finished' and 'the agency app is not installed on the new sub-account' in error, f'{len(polls)} checks')
    FAIL['never_install'].discard(J['business'])
    locations_named(J['business'])[0]['_install_after'] = 0
    status, ans, secs, _, ex = r.retry(J['evt'])
    r.check('Agency app not on the sub-account', 'after the app is installed, the retry fills the custom values and finishes',
            ledger(tid)[J['evt']]['status'] == 'done' and {v['name']: v['value'] for v in locations_named(J['business'])[0]['_values']}['Owner Name'] == J['name'], '')

    # ------------------------------------------------------------------ K: a user with the client's email already exists elsewhere
    status, ans, secs, _, ex = r.stripe(event(K['evt'], K['session']))
    node, error = r.stopped_at(ex)
    r.ids['userexists'] = ex['id']
    kl = locations_named(K['business'])
    r.check('Existing user', 'a HighLevel user with that email exists without access to the new sub-account: no second user, the alert says what to do',
            len(users_with(K['email'])) == 1 and not [e for e in r.new('ghl') if e['method'] == 'POST' and e['path'] == '/users/']
            and 'already exists' in error and 'Agency view &gt; Team' in error, error[:140])
    users_with(K['email'])[0]['roles']['locationIds'].append(kl[0]['id'])
    status, ans, secs, _, ex = r.retry(K['evt'])
    st = steps_of(tid, K['evt'])
    r.check('Existing user', 'after a person gives that user access, the retry picks the user up as it is and finishes',
            st.get('user', {}).get('adopted') is True and ledger(tid)[K['evt']]['status'] == 'done' and len(users_with(K['email'])) == 1, '')

    # ------------------------------------------------------------------ L: a returning client with a lost opportunity
    status, ans, secs, _, ex = r.stripe(event(L['evt'], L['session']))
    flow = r.new()
    lc = contact_by_email(L['email'])
    lopps = [o for o in OPPORTUNITIES.values() if o['contactId'] == lc['id']]
    moves = [e for e in flow if e['service'] == 'ghl' and e['method'] == 'PUT' and e['path'].startswith('/opportunities/')]
    r.check('Returning client', 'the existing contact is updated (no duplicate) and the old lost opportunity is reopened in the Onboarding stage instead of a second one',
            len([c for c in CONTACTS.values() if c['email'] == L['email']]) == 1 and len(lopps) == 1 and lopps[0]['status'] == 'open'
            and lopps[0]['pipelineStageId'] == STAGE_ONBOARDING and moves and moves[0]['body'] == {'pipelineStageId': STAGE_ONBOARDING, 'status': 'open'}
            and 'lost opportunity reopened in Onboarding' in (slack_texts(flow, 'Linden') or [''])[0], json.dumps(moves[0]['body']) if moves else '')
    sl = slack_texts(flow, 'Linden')
    r.check('Returning client', 'the contact already has the new-client tag from the first onboarding, so the native workflow (re-entry off) cannot start again: no tag call, and Slack says to send the welcome by hand',
            not [e for e in flow if e['service'] == 'ghl' and e['path'].endswith('/tags')] and sl and sl[0].startswith(':warning: *New client onboarded, with something to finish*')
            and 'tag new-client was already on the contact' in sl[0] and 'Send the welcome email and create the tasks by hand' in sl[0]
            and steps_of(tid, L['evt']).get('tag', {}).get('alreadyThere') is True and ledger(tid)[L['evt']]['status'] == 'done' and ex['status'] == 'success',
            sl[0].splitlines()[-1][:110] if sl else 'no message')

    # ------------------------------------------------------------------ M: the note is saved but the answer is lost
    mc = add_contact('Drew', 'Fischer', M['email'], '+15550100154', M['business'])
    FAIL['note_lost'].add(mc)
    status, ans, secs, _, ex = r.stripe(event(M['evt'], M['session']))
    flow = r.new()
    names = calls(flow)
    r.check('Lost answer: note', 'the note was saved but answered 504; the notes are read again, the note is found by its marker and not added twice',
            names.count('addNote') == 1 and names.count('findNotes') == 2 and len(NOTES.get(mc, [])) == 1 and steps_of(tid, M['evt'])['note']['adopted'] is True
            and ledger(tid)[M['evt']]['status'] == 'done', ', '.join(squash(names[-6:])))

    # ------------------------------------------------------------------ R: the snapshot status has no record for the new sub-account
    FAIL['no_push_record'].add(R['business'])
    status, ans, secs, _, ex = r.stripe(event(R['evt'], R['session']))
    node, _ = r.stopped_at(ex)
    flow = r.new()
    rl = locations_named(R['business'])
    cal_r = next((c['id'] for c in rl[0]['_calendars'] if c['name'] == 'Discovery Call'), '') if rl else ''
    vals = {v['name']: v['value'] for v in rl[0]['_values']} if rl else {}
    reads = [e for e in flow if e['service'] == 'ghl' and e['method'] == 'GET' and e['path'].endswith('/customValues')]
    r.check('Snapshot status not available', 'the snapshot status has no record for the new sub-account (HighLevel documents it for pushes): the custom values are read again until the snapshot has loaded them, then all are filled',
            [len(e['response'].get('customValues', [])) for e in reads] == [0, 0, 14] and all(vals.get(k) == v for k, v in expected_values(R, 'https://api.leadconnectorhq.com/widget/booking/' + cal_r).items())
            and ledger(tid)[R['evt']]['status'] == 'done' and node == 'Run Failed?' and ex['status'] == 'success', ', '.join(squash(calls(flow)))[:150])

    # ------------------------------------------------------------------ S: the create made nothing, and another client's sub-account has the same name
    FAIL['create_502'][S['business']] = 1
    # another client of the agency, onboarded months ago, whose business has the same name
    decoy = LOCATIONS[add_location(S['business'], 'office@sterlingbooks-denver.example.com', snapshot='snapLaunch01', created=time.time() - 86400 * 90, decoy=True)]
    decoy_values = copy.deepcopy(decoy['_values'])
    status, ans, secs, _, ex = r.stripe(event(S['evt'], S['session']))
    node, error = r.stopped_at(ex)
    r.ids['namesake'] = ex['id']
    flow = r.new()
    r.check('Lost answer: a namesake', "the create answered 502 and made nothing; another client's older sub-account has the same name: it is not taken, no user or custom value goes into it, a person is asked with its ID",
            squash(calls(flow)) == ['findLocation', 'createLocation', 'findLocation', 'listLocations'] and not locations_named(S['business']) and not users_with(S['email'])
            and node == 'Stop: Onboarding Not Finished' and f"ID {decoy['id']}, email office@sterlingbooks-denver.example.com" in error
            and 'but the workflow cannot tell whether it is this client' in error and '"createSubAccount": true' in error, error[:160])
    status, ans, secs, _, ex = r.retry(S['evt'], createSubAccount=True)
    flow = r.new()
    own = locations_named(S['business'])
    r.check('Lost answer: a namesake', "after a person checked, the retry with createSubAccount: true creates the client's own sub-account; the namesake keeps its custom values and gets no user and no token request",
            squash(calls(flow))[:4] == ['findLocation', 'listLocations', 'createLocation', 'findUser'] and len(own) == 1 and ledger(tid)[S['evt']]['status'] == 'done'
            and ledger(tid)[S['evt']]['locationId'] == own[0]['id'] and decoy['_values'] == decoy_values
            and not any(decoy['id'] in u['roles']['locationIds'] for u in USERS.values())
            and not [e for e in LOG if e['service'] == 'ghl' and decoy['id'] in e['path'] + json.dumps(e['body'] or {}) + json.dumps(e['query'])], ', '.join(squash(calls(flow)))[:120])

    # ------------------------------------------------------------------ T: the create keeps getting 429
    FAIL['create_429'][T['business']] = 3
    status, ans, secs, _, ex = r.stripe(event(T['evt'], T['session']))
    node, error = r.stopped_at(ex)
    r.ids['create429'] = ex['id']
    flow = r.new()
    r.check('Rate limit on the create', 'the create gets 429 three times (waiting for Retry-After each time): nothing was created, so the alert names the rate limit and asks nobody to look for a sub-account',
            squash(calls(flow)) == ['findLocation', 'createLocation x3'] and not locations_named(T['business']) and node == 'Stop: Onboarding Not Finished'
            and 'still answered 429 (rate limit) after 3 tries' in error and 'answer never arrived' not in error
            and not steps_of(tid, T['evt']).get('subaccount', {}).get('pending'), error[:150])
    status, ans, secs, _, ex = r.retry(T['evt'])
    flow = r.new()
    r.check('Rate limit on the create', 'a plain retry creates the sub-account without asking a person: one sub-account, done',
            squash(calls(flow))[:3] == ['findLocation', 'createLocation', 'findUser'] and len(locations_named(T['business'])) == 1
            and ledger(tid)[T['evt']]['status'] == 'done', ', '.join(squash(calls(flow)))[:120])

    # ------------------------------------------------------------------ U: the create a person allowed loses its answer too
    FAIL['create_502'][U['business']] = 1
    status, ans, secs, _, ex = r.stripe(event(U['evt'], U['session']))
    r.ids['u502'] = ex['id']
    FAIL['create_hidden'].add(U['business'])
    status, ans, secs, _, ex = r.retry(U['evt'], createSubAccount=True)
    node, error = r.stopped_at(ex)
    r.ids['uhidden'] = ex['id']
    flow = r.new()
    hidden_u = locations_named(U['business'] + ' (copy)')
    r.check('Lost answer after "create it"', 'createSubAccount: true covers one create: when that create loses its answer too (502, saved where the search cannot find it), nothing is created again and a person is asked',
            squash(calls(flow)) == ['findLocation', 'listLocations', 'createLocation', 'findLocation', 'listLocations'] and len(hidden_u) == 1
            and not locations_named(U['business']) and node == 'Stop: Onboarding Not Finished' and 'its answer never arrived' in error, error[:140])
    status, ans, secs, _, ex = r.retry(U['evt'], subAccountId=hidden_u[0]['id'] if hidden_u else 'x')
    r.check('Lost answer after "create it"', 'the retry with the ID the person found finishes it: still one sub-account',
            ledger(tid)[U['evt']]['status'] == 'done' and len(locations_named(U['business'] + ' (copy)')) == 1 and not locations_named(U['business']),
            ledger(tid)[U['evt']]['status'])

    # ------------------------------------------------------------------ N: Slack is down for the summary
    FAIL['slack_fail'].add('Northside')
    status, ans, secs, _, ex = r.stripe(event(N['evt'], N['session']))
    node, error = r.stopped_at(ex)
    r.ids['slackdown'] = ex['id']
    tries = [e for e in r.new('slack') if 'Northside' in e['body']['text']]
    r.check('Slack down', 'the summary is tried 3 times; everything else is done; the run fails with "tell the team in Slack" as the only open item',
            len(tries) == 3 and node == 'Stop: Onboarding Not Finished' and 'Still to do: tell the team in Slack.' in error
            and 'new-client' in contact_by_email(N['email'])['tags'], error[error.find('Still to do'):][:60])
    FAIL['slack_fail'].clear()
    status, ans, secs, _, ex = r.retry(N['evt'])
    flow = r.new()
    sl = slack_texts(flow, 'Northside')
    r.check('Slack down', 'the retry only posts the summary; the business name is escaped, so <!here> cannot ping the channel',
            calls(flow) == ['slack'] and sl and 'Northside Gym &lt;!here&gt;' in sl[0] and '<!here>' not in sl[0] and ledger(tid)[N['evt']]['status'] == 'done', calls(flow))

    # ------------------------------------------------------------------ O: unknown plan and no business name
    status, ans, secs, _, ex = r.stripe(event(O['evt'], O['session']))
    node, error = r.stopped_at(ex)
    r.ids['review'] = ex['id']
    r.check('Needs a person', 'a paid checkout with an unknown plan and no business name: 200 to Stripe, recorded as needs_review, nothing created, an alert with both problems',
            status == 200 and ans == {'received': True} and not r.new('ghl') and ledger(tid)[O['evt']]['status'] == 'needs_review' and node == 'Stop: Needs Review'
            and 'the plan "enterprise" from the checkout is not in PLAN_SNAPSHOTS' in error and 'no business name' in error, error[:120])
    status, ans, secs, _, ex = r.retry(O['evt'])
    r.ids['review2'] = ex['id']
    r.check('Needs a person', 'a retry before the problem is fixed answers 422 with the problems and creates nothing',
            status == 422 and ans.get('error') == 'needs review' and len(ans.get('problems', [])) == 2 and not r.new('ghl'), str(status))

    # ------------------------------------------------------------------ P: a run that n8n never finished (a restart in the middle)
    s, d = P['session'], P['session']['customer_details']
    stale = datetime.fromtimestamp(time.time() - 7200, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    data = {'planKey': 'launch', 'client': {'business': P['business'], 'name': P['name'], 'firstName': 'Jesse', 'lastName': 'Park', 'email': P['email'],
                                            'phone': d['phone'], 'address1': d['address']['line1'], 'city': d['address']['city'], 'state': d['address']['state'],
                                            'postalCode': d['address']['postal_code'], 'country': 'US', 'website': ''},
            'amount': {'total': 150000, 'currency': 'USD'}, 'sessionId': s['id'], 'paymentIntent': s['payment_intent'], 'customerId': s['customer'],
            'livemode': False, 'eventType': 'checkout.session.completed', 'receivedAt': stale}
    before = {e['id'] for e in r.executions()}
    api('POST', f'/api/v1/data-tables/{tid}/rows', {'returnType': 'count', 'data': [{
        'key': P['evt'], 'sessionId': s['id'], 'status': 'in_progress', 'plan': 'launch', 'business': P['business'], 'email': P['email'], 'locationId': '',
        'userId': '', 'contactId': '', 'opportunityId': '', 'steps': '{}', 'attempts': 1, 'note': 'Run 99999 working', 'data': json.dumps(data),
        'kickoff': '', 'changedAt': stale}]})
    found, end = [], time.time() + 150
    while not found and time.time() < end:
        time.sleep(3)
        found = [e for e in r.executions() if e['id'] not in before and e.get('mode') == 'trigger' and e.get('status') == 'error']
    node, error = r.stopped_at(found[0]) if found else ('none', '')
    if found:
        r.ids['stuck'] = found[0]['id']
    r.check('Stuck run check', 'a row left in progress for 2 hours (n8n restarted mid-run) is found by the scheduled check, marked failed and reported once',
            found and node == 'Stop: Unfinished Onboarding' and ledger(tid)[P['evt']]['status'] == 'failed' and 'Pinecrest Florals' in error
            and 'n8n restarted in the middle of the run' in error and 'done so far: nothing' in error and '"eventId": "..."' in error, error[:120])
    status, ans, secs, _, ex = r.retry(P['evt'])
    r.check('Stuck run check', 'the retry then runs the whole onboarding from the ledger data (the Stripe event is not needed again)',
            status == 202 and ans.get('continuesWith') == 'create the sub-account from the snapshot' and ledger(tid)[P['evt']]['status'] == 'done'
            and len(locations_named(P['business'])) == 1 and len(users_with(P['email'])) == 1, str(ans))

    # ------------------------------------------------------------------ the Retry webhook's own refusals
    status, ans, secs, _, ex = r.retry(A['evt'], secret='wrong-secret')
    node, _ = r.stopped_at(ex)
    r.ids['retrysecret'] = ex['id']
    r.check('Retry webhook', 'a wrong X-Run-Secret is refused with 401, nothing is read, the run fails for an alert',
            status == 401 and ans == {'started': False, 'error': 'unauthorized'} and node == 'Stop: Bad Secret' and not r.new('ghl'), f'{status}')
    status, ans, secs, _, ex = r.retry('evt_testunknown000001')
    r.check('Retry webhook', 'an event ID that is not in the ledger gets 404', status == 404 and ans.get('error') == 'no onboarding with this event ID in the ledger', str(status))
    status, ans, secs, _, ex = r.retry(A['evt'])
    r.check('Retry webhook', 'a finished onboarding gets 409 and nothing is called', status == 409 and not r.new('ghl') and not r.new('slack'), str(status))
    status, ans, secs, _, ex = r.retry('not-an-event-id')
    r.check('Retry webhook', 'a body without a Stripe event ID gets 400', status == 400 and ex['status'] == 'success', str(status))

    # ------------------------------------------------------------------ status calls from the native GoHighLevel workflows
    ac = contact_by_email(A['email'])
    status, ans, secs, _, ex = r.status_call({'event': 'kickoff_booked', 'contactId': ac['id']}, secret='wrong-secret-value-000000000')
    node, _ = r.stopped_at(ex)
    r.ids['statussecret'] = ex['id']
    r.check('Status webhook', 'a call with a wrong X-Onboarding-Secret is refused with 401 and an alert; nothing changes',
            status == 401 and node == 'Stop: Bad Status Secret' and not r.new('slack') and not ledger(tid)[A['evt']]['kickoff'], str(status))
    status, ans, secs, _, ex = r.status_call({'event': 'kickoff_booked', 'contactId': ac['id'], 'email': A['email'], 'company': A['business'],
                                              'appointmentStart': 'Tuesday, November 3, 2026 10:00 AM'}, form=True)
    sl = slack_texts(r.new('slack'), 'Kickoff call booked')
    r.check('Status webhook', 'kickoff booked (Custom Webhook POST: the secret header and form-style key and value pairs): 200, the ledger row gets the kickoff, Slack gets a note',
            status == 200 and ans == {'ok': True} and ledger(tid)[A['evt']]['kickoff'].startswith('booked for Tuesday, November 3, 2026 10:00 AM')
            and sl == [':calendar: *Kickoff call booked*: Lumière Café for Tuesday, November 3, 2026 10:00 AM.'], sl[0] if sl else 'no message')
    payload = {'contact_id': contact_by_email(C['email'])['id'], 'first_name': 'Dana', 'last_name': 'Ruiz', 'email': C['email'], 'tags': 'new-client',
               'location': {'id': HOME, 'name': 'Brightline Growth Agency'}, 'workflow': {'id': 'wfOnboarding01', 'name': 'Client onboarding: welcome and tasks'},
               'customData': {'secret': ENV['GHL_STATUS_SECRET'], 'event': 'kickoff_not_booked'}}
    status, ans, secs, _, ex = r.status_call(payload, secret=None)
    sl = slack_texts(r.new('slack'), 'not booked yet')
    r.check('Status webhook', 'kickoff not booked, sent by the standard Webhook action (secret and event in customData): 200 and a Slack reminder',
            status == 200 and sl and 'Copperline Electric (dana.ruiz@copperline.example.com)' in sl[0] and ledger(tid)[C['evt']]['kickoff'].startswith('not booked'), sl[0][:90] if sl else '')
    status, ans, secs, _, ex = r.status_call({'event': 'kickoff_booked', 'contactId': 'ctcManual9999', 'company': 'Walk-in Client Co'})
    sl = slack_texts(r.new('slack'), 'Walk-in Client Co')
    r.check('Status webhook', 'a contact that is not in the ledger (onboarded by hand) still gets its Slack note, marked as such',
            status == 200 and sl and 'not in the onboarding ledger' in sl[0], sl[0][:100] if sl else '')
    status, ans, secs, _, ex = r.status_call({'event': 'something_else', 'contactId': ac['id']})
    node, _ = r.stopped_at(ex)
    r.ids['statusbad'] = ex['id']
    r.check('Status webhook', 'a call without a known event is refused with 400 and an alert (the Custom Webhook body is wrong)',
            status == 400 and node == 'Stop: Bad Status Call', str(status))

    # ------------------------------------------------------------------ integrity over the whole run
    onboarded = [Q, A, B, C, D, E, G, H, I, J, K, L, M, N, P, R, S, T]
    found_by_person = [F, U]
    r.check('Whole run', 'every client has exactly one sub-account (F and U: the one a person found), one admin user and one opportunity in the pipeline',
            all(len(locations_named(c['business'])) == 1 for c in onboarded)
            and all(len(locations_named(c['business'] + ' (copy)')) == 1 and not locations_named(c['business']) for c in found_by_person)
            and all(len(users_with(c['email'])) == 1 for c in onboarded + found_by_person)
            and all(len([o for o in OPPORTUNITIES.values() if o['contactId'] == contact_by_email(c['email'])['id'] and o['pipelineId'] == PIPELINE]) == 1
                    for c in onboarded + found_by_person),
            f'{len(LOCATIONS)} sub-accounts in the mock')
    rows = ledger(tid)
    r.check('Whole run', 'the ledger: done for every onboarded client, needs_review for the unknown plan, no other rows (ignored events leave none)',
            all(rows[c['evt']]['status'] == 'done' for c in onboarded + found_by_person) and rows[O['evt']]['status'] == 'needs_review'
            and len(rows) == len(onboarded) + len(found_by_person) + 1,
            json.dumps({k[-6:]: v['status'] for k, v in rows.items()}))
    bad = [e for e in LOG if e['service'] == 'ghl' and e['status'] in (400, 422) and e['path'] != '/users/']
    r.check('Whole run', 'no request was refused by the mock as malformed (no 400 or 422 from GoHighLevel)', not bad,
            json.dumps([(e['method'], e['path'], e['response']) for e in bad[:3]], ensure_ascii=True)[:300])

    # ------------------------------------------------------------------ the error alert workflow
    failed = sorted((e['id'] for e in r.executions() if e.get('status') == 'error'), key=int)
    linked, got = alerts_for(failed)
    expected = sorted(r.ids.values(), key=int)
    r.check('Error alert workflow', 'every failed run raised exactly one Slack alert with its execution link, and no other alerts',
            failed == expected and all(len(v) == 1 for v in linked.values()) and len(got) == len(failed), f'{len(failed)} failed runs, {len(got)} alerts')
    a = (linked.get(r.ids['user503']) or [{'body': {'text': ''}}])[0]['body']['text']
    r.check('Error alert workflow', 'the alert names the node and carries the whole message, escaped for Slack',
            f'*{TEMP_NAME}* failed at *Stop: Onboarding Not Finished*' in a and 'Dune &amp; Dawn Yoga &lt;b&gt;' in a and '<b>' not in a, a.splitlines()[1][:90] if '\n' in a else a[:90])


def run_bad_settings(results, error_wid, tid):
    env = {**ENV, 'LEDGER_TABLE_ID': tid, 'STRIPE_WEBHOOK_SECRET': 'not-a-signing-secret', 'PLAN_SNAPSHOTS': '{"launch": ', 'GHL_APP_ID': '',
           'CLIENT_TIMEZONE': 'Mars/Olympus'}
    wid = import_active(env, '-2', error_wid)
    r = Runner(wid, '-2')
    try:
        status, ans, secs, _, ex = r.stripe(event(A['evt'], A['session']))
        node, error = r.stopped_at(ex)
        problems = ["GHL_APP_ID must be the ID of your HighLevel agency app (Marketplace developer portal > your app)",
                    "STRIPE_WEBHOOK_SECRET must be the endpoint's signing secret from Stripe (it starts with whsec_)",
                    'CLIENT_TIMEZONE "Mars/Olympus" is not a valid time zone',
                    'PLAN_SNAPSHOTS must be JSON that maps each plan to its snapshot, such as {"launch": {"snapshotId": "abc123", "label": "Launch"}}']
        results.append(('Broken settings', 'a Stripe delivery gets a plain 500 (Stripe sends it again after the fix), nothing is called, the run fails',
                        status == 500 and ans == {'received': False, 'error': 'settings'} and not r.new('ghl') and node == 'Stop: Bad Settings' and ex['status'] == 'error', f'{status}, ended at {node}'))
        linked, _ = alerts_for([ex['id']])
        text = (linked[ex['id']] or [{'body': {'text': ''}}])[0]['body']['text']
        results.append(('Broken settings', 'the alert names every problem', len(linked[ex['id']]) == 1 and all(p.replace('>', '&gt;') in text or p in text for p in problems),
                        text.splitlines()[1][:100] if '\n' in text else text[:100]))
        status, ans, secs, _, ex = r.retry(A['evt'])
        results.append(('Broken settings', 'the Retry webhook answers 500 with the list of problems', status == 500 and ans.get('problems') == problems, json.dumps(ans)[:200]))
        status, ans, secs, _, ex = r.status_call({'event': 'kickoff_booked', 'contactId': 'ctc0001'})
        results.append(('Broken settings', 'a status call gets a plain 500', status == 500 and ans == {'received': False, 'error': 'settings'}, str(status)))
        linked, _ = alerts_for([e['id'] for e in r.executions() if e.get('status') == 'error' and e.get('mode') == 'webhook'])
        results.append(('Broken settings', 'each of the three refused calls raised one alert', len(linked) == 3 and all(len(v) == 1 for v in linked.values()), f'{len(linked)} failed runs'))
    finally:
        remove(wid)


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    wfs, tables = remove_leftovers()
    if wfs or tables:
        print(f'Removed {wfs} temporary workflow(s) and {tables} data table(s) left by an interrupted run.')
    seed()
    server = MockServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results = []
    tid = create_table()
    try:
        ENV['LEDGER_TABLE_ID'] = tid
        error_wid = import_error_workflow()
        try:
            wid = import_active(ENV, '', error_wid)
            r = Runner(wid)
            try:
                run_main(r, tid)
            finally:
                results = r.results
                remove(wid)
            run_bad_settings(results, error_wid, tid)
        finally:
            remove(error_wid)
    finally:
        api('DELETE', f'/api/v1/data-tables/{tid}')
        server.shutdown()
        if CONNECTION_ERRORS:
            print(f'Mock connection problems: {CONNECTION_ERRORS[:5]}', flush=True)
        if os.environ.get('SAVE_REQUESTS'):
            Path(os.environ['SAVE_REQUESTS']).write_text(json.dumps([{k: e[k] for k in ('service', 'method', 'path', 'query', 'body', 'status')} for e in LOG],
                                                                    indent=1, ensure_ascii=False), encoding='utf-8')

    passed = sum(ok for _, _, ok, _ in results)
    last = None
    for scenario, label, ok, detail in results:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(results)} checks passed')
    sys.exit(0 if results and passed == len(results) else 1)


if __name__ == '__main__':
    main()
