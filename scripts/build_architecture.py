#!/usr/bin/env python3
"""Build the Atraxis architecture SVG from existing vector logos."""
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from html import escape

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'assets/diagrams'
NS = 'http://www.w3.org/2000/svg'
ET.register_namespace('', NS)


def logo(path, key, x, y, w, h):
    node = ET.fromstring(path.read_text())
    for element in node.iter():
        if 'id' in element.attrib:
            element.set('id', key + '-' + element.get('id'))
        for attr, value in list(element.attrib.items()):
            value = re.sub(r'url\(#([^)]*)\)', lambda m: f'url(#{key}-{m[1]})', value)
            if attr == 'aria-labelledby':
                value = ' '.join(key + '-' + item for item in value.split())
            element.set(attr, value)
    node.attrib.update(x=str(x), y=str(y), width=str(w), height=str(h))
    return ET.tostring(node, encoding='unicode')


def build():
    parts = [f'''<svg xmlns="{NS}" width="680" height="752" viewBox="0 0 680 752" role="img" aria-labelledby="title desc">
<title id="title">Atraxis architecture</title>
<desc id="desc">Atraxis reads configured Monit-Docker and CertLord APIs directly, or queries Prometheus. In the optional metrics route, Prometheus scrapes Covenant, which reads the source APIs. Atraxis calls Atropos through its independent client. Atropos submits explicitly authorized jobs to Auton and configured notifications to Alertmanager. Prometheus can also send configured metric alerts to Alertmanager. Arrows point from caller to called service; query responses return in reverse.</desc>
<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0 10 5 0 10Z" fill="#84929d"/></marker></defs>
<style>text{{font-family:DejaVu Sans,Arial,sans-serif;fill:#253846}}.title{{font-size:16px;font-weight:500}}.body{{font-size:13px}}.small{{font-size:11px;fill:#637480}}.tag{{font-size:10px;letter-spacing:1.2px;fill:#637480}}.line{{fill:none;stroke:#84929d;stroke-width:1;marker-end:url(#arrow)}}.box{{fill:white;stroke:#dce3e8;stroke-width:1}}</style>
<rect width="680" height="752" fill="white"/>''']

    def text(x, y, value, cls='body', anchor='start'):
        parts.append(f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{escape(value)}</text>')

    def box(x, y, w, h, fill='white', dashed=False):
        dash = ' stroke-dasharray="4 4"' if dashed else ''
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" fill="{fill}" stroke="#c5cfd6" stroke-width="1"{dash}/>')

    def line(d):
        parts.append(f'<path d="{d}" class="line"/>')

    def product(key, name, x, y, subtitle):
        parts.append(logo(OUT/'logos'/f'{key}.svg', key, x, y, 30, 30))
        text(x + 44, y + 13, name, 'title')
        if subtitle:
            text(x + 44, y + 34, subtitle, 'small')

    text(36, 27, 'CONFIGURED SOURCE APIs', 'tag')
    box(24, 43, 612, 90)
    product('monit-docker', 'Monit-Docker', 40, 65, 'Container observations')
    product('certlord', 'CertLord', 380, 65, 'Certificate observations')

    # The two callers reach the source group at different ports: no crossing.
    line('M166 270 V135')
    text(180, 197, 'Reads source APIs', 'small')
    text(180, 215, 'Direct adapters', 'small')

    box(364, 170, 272, 240, '#fcfdfe', dashed=True)
    text(380, 190, 'OPTIONAL METRICS ROUTE', 'tag')
    box(380, 206, 240, 66)
    product('covenant', 'Covenant', 394, 220, 'Exposes collected metrics')
    line('M592 206 V135')
    text(578, 155, 'Reads source APIs', 'small', 'end')
    box(380, 330, 240, 64)
    product('prometheus', 'Prometheus', 394, 343, 'Metrics storage · alert rules')
    line('M500 328 V274')
    text(512, 299, 'Scrapes', 'small')
    text(512, 315, '/metrics', 'small')

    box(24, 270, 284, 140, '#fcfdfe')
    product('atraxis', 'Atraxis', 40, 285, '')
    text(40, 337, 'Inventory and operator console', 'body')
    text(40, 363, 'Web · terminal · authenticated API', 'small')
    text(40, 387, 'Source adapters · freshness · access', 'small')
    line('M310 365 H378')
    text(344, 341, 'Queries', 'small', 'middle')
    text(344, 356, 'API', 'small', 'middle')

    # Rule management is separate from read-only observation collection.
    line('M166 412 V488')
    text(180, 445, 'Calls authenticated API', 'small')
    text(180, 463, 'atropos-client', 'small')
    box(24, 490, 284, 90)
    product('atropos', 'Atropos', 40, 504, 'Rules · permissions · workflows')
    text(40, 563, 'Optional rule and workflow engine', 'small')

    line('M166 582 V653')
    text(180, 615, 'Authorized jobs', 'small')
    line('M310 545 H500 V653')
    text(512, 591, 'Configured', 'small')
    text(512, 608, 'notifications', 'small')

    # Prometheus notifications have their own lane outside the metrics group.
    line('M622 365 H658 V681 H622')
    text(646, 474, 'Sends metric alerts', 'small', 'end')

    box(24, 655, 284, 58)
    product('auton', 'Auton', 40, 665, 'Optional job execution')
    box(380, 655, 240, 58)
    product('alertmanager', 'Alertmanager', 394, 665, 'Optional notification routing')
    text(340, 739, 'Arrows show who calls whom. Query responses return in reverse.', 'small', 'middle')
    parts.append('</svg>')
    (OUT/'atraxis-architecture.svg').write_text(''.join(parts) + '\n')


if __name__ == '__main__':
    build()

