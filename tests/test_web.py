"""Synthetic snapshots: no upstream systems or rule engine are involved."""
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import unittest

from atraxis.web import HEADERS, MAX_DISPLAY_ROWS, render_inventory


def inventory():
    return dict(ready=True, reason=None, sources=[dict(source_id='docker-lab',
        kind='monit_docker', current=True, availability='available',
        last_attempt=dict(outcome='success'), last_valid_snapshot=dict(age_seconds=5,
        resources=[dict(native_id='a'*64, fields=dict(name='worker', status='running',
            cpu_percent=250, mem_percent=None, maintenance_active=False))]))])


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.tags=[]; self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class WebPresentationTests(unittest.TestCase):
    def test_current_values_unknown_memory_and_native_cpu(self):
        payload=inventory(); original=deepcopy(payload)
        page=render_inventory(payload, now=datetime(2026,10,7,8,0,tzinfo=timezone.utc))
        self.assertIn('250%', page)
        self.assertIn('Unknown</td>', page)
        self.assertIn('Current at page load', page)
        self.assertIn('2026-10-07 08:00:00 UTC', page)
        self.assertEqual(payload, original)

    def test_source_failure_retains_observations_as_historical(self):
        payload=inventory(); payload.update(ready=False, reason='sources_not_current')
        payload['sources'][0].update(current=False, availability='unavailable', last_attempt=dict(outcome='timeout'))
        page=render_inventory(payload)
        self.assertIn('Historical snapshot', page)
        self.assertIn('These measurements do not describe the current state', page)
        self.assertIn('250%', page)
        self.assertIn('<strong>0</strong><span>Resources in current sources', page)
        self.assertNotIn('Current at page load', page)

    def test_empty_snapshot_is_distinct_from_no_observation(self):
        payload=inventory(); source=payload['sources'][0]
        source['last_valid_snapshot']['resources']=[]
        self.assertIn('current snapshot contains no resources', render_inventory(payload))
        source.update(current=False, last_valid_snapshot=None, last_attempt=None, availability='unknown')
        payload.update(ready=False, reason='sources_not_current')
        page=render_inventory(payload)
        self.assertIn('Resource count and measurements are unknown', page)
        self.assertIn('Snapshot resources: Unknown', page)
        self.assertNotIn('current snapshot contains no resources', page)

    def test_stopped_collection_and_no_configured_sources(self):
        payload=inventory(); payload.update(ready=False, reason='collector_stopped')
        payload['sources'][0]['current']=False
        page=render_inventory(payload)
        self.assertIn('Collection stopped', page)
        self.assertIn('Historical snapshot', page)
        page=render_inventory(dict(ready=False, reason='collectors_not_configured', sources=[]))
        self.assertIn('No sources configured', page)

    def test_source_names_and_values_cannot_inject_markup_or_attributes(self):
        payload=inventory(); source=payload['sources'][0]
        attack='\"><script>alert(1)</script><img src=x onerror=alert(2)>'
        source['source_id']=attack
        resource=source['last_valid_snapshot']['resources'][0]
        resource['native_id']=attack
        resource['fields'].update(name=attack, status=attack, health=attack, password='secret-marker')
        page=render_inventory(payload)
        self.assertNotIn(attack, page)
        self.assertNotIn('secret-marker', page)
        for tag, attrs in Elements(page).tags:
            self.assertNotIn(tag, ('script','img','iframe','form'))
            self.assertFalse(any(key.startswith('on') for key in attrs))

    def test_certificate_is_not_inferred_healthy_from_existence(self):
        payload=inventory(); source=payload['sources'][0]
        source.update(kind='certlord', source_id='certificates')
        source['last_valid_snapshot']['resources']=[dict(native_id='fixture-id',
            fields=dict(domains=['example.test'], status='exists', material_state='missing', not_after=None))]
        page=render_inventory(payload)
        self.assertIn('example.test', page)
        self.assertIn('missing</td>', page)
        self.assertIn('Unknown</td>', page)
        self.assertNotIn('Healthy', page)

    def test_display_limit_is_explicit_and_does_not_change_total(self):
        payload=inventory(); source=payload['sources'][0]
        source['last_valid_snapshot']['resources'] *= MAX_DISPLAY_ROWS+1
        page=render_inventory(payload)
        self.assertEqual(page.count('<summary>Details</summary>'), MAX_DISPLAY_ROWS)
        self.assertIn('Showing the first 200 of 201 resources', page)
        self.assertIn('Snapshot resources: 201', page)

    def test_document_has_local_navigation_and_strict_policy(self):
        page=render_inventory(inventory())
        self.assertIn('lang="en"', page)
        self.assertIn('name="viewport"', page)
        links=[attrs['href'] for tag,attrs in Elements(page).tags if tag=='a']
        self.assertEqual(links, ['#inventory', '/ui/search', '/ui'])
        self.assertEqual(HEADERS['Cache-Control'], 'no-store')
        self.assertIn("default-src 'none'", HEADERS['Content-Security-Policy'])
        self.assertNotIn('unsafe-inline', HEADERS['Content-Security-Policy'])
