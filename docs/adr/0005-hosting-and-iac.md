# ADR 0005 — Hosting and infrastructure as code

**Status:** Accepted · 2026-09-12 (proposed) → accepted 2026-09-12 · solutions-architect; ratified by the
owner per `docs/00-PLAN.md` decisions log ("Sprint 2 started..." row) and D1 ("Hosting posture (ADR 0005):
VMs + Compose + OpenTofu (solo/contractor)... Default if the owner says nothing: VMs + Compose") — the
owner proceeded on D1's default, which this ADR records as acceptance.
**Ratification:** the "Blocked on" open questions below are resolved for MVP purposes by that default: open
question 2 (solo founder plus contractors, **[A-2]**) and open question 3 (US entity/region, **[A-3]**) both
carry their stated defaults forward; nothing here changes if the owner later answers either differently — see
"Owner questions this waits on" below, kept as the reversal path rather than removed.
**Related:** `docs/20-architecture.md` §4.1, §11 (security), §13 (scaling path), §14 (cost), ADR 0003.
**Implemented by:** `infra/terraform/` (OpenTofu), `infra/compose/` (Docker Compose), `infra/sops/` (SOPS +
age), `infra/scripts/` (deploy, rollback, backup, restore drill, secret rotation), `docs/60-deployment.md`
(the deployment design and runbooks this ADR authorises).

## Context

The run-time is six process types — `api`, `web`, `scheduler`, `worker-plain`, `worker-browser`, `worker-model` —
from a single container image (`docs/20` §4.1), plus managed Postgres, object storage and a Cloudflare edge that
already fronts bankablehq.com (`.github/workflows/`). Constraints: an infrastructure ceiling in the low hundreds
of dollars per month (`docs/01` §3.4); one operator with no dedicated ops person (**[A-2]**); a Chromium worker
that needs 2 GB of RAM and its own container; **per-source egress control** — workers may only reach the hosts
listed for their source, and a residential-proxy credential must exist on exactly one pool (`docs/20` §4.3,
§11), which rules out platforms that hide the network; and no vendor-console clicking, because a solo operator
cannot reconstruct undocumented infrastructure.

## Options considered

| Option | Est. USD/mo (MVP) | For | Against |
|---|---|---|---|
| **2–3 small cloud VMs + Docker Compose + managed Postgres + Cloudflare edge** | ~45 compute + 35–70 database | Cheapest reliable shape; complete control of egress and the Chromium container; everything is in the repo; deploy is `docker compose up` over SSH; scale by adding replicas or a VM | The operator patches the hosts; no rolling deploys out of the box (brief downtime or a simple blue/green); no autoscaling |
| AWS ECS Fargate + RDS + S3 | ~2× the above, plus NAT gateway | Boring, managed, IAM-integrated, rolling deploys | Roughly double cost at this size; the NAT gateway tax on egress-heavy fetching; more moving parts than one person needs; egress allowlisting is per-task and fiddly |
| Fly.io / Render / Railway | ~60–120 | Simplest deploys, managed Postgres, preview environments | Chromium workers and strict per-source egress control are awkward or unsupported; less control over outbound IPs, which matters because several sources block datacentre ranges and one class needs a residential proxy (`docs/02` §7) |
| Kubernetes (managed) | ~150+ and a person | Everything scales; standard | A control plane and a skill set for six processes and 10⁵ rows. Explicitly rejected in `docs/20` §4.1 |
| Single VM, everything including Postgres | ~20 | Cheapest possible | The database loses PITR and managed backups; a browser worker OOM takes the API down with it |

## Decision

**2–3 small cloud VMs in a US region running Docker Compose**, one image with per-process entrypoints; **managed
Postgres with PITR** (ADR 0003); **Cloudflare R2** for object storage; **Cloudflare** for DNS, CDN, WAF and edge
rate limiting, with the admin hostname optionally behind Cloudflare Access. Infrastructure as code:
**OpenTofu** for cloud resources, per-environment **Docker Compose** files, **SOPS + age** for secrets decrypted
in CI, **GitHub Actions** for lint (ruff), types (mypy), tests (pytest with connector fixtures), image build and
deploy over SSH, with a preview environment per PR for the web app.

**Compute provider, decided at implementation (Sprint 2 DevOps task): Hetzner Cloud**, not DigitalOcean.
Reasons: (1) `docs/20` §14's cost line already prices compute against "Hetzner Cloud or equivalent, US
region" at ≈ USD 45/mo for the app + worker VMs, so Hetzner is the estimate this ADR's cost consequence
below is measured against, not a new assumption; (2) Hetzner's `cx32` (4 vCPU/8 GB) at the Ashburn, VA
location matches the US-region requirement (**[A-3]**) at a lower list price than DigitalOcean's equivalent
droplet size; (3) Hetzner Cloud has a mature, actively maintained OpenTofu/Terraform provider
(`hetznercloud/hcloud`) with private networks and firewalls, which is what per-source egress control
(`docs/20` §4.3, §11) is built on in `infra/terraform/main.tf`. DNS and object storage stay on **Cloudflare**
(R2 + Cloudflare DNS) rather than moving to Hetzner's own DNS/Object Storage offerings: this ADR's decision
text above and ADR 0003 already commit to Cloudflare R2 specifically (zero egress fees), and Cloudflare
already fronts bankablehq.com (`infra/cloudflare/lovable-proxy-worker.js`, moved from `.github/workflows/blank.yml` on 2026-09-15) — one Cloudflare account, one set of
credentials to rotate for the edge tier, independent of which VM provider hosts compute. See
`infra/terraform/versions.tf` for the provider-choice note recorded next to the code.

## Reversal path (formerly "owner questions this waits on")

The decision above is accepted for MVP on the stated defaults; these are not blockers but the conditions
under which this ADR would be superseded, not amended (ADR 0001 rule 2):

1. **Open question 2 (budget and team).** If the build is funded with a dedicated ops person, go straight to a
   managed container platform (ECS Fargate or a managed Kubernetes) and skip the Compose stage; the single image
   and twelve-factor configuration make that a deployment change, not a rewrite.
2. **Open question 3 (entity and jurisdiction).** The hosting region is an OpenTofu variable, but moving it after
   launch means moving the database and the object storage. EU customers and a non-US entity would also pull the
   data-processing addendum forward (**[A-3]**).

## Consequences

- Cost stays inside the ceiling: compute ≈ USD 45/mo of a core total of ≈ USD 195–415 (`docs/20` §14).
- Egress policy is enforceable because we own the network path: an allowlist compiled from `data/sources.yaml`
  into the egress proxy, and the residential-proxy credential present only on the pool that needs it.
- The operator owns host patching, container restarts and the monthly restore drill. Acceptable for one operator;
  the first thing to hand to a contractor if the answer to question 2 changes.
- No rolling deploys at MVP: deploys are a short restart behind the edge cache, which serves public pages
  throughout (`docs/20` §12).
- Reversal cost: low. The image, the Compose files and the OpenTofu modules are the artefacts; a managed platform
  consumes the same image.
