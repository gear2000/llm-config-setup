# /cc-convert-pattern-10

Claude Code converter for Pattern 10 (HIL relay plus plan-implementer). It separates an approved plan into work phases and one named YAML workflow per phase, and writes `workflow.yaml`. Preparation only: it never executes. `/cc-convert --herdr` remains the separate phase-leader converter.

Use the shared conversion contract below with command name `cc-convert-pattern-10` and target pattern 10. Pattern 10 and Pattern 20 read the same `workflow.yaml` and the same pool; only the launch command differs.
