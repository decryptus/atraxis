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
archives for 1.0.6. PRs validate their hashes and install the wheel. Once reviewed,
advance `release/publish-pypi-1.0.6` to current main. PyPI must trust
`decryptus/atraxis`, workflow `publish-pypi.yml`, environment `pypi`.
The workflow checks each existing registry file against the release and uploads
only missing files. Its final job downloads both files and installs from the index.

### Verified 1.0.6 promotion inputs

The immutable [v1.0.6 tag](https://github.com/decryptus/atraxis/releases/tag/v1.0.6)
points to `474bd3e5ee999e9f68dc869fbd1707c160a38fa4`. The Linux amd64 image
`decryptus/atraxis:1.0.6` has verified registry digest
`sha256:cfc79270d1c4ec5e768cdafca466084ed486c8dc7b8c04606797c0596fa21157` and records that same revision.
The promotion PR pulls that exact image on a fresh runner and repeats its installed-image checks.

The four downloaded GitHub release assets were verified byte for byte:

| Asset | SHA256 |
| --- | --- |
| `atraxis-1.0.6-py3-none-any.whl` | `650ecdb6ee92ae3d18fff049cefb44354307e35a2e47cb3945e5a13aaae300fc` |
| `atraxis-1.0.6.tar.gz` | `60959cd251f3517a84974b7f1c61081b04430e244d144e1417727a07f3450497` |
| `release-provenance.json` | `e55a665dec7920105fcca0982308064b5b7be697e3470b3a3cb6257f7a36f8f3` |
| `SHA256SUMS` | `88c2145cc4fe3bf31c945fb41923e7b0ee3fc7b58a1212cea3ecbc8c07e21fca` |

PyPI promotion uploads the two distribution archives above without rebuilding.
The tag, release assets and qualification evidence remain immutable.
