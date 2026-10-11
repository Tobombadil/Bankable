# Hand check worksheet (2026-10-10)

**Format reference only: do not hand-check this file.** It was generated on 2026-10-10 from a copy of the 4-source e2e store (`web/.data/e2e-test.db`: EIA-860M plus the CAISO, ERCOT and NYISO queues), with sites built on the copy by `python -m services.sites`, because the rehearsal store was offline. That store holds no interconnection points and has never been through the resolver, so every site here is grouped by EIA plant and the Merged records sheet is empty (0 records with two or more source links). Its record and site ids exist only in that store, so its links do not work on the beta.

The real worksheet must be generated against the beta store after it is seeded (public ids are minted per store), with the beta posture, on the beta host from the directory holding its compose files (`docker compose ...` as in docs/64 §5), then copied back to `data/eval/handcheck/<date>-beta/`:

    mkdir -p /opt/infraque/handcheck && chown 10001 /opt/infraque/handcheck   # the image runs as uid 10001
    docker compose ... run --rm -v /opt/infraque/handcheck:/out worker \
      python -m services.sites.handcheck generate --out /out/2026-10-14-beta \
      --seed 20261014 --posture noncommercial --base-url https://<beta domain>

`DATABASE_URL` comes from the container environment. Score the filled copy with `python -m services.sites.handcheck score <filled.xlsx>`.

Produced by `python -m services.sites.handcheck generate --database-url <url> --out data/eval/handcheck/2026-10-10-e2e-reference --seed 20261014 --tier public --posture noncommercial`
from a copy of the 4-source e2e store (web/.data/e2e-test.db, EIA-860M and the CAISO, ERCOT and NYISO queues) with sites built on the copy, read only, at 2026-10-10 19:50 UTC
(seed 20261014, tier `public`, posture `noncommercial`,
site rules `2026-10-10.1`).

**Record and site ids and links belong to this store.** Ids are minted at load, so for working
links regenerate against the store you will browse (the beta) with the same seed, and put its
address in the base-URL cell on the Instructions sheet. Each row also carries stable keys
(`source_id:source_record_id`, a hash where the licence does not let the platform print the id;
EIA plant ids), so items can be matched across stores.

Files: `handcheck.xlsx` (fill in the yellow cells); CSV twins of each data sheet (`sites.csv`,
`merged_records.csv`, `site_members.csv`, `merged_links.csv`, `strata.csv`); `manifest.json`
(counts, strata, file hashes). Score with `python -m services.sites.handcheck score handcheck.xlsx`.

Sites: 50 of 325 served. Merged records:
0 of 0 served with two or more source links.

| Sheet | Stratum | What | Population | Sample | Selection |
|---|---|---|---|---|---|
| Sites | `largest` | The largest sites by member count, all checked | 5 | 5 | all |
| Sites | `eia_plant/high` | Grouped by same EIA plant; lowest label confidence high | 320 | 45 | permanent random numbers |

`handcheck.xlsx` was recalculated once with LibreOffice after generation (438 formulas, 0 errors) so
link labels and progress counts are cached for previewers; `manifest.json` hashes the recalculated file.
Excel recalculates on open in any case.
