# Deploy checklist

- Run `./scripts/safe_to_deploy.py` on production and follow its result.
- Run `./scripts/verify_imports.py` and require `IMPORTS_OK`.
- Run `python3 scripts/verify_js.py` and require `JS_OK`.
- When `generate_short.html` preview code changes, run `python scripts/test_design_preview_smoke.py` with Playwright installed and require every check plus `SMOKE_OK`.
- Restart only the services required by the change and confirm they are active.
- Stage only task files, inspect the diff, commit, push, and confirm the branch is up to date.
