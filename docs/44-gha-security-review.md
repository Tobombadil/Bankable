# 44 — GitHub Actions security review (2026-10-24)

DevOps lane, using the vendored `gha-security-review` skill (`.claude/skills/gha-security-review`, Sentry, Apache-2.0;
`docs/43`). Its method: report only what can be traced end to end (entry point, payload, execution, impact,
proof-of-concept), mark a partly traced path "needs verification", and list what was cleared.

**Result.** No workflow can be exploited from a fork pull request, an issue or a comment. Two High findings and one
Medium (needs verification) remain, all from upstream supply chain: code fetched at run time into a job that can
publish images or holds the R2 token. Both High findings are fixed in this lane by pinning, with no change to what
any workflow does. The largest risk not covered by the skill's threat model is set out in §5 and closed by an owner
setting (§6).

## 1. Scope and method

- **Read in full:** the nine files in `.github/workflows/` and every script they call: `infra/ci/beta.sh`,
  `infra/ci/beta.py` (the input checks), `infra/scripts/build_{basemap,flood_layer,grid_layers,parcels_layer,
  prospect_layers}.sh`, the `Makefile` targets CI runs, `infra/docker/Dockerfile*`. The repository has no composite
  or local actions (`.github/actions/` does not exist).
- **Triggers:** `pull_request` (ci, flood-layer, grid-layers, parcels-layer); `push` to `main` (ci) and `v*` tags
  (release); `workflow_run` (release); `schedule` and `workflow_dispatch` (the rest). None uses
  `pull_request_target`, `issue_comment` or a self-hosted runner, and none runs an AI agent step.
- **Secrets:** eight of the nine read a secret. `connectors-nightly.yml` is the exception. Production values live in
  the `production` GitHub Environment (`docs/64` §2 step F).
- **Pins** were checked against the action repositories' own tags with `git ls-remote` on 2026-10-24 (§9).

## 2. Threat model: the repository is private

The skill's attacker has no write access and acts through fork pull requests, issues and comments. The repository
itself says it is private, with no outside access:

- Owner, 2026-10-10 (`docs/00-PLAN.md` decisions log, answer 5): "Nobody outside the owner has had repository
  access, and the repository will not go public"; also `docs/51-review-2026-10-10.md` §2.3, owner answer 5
  ("the repository stays private").
- `docs/63-operations.md` keeps "repo going public" as a future trigger (the history rewrite).
- One phrase in the 2026-10-22 row and in `docs/19` §2.3 calls it "this public repository". In context it means
  the product repository as opposed to a private content pack. It is not a change of visibility, and the owner
  should confirm that reading.

What this changes:

1. **Today nobody fits the skill's attacker.** On a private repository only people with read access can fork it or
   open issues, and only the owner has access. On a private repository GitHub runs fork pull-request workflows only
   if "Run workflows from fork pull requests" is enabled, and sends them secrets or a write token only if those two
   boxes are ticked as well
   ([managing Actions settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)).
   Even so, every `pull_request` workflow here is safe as written: no secret and no write scope reaches a
   pull-request job (§8). That stays true if the repository goes public.
2. **Supply chain does not depend on visibility.** An attacker who compromises an upstream action or tool reaches
   the job regardless. That is where all three findings sit.
3. **The relevant insiders are automation.** Agent sessions push their own branches, and the coordinator dispatches
   `deploy-beta` (`docs/64` §2 step G). The skill excludes insiders by design (its `VENDORED.md`). §5 covers the one
   path from such access to the production secrets, because it is the largest in this repository.

**Blast radius of the R2 token.** It matters to GHA-002, GHA-003 and §5. The token is "Object Read & Write, Apply
to all buckets in this account" (`docs/64` step C). The state bucket `beta-tfstate` holds `deploy_ssh_private_key`
(root on the server) and the generated application secrets (`infra/terraform/generated.tf:45-65`). Whoever holds
the R2 token can therefore take over the production server.

## 3. Findings

### GHA-001 — Release images are built and pushed by third-party actions on mutable tags (High)

- **Where:** `release.yml:111-112, 133` (`docker/setup-buildx-action@v3`, `docker/login-action@v3`,
  `docker/build-push-action@v6`) in `push-images`, which holds `packages: write` and logs in to GHCR with the run's
  token. The same actions ran on tags at `ci.yml:499-532` (`images` job). On a push to `main` that job writes the
  BuildKit cache (`cache-to: type=gha`, default scope, `main` branch scope) that `release.yml:148` imports
  (`cache-from: type=gha`).
- **Trigger:** `workflow_run` after a green `ci` push run on `main`, and `push` of a `v*` tag.
- **Confidence:** HIGH. The path is traced from the tag to production. Its entry point, like any supply-chain
  finding, is an upstream compromise.
