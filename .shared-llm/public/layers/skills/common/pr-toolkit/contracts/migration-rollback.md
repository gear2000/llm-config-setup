## Migration and rollback contract

Applies where changed in-scope behavior affects deployment transitions, persisted state, or recovery. Record why migration/rollback checks are not applicable when no such transition is affected; do not demand migrations for every change.

- Upgrade order and any dual-read/write or mixed-version period preserve the declared supported contracts.
- Recovery accounts for data already written by the new version, event replay, caches, and reused files where relevant, not merely restoration of an old executable.
- Claimed rollback identifies whether old readers can consume new data, what migration reversal loses, and required backups or recovery prerequisites.
- Irreversible changes are explicitly documented and permitted by the accepted policy, with required coordination and recovery evidence. An intentional supported irreversibility is distinct from an accidental break or an unverified rollback claim.
- Source/design evidence and observed migration or recovery results remain distinguishable; a script's existence does not prove a successful transition.
