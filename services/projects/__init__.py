"""Private projects: "Build a deal" (docs/19; ADR 0009).

A deal is a private project on Inframation's own graph, linked to public records by reference.
This package holds its tables (`models`), the one authorisation function every read and write of a
private row goes through (`authz.authorize`), the bankability checklist (`checklist`, loaded from
`data/checklist/` and, privately, by `load_checklist`) and invitations (`invitations`). Data rooms
(docs/19 §12) are `data_room` (folders, files, the access log), `data_room_files` (the type
allowlist), `data_room_store` (private storage and sealing) and `data_room_scan` (the scanning hook).

**Who may import it** (`infra/importlinter.ini`, contracts `private-projects-isolated` and
`private-projects-named-importers`): this package, `services.api.my_projects`,
`services.api.account_erasure` and `services.retention`. No public read path, alert, social, match,
CRM or web module may, so private data cannot reach a public surface through an import.

Nothing here writes to `event` or to the admin audit log (docs/19 §4.1): a project's history is
its own `private_project_activity` table, shown to its members only.
"""
