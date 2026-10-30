"""Read-only HTML presentation of the inventory service's public snapshot."""
from datetime import datetime, timezone
from html import escape
import base64
import hashlib
from urllib.parse import urlencode

MAX_DISPLAY_ROWS = 200
SOURCE_NAMES = {'monit_docker': 'monit-docker', 'certlord': 'CertLord', 'prometheus': 'Prometheus metrics',
                'prometheus_monit': 'Monit-Docker via Prometheus',
                'prometheus_certlord': 'CertLord via Covenant / Prometheus'}
REASONS = {'no_visible_sources': 'No sources are available in your inventory scope','collectors_not_configured': 'No sources configured',
           'collector_stopped': 'Collection stopped',
           'sources_not_current': 'Some observations are not current'}
DETAIL_FIELDS = ('health', 'mem_usage', 'mem_limit', 'io_read', 'io_write',
                 'net_rx', 'net_tx', 'pids_current', 'pids_limit', 'oom_events',
                 'maintenance_active', 'maintenance_until', 'not_before',
                 'not_after', 'renewal_owner', 'pending_action', 'material_state')

CSS = '''
:root{color-scheme:dark;--bg:#09111f;--panel:#111e30;--line:#293b53;--muted:#a9bad0;--blue:#94d3ff;--green:#9ce2c2;--amber:#ffd58d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#edf5ff;font:16px/1.6 system-ui,sans-serif}
a{color:var(--blue)}a:focus-visible,summary:focus-visible{outline:3px solid var(--amber);outline-offset:5px}
.skip{position:absolute;top:-100px;left:12px}.skip:focus{top:12px;background:var(--bg);padding:12px;z-index:1}
input,select{max-width:100%;padding:8px;background:var(--bg);color:inherit;border:1px solid var(--line)}button{color:inherit;font:inherit}nav{margin:16px 0}header nav{display:flex;flex-wrap:wrap;gap:8px 16px;justify-content:flex-end;min-width:0}
header{border-bottom:1px solid var(--line);padding:22px max(24px,calc((100vw - 1200px)/2));display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:20px}
.brand{font-size:24px;font-weight:800;letter-spacing:.14em}.brand span{color:var(--blue)}.eyebrow{font-size:12px;letter-spacing:.15em;text-transform:uppercase;color:var(--muted)}
main{max-width:1248px;margin:auto;padding:42px 24px}h1{font-size:clamp(28px,4vw,42px);line-height:1.15;margin:8px 0 16px;letter-spacing:-.03em}h2{font-size:22px;margin:0}h3{font-size:16px;margin:0}
p{margin:8px 0 16px}.muted,small{color:var(--muted)}.intro,.source-heading{display:flex;justify-content:space-between;align-items:center;gap:24px}.button{display:inline-block;background:#203e5b;border:1px solid #567b9e;border-radius:8px;padding:10px 20px;text-decoration:none;white-space:nowrap}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:26px 0}.metric,.source,.empty{background:var(--panel);border:1px solid var(--line);border-radius:12px}.metric{padding:20px 24px}.metric strong{display:block;font-size:32px;line-height:1.3}.metric span{color:var(--muted)}
.notice{padding:16px 20px;border-left:3px solid var(--amber);background:#28241d;border-radius:0 8px 8px 0}.notice.good{border-color:var(--green);background:#132920}.source{padding:24px;margin-top:20px;overflow:hidden}.source-heading{align-items:flex-start}.source-heading h2{overflow-wrap:anywhere}.badge{display:inline-block;padding:3px 10px;font-size:13px;border:1px solid currentColor;border-radius:20px;color:var(--amber);white-space:nowrap}.badge.current{color:var(--green)}.badge.unknown{color:var(--muted)}
.source-meta{display:flex;flex-wrap:wrap;gap:8px 24px;color:var(--muted);font-size:14px;margin:16px 0}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;text-align:left;font-size:14px}th{color:var(--muted);font-size:12px;letter-spacing:.06em;text-transform:uppercase;font-weight:500}td,th{padding:14px 12px;border-bottom:1px solid var(--line);vertical-align:top}td:first-child,th:first-child{padding-left:0}td:last-child,th:last-child{padding-right:0}td.name{min-width:210px;max-width:440px;overflow-wrap:anywhere}.resource-name{font-weight:650;font-size:15px}.value{white-space:nowrap;font-variant-numeric:tabular-nums}summary{cursor:pointer;color:var(--blue)}details{margin-top:8px}dl{margin:12px 0}dt{color:var(--muted);font-size:12px}dd{margin:0 0 8px;overflow-wrap:anywhere}code{font-size:12px;overflow-wrap:anywhere;white-space:normal}.empty{padding:30px}.source .empty{border-style:dashed;padding:18px}footer{margin-top:26px;color:var(--muted);font-size:13px}
.search-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-bottom:20px}.search-fields p{margin:0}.search-fields label{display:grid;gap:6px}.search-fields input,.search-fields select{width:100%;font:inherit}.search-fields .search-query{grid-column:1/-1}
@media(max-width:640px){.search-fields{grid-template-columns:1fr}header{padding:16px 20px}.brand{font-size:20px}main{padding:26px 16px}.intro{align-items:flex-start;flex-direction:column;gap:8px}.metrics{gap:8px}.metric{padding:14px 10px}.metric strong{font-size:26px}.metric span{font-size:12px}.source{padding:16px}.source-heading{gap:10px}.source-heading h2{font-size:19px}header .eyebrow{font-size:10px}}
'''
STYLE_HASH = base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()
HEADERS = {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store',
           'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
           'Referrer-Policy': 'no-referrer',
           'Content-Security-Policy': "default-src 'none'; style-src 'sha256-" + STYLE_HASH
               + "'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"}


