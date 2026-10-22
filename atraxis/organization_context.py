"""Explicit organization selection; a selection never grants access."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from atraxis.accounts import identifier

MAX_PATH_BYTES = 4096
MAX_QUERY_FIELDS = 32


def selected(entries, organization=None):
    if organization is not None and not identifier(organization):
        raise ValueError('invalid_organization_context')
    matches = [e for e in entries if organization is None or e['organization']==organization]
    if len(matches)!=1:
        raise PermissionError('organization_selection_required' if organization is None and matches else 'organization_access_denied')
    return matches[0]


def split_path(path):
    if type(path) is not str or len(path.encode())>MAX_PATH_BYTES:
        raise ValueError('invalid_organization_context')
    parsed=urlsplit(path)
    pairs=parse_qsl(parsed.query,keep_blank_values=True,max_num_fields=MAX_QUERY_FIELDS,errors='strict')
    organizations=[value for key,value in pairs if key=='organization']
    if len(organizations)>1 or organizations and not identifier(organizations[0]):
        raise ValueError('invalid_organization_context')
    remaining=[(key,value) for key,value in pairs if key!='organization']
    return urlunsplit(parsed._replace(query=urlencode(remaining))), organizations[0] if organizations else None


def scoped_url(url, organization):
    parsed=urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.path not in ('/ui','/ui/search','/ui/interventions','/ui/interventions/detail') and not parsed.path.startswith('/ui/rules'):
        return url
    pairs=parse_qsl(parsed.query,keep_blank_values=True)
    if any(key=='organization' for key,_ in pairs): return url
    return urlunsplit(parsed._replace(query=urlencode(pairs+[('organization',organization)])))
