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
archives for 1.0.7. PRs validate their hashes and install the wheel. Once reviewed,
advance `release/publish-pypi-1.0.7` to current main. PyPI must trust
`decryptus/atraxis`, workflow `publish-pypi.yml`, environment `pypi`.
The workflow checks each existing registry file against the release and uploads
only missing files. Its final job downloads both files and installs from the index.

### Verified 1.0.7 promotion inputs

The immutable [v1.0.7 tag](https://github.com/decryptus/atraxis/releases/tag/v1.0.7)
points to `8e51fa15fa9c9348d8d253ac300ba90493ecf97f`. The Linux amd64 image
`decryptus/atraxis:1.0.7` has verified registry digest
`sha256:f75291bd5666e285a35550ad1fa39a4c2ee5b3916c877742f32fbb7633ebba4e` and records that same revision.
The promotion PR pulls that exact image on a fresh runner and repeats its installed-image checks.

The four downloaded GitHub release assets were verified byte for byte:

| Asset | SHA256 |
| --- | --- |
| `atraxis-1.0.7-py3-none-any.whl` | `771ecf96fdcb6761b9567fec60914979f11242cf4c66426d4153405e00db766a` |
| `atraxis-1.0.7.tar.gz` | `ebf1c572cf2bbc99f336847481dd4f08d28b73943c9d19fb7bf64a3ca7160330` |
| `release-provenance.json` | `1e61887ccdcd9b40bd7686100b588be94e99eaed058cb464d4c2608186753580` |
| `SHA256SUMS` | `357afe2fdbf6df9d71975ef4bd5d3c819936763c9aec189fb22e37c19140673b` |

PyPI promotion uploads the two distribution archives above without rebuilding.
The tag, release assets and qualification evidence remain immutable.
