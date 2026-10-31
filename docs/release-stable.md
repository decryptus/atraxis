# Reviewed stable publication

This contributor procedure publishes the GitHub release for the bounded
[1.0 support matrix](v1.0.md). It does not publish to a registry or deploy services.
The historical [candidate procedure](release-candidate.md) remains separate.

Review the operational gate manifest and its private evidence, then merge the
final package/documentation PR after all eight required workflows pass. Runtime
module hashes must match the operationally qualified source. The stable publisher
checks that its checkout is the current main commit and has the tested PR tree.
It downloads the exact Python 3.12 package artifact; Python 3.11 must also pass
in the required package workflow. Changed/missing archives or hashes, open gates,
a changed runtime module, an existing tag/release or a failed check abort publication.

Publication is explicit: either dispatch `Publish reviewed stable release` on
main with the tested PR head, or move the dedicated `release/publish-1.0.0` branch
from a commit preceding that workflow to the exact reviewed merge on main.
Only that named publication branch triggers the push path. It resolves the single
merged PR for the main commit and checks its exact head; ordinary main pushes,
PRs and tags do not publish. Do not repeat a publication after an uncertain result.

The publisher creates a draft, attaches the qualified wheel, source archive,
provenance and checksums, then publishes it as stable. Failure after draft creation
leaves evidence for inspection; never overwrite/delete an existing tag or release
to make a retry pass. After publication verify tag target, release flags, attached
hashes and the installed wheel identity. Keep private infrastructure and operational
records out of public artifacts; the reviewed manifest contains only scope and
content hashes of those records.
