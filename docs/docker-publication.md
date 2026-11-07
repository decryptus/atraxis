# Container build and publication

The Docker Hub workflow builds `deploy/docker/Dockerfile` on PRs and main without
pushing. The root Dockerfile remains the base-package development fixture. The
published-image recipe includes the optional rules client from its published
wheel, with its SHA-256 fixed in `deploy/docker/client.txt`. It reuses the Compose
Python image, Debian snapshot, builder and web dependency locks; those locks must
remain consistent with the package version. This is Linux amd64 coverage only.

Image acceptance checks UID/GID 10001, dependency consistency, the published client
and absence of engine/ZEN. It runs the existing client suite, real inventory API
and SIGTERM checks, and recreates the container against a persistent account
volume. The dependency inventory is retained in the image at
`/usr/local/share/atraxis/environment.json`; CI image evidence is kept for 14 days.
This does not extend operational or endurance claims to arbitrary deployments.

## Publish

Finalize the stable package and source tag before publishing. Development
versions cannot be published under stable tags. Configure repository secret
**DOCKERHUB_TOKEN** for Docker Hub user `decryptus`, with write access to
`decryptus/atraxis`. Use **Docker Hub image → Run workflow**, branch `main`, with
`publish` selected, or explicitly create/update `release/dockerhub` at the
reviewed release commit. Main pushes and PRs only build/test.

The publication helper requires `vX.Y.Z` to exist and the runtime file set,
all runtime Python bytes and package metadata to match that source tag. It pushes
the exact tested image without rebuilding. Existing version tags cannot be
replaced by different image bytes; identical bytes permit retries. Moving aliases
`X.Y`, `X` and `latest` cannot downgrade newer stable tags. Registry errors abort
publication. The OCI revision records the recipe commit separately from the
stable package version. Do not rewrite existing release tags to add a recipe.

After publication, pull the exact tag, verify its digest/metadata and exercise the
pulled image before announcing availability. Publishing does not deploy a site.

## Python package publication

The `publish-pypi.yml` workflow promotes the two signed-off GitHub release
archives for 1.0.9. PRs validate their hashes and install the wheel. Once reviewed,
advance `release/publish-pypi-1.0.9` to current main. PyPI must trust
`decryptus/atraxis`, workflow `publish-pypi.yml`, environment `pypi`.
The workflow checks each existing registry file against the release and uploads
only missing files. Its final job downloads both files and installs from the index.

### Verified 1.0.9 promotion inputs

The immutable [v1.0.9 tag](https://github.com/decryptus/atraxis/releases/tag/v1.0.9)
points to `607029b358dd77a8461b5138589be2760b11de44`. The Linux amd64 image
`decryptus/atraxis:1.0.7` has verified registry digest
`sha256:f75291bd5666e285a35550ad1fa39a4c2ee5b3916c877742f32fbb7633ebba4e` and records the 1.0.7 service revision `8e51fa15fa9c9348d8d253ac300ba90493ecf97f`.
The promotion PR pulls that exact image on a fresh runner and repeats its installed-image checks.

The four downloaded GitHub release assets were verified byte for byte:

| Asset | SHA256 |
| --- | --- |
| `atraxis-1.0.9-py3-none-any.whl` | `1803680bc10df265bb8cce2f8a8445766c167dbcb48f73329742f3529aaeddf4` |
| `atraxis-1.0.9.tar.gz` | `be8c1f18f4f5162c3365ffe77e5e6727f64ec7e6d4b73ef00fde1ecc71ee877c` |
| `release-provenance.json` | `52cf8ad395f227f111849502d75a56c587102d0f3701dd77b005b1fe9e4176c7` |
| `SHA256SUMS` | `395827bd1c76657537fdce826eb4aad00725bf0303f5bbb6664cb5eba624c17e` |

PyPI promotion uploads the two distribution archives above without rebuilding.
The tag, release assets and qualification evidence remain immutable.
