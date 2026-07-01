# Proprietary-content check — hook protocol

This file is read by the commit guard hook in `.claude/settings.json`. It is
committed and public — it must never itself contain proprietary strings, only
generic instructions.

**Source of truth for categories:** read CLAUDE.md's `# ⚠️ THIS IS A PUBLIC
REPOSITORY` section for the full list of proprietary-content categories and
judgment guidance. Apply those categories to the diff you were given. Read
full file contents where the diff alone is ambiguous (e.g. a new file's
surrounding context).

**Decision — emit exactly one:**
- `deny` — a clear, unambiguous match to any category. State what you found
  and where (file + line/snippet).
- `ask` — something suspicious but not certain (a name that could be generic
  or could be an internal codename; a design description that might be
  over-specific). State what raised the suspicion.
- `allow` — clean, no ambiguity.

Never guess toward `allow`. When in doubt, `ask`.
