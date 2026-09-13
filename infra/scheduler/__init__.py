"""Thin scheduling/worker glue around Procrastinate (ADR 0004).

This package is DevOps-owned infrastructure code, not a pipeline stage: it reads
``data/sources.yaml`` cadences and enqueues `python -m pipeline.connectors run <source_id>` as a
job; the pipeline package itself (owned by data-engineer) is never imported here beyond the CLI
entrypoint it already exposes. Kept intentionally small per the task brief ("a thin runner is
fine", docs/adr/0004).
"""
