# Manual release candidate publication

This contributor procedure publishes a GitHub prerelease only. See the
[release process](release-process.md) for how candidates relate to qualification
and stable publication. It does not
publish to PyPI, deploy a site or close the operational gates in the
[1.0 plan](v1-delivery.md). See the [user-facing matrix](v1-candidate.md).

Candidates use the version `X.Y.ZrcN`. Merging the reviewed PR that sets such a
version into `main` or a `stable/X.Y.Z` branch is the explicit request to publish
it: the `Publish reviewed release candidate` workflow then runs on that push. It
can also be dispatched on the branch head with the full reviewed PR head SHA.
Pushes that do not carry a candidate version stop after the version check.
The tree must equal the branch head, the tag must not exist yet and the most recent
pull-request run of each of the eleven required workflows must have succeeded on
that exact head. A changed tree, failed or pending run, missing archive or changed
digest aborts publication. The release notes come from `docs/releases/X.Y.ZrcN.md`.

The workflow downloads the existing Python 3.12 installed-package artifact from
the qualified package run. It verifies the recorded commit, tree, distribution,
version, exact wheel/sdist filenames, sizes and SHA-256 values. It does not rebuild
packages after qualification. Python 3.11 remains covered by the same required
package workflow. Exact provenance and checksums accompany both archives.

The package workflow compares every tracked source file byte-for-byte with the
source archive, including documentation, scripts, examples and test helpers.
It extracts that archive outside the checkout and runs collection plus the core
suite on Python 3.11 and 3.12. Installed-wheel checks remain separate so that
source imports cannot hide a missing installed module.

The tag points at the main checkout, whose tree matches the tested source. Both
source commits are recorded. Existing tags/releases are never overwritten. The
release is created as a draft, files are attached, and only then is it made public
with the prerelease flag. Failure after draft creation leaves a draft for review;
do not delete or retry over it blindly. Publication is not automatic on push,
merge or tag creation. No production endpoints, registry secrets or new
notification destinations are used.

The provenance file records the candidate wheel SHA-256. After publication the
workflow dispatches the two-hour endurance workflow on the candidate tag. That run
verifies the published archives and checksums, builds the acceptance image from
the exact candidate wheel, checks the installed files against the tagged source and
records `g3-evidence.json` (candidate identity, run identity, measured duration,
rounds and recovery) in its `endurance-progress` artifact. An incomplete or failed
run is recorded as failed. G1 and G2 are separate; see the
[release process](release-process.md).

Candidate release text is in `docs/releases/X.Y.ZrcN.md` (for example
`docs/releases/1.0.0rc1.md`); its limitations must remain consistent with the user
matrix and actual retained evidence.
