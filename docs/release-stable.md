# Reviewed stable publication

This contributor procedure publishes the GitHub release for the bounded
[1.0 support matrix](v1.0.md). It does not publish to a registry or deploy services.
The historical [candidate procedure](release-candidate.md) remains separate.

Review the operational gate manifest and its private evidence, then merge the
final package/documentation PR after all eleven required workflows pass. Runtime
module hashes must match the operationally qualified source. The stable publisher
checks that its checkout is the current main commit and has the tested PR tree.
It downloads the exact Python 3.12 package artifact; Python 3.11 must also pass
in the required package workflow. Changed/missing archives or hashes, open gates,
a changed runtime module, an existing tag/release or a failed check abort publication.

Publication is explicit: either dispatch `Publish reviewed stable release` on the
release branch head with the tested PR head, or create the branch
`release/publish-X.Y.Z` at the exact reviewed merge commit. The release branch is
`main`, or `stable/X.Y.Z` when `main` has moved on with other runtime changes.
The publication branch must name the version in `pyproject.toml`, and its commit
must be the current head of `main` or `stable/X.Y.Z`. The workflow resolves the
single merged PR for that commit and checks its exact head; ordinary pushes, PRs
and tags do not publish. Do not repeat a publication after an uncertain result.

The publisher creates a draft, attaches the qualified wheel, source archive,
provenance and checksums, then publishes it as stable. Failure after draft creation
leaves evidence for inspection; never overwrite/delete an existing tag or release
to make a retry pass. After publication verify tag target, release flags, attached
hashes and the installed wheel identity. Keep private infrastructure and operational
records out of public artifacts; the reviewed manifest contains only scope and
content hashes of those records.

For a runtime-identical 1.0 patch, the versioned qualification manifest records
the SHA-256 of the original 1.0.0 manifest and must retain its exact runtime map.
The publisher checks both before staging archives. The 1.0.1 transition to stable
Atropos packages has separate dependency-installation and container evidence;
it does not rerun or extend the historical operational/endurance campaign.

From 1.0.11, the qualification manifest uses schema 2 and names the qualified
candidate: its version (`X.Y.ZrcN`, or `X.Y.Z` for an unpublished package artifact),
tag or package artifact, source commit, wheel SHA-256 and version-normalized payload
digest, plus one passed evidence record per gate (G1, G2, G3) bound to that wheel.
The workflow downloads the candidate wheel; the publisher checks its SHA-256,
recomputes its payload digest and requires the final wheel to have the same payload,
ignoring only the version header, the `dist-info` directory name, the generated
`RECORD`, the long description and ZIP timestamps. Any other difference needs a new
candidate and new qualification. Schema 1 is refused after 1.0.10.

For 1.0.2, 1.0.4, 1.0.5, 1.0.6 and 1.0.7 the publisher checks the final wheel against the qualified pilot
wheel's installed files, entry points and metadata headers. The ZIP timestamps,
generated RECORD and long README description may vary between builds. The
published archives come from the successful package job for the final PR head.

For 1.0.3 the publisher compares the application modules to the 1.0.2 manifest. The installed-image tests separately verify the changed HTTP dependency.

For 1.0.8 the release check compares the wheel with the 1.0.7 qualified payload,
allowing only the package version and the installer module verified by guided CI.
Both the installer and the tagged Compose starter deploy the verified 1.0.7 image.
