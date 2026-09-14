# /do-convert-pattern-20

Pi converter for Pattern 20 (this pane runs the phases). It separates an approved plan into work phases and one named YAML workflow per phase, and writes `workflow.yaml`. Preparation only: it never executes. `/do-convert --herdr` remains the separate phase-leader converter.

Use the shared conversion contract below with command name `do-convert-pattern-20` and target pattern 20. Pattern 10 and Pattern 20 read the same `workflow.yaml` and the same pool; only the launch command differs.
