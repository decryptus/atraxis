"""Bounded search of an already authorized account-administration view."""
from dataclasses import dataclass, asdict
from urllib.parse import parse_qsl, urlsplit
from atraxis.accounts import identifier

KINDS = frozenset({'all', 'tenant', 'user', 'group', 'organization'})
MAX_QUERY = 128
MAX_PATH = 2048
MAX_PAGE = 100000
MAX_LIMIT = 200

@dataclass(frozen=True)
class AccountQuery:
    q: str = ''
    kind: str = 'all'
    organization: str = ''
    page: int = 1
    limit: int = 50

    def __post_init__(self):
        if (type(self.q) is not str or len(self.q)>MAX_QUERY or self.q and not self.q.isprintable()
            or type(self.kind) is not str or self.kind not in KINDS
            or type(self.organization) is not str or self.organization and not identifier(self.organization)
            or type(self.page) is not int or not 1<=self.page<=MAX_PAGE
            or type(self.limit) is not int or not 1<=self.limit<=MAX_LIMIT):
            raise ValueError('invalid_account_search')

    def parameters(self): return asdict(self)


def parse_query(path):
    if type(path) is not str or len(path.encode())>MAX_PATH: raise ValueError('invalid_account_search')
    pairs=parse_qsl(urlsplit(path).query,keep_blank_values=True,strict_parsing=True,max_num_fields=5,errors='strict')
    fields=dict(pairs)
    if len(fields)!=len(pairs) or set(fields)-set(AccountQuery().parameters()): raise ValueError('invalid_account_search')
    for key in ('page','limit'):
        if key in fields:
            if not fields[key].isascii() or not fields[key].isdigit() or len(fields[key])>6: raise ValueError('invalid_account_search')
            fields[key]=int(fields[key])
    return AccountQuery(**fields)


def search_accounts(data,query):
    rows=[];needle=query.q.casefold()
    for org in data['organizations']:
        if query.organization and org['id']!=query.organization: continue
        candidates=[dict(kind='organization',id=org['id'],name=org['name'])]
        candidates.extend(dict(kind='group',id=g['id'],name=g['name']) for g in org['groups'])
        # IDs are deliberately used for users: the administration projection does
        # not disclose the global directory or users outside visible memberships.
        candidates.extend(dict(kind='user',id=u,name=u) for u in sorted({m['user'] for m in org['memberships']}))
        for row in candidates:
            if query.kind!='all' and row['kind']!=query.kind: continue
            if needle and not any(needle in row[k].casefold() for k in ('id','name')): continue
            rows.append(dict(row,organization=org['id']))
    for kind, entities in data.get('entities', {}).items():
        for entity in entities:
            row = dict(kind=kind[:-1], id=entity['id'], name=entity['name'],
                       organization=entity.get('organization', entity['id'] if kind=='organizations' else ''))
            if query.organization and row['organization'] != query.organization: continue
            if query.kind != 'all' and row['kind'] != query.kind: continue
            if needle and not any(needle in row[k].casefold() for k in ('id','name')): continue
            rows.append(row)
    rows = list({(r['organization'], r['kind'], r['id']): r for r in rows}.values())
    rows.sort(key=lambda row:(row['organization'],row['kind'],row['id']))
    start=(query.page-1)*query.limit
    return dict(subject=data['subject'],revision=data['revision'],query=query.parameters(),
                total=len(rows),items=rows[start:start+query.limit],has_more=start+query.limit<len(rows))
