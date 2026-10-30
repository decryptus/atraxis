"""Bounded inventory search client, no fallback to unscoped or loaded-row search."""
from dataclasses import replace
from uuid import UUID
from atraxis.client import (_object, CONTAINER_FIELDS, CERTIFICATE_FIELDS, DOCKER_ID,
                            REASONS, validate_inventory)
from atraxis.parsing import require, text
from atraxis.transport import HTTPTransport, WORKER_OUTCOMES
from atraxis.read_request import SEARCH_KIND
from atraxis.inventory_search import SearchQuery
from atraxis.prometheus import validate_fields as metric_fields

RESULT_FIELDS = frozenset({'schema_version','context','query','ready','reason','total','items','unavailable','has_more'})
ROW_FIELDS = frozenset({'source_id','kind','native_id','state','fields'})
MAX_TOTAL = 320000
MAX_SOURCES = 32


def validate_result(data, query, organization):
    require(type(data) is dict and set(data) == RESULT_FIELDS)
    require(type(data['schema_version']) is int and data['schema_version'] == 1)
    require(type(data['query']) is dict)
    require(SearchQuery(**data['query']).parameters() == query.parameters())
    require(type(data['ready']) is bool and type(data['has_more']) is bool)
    require((data['ready'] and data['reason'] is None) or
            (not data['ready'] and type(data['reason']) is str and data['reason'] in REASONS))
    context=data['context']
    if context is not None:
        validate_inventory(dict(schema_version=1, ready=False, reason='no_visible_sources', collecting=False, sources=[], context=context))
    require(organization is None or context is not None and context['organization'] == organization)
    require(type(data['total']) is int and 0 <= data['total'] <= MAX_TOTAL)
    require(type(data['items']) is list and len(data['items']) == min(query.limit,max(0,data['total']-(query.page-1)*query.limit)))
    require(data['has_more'] == (query.page*query.limit < data['total']))
    rows=[];seen=set()
    for row in data['items']:
        require(type(row) is dict and set(row) == ROW_FIELDS)
        sid=text(row['source_id']);native=text(row['native_id'])
        require(type(row['kind']) is str and row['kind'] in ('container','certificate','metric'))
        require(type(row['state']) is str and row['state'] in ('current','historical'))
        require(DOCKER_ID.fullmatch(native) is not None if row['kind'] in ('container','metric') else str(UUID(native))==native)
        key=sid,row['kind'],native
        require(key not in seen);seen.add(key)
        fields=(metric_fields(row['fields'],native) if row['kind']=='metric' else
                _object(row['fields'],CONTAINER_FIELDS if row['kind']=='container' else CERTIFICATE_FIELDS))
        if row['kind']=='container':text(fields.get('name'))
        elif row['kind']=='certificate':
            require(type(fields.get('domains')) is list and bool(fields['domains']))
            for domain in fields['domains']:text(domain)
        rows.append(dict(row,fields=fields))
    unavailable=data['unavailable']
    require(type(unavailable) is list and len(unavailable)<=MAX_SOURCES)
    for item in unavailable:
        require(type(item) is dict and set(item)=={'source_id','state'})
        text(item['source_id'])
        require(type(item['state']) is str and item['state'] in ('historical','unknown'))
    return dict(data,items=rows)


def read_search(config, query, cancel=None):
    request=replace(config,kind=SEARCH_KIND,search_query=query.parameters())
    result=HTTPTransport().fetch(request,cancel=cancel)
    if result.outcome!='success':
        return None,result.outcome if result.outcome in WORKER_OUTCOMES | {'cancelled','busy'} else 'transport_error'
    try:
        return validate_result(result.payload,query,config.organization),None
    except (ValueError,TypeError,KeyError,OverflowError,RecursionError):
        return None,'invalid_payload'
