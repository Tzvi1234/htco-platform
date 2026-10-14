# Identity and access — Operations

Nothing runs in production yet.

## Changing a permission

1. Change the matrix in `docs/architecture/PERMISSION_MATRIX.md` and `_MATRIX` in `packages/application/src/htco_application/access/policy.py` in the same pull request; the matrix test fails if they differ.
2. Any permission change needs the owner's approval ("Ask the owner first" in CLAUDE.md) and a security note in the pull request.
