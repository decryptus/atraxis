"""Bounded CLI requests and revision-bound reviews for local administrators."""
import hashlib
import json
import re

import yaml

from atraxis.accounts import AccountError
from atraxis.config import _StrictLoader
from atraxis.rule_files import read_private

MAX_REQUEST_BYTES = 32768
REVIEW_REVISION = re.compile(r'[0-9a-f]{64}')


def add_review_commands(actions):
    actions.add_parser('show', help='show current authorized state and recent audit')
    for name in ('check', 'apply'):
        command = actions.add_parser(name)
        command.add_argument('--file', required=True, help='private YAML or JSON change request')
        if name == 'apply':
            command.add_argument('--expected-revision', required=True, help='revision from check for this exact request')
            command.add_argument('--confirm', required=True, action='store_true')


def read_request(path):
    return yaml.load(read_private(path, MAX_REQUEST_BYTES), Loader=_StrictLoader)


def review_revision(admin, plan):
    """Bind the request, identity, authority and data revisions without exposing secrets."""
    review = dict(plan=plan, profile=str(admin.path),
                  configuration=str(admin.config_path) if hasattr(admin, 'config_path') else None)
    raw = json.dumps(review, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def apply_review(admin, plan, expected_revision):
    if (type(expected_revision) is not str or not REVIEW_REVISION.fullmatch(expected_revision)
            or review_revision(admin, plan) != expected_revision):
        raise AccountError('administration_review_conflict')
    # The domain commit revalidates the complete plan under its ordered locks.
    return admin.commit(plan)
