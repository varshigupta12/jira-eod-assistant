# Contributing

Thanks for helping improve Jira EOD Reporter.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## Pull requests

- Keep changes focused and include tests for behavior changes.
- Preserve compatibility with Jira Cloud REST API v3.
- Never include Jira content, API tokens, webhook URLs, or other credentials.
- Update `README.md` when configuration or user-facing behavior changes.
- Validate both workflows when changing scheduling or environment variables.

## Reporting bugs

Include the Python version, sanitized configuration, workflow name, and full
error message. Remove company names, Jira content, account IDs, and secrets.
