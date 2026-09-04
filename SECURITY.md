# Security Policy

## Reporting a vulnerability

Do not open a public issue for suspected vulnerabilities or exposed
credentials. Use GitHub's private vulnerability reporting feature for this
repository.

Include reproduction steps and affected versions without attaching real Jira
content, API tokens, or Mattermost webhook URLs.

## Credential handling

Store credentials only as GitHub Actions secrets or local environment
variables. Rotate a credential immediately if it is committed, logged, or
shared unintentionally.

Run production reports from a private deployment fork. Jira API failures can
include operational metadata in Actions logs, and logs in a public repository
are publicly readable. The workflows in this source repository are
manual-only by default for that reason.
