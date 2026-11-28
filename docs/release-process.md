# Development, qualification and release process

This contributor procedure separates four independent steps: merging code,
building a release candidate, qualifying that candidate operationally and
publishing a stable release. Operational qualification protects stable
publication; it does not gate ordinary development.

| Step | Trigger | Produces | Does not authorize |
| --- | --- | --- | --- |
| Merge | Reviewed PR, required PR workflows green | A commit on `main` or `stable/X.Y.Z` | Publication, deployment |
| Candidate | Merged `X.Y.ZrcN` version | GitHub prerelease with the exact CI archives | PyPI, Docker Hub, deployment |
| Qualification | A published candidate | Gate evidence bound to the candidate wheel SHA-256 | Publication by itself |
| Stable | Reviewed manifest closing G1, G2 and G3 | GitHub release, then PyPI, Docker Hub and site | Production upgrades |

## 1. Merging pull requests

- A development PR merges when the required PR workflows pass and the owner has
  reviewed it. No operational recovery, endurance or live-source run is required
  for a merge.
- Merging into `main` never publishes a package or image and never deploys a
  service. Publication workflows run only on their explicit triggers below.
- The PR workflows keep the existing quality bar: unit and integration suites with
  the collection guard, installed-package checks on every supported interpreter,
  Compose, chain, browser, registry and guided-installation acceptance.
- `main` may change runtime code while a previous candidate is being qualified.

## 2. Release candidates

- A candidate uses the PEP 440 version `X.Y.ZrcN`. Bump the version in a dedicated
  PR (`pyproject.toml`, `deploy/compose/locks/versions.json`, release notes
  `docs/releases/X.Y.ZrcN.md`). CI checks read the expected version from
  `pyproject.toml`; they hold no per-release constants.
- After that PR merges, the candidate workflow publishes the GitHub prerelease
  `vX.Y.ZrcN`. It reuses the Python 3.12 package artifact of the merged PR head,
  verifies the source commit, tree, file names, sizes and SHA-256 values, and never
  rebuilds after testing. An existing tag or release is never overwritten.
- Candidates are not uploaded to PyPI and no Docker Hub tag is pushed for them.
- A candidate is identified by its tag, source commit and wheel SHA-256. Every
  qualification record refers to that wheel SHA-256.
- Development continues on `main` immediately; a new candidate (`rcN+1`) can be
  cut at any time and supersedes the previous one for qualification.

## 3. Operational qualification

Qualification runs against the exact candidate wheel. Results are recorded as
evidence files, each carrying the candidate wheel SHA-256, the producing run and
its outcome. Missing, failed, stale or mismatched evidence leaves the gate open.

- **G1 — live sources.** The candidate reads every approved live source through
  its real transport and reports them current. Source authority must be unchanged.
- **G2 — cold recovery on a distinct host.** A recent cold capture of the
  operational data is restored on a separate, isolated host. The candidate serves
  the restored data with execution paused and zero workers; accounts, audits,
  SQLite integrity, TLS renewal, login revocation and throttling are verified.
  The capture comes from the existing nightly cold backup; qualification must not
  add an extra service interruption.
- **G3 — endurance.** The two-hour synthetic saturation and recovery profile runs
  on a disposable GitHub runner with an image built from the candidate wheel.

G3 is automatic: publishing a candidate dispatches the qualification workflow.
G1 and G2 touch live systems. They run through closed, root-installed commands of
`decryptus/deploy`, behind a GitHub environment that requires the owner's
approval. Application repositories never receive server administration
credentials; `decryptus/si` keeps system maintenance. Website repositories are not
used for new server operations.

Until the `deploy` qualification commands are installed and validated, G1 and G2
cannot be closed automatically. Any interim run must still produce evidence bound
to the candidate wheel SHA-256 and be reviewed before the gates close.

Never fabricate a result, edit evidence by hand or reuse evidence produced for a
different wheel payload. Qualifying a candidate never blocks merges to `main`.

## 4. Stable publication

- The stable release is cut from the qualified candidate's source line: from
  `main` when its runtime still equals the candidate's, otherwise from a
  `stable/X.Y.Z` branch created at the candidate tag. Changes to a stable branch go
  through reviewed PRs with the same required workflows.
- The stable PR changes the version from `X.Y.ZrcN` to `X.Y.Z`, adds the
  qualification manifest `docs/releases/X.Y.Z-qualification.json` and synchronizes
  the release notes, roadmap and documentation.
- The publisher keeps every existing protection: all eleven required PR workflows
  pass on the reviewed head, the release commit has that tested tree, the package
  archives come from the tested package run, and tags or releases are never
  overwritten.
- The final wheel must be payload-equivalent to the qualified candidate wheel:
  identical installed files, entry points and metadata headers, ignoring only the
  version string, the `dist-info` directory name, the generated `RECORD`, the long
  description and ZIP timestamps. Any other difference requires a new candidate
  and new qualification.
- After the GitHub release, promote the same archives to PyPI, publish the Docker
  Hub image built from the tested source, regenerate the documentation and
  atraxis.io, and merge any `stable/X.Y.Z` changes back into `main` by PR.

## G1 and G2 evidence contract

The `decryptus/deploy` qualification commands receive only the candidate tag and
wheel SHA-256. They download the published wheel themselves, verify its digest and
never accept a wheel, path or command from the caller. Each run returns one JSON
record and keeps private data, credentials and logs on the hosts:

- `schema`, `gate` (`G1` or `G2`), `status` (`passed` or `failed`);
- `candidate`: tag, version, wheel SHA-256;
- `run`: the GitHub run identity that requested it, plus start and end times;
- G1: number of approved sources read, all current, source authority unchanged;
- G2: capture identity and age, distinct host, restored services and SQLite
  databases, workers (0), execution paused, accounts and audits equal, TLS renewal,
  login revocation and throttling, final source authority unchanged.

The qualification manifest stores each record's SHA-256 and the producing run.
Only aggregate results may be published; capture contents stay private.

## Implementation status

- Implemented: generic `X.Y.ZrcN` candidates published on merge, automatic G3 on
  the exact candidate wheel with `g3-evidence.json`.
- In review: stable publication from a payload-equivalent final build (schema 2
  manifest, `main` or `stable/X.Y.Z` base, no per-version workflow edits).
- Pending: G1/G2 through the `decryptus/deploy` qualification commands. Until they
  exist, G1 and G2 need reviewed evidence produced to this contract.
