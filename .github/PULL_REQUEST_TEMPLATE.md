## Summary

Describe the user-visible behavior and why this change is needed.

## Risk

Describe data, migration, filesystem, authentication, API, or compatibility
impact. Write `None` when the change has no such impact.

## Validation

List the exact commands and results used to validate the change.

- [ ] `make test`
- [ ] Focused tests cover the changed behavior
- [ ] `make test-release-safe` for critical or release-facing changes
- [ ] No credentials, private URLs, or host-specific paths are included
- [ ] Documentation and `CHANGELOG.md` are updated when user-visible behavior changes
