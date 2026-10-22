"""Bounded rule file decoding shared by command and terminal adapters."""
import json
from pathlib import Path

MAX_INPUT_BYTES = 65536

def read_text(path):
    with Path(path).open('rb') as stream:
        content = stream.read(MAX_INPUT_BYTES + 1)
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError('rule_input_too_large')
    return content.decode('utf-8')


def read_json(path):
    def pairs(values):
        output = {}
        for key, value in values:
            if key in output:
                raise ValueError('duplicate_json_key')
            output[key] = value
        return output
    return json.loads(read_text(path), object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('non_finite_json')))


