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
archives for 1.0.5. PRs validate their hashes and install the wheel. Once reviewed,
advance `release/publish-pypi-1.0.5` to current main. PyPI must trust
`decryptus/atraxis`, workflow `publish-pypi.yml`, environment `pypi`.
The workflow checks each existing registry file against the release and uploads
only missing files. Its final job downloads both files and installs from the index.

### Verified 1.0.5 promotion inputs

The immutable [v1.0.5 tag](https://github.com/decryptus/atraxis/releases/tag/v1.0.5)
points to `a419b0303d22cf47d9503a1a441639d0e629edb9`. The Linux amd64 image
`decryptus/atraxis:1.0.5` has verified registry digest
`sha256:092d8494279ff253f3cd2287c83f809a341f5835ab837621e8549354300b980e` and records that same revision.
The promotion PR pulls that exact image on a fresh runner and repeats its installed-image checks.

The four downloaded GitHub release assets were verified byte for byte:

| Asset | SHA256 |
| --- | --- |
| `atraxis-1.0.5-py3-none-any.whl` | `0b205d5953e9380ad461530464b590f1b706a41658d1cb9a2156984336d6c101` |
| `atraxis-1.0.5.tar.gz` | `91181825536d0b911baa7bff2c5a1b8ae51f129b25f288e2f2adaa2f20e74720` |
| `release-provenance.json` | `d999b1ed1bda22eca75a5b774e1f5d3b7cbf541587f1a8765fa2cd0575faaf3e` |
| `SHA256SUMS` | `eae8550c2ec77eb8525d036b4c987f3ee29628eb59afa0f700eea248b5695df7` |

PyPI promotion uploads the two distribution archives above without rebuilding.
The tag, release assets and qualification evidence remain immutable.
