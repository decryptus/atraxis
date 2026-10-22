"""Presentation contract for engine-owned intervention search; no storage access."""
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
from urllib.parse import parse_qsl,urlsplit
import json
import math
import re
from atraxis.web_rules import NAME,IDENTIFIER,REVISION

STATUSES=frozenset({'queued','running','retrying','succeeded','failed','cancelled','uncertain'})
MODES=frozenset({'all','real','test'})
MAX_ROWS=10000
MAX_PAGE=100000
MAX_LIMIT=200
MAX_TEXT=128
MAX_BYTES=524288
DIGEST=re.compile(r'[a-f0-9]{64}')
FIELDS=frozenset({'id','rule','revision','event_id','mode','status','target','workflow','created_at','test_context'})

@dataclass(frozen=True)
class InterventionQuery:
    q: str = ''
    mode: str = 'all'
    status: str = ''
    target: str = ''
    workflow: str = ''
    since: int | None = None
    until: int | None = None
    page: int = 1
    limit: int = 50

    def __post_init__(self):
        if (any(type(v) is not str or len(v)>MAX_TEXT or v and not v.isprintable() for v in (self.q,self.target,self.workflow))
            or type(self.mode) is not str or self.mode not in MODES
            or type(self.status) is not str or self.status and self.status not in STATUSES
            or any(v is not None and (type(v) is not int or not 0<=v<=253402300799) for v in (self.since,self.until))
            or self.since is not None and self.until is not None and self.since>self.until
            or type(self.page) is not int or not 1<=self.page<=MAX_PAGE
            or type(self.limit) is not int or not 1<=self.limit<=MAX_LIMIT):raise ValueError('invalid_intervention_search')

    def parameters(self):
        result=asdict(self);result['query']=result.pop('q');return result


def date_value(value):
    if not value:return None
    if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z?',value):raise ValueError('invalid_search_date')
    return int(datetime.fromisoformat(value.removesuffix('Z')).replace(tzinfo=timezone.utc).timestamp())


def date_text(value):
    return '' if value is None else datetime.fromtimestamp(value,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def form_values(query):
    result=asdict(query)
    for key in ('since','until'):result[key]=date_text(result[key])
    return result


def parse_query(path):
    if type(path) is not str or len(path.encode())>4096:raise ValueError('invalid_intervention_search')
    pairs=parse_qsl(urlsplit(path).query,keep_blank_values=True,strict_parsing=True,max_num_fields=11,errors='strict')
    values=dict(pairs)
    if len(values)!=len(pairs) or set(values)-set(asdict(InterventionQuery()))-{'name'}:raise ValueError('invalid_intervention_search')
    name=values.pop('name','')
    if name and not NAME.fullmatch(name):raise ValueError('invalid_intervention_search')
    for key in ('page','limit'):
        if key in values:
            if not values[key].isascii() or not values[key].isdigit() or len(values[key])>6:raise ValueError('invalid_intervention_search')
            values[key]=int(values[key])
    for key in ('since','until'):
        if key in values:values[key]=date_value(values[key])
    return name,InterventionQuery(**values)


def checked(data,name,query,actor=None):
    if (type(data) is not dict or set(data)!={'schema_version','subject','organization','rule','query','total','items','has_more'}
        or type(data['schema_version']) is not int or data['schema_version']!=1
        or data['rule']!=name or data['query']!=query.parameters()
        or any(type(data[k]) is not str or not IDENTIFIER.fullmatch(data[k]) for k in ('subject','organization'))
        or actor is not None and any(data[k]!=actor[k] for k in ('subject','organization'))
        or type(data['total']) is not int or not 0<=data['total']<=MAX_ROWS
        or type(data['has_more']) is not bool or type(data['items']) is not list
        or len(json.dumps(data,allow_nan=False).encode())>MAX_BYTES):raise ValueError('invalid_intervention_result')
    start=(query.page-1)*query.limit
    if len(data['items'])!=min(query.limit,max(0,data['total']-start)) or data['has_more']!=(start+query.limit<data['total']):raise ValueError('invalid_intervention_result')
    seen=set()
    for row in data['items']:
        if (type(row) is not dict or set(row)!=FIELDS
            or any(type(row[k]) is not str for k in FIELDS-{'created_at','test_context'})
            or not DIGEST.fullmatch(row['id']) or not REVISION.fullmatch(row['revision'])
            or row['rule']!=name or not IDENTIFIER.fullmatch(row['event_id']) or not NAME.fullmatch(row['workflow'])
            or row['mode'] not in ('real','test') or row['status'] not in STATUSES
            or row['mode']=='test' and (type(row['test_context']) is not dict
                or set(row['test_context'])!={'test','id'} or row['test_context'].get('test') is not True
                or row['test_context'].get('id')!=row['id'])
            or row['mode']=='real' and row['test_context'] is not None
            or not row['target'].startswith(data['organization']+'/') or len(row['target'])>256
            or type(row['created_at']) not in (int,float) or not math.isfinite(row['created_at'])
            or not 0<=row['created_at']<=253402300799):raise ValueError('invalid_intervention_result')
        key=(row['mode'],row['id'])
        if key in seen:raise ValueError('invalid_intervention_result')
        seen.add(key)
    return data


class SearchService:
    def __init__(self,application,name):self.application,self.name=application,name
    def search(self,query):
        if not NAME.fullmatch(self.name):raise ValueError('invalid_intervention_search')
        return checked(self.application.search_interventions(self.name,**query.parameters()),self.name,query)
