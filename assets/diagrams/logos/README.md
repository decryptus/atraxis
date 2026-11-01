# Architecture artwork

Product symbols are copied from the existing Atraxis ecosystem website assets.
The Atropos symbol is from decryptus/atropos; other project symbols retain their
original geometry and colours. All product names use the same diagram typography.

Prometheus and Alertmanager use the official Prometheus flame from
`prometheus/prometheus`, `documentation/images/prometheus-logo.svg`,
blob `026f9e5bcc779247068b165976fd498d155ecf4b`. See LICENSE-prometheus.txt.
Names and trademarks remain with their respective projects; inclusion does not
imply endorsement.

Run `python scripts/build_architecture.py` from the Atraxis repository to rebuild
the self-contained English SVG. Arrows indicate the caller and called service;
query responses travel back in the opposite direction.
