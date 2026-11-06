"""Verify candidate authentication through real nginx on a disposable network."""
import json


CLIENT = '''import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import ssl
import sys
import time

ca, password = json.loads(sys.argv[1])
context = ssl.create_default_context(cadata=ca)

def request(index, username=None, secret=None, address='127.0.0.2'):
    # Different claimed addresses must not reset the actual TCP client's budget.
    headers = {'X-Forwarded-For': f'192.0.2.{index + 1}',
               'X-Real-IP': f'198.51.100.{index + 1}'}
    if username is not None:
        token = base64.b64encode(f'{username}:{secret}'.encode()).decode()
        headers['Authorization'] = 'Basic ' + token
    connection = http.client.HTTPSConnection('localhost', 8443, context=context,
        source_address=(address, 0), timeout=10)
    try:
        connection.request('GET', '/healthz', headers=headers)
        response = connection.getresponse()
        assert len(response.read(65537)) <= 65536
        return response.status, response.getheader('Retry-After')
    finally:
        connection.close()

with ThreadPoolExecutor(max_workers=16) as pool:
    burst = list(pool.map(request, range(80)))
assert {status for status, _ in burst} <= {401, 429}, 'unexpected_burst_status'
assert any(status == 429 for status, _ in burst), 'ingress_burst_not_limited'
assert request(0, 'alice', password, '127.0.0.3')[0] == 200, 'other_client_blocked'
time.sleep(3)

# Stay below nginx's rate while exceeding the old shared 64-failure budget.
started = time.monotonic()
for index in range(65):
    assert request(index, f'unknown-{index}', 'wrong')[0] == 401, 'shared_peer_lockout'
    time.sleep(0.21)
assert time.monotonic() - started < 60, 'spray_exceeded_failure_window'
assert request(0, 'alice', password, '127.0.0.3')[0] == 200, 'other_account_blocked'

for index in range(8):
    assert request(index, 'targeted-account', 'wrong')[0] == 401, 'premature_account_limit'
    time.sleep(0.21)
status, retry = request(9, 'targeted-account', 'wrong')
assert status == 429 and retry and 0 < int(retry) <= 60, 'account_limit_missing'
assert request(0, 'alice', password, '127.0.0.3')[0] == 200, 'account_limit_not_isolated'
print(json.dumps(dict(nginx_burst_rejected=True, forwarded_headers_ignored=True,
    distinct_client_access=True, unrelated_failures=65, same_account_limit=8)))
'''


def exercise_ingress(compose, authority, password):
    # Execute inside the candidate web container's shared proxy namespace. Two
    # real loopback source addresses reach nginx; both reach Atraxis as 127.0.0.1.
    result = compose('exec', '-T', 'web', 'python', '-c', CLIENT,
                     json.dumps([authority.pem().decode(), password]), timeout=60)
    return json.loads(result.stdout)
