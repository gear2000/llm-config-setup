# /herdr-run

Deprecated one-release alias for `/herdr-control`.

Warn the user:

```text
WARNING: /herdr-run is deprecated and internal. Use `just herdr-start <converted-run-dir>` from a shell; the launcher assigns `/herdr-control` automatically.
```

Then delegate to:

```text
/herdr-control <same arguments>
```

Do not add validation, conversion, startup logic, or phase handling here. This alias exists only for migration.
