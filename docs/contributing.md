# Collab Hub Pack Contribution Standards

## Pull Requests

PRs must link to their parent issue and explain, in plain English, how the change solves or ties to that issue. Use a closing keyword such as `Fixes #123`, or GitHub's Development link, so the project board can show the issue/PR relationship.

The PR template checklist must be completed before review. If a requirement does not apply, leave the checkbox unchecked and explain why in the PR body.

Do not request final code-owner review while the PR is still in draft. If you are unsure who should review, ask in Slack and include the PR link. Preliminary peer review is encouraged before escalating to a code owner, especially for new contributors.

## UI Evidence

User-facing UI changes must include useful before/after screenshots when the change affects an existing screen. For new UI, include screenshots that show the new state clearly.

If a PR changes a workflow or introduces a new workflow that requires more than one click, it must include an attached demo video showing the new UI behavior introduced by the PR.

## Tests

Major functionality must include performant unit tests that run in PR CI.

Bug fixes must include regression tests that would have failed before the fix.

Tests should be scoped to the changed behavior and should avoid slow, flaky, or environment-dependent setup unless the change specifically requires it.

## Code Owners

The Collab Hub pack has parent-level code owners in `.github/CODEOWNERS`.

At least one code-owner approval is expected before merge. GitHub branch protection must require CODEOWNER reviews for this to be enforced automatically.
