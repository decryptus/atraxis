"""Real fixture HTTP: count every accepted request and deliberately lose its ACK."""
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'compose_tests/uncertain_target.py'
SPEC = importlib.util.spec_from_file_location('uncertain_target', MODULE)
target = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(target)
TOKEN = 'disposable-receipt-test-token'
PAYLOAD = [dict(labels=dict(atropos_delivery_id='d' * 64),
    annotations=dict(atropos_run_id='a' * 64, atropos_step='send'))]


class UncertainTargetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'receipts.jsonl'

    def request(self, port, token=TOKEN, payload=PAYLOAD, path=target.API_PATH):
        connection = http.client.HTTPConnection(target.HOST, port, timeout=3)
        self.addCleanup(connection.close)
        connection.request('POST', path, json.dumps(payload).encode(),
            {'Authorization': 'Bearer ' + token})
        return connection.getresponse()

    def test_acceptance_is_durable_even_when_response_is_lost(self):
        with target.receiver(self.path, TOKEN, port=0) as port:
            with self.assertRaises(http.client.RemoteDisconnected):
                self.request(port)
        records = target.receipts(self.path)
        self.assertEqual(records, [dict(run='a' * 64, step='send', delivery='d' * 64,
            sha256=hashlib.sha256(json.dumps(PAYLOAD).encode()).hexdigest())])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_repeat_is_counted_across_receiver_recreation(self):
        for _ in range(2):
            with target.receiver(self.path, TOKEN, port=0) as port:
                with self.assertRaises(http.client.RemoteDisconnected):
                    self.request(port)
        self.assertEqual(len(target.receipts(self.path)), 2)

    def test_wrong_token_path_and_malformed_receipt_have_no_effect(self):
        with target.receiver(self.path, TOKEN, port=0) as port:
            for kwargs in (dict(token='invalid'), dict(path='/unexpected'), dict(payload={})):
                with self.subTest(kwargs=kwargs):
                    with self.request(port, **kwargs) as response:
                        self.assertEqual(response.status, 400)
                    self.assertEqual(target.receipts(self.path), [])

    def test_corrupt_or_oversized_ledger_is_not_replaced(self):
        for value in (b'corrupt\n', b'x' * (target.MAX_LEDGER_BYTES + 1)):
            self.path.write_bytes(value)
            with target.receiver(self.path, TOKEN, port=0) as port:
                with self.request(port) as response:
                    self.assertEqual(response.status, 400)
            self.assertEqual(self.path.read_bytes(), value)