def text(value):
    return escape(str(value), quote=True)


def value(item, suffix=''):
    return 'Unknown' if item is None else text(item) + suffix


def details(resource):
    fields = resource['fields']
    rows = '<dt>Resource ID</dt><dd><code>' + text(resource['native_id']) + '</code></dd>'
    if resource.get('kind') == 'metric':
        rows += '<dt>Sample value</dt><dd>' + value(fields['value']) + '</dd>'
        rows += '<dt>Prometheus sample time (UTC)</dt><dd>' + sample_time(fields['sampled_at']) + '</dd>'
        for label in fields['labels']:
            rows += '<dt>' + text(label['name']) + '</dt><dd>' + text(label['value']) + '</dd>'
    for name in DETAIL_FIELDS:
        if name in fields:
            item = fields[name]
            if isinstance(item, bool):
                item = 'Yes' if item else 'No'
            rows += '<dt>' + text(name.replace('_', ' ').capitalize()) + '</dt><dd>' + value(item) + '</dd>'
    return '<details><summary>Details</summary><dl>' + rows + '</dl></details>'


def sample_time(stamp):
    try:
        return text(datetime.fromtimestamp(stamp, timezone.utc).isoformat())
    except (ValueError, OverflowError, OSError):
        return text(stamp) + ' Unix seconds'


