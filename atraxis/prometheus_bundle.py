"""Bounded matrix decoding shared by explicit source metric contracts."""

import math
from atraxis.parsing import number, require
from atraxis.prometheus import (
    LABEL_NAME,
    MAX_SERIES_SAMPLES,
    MAX_TOTAL_SAMPLES,
    SAMPLE_VALUE,
    label_value,
)

MAX_RAW_LABELS = 32


def matrix_series(payload, names, match, max_series):
    require(type(payload) is dict and payload.get("status") == "success")
    require(not payload.get("warnings") and not payload.get("infos"))
    data = payload.get("data")
    require(type(data) is dict and data.get("resultType") == "matrix")
    rows = data.get("result")
    require(type(rows) is list and len(rows) <= max_series)
    grouped = {suffix: [] for suffix in names.values()}
    total = 0
    for row in rows:
        require(type(row) is dict and set(row) == {"metric", "values"})
        labels, samples = row["metric"], row["values"]
        require(type(labels) is dict and len(labels) <= MAX_RAW_LABELS)
        require(
            all(
                type(k) is str and LABEL_NAME.fullmatch(k) and label_value(v)
                for k, v in labels.items()
            )
        )
        require(labels.get("__name__") in names)
        require(all(labels.get(k) == v for k, v in match.items()))
        require(type(samples) is list and 1 <= len(samples) <= MAX_SERIES_SAMPLES)
        total += len(samples)
        require(total <= MAX_TOTAL_SAMPLES)
        previous = -1
        for sample in samples:
            require(type(sample) is list and len(sample) == 2)
            stamp, raw = sample
            number(stamp)
            require(stamp > previous)
            previous = stamp
            require(type(raw) is str and len(raw) <= 64 and SAMPLE_VALUE.fullmatch(raw))
            value = float(raw)
            require(math.isfinite(value))
        grouped[names[labels["__name__"]]].append((stamp, value, labels))
    return grouped
