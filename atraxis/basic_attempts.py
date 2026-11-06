"""Process-local limits on failed Basic checks at the inventory listener."""
import base64
from collections import OrderedDict
from hashlib import sha256
from math import ceil
from threading import Lock
from time import monotonic

MAX_FAILURES = 8
MAX_PEER_FAILURES = 64
WINDOW_SECONDS = 60
MAX_PEERS = 4096


class BasicAttempts:
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.lock = Lock()
        self.peers = OrderedDict()

    @staticmethod
    def account_key(authorization):
        # Use the same Basic username bytes as HTTPdis, without retaining the
        # claimed name or consulting the account list before authentication.
        try:
            scheme, encoded = authorization.split(None, 1)
            user, separator, _ = base64.b64decode(encoded).partition(b':')
            if scheme.lower() == 'basic' and separator:
                return sha256(user).digest()
        except ValueError:
            pass
        return None

    def _expire(self, now):
        while self.peers:
            oldest = next(iter(self.peers.values()))
            if oldest['until'] > now:
                break
            self.peers.popitem(last=False)

    def retry_after(self, peer, account):
        with self.lock:
            now = self.clock()
            self._expire(now)
            entry = self.peers.get(peer)
            if entry is None:
                return 0
            elif entry['total'] < MAX_PEER_FAILURES and entry['accounts'].get(account, 0) < MAX_FAILURES:
                return 0
            return max(1, ceil(entry['until'] - now))

    def failed(self, peer, account):
        with self.lock:
            now = self.clock()
            self._expire(now)
            entry = self.peers.get(peer)
            if entry is None:
                if len(self.peers) >= MAX_PEERS:
                    self.peers.popitem(last=False)
                entry = self.peers[peer] = dict(until=now + WINDOW_SECONDS, total=0, accounts={})
            # Concurrent checks already admitted may finish after this limit.
            if entry['total'] < MAX_PEER_FAILURES:
                entry['total'] += 1
                accounts = entry['accounts']
                accounts[account] = accounts.get(account, 0) + 1
