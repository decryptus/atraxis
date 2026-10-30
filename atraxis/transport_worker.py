"""Private one-request process. Parent deadline includes DNS and blocking I/O."""
import base64
import http.client
from ipaddress import ip_address
import json
import math
import socket
import ssl
import sys
from urllib.parse import urlsplit, urlencode

from atraxis.source_config import (MAX_CA_BYTES, MAX_RESPONSE_BYTES, MAX_TIMEOUT,
                                 MIN_RESPONSE_BYTES, MIN_TIMEOUT,
                                 InvalidSourceConfiguration, _origin, _read_file,
                                 _unique_pairs, read_credentials)
from atraxis.read_request import READ_ROUTES, INVENTORY_KIND, SEARCH_KIND
from atraxis.accounts import identifier
from atraxis.source_adapters import SOURCE_KINDS, get_adapter


MAX_REQUEST_BYTES = 131072
READ_CHUNK_BYTES = 65536
STATUS_OUTCOMES = {401: 'authentication_error', 403: 'forbidden',
                   404: 'endpoint_unavailable', 410: 'endpoint_unavailable'}


def reject_constant(value):
    raise ValueError('invalid_payload')


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('invalid_payload')
    return number


def fetch(document):
    connection = None
    try:
        origin = _origin(document['origin'], document['origin'].startswith('http://'))
        route = (get_adapter(document['kind']).read_route(document)
                 if document['kind'] in SOURCE_KINDS else READ_ROUTES[document['kind']])
        organization = document.get('organization')
        if organization is not None:
            if document['kind'] not in (INVENTORY_KIND, SEARCH_KIND) or not identifier(organization):
                return {'outcome': 'configuration_error'}
            route += '?' + urlencode({'organization': organization})
        limit, timeout = document['max_response_bytes'], document['timeout']
        if type(limit) is not int or not MIN_RESPONSE_BYTES <= limit <= MAX_RESPONSE_BYTES:
            return {'outcome': 'configuration_error'}
        if type(timeout) is not int or not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
            return {'outcome': 'configuration_error'}
        if document.get('search_query') is not None:
            from atraxis.inventory_search import SearchQuery
            if document['kind'] != SEARCH_KIND or type(document['search_query']) is not dict:
                return {'outcome': 'configuration_error'}
            query = SearchQuery(**document['search_query'])
            route += ('&' if '?' in route else '?') + urlencode(query.parameters())
        _, credentials = read_credentials(document['credentials_file'], '.')
        authorization = base64.b64encode((credentials['username'] + ':' +
                                         credentials['password']).encode('utf-8')).decode('ascii')
        parsed = urlsplit(origin)
        if parsed.scheme == 'https':
            context = ssl.create_default_context()
            if document['ca_file'] is not None:
                _, data = _read_file(document['ca_file'], '.', MAX_CA_BYTES)
                context = ssl.create_default_context(cadata=data.decode('ascii'))
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port,
                                                     timeout=timeout, context=context)
        else:
            # Revalidate at the I/O boundary, including hand-built SourceConfig.
            if not ip_address(parsed.hostname).is_loopback or document['ca_file'] is not None:
                return {'outcome': 'configuration_error'}
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
        connection.request('GET', route, headers={'Authorization': 'Basic ' + authorization,
                           'Accept': 'application/json', 'Accept-Encoding': 'identity',
                           'Connection': 'close', 'User-Agent': 'Atraxis-read-only/0.0.1'})
        response = connection.getresponse()
        if 300 <= response.status <= 399:
            return {'outcome': 'redirect_refused'}
        if response.status == 409 and document['kind'] in (INVENTORY_KIND, SEARCH_KIND):
            return {'outcome': 'organization_required'}
        if response.status != 200:
            return {'outcome': STATUS_OUTCOMES.get(response.status, 'source_error')}
        if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
            return {'outcome': 'invalid_payload'}
        if response.getheader('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return {'outcome': 'invalid_payload'}
        lengths = response.headers.get_all('Content-Length', [])
        transfers = response.headers.get_all('Transfer-Encoding', [])
        if (len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers)
                or (transfers and transfers[0].lower() != 'chunked')):
            return {'outcome': 'invalid_payload'}
        length = None
        if lengths:
            if not lengths[0].isascii() or not lengths[0].isdigit():
                return {'outcome': 'invalid_payload'}
            length = int(lengths[0])
            if length > limit:
                return {'outcome': 'response_too_large'}
        body = bytearray()
        while len(body) <= limit:
            chunk = response.read(min(READ_CHUNK_BYTES, limit + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > limit:
            return {'outcome': 'response_too_large'}
        if length is not None and len(body) != length:
            return {'outcome': 'invalid_payload'}
        payload = json.loads(body.decode('utf-8'), object_pairs_hook=_unique_pairs,
                             parse_constant=reject_constant, parse_float=finite_float)
        if type(payload) is not dict:
            return {'outcome': 'invalid_payload'}
        return {'outcome': 'success', 'body': base64.b64encode(body).decode('ascii')}
    except InvalidSourceConfiguration:
        return {'outcome': 'configuration_error' if connection is None else 'invalid_payload'}
    except ssl.SSLError:
        return {'outcome': 'tls_error'}
    except (TimeoutError, socket.timeout):
        return {'outcome': 'timeout'}
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return {'outcome': 'invalid_payload' if connection is not None else 'configuration_error'}
    except (OSError, http.client.HTTPException):
        return {'outcome': 'transport_error'}
    finally:
        if connection is not None:
            connection.close()


def main():
    try:
        data = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        answer = ({'outcome': 'configuration_error'} if len(data) > MAX_REQUEST_BYTES
                  else fetch(json.loads(data)))
    except Exception:
        answer = {'outcome': 'transport_error'}
    sys.stdout.write(json.dumps(answer))


if __name__ == '__main__':
    main()
