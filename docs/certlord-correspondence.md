# CertLord complete observation acceptance

CertLord correspondence now uses the JSON observation contract, including domains,
versions, fingerprints and full TLS verification evidence. The implementation and
shared acceptance criteria are described in [complete observation correspondence](source-correspondence.md).

`test_certlord_correspondence.py` compares the entire certificate field dictionary
and source metadata through real Covenant and two Prometheus instances. It checks
renewal with stable UUID, label transitions, verification failure, missing material,
zero retries, expiry, deletion, stale individual observations, source/scrape failure
and recovery. Native `certificate_observation` is exercised with synthetic material.
A fresh collection with an old certificate must be stale in both paths.

The source's API consumer parser remains the validation authority. No subset-only
comparison or assertion that missing fields are expected can establish parity.