def source_panel(source):
    snapshot = source['last_valid_snapshot']
    partial = source.get('visibility') == 'resources'
    current = source['current']
    state = 'current' if current else 'historical' if snapshot is not None else 'unknown'
    label = {'current': 'Current at page load', 'historical': 'Historical snapshot', 'unknown': 'No observation'}[state]
    age = snapshot['age_seconds'] if snapshot is not None else None
    count = len(snapshot['resources']) if snapshot is not None else None
    attempt = source['last_attempt']
    outcome = 'Not collected yet' if attempt is None else attempt['outcome'].replace('_', ' ')
    result = '<section class="source"><div class="source-heading"><div><div class="eyebrow">' + text(SOURCE_NAMES[source['kind']]) + '</div><h2>' + text(source['source_id']) + '</h2></div><span class="badge ' + state + '">' + label + '</span></div>'
    if partial:
        result += '<p class="notice">Selected resources only. Counts reflect your permissions.</p>'
    if source['kind'] == 'prometheus':
        result += '<p class="muted">Latest retained Prometheus samples. Sample age does not prove when the original data changed or that its source is healthy.</p>'
    result += '<div class="source-meta"><span>Availability: ' + text(source['availability']) + '</span><span>Last collection: ' + text(outcome) + '</span><span>Observation age at load: ' + ('Unknown' if age is None else text('%.1f s' % age)) + '</span><span>' + ('Visible resources: ' if partial else 'Snapshot resources: ') + value(count) + '</span></div>'
    if snapshot is None:
        return result + '<div class="empty">No valid snapshot available. Resource count and measurements are unknown.</div></section>'
    if not current:
        result += '<p class="notice">Retained data only. These measurements do not describe the current state.</p>'
    resources = snapshot['resources']
    if not resources:
        message = ('No selected resources are present in this snapshot.' if partial and current else
                   'No selected resources are present in the retained snapshot. Current inventory is unknown.' if partial else
                   'The current snapshot contains no resources.' if current else 'The retained snapshot contained no resources. Current inventory is unknown.')
        return result + '<div class="empty">' + message + '</div></section>'
    container = source['kind'] in ('monit_docker', 'prometheus_monit')
    metric = source['kind'] == 'prometheus'
    titles = (('Metric', 'Sample value', 'Sample time (UTC)', 'Labels') if metric else
              ('Container', 'Reported state', 'CPU', 'Memory') if container else ('Certificate', 'Reported state', 'Material', 'Expires'))
    result += '<div class="table-wrap" role="region" aria-label="Resources for ' + text(source['source_id']) + '" tabindex="0"><table><thead><tr>' + ''.join('<th scope="col">' + title + '</th>' for title in titles) + '</tr></thead><tbody>'
    for resource in resources[:MAX_DISPLAY_ROWS]:
        fields = resource['fields']
        name = fields['name'] if container or metric else (', '.join(fields.get('domains') or []) or resource['native_id'])
        if metric:
            labels = ', '.join(label['name'] + '=' + label['value'] for label in fields['labels'])
            result += '<tr><td class="name"><span class="resource-name">' + text(name) + '</span>' + details(resource) + '</td><td>' + value(fields['value']) + '</td><td>' + sample_time(fields['sampled_at']) + '</td><td class="name">' + text(labels) + '</td></tr>'
            continue
        cells = (value(fields.get('cpu_percent'), '%'), value(fields.get('mem_percent'), '%')) if container else (value(fields.get('material_state')), value(fields.get('not_after')))
        result += '<tr><td class="name"><span class="resource-name">' + text(name) + '</span>' + details(resource) + '</td><td>' + value(fields.get('status')) + '</td>' + ''.join('<td class="value">' + item + '</td>' for item in cells) + '</tr>'
    result += '</tbody></table></div>'
    if len(resources) > MAX_DISPLAY_ROWS:
        result += '<p class="muted">Showing the first %s of %s resources. Use the inventory CLI or API for the complete snapshot.</p>' % (MAX_DISPLAY_ROWS, len(resources))
    return result + '</section>'


def context_navigation(scope):
    links = []
    for org in scope['organizations']:
        current = ' aria-current="page"' if org['id'] == scope['organization'] else ''
        links.append('<a href="/ui?'+urlencode({'organization':org['id']})+'"'+current+'>'+('<strong>'+text(org['name'])+'</strong>' if current else text(org['name']))+'</a>')
    return '<section class="source"><h2>Organization</h2><nav aria-label="Organization">'+' · '.join(links)+'</nav><p class="muted">Inventory context only. Rule operations use their separately configured organization.</p></section>'


