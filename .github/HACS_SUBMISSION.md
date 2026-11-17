# HACS submission checklist

Target repository: `EcoFlow-oasis-official/ha-EcoFlow-official`

Integration domain: `ha_ecoflow_official`

## Checks provided by this repository

| Workflow | Checks | Triggers |
| --- | --- | --- |
| Validate | Official HACS Action (`integration`, no ignored checks) and official Hassfest | Push, pull request, published release, daily schedule, manual run |
| Tests | Repository preflight, Python 3.12/3.13/3.14 regression tests, Node.js frontend tests | Push, pull request, published release, manual run |

Both workflows use read-only repository permissions and the built-in GitHub token.
No additional repository secrets are required. They do not publish releases,
change repository settings, or open a HACS submission PR. Dependabot checks
versioned GitHub Actions weekly; the official HACS/Hassfest actions follow their
upstream branches as documented by those projects.

The preflight checks integration folder/domain consistency, required metadata,
translation keys, brand icon sizes, Python syntax and encrypted source headers.
On GitHub it also verifies the case of Git-tracked component paths, which can
differ from the folder shown by Windows Explorer.
For tag pushes and published releases, it also checks that the tag matches
`manifest.json` (either `1.3.1` or `v1.3.1` for manifest version `1.3.1`).
This local check does not replace the official HACS and Hassfest jobs.

## Before opening the HACS PR

- [ ] Push these changes and confirm **HACS validation**, **Hassfest**, all three
  Python jobs and **Frontend** are green for the intended release commit.
- [ ] Ensure the repository is public, has a description, has **Issues** enabled,
  and has relevant topics (for example `home-assistant`, `hacs`, `ecoflow`, `modbus`).
  These are GitHub repository settings, not fields in `hacs.json`.
- [ ] Confirm `codeowners` names a valid GitHub maintainer and GitHub recognizes
  the existing MIT license.
- [ ] Verify the integration installs successfully through HACS as a custom
  repository, and confirm device discovery and control on a real supported device.
- [ ] Update `manifest.json` and the changelog for the intended version, then
  create a **full GitHub Release** for that exact tested commit. A tag alone is
  insufficient for inclusion in the HACS default list. Do not move an existing
  published tag to another commit.
- [ ] Confirm the release-triggered checks pass. HACS may inspect remote GitHub
  metadata and the latest release, so passing local tests alone is insufficient.
- [ ] As the repository owner or a major contributor, fork
  [hacs/default](https://github.com/hacs/default), create a branch from its
  `master` branch, and add `EcoFlow-oasis-official/ha-EcoFlow-official` to the
  `integration` JSON list in alphabetical order. Complete its PR template and
  allow maintainers to edit the PR branch.

Acceptance into HACS is reviewed by the HACS maintainers; passing CI does not
automatically add this repository to the default store.

## Local checks

Run from the repository root (Python 3.12+ and Node.js 22):

```sh
python scripts/check_repository.py
python scripts/check_repository.py --tag v1.3.1
python -m unittest discover -s tests -v
node tests/frontend-layout.mjs
```

Change the example tag when the manifest version changes. Local regression tests
use small Home Assistant substitutes; they do not replace real-device testing.
Hassfest runs in its official Linux Docker image on GitHub-hosted runners.

Official references:

- [HACS inclusion requirements](https://www.hacs.xyz/docs/publish/include/)
- [HACS GitHub Action](https://www.hacs.xyz/docs/publish/action/)
- [Integration repository requirements](https://www.hacs.xyz/docs/publish/integration/)
