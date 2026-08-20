# 0010. Public repository, and the exposure that comes with it

Date: 2026-08-20
Status: Accepted

## Context

On 2026-08-19 a pull request whose CI had failed was merged into `main`, breaking every
install (see [0008](0008-renovate-as-an-action.md)). CI had caught the problem and reported
it. Nothing stopped the merge.

The fix is a required status check. On GitHub Free with a private repository, that is not
available. Tested directly rather than read from a pricing page:

| Feature | private + Free |
| --- | --- |
| Branch protection | `Upgrade to GitHub Pro or make this repository public` |
| Rulesets | same message |
| Environments (create) | works |
| Deployment branch policy | works |
| **Required reviewers** | `Please ensure the billing plan supports the required reviewers protection rule` |

The last row also blocks the approval gate that a promotion pipeline needs, so the
limitation reached beyond this one incident.

Three ways out: pay for GitHub Pro, migrate to a forge that includes branch policies for
free (GitLab, Azure DevOps), or make the repository public. Migration would have discarded
nine decision records and the Actions mechanics built around them.

One obstacle to going public: the working reference was called `CLAUDE.md`, and the author
did not want AI tooling visible in a portfolio repository. An audit found that to be the
only trace — no attribution in commit messages, no mention in any file body, `.claude/`
never committed. It was renamed to `docs/engineering.md` and removed from history with
`git filter-repo`.

## Decision

Make the repository public.

Harden the self-hosted runner exposure explicitly rather than relying on the current
configuration happening to be safe:

```
fork-pr-contributor-approval    all_external_contributors
default_workflow_permissions    read
branch protection on main       required check "test", enforce_admins, no force pushes
GHCR package                    stays private
```

## Consequences

- Required status checks now apply, including to the repository owner. The 2026-08-19
  incident is no longer possible.
- Environment protection rules become available, so [0011] can use a real approval gate
  rather than a manual `workflow_dispatch`.
- Actions minutes are unlimited for public repositories.
- The work is visible. That is a benefit for a portfolio and a constraint on what may ever
  be committed — every future secret, hostname and internal path is public by default now.
- `allow_force_pushes: false` means another history rewrite requires disabling protection
  first. Deliberate.
- Removing the AI trace cost every pre-rewrite commit SHA. Images in GHCR tagged with old
  SHAs no longer correspond to any commit, so the guarantee from
  [0006](0006-compiled-requirements-with-uv.md) holds only from the rewrite forward. The
  force push itself triggered a build, so the current deployment is consistent again.
- The runner remains a permanent, non-ephemeral process on a personal machine. The guards
  below make it unreachable from a fork; they do not make it disposable.

## Background

### Why a public repository plus a self-hosted runner is the dangerous combination

On a public repository anyone can open a pull request. If a workflow triggered by that pull
request runs on a self-hosted runner, an attacker's code executes on the host — here a
personal machine, with access to the Docker socket, the deployment directory and the
`.env` written from repository secrets. It is remote code execution by design, not by bug.

Four things keep that closed here, and all four must stay true:

1. **`ci.yml` runs on `ubuntu-latest`.** It is the only workflow with a `pull_request`
   trigger. Moving either of its jobs to `self-hosted` would open the hole immediately.
2. **`deploy.yml` is `workflow_run`-triggered and guarded.** Its condition requires
   `event == 'push'` and `head_branch == 'main'`. A fork pull request produces
   `event == 'pull_request'`, so the job never starts.
3. **`runner-test.yml` is `workflow_dispatch` only**, which requires write access.
4. **No `pull_request_target` anywhere.** That trigger runs in the context of the base
   repository — with secrets — and combining it with a checkout of the pull request's head
   is the classic way to leak them.

`fork-pr-contributor-approval: all_external_contributors` adds a fifth layer: no external
workflow run starts at all without explicit approval. It is defence in depth, not the
primary control. The primary control is rule 1.

### What the branch protection settings mean

```json
{
  "required_status_checks": {"strict": false, "contexts": ["test"]},
  "required_pull_request_reviews": {"required_approving_review_count": 0},
  "enforce_admins": true,
  "allow_force_pushes": false
}
```

- Only `test` is required. `build` is skipped on pull requests by its own `if:` condition,
  and requiring a check that never reports would block every merge.
- `required_approving_review_count: 0` still requires a *pull request* — it only waives the
  approval. Direct pushes to `main` are rejected.
- `strict: false` does not force a branch to be up to date with `main` before merging.
  With `true`, every unrelated merge would demand a rebase; for a single-author repository
  that is friction without benefit.
- `enforce_admins: true` is the setting that matters. Without it the rule would not apply
  to the only person who can break it.

If a misconfiguration ever locks the repository, protection can be removed with
`DELETE /repos/{owner}/{repo}/branches/main/protection`.

### Deliberately not done

- **Ephemeral runners.** A runner registered with `--ephemeral` handles one job and exits,
  so nothing persists between jobs. It is the correct answer if untrusted code ever runs on
  the runner. Since rule 1 above means it does not, this is deferred.
- **Pinning actions to commit SHAs.** `sha_pinning_required` is currently `false`, and
  actions are referenced by tag. A tag can be moved to point at different code. Belongs
  with the Phase 4 supply chain work, together with image scanning.
- **Removing `runner-test.yml`.** It has served its purpose and is one more self-hosted
  workflow than necessary. Low value, low risk, left for a tidier moment.

- [Security hardening for GitHub Actions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions)
- [Self-hosted runner security](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access#about-self-hosted-runner-security)