- **What the attacker controls:** the code a moved tag points to (account takeover or a stolen maintainer token at
  `docker/*`). Precedent: the `tj-actions/changed-files` tag rewrite, CVE-2025-30066
  ([GitHub hardening guide](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#using-third-party-actions)).
- **Payload path:** the moved `@v6` resolves at job start. The modified build-push action adds a layer to each image
  before pushing, under the legitimate `sha-<7>` and `latest` tags. `infra/ci/beta.sh` `images` checks only that the
  tag exists (`docker manifest inspect`, `beta.sh:135-145`). `deploy` then runs that image on the server, with the
  env file holding the generated application secrets, the R2 token and the ERCOT credentials.
- **PoC:** (1) take over a `docker/*` maintainer token; (2) push a commit with the extra layer to
  `build-push-action`, then move `v6` to it; (3) wait for the next merge to `main`, where `ci` goes green and
  `release-images` pushes the altered `bankable-api:sha-…`; (4) the next `deploy-beta` run with an empty tag picks
  that build (`beta.sh` `default_tag`); (5) the container calls out with its environment. Step 2 against the
  ci.yml copy instead, on a push to `main`, poisons the BuildKit cache that step 3 then builds from.
- **Fix (applied):** pinned to the commits the major tags named on 2026-10-24, with the version in a comment, in both
  files: `docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f # v3.12.0`,
  `docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9 # v3.7.0`,
  `docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8 # v6.19.2`. Because each is the commit the tag
  already named, nothing that runs changes. Updates become explicit (Dependabot or Renovate can bump SHAs).

### GHA-002 — The monthly basemap build runs `go install …@latest` inside the step that holds the R2 token (High)

- **Where:** `basemap.yml:76-89` (step env has `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`) runs
  `infra/scripts/build_basemap.sh`. With no `go-pmtiles` on PATH, the script runs
  `GOFLAGS=-mod=mod go install github.com/protomaps/go-pmtiles@latest` (`build_basemap.sh:62-65`) and then runs the
  binary in that same environment. `deploy-beta.yml`'s basemap job already avoided this: it installs 1.31.2, pinned
  and checksummed (`beta.sh` `PMTILES_VERSION`, `PMTILES_SHA256`), before calling the same script.
- **Trigger:** `schedule` (05:23 UTC on the 1st) and `workflow_dispatch`.
- **Confidence:** HIGH.
- **What the attacker controls:** the next tag of `protomaps/go-pmtiles` (a compromised maintainer account). The Go
  checksum database stops an existing version from changing. It does not stop a new malicious version, which
  `@latest` picks up.
- **Payload path:** `v1.31.3` with a payload in `main()` → the Go proxy serves it to `@latest` → the binary runs
  `extract` with the R2 token in its environment → the token reads `beta-tfstate` → the deploy SSH key gives root
  on the server (§2). Alternatively, it rewrites the `basemap.pmtiles` that every visitor loads.
- **PoC:** (1) publish `v1.31.3` from the compromised account; (2) wait for the 1st of the month; (3) read the
  token from `os.Environ()` and copy `s3://beta-tfstate/…` out.
- **Fix (applied):** one step before the build, `infra/ci/beta.sh install-pmtiles` (`basemap.yml:73-75`), gated like
  the build and with no secret in its env. The script then finds the binary and never reaches `go install`.
  `@latest` named v1.31.2 on 2026-10-24, the pinned version, so the tool version does not change. The step was run
  here against a scratch `RUNNER_TEMP`: the download matched the recorded SHA-256 and the directory went to
  `GITHUB_PATH` (§9).

### Needs verification

#### GHA-003 — The `main` pip cache crosses from CI jobs that run unpinned downloads into the production jobs (Medium)

- **Where:** two `ci.yml` jobs run on every push to `main` (`ci.yml:57-58`) with `setup-python` `cache: pip`, and
  execute code fetched at run time with no version pin or checksum:
  - `security`: the gitleaks tarball (`ci.yml:445-448`, unlike `make caddy` and `beta.sh`, which verify theirs);
  - `a11y-and-performance`: `npm install --no-save @axe-core/cli` and `@lhci/cli` with no version
    (`ci.yml:583, 617`), whose install scripts run, then `npx`.

  The production-environment jobs restore the same scope's pip cache, then run with the R2 token in step env:
  flood-layer (`plan`, `state`, `publish`), parcels-layer (`plan`, `county-batch`, `publish`), grid-layers and
  prospect-layers. Caches are shared per branch, not per workflow
  ([cache access restrictions](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/caching-dependencies-to-speed-up-workflows#restrictions-for-accessing-a-cache)).
- **Trigger:** `push` to `main` writes the cache. `schedule` and `workflow_dispatch` on `main` read it.
- **Confidence:** MEDIUM. The cache sharing is confirmed by design; the key match is not (below).
- **What the attacker controls:** a new release of `@axe-core/cli`, `@lhci/cli` or one of their dependencies
  (picked up because no version is named), or the gitleaks v8.21.2 release asset (no checksum).
- **Path:** a compromised npm release or a replaced gitleaks asset runs in the CI job and alters `~/.cache/pip`.
  On a primary-key miss, setup-python's post step saves it under
  `setup-python-<os>-<arch>-<osVersion>-<osName>-python-<ver>-pip-<hash>`. A production job restores that key or,
  on its own miss, the newest entry under the hash-free prefix (`restoreKey`, `src/cache-distributions/pip-cache.ts`
  lines 70-71 at both `v5` and the pinned v6.3.0 commit). It then runs `pip install -r requirements.txt` (no hashes;
  most lines are `>=` ranges), and the build step imports that code with the R2 token in its environment.
- **PoC:** (1) publish a dependency release of `@lhci/cli` whose install script rewrites a cached wheel in
  `~/.cache/pip` (any package in `requirements.txt`) to add a `.pth` file that reads `R2_SECRET_ACCESS_KEY`;
  (2) wait for a merge that changes `requirements.txt`, so every CI job misses the primary key and the poisoned
  job's post step saves; (3) the next monthly grid-layers or prospect-layers build (setup-python v5, as in ci.yml)
  restores it, installs the altered wheel, and the build step's interpreter loads the `.pth`.
- **Severity:** Medium, because it rests on the unconfirmed links below. If they hold, it is High: same token, same
  blast radius as GHA-002.
- **Uncertain links:**
  1. The key includes the OS version. ci.yml uses `ubuntu-latest`, which moves to 26.04 from 2026-10-19
     (`basemap.yml:34`), while the production jobs pin `ubuntu-24.04`. While they differ the prefixes differ, and
     the path is closed by accident.
  2. The key includes the Python patch version.
  3. Only the first save of a key wins.
  4. v6.3.0 builds the OS segment in a helper not read here.
- **To verify:** in the Actions tab's Caches list, compare the `setup-python-…-pip-…` keys created by `ci` runs on
  `main` with the "Cache restored from key" line in the last flood, parcels, grid or prospect run.
- **Fix (not applied; it changes caching or installed versions):** remove `cache: pip` from the production-environment
  jobs, the trust-boundary fix that costs about a minute per job, or pin and checksum the two ci.yml downloads (§7).

## 4. Hardening without a finding

`release.yml` granted `packages: write` and `actions: read` at workflow level, so `ci-green`, which only reads the
`ci` runs, also held `packages: write`. No exploitable path existed: `ci-green` runs no third-party code, and its
only input is a SHA passed through env and quoted. The scopes now sit on each job: `ci-green` has `contents: read`
and `actions: read`; `push-images` keeps `contents: read`, `packages: write` and `actions: read`, as before. Nothing
else changed in that file's behaviour.

## 5. Outside the skill's model: anyone who can push a branch can read the production secrets

Nothing in the repository shows a deployment-branch rule or required reviewers on the `production` Environment.
`docs/64` §2 step F only adds its values, and the setting cannot be read from the repository. If there is none, then
any identity that can push a branch (the owner, an agent session, a leaked token, or an agent misled by text it read
from a source page) can push a workflow file with `on: push` and `environment: production`. It then receives
`HCLOUD_TOKEN`, `CLOUDFLARE_API_TOKEN`, the R2 token and the rest on that branch's first run. No review gate applies,
because a `push` workflow runs from the pushed branch's own file. The fix is owner step 1: every legitimate
production job runs on `main` (schedules, and dispatches from `main`; `beta.sh` resolves images from `release.yml`
runs on `main`). A dispatch from a feature branch would then stop at the environment, which is the intended effect.

## 6. Owner steps

1. Settings > Environments > `production` > Deployment branches and tags > Selected branches and tags > Add rule:
   branch `main` (closes §5).
2. Settings > Actions > General > Fork pull request workflows in private repositories: leave "Send write tokens to
   workflows from fork pull requests" and "Send secrets and variables to workflows from fork pull requests" unticked
   (keeps §2 point 1 true if a collaborator is ever added).
3. Only if the repository goes public: Settings > Actions > General > Approval for running fork pull request
   workflows from contributors > "Require approval for all external contributors".

## 7. Follow-ups that need a workflow change or a decision (not made here)

- **GHA-003:** drop `cache: pip` in the eight production-environment jobs, or pin `@axe-core/cli` and `@lhci/cli`
  to exact versions and checksum the gitleaks tarball the way `make caddy` does.
- **Narrow the R2 token the layer builds hold:** a second Cloudflare R2 token limited to the object-storage and tiles
  buckets, so a compromised build cannot read `beta-tfstate`. This needs differently named secrets, or a second
  Environment, in the four layer workflows plus basemap.
- **`build_basemap.sh:65`:** the `@latest` fallback is now unreachable in CI. Pinning it to
  `@v${PMTILES_VERSION}` would cover local runs too.

## 8. Reviewed and cleared

- **Pinning:** every third-party action is now pinned by SHA: the three `docker/*` actions (GHA-001) and
  `aquasecurity/trivy-action@ed142fd…` (`ci.yml:541`), whose SHA is the peeled commit of the annotated tag v0.36.0.
  First-party `actions/*` on version tags (ci, release, basemap, grid-layers, prospect-layers, connectors-nightly) are
  outside the skill's pinning policy. The first-party SHA pins carry correct comments: checkout `d23441a…` is
  v6.1.0, and setup-python `ece7cb06…` is v6.3.0.
- **`permissions:`:** all nine set them at workflow level, read-only apart from release (§4) and deploy-beta
  (`packages: read` and `actions: read`, read scopes that `infra/test_deploy_beta_workflow.py` pins). No workflow
  relies on the repository's default token.
- **`pull_request_target` / `workflow_run`:** there is no `pull_request_target`. `release.yml`'s `workflow_run` runs
  only after a successful `ci` push run from this repository (`release.yml:44-48`). It checks out that run's SHA,
  downloads no artifact from the triggering run, and runs no code before the gate. The checkout keeps its token in
  `.git/config`, but the Dockerfiles `COPY` named paths only, so no image layer holds it.
- **`${{ }}` in `run:`:** none reads `github.event.*`. Three matrix values are interpolated:
  - `release.yml` `matrix.name`: a static list;
  - `flood-layer.yml:188` `matrix.state`: state codes from the vendored `us_states.geojson`, re-checked as
    `^[A-Z]{2}$`;
  - `parcels-layer.yml:198` `matrix.batch`: FIPS codes parsed with `landparcels_(48\d{3})_lp\.zip$` and re-checked
    as `^48[0-9]{3}$`.

  All three run on schedule or dispatch only.
- **Secrets scoping:** secrets are set on the steps that use them. Job-level env carries only the account id and
  bucket name. Every production-environment job uses first-party actions only. `ci.yml` `preview-deploy` reads
  `PREVIEW_DEPLOY_TOKEN`, which GitHub does not send to fork pull requests.
- **`deploy-beta` dispatch inputs:** `action` is a `choice` and reaches the shell only as `$ACTION`. `tag` reaches it
  only as `$INPUT_TAG`, quoted, and is matched against `^sha-[0-9a-f]{7}$` or `^v[0-9A-Za-z._-]+$` (`beta.sh:118-122`,
  `beta.py:118, 300-309`) before it is written to `GITHUB_OUTPUT`. No input is interpolated into `run:`. Dispatch
  needs write access, so this is outside the skill's model in any case.

## 9. Validation

- `actionlint` is not installed, and nothing was installed. Every workflow parses with PyYAML. A check script
  asserted: no third-party action without a 40-character SHA (9 of 9 pinned); no `github.event.*` in `run:`; no
  `pull_request_target`; `packages: write` only on `push-images`; the pinned installer placed after checkout and
  before the basemap build, under the same gate and without secrets.
- The SHAs come from `git ls-remote --tags` against each action's repository. They are lightweight tags with no
  peeled entries, so the listed object is the commit. The skill's own example pin for build-push-action v6.18.0
  (`263435318d…`) matches the same listing.
- `infra/ci/beta.sh install-pmtiles` run against a scratch `RUNNER_TEMP`: SHA-256 verified, binary placed,
  `GITHUB_PATH` written.
- `pytest -p no:cacheprovider` before and after, with the same results:
  - `tests/test_spec_operation_inventory.py`, `infra/test_deploy_beta_workflow.py`, `infra/ci/test_beta.py`,
    `infra/test_scripts.py`, `tests/test_postgres.py`: 179 passed, 15 skipped (Postgres: no `POSTGIS_TEST_URL`);
  - the workflow-reading modules for the layer builds and compose: 199 passed, 3 skipped.

## 10. Assumptions

- The repository's visibility and the `production` Environment's protection rules are taken from the repository's
  own documents. GitHub was not queried.
- The pinned SHAs are trusted as of 2026-10-24: they are what the tags named that day, with no sign of tampering.