def render_context_selection(scope):
    body = render_inventory(dict(ready=False, reason='no_visible_sources', sources=[]))
    start = body.index('<div class="notice')
    end = body.index('<footer>', start)
    return body[:start] + context_navigation(scope) + '<p>Select an organization to view its inventory.</p>' + body[end:]


def render_inventory(payload, *, now=None, rules_enabled=False, accounts_enabled=False, inventory_admin_enabled=False):
    """Input is a ReadService snapshot, never a raw upstream response."""
    if 'context' in payload:
        context = payload['context']
        base = dict(payload)
        del base['context']
        page = render_inventory(base, now=now, rules_enabled=rules_enabled, accounts_enabled=accounts_enabled, inventory_admin_enabled=inventory_admin_enabled)
        page = page.replace('<div class="intro">', context_navigation(context)+'<div class="intro">', 1)
        page = page.replace('href="/ui">Refresh snapshot', 'href="/ui?'+urlencode({'organization':context['organization']})+'">Refresh snapshot')
        page = page.replace('href="/ui/search"', 'href="/ui/search?'+urlencode({'organization':context['organization']})+'"')
        for route in ('/ui/rules','/ui/interventions'):
            page = page.replace('href="'+route+'"', 'href="'+route+'?'+urlencode({'organization':context['organization']})+'"')
        page = page.replace('All configured sources', 'All visible sources').replace('Configured sources', 'Visible sources')
        if not payload['sources']:
            start = page.index('<div class="empty"><h2>No sources configured')
            end = page.index('</div>', start)+6
            page = page[:start]+'<div class="empty"><h2>No sources available</h2><p>No configured sources are visible with your current permissions.</p></div>'+page[end:]
        return page
    if rules_enabled or accounts_enabled or inventory_admin_enabled:
        links = ('<a href="/ui/rules">Rules</a>' if rules_enabled else '') + (' <a href="/ui/accounts">Accounts</a>' if accounts_enabled else '') + (' <a href="/ui/inventory-access">Inventory access</a>' if inventory_admin_enabled else '')
        return render_inventory(payload, now=now).replace('<div class="eyebrow">Operations / Inventory</div>', '<nav aria-label="Main">'+links+'</nav>', 1)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    sources = payload['sources']
    current = sum(source['current'] for source in sources)
    observed = sum(len(source['last_valid_snapshot']['resources']) for source in sources if source['current'])
    ready = payload['ready']
    notice = 'All configured sources have current observations.' if ready else REASONS[payload['reason']] + '.'
    metrics = ''.join('<div class="metric"><strong>' + str(count) + '</strong><span>' + label + '</span></div>' for count, label in ((len(sources), 'Configured sources'), (current, 'Current sources'), (observed, 'Resources in current sources')))
    panels = ''.join(source_panel(source) for source in sources) if sources else '<div class="empty"><h2>No sources configured</h2><p class="muted">Add a monit-docker, CertLord or Prometheus source to the daemon configuration, then restart collection.</p></div>'
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Inventory · Atraxis</title><style>' + CSS + '</style></head><body><a class="skip" href="#inventory">Skip to inventory</a><header><div class="brand">ATRA<span>XIS</span></div><div class="eyebrow">Operations / Inventory</div><a href="/ui/search">Search inventory</a></header><main id="inventory"><div class="intro"><div><div class="eyebrow">Your infrastructure, in view</div><h1>Inventory overview</h1><p class="muted">Containers, certificates, metrics and the sources behind them.</p></div><a class="button" href="/ui">Refresh snapshot</a></div><p class="muted">Snapshot at ' + stamp + '. Refresh to check for changes.</p><div class="notice' + (' good' if ready else '') + '">' + notice + '</div><div class="metrics">' + metrics + '</div>' + panels + '<footer>Read-only inventory · Values are observations, not inferred health scores. CPU may exceed 100% on multi-core hosts.</footer></main></body></html>'
