## Summary

<!-- Briefly explain what this pull request changes and why. -->

## Changes

<!-- List specific code modifications, new files, or updated modules. -->

## Security and quality checklist

Review and check each item before requesting a merge:

- [ ] **Security policy**: Changes do not weaken `relay/` sandbox rules, capability checks, or the RPC firewall.
- [ ] **No comments rule**: No `#` comments were added in `.py` or `.hmod` files (`git diff '*.py' '*.hmod' | grep -E "^\+\s*#"` returns empty).
- [ ] **Internationalization**: All user-facing strings use `lexicon/` keys and avoid hardcoded text.
- [ ] **Type verification**: Strict type checks pass cleanly (`uv pip install basedpyright==1.40.1` reports zero errors).
- [ ] **Security SAST**: Semgrep security scan passes with zero blocking findings.
- [ ] **Credentials**: No session strings, master vault tokens, or test secrets are present in code or commit history.

## Related issues

<!-- Fixes #123 -->
