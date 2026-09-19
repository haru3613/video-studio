# Video Studio

This repository prepares the reusable Video Studio workflow for open-source
release. Read `docs/open-source-plan.md` before importing implementation or
changing the release scope.

- Import source through an explicit file allowlist after reviewing dependencies
  and redistribution rights. Keep provenance and third-party notices with it.
- Keep credentials, operator identity, production projects, generated media,
  browser state, and private repository history outside this repository.
- Preserve the workflow's artifact binding, resumability, and quality gates
  when extracting code. Document behavior that is still unavailable.
- Run `scripts/verify` for repository changes. Extend it with real implementation
  checks as executable components are introduced.
- Obtain the owner's decision before adding a license, making the repository
  public, or publishing a release.
