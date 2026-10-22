# Retention and archive capacity

Retention inspection is a trusted-host, offline operation. It reads one protected
file and prints a JSON report. It never opens an account database, contacts a
source, prunes history or changes running configuration.

```sh
atraxis retention --accounts-backup /private/accounts-backup.json
atraxis retention --config /private/atraxis.yml
```

Account archives must be current format with a valid integrity digest. Configuration
inspection validates the document shape and audit events, without opening referenced
profiles or credentials; it does not establish that the daemon can start. Run
`doctor` separately for that purpose. The commands require regular private files
and reject symlinks, oversized input and invalid formats without exposing contents.

The report includes bytes used, supported limit, remaining capacity and an
`attention_required` flag. At **80%** of any supported limit it exits **1**; below
that threshold it exits **0**. Invalid/unreadable input exits **2**. This threshold
is an early warning, not an expiry or permission to delete data. Account backups
are limited to 32 MiB and 100,000 audit entries. Configuration files are limited
to 64 KiB and 100 configuration events. These are existing format/write limits,
not new runtime quotas; an account database can contain more audit than one backup
currently supports. The command cannot inspect an archive that already exceeds its
format limits, so inspect successive successful backups before reaching the bound.

Counts describe the supplied file, not current database state, filesystem free
space or memory consumption. Protect reports as operational metadata. No account
names, grants, credential paths or event payloads are printed. Record the backup
date alongside the account archive: its current format does not include capture time.

## Preservation procedure

1. Record a protected, dated backup and its digest before maintenance. Include the
   matching daemon configuration, account and inventory authority profiles, engine
   profiles, credentials and CA material in a separately protected backup set.
2. Inspect the backups and their capacity. Compare successive reports to anticipate
   growth. A valid digest proves file integrity, not successful restoration.
3. Rehearse account restoration into a fresh destination while the restored service
   remains stopped. Verify membership, audit and search before switching traffic.
4. Keep the source and at least one independently verified restore set until that
   rehearsal and any incident reconciliation are complete. Do not run restored
   copies against the same execution targets in parallel.
5. At capacity warnings, plan an increased supported archive capacity or an explicitly
   designed archival procedure. There is currently no automatic audit pruning,
   history rotation or online archival command. Do not reset the audit to make an
   error disappear; the existing configuration editor refuses writes at its bound.

Choose backup-copy retention according to operational obligations and required
recovery points; the application does not invent a number of days or schedule
expiration. Removing an old backup copy is distinct from pruning the live database.
A backup alone does not authorize removal of live audit or authorization evidence.

Atropos exposes its own `storage-retention --input` report for engine archives;
Atraxis does not inspect or manage engine storage. See the engine's retention guide
for deduplication, target locks and uncertain outcomes. Multi-host restoration,
coordinated secret backups, sustained growth measurements and live-history pruning
remain separate qualification work.
