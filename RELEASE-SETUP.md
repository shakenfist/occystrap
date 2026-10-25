# Release Infrastructure Setup

This document describes how to configure PyPI and GitHub to enable automated
releases using GitHub Actions with Sigstore signing.

## Overview

The release process uses:

- **PyPI Trusted Publishers (OIDC)**: No API tokens needed; PyPI trusts the
  GitHub Actions workflow directly
- **Sigstore/gitsign**: Keyless signing for git tags (no GPG private key
  management)
- **GitHub Environments**: Required reviewer approval before releases proceed
- **Protected Tags**: Restrict who can create release tags

## One-Time Setup Steps

### 1. Configure PyPI Trusted Publisher

This allows the GitHub Actions workflow to publish to PyPI without storing any
API tokens.

1. Log in to [pypi.org](https://pypi.org) with your account
2. Navigate to your project: `occystrap`
3. Go to **Settings** (or **Your projects** > **Manage**)
4. Click **Publishing** in the left sidebar
5. Under **Trusted Publishers**, click **Add a new publisher**
6. Fill in the form:
   - **Owner**: `shakenfist`
   - **Repository name**: `occystrap`
   - **Workflow name**: `release.yml`
   - **Environment name**: `release` (must match the workflow)
7. Click **Add**

The workflow will now be able to publish without any stored credentials.

**Note**: If the `occystrap` package already exists on PyPI under a different
publishing method, you can add the trusted publisher alongside the existing
setup and then remove the old API token once verified.

### 2. Create GitHub Environment with Required Reviewers

This ensures releases only happen after explicit approval.

1. Go to the repository on GitHub: `shakenfist/occystrap`
2. Click **Settings** > **Environments**
3. Click **New environment**
4. Name it: `release`
5. Click **Configure environment**
6. Under **Environment protection rules**:
   - Check **Required reviewers**
   - Add yourself (and any other trusted maintainers)
   - Optionally add a **Wait timer** (e.g., 5 minutes) for additional safety
7. Under **Deployment branches and tags**:
   - Select **Selected branches and tags**
   - Add a rule: `v*` (to only allow release tags)
8. Click **Save protection rules**

The consistency audit's `release-environment-protection` criterion
checks this: the environment must exist, have a required reviewer, and
deploy only from tags. It is described in
`docs/audits/release-environment-protection.md` in
shakenfist/development.

### 3. Configure Protected Tags (Recommended)

This prevents unauthorized users from creating release tags.

1. Go to **Settings** > **Rules** > **Rulesets**
2. Click **New ruleset** > **New tag ruleset**
3. Configure:
   - **Ruleset name**: `Release tags`
   - **Enforcement status**: `Active`
   - **Target tags**: Add pattern `v*`
   - **Rules**: Check **Restrict creations** and **Restrict deletions**
   - **Bypass list**: Add repository admins or specific maintainers
4. Click **Create**

### 4. Configure the Merge Queue

The release gate counts a CI run only from the merge queue, or from a
push to the default branch, which is `develop` here. The queue is
disabled while
[occystrap#169](https://github.com/shakenfist/occystrap/issues/169) is
open, because it never built a merge group. Until then the ruleset
has steps 1 to 3, step 5 requires only "Can enqueue" and "Can
enqueue: unit", pull requests merge directly, and the CI workflows'
push run on `develop` is the one the gate counts. To re-enable the
queue, add step 4 and the two "Can merge" checks in step 5; the
workflows keep their `merge_group` trigger, so nothing else changes.

1. Go to **Settings** > **Rules** > **Rulesets** > **New ruleset** >
   **New branch ruleset**
2. Name it `Develop branch`, set **Enforcement status** to
   **Active**, and target the default branch (`develop`)
3. Enable **Restrict deletions**, **Block force pushes**, and
   **Require a pull request before merging**
4. Enable **Require merge queue**, with merge method **Merge**,
   **Build concurrency** 1, **Minimum group size** 1, **Maximum group
   size** 5, **Wait time** 5 minutes, **Require all queue entries to
   pass required checks** (`ALLGREEN`) and a **Status check timeout**
   of 360 minutes. A serial queue is what the consistency audit's
   `merge-queue-config` criterion accepts, and the workflows'
   concurrency groups depend on it
5. Enable **Require status checks to pass**, and add "Can enqueue"
   and "Can enqueue: unit", plus "Can merge" and "Can merge: unit"
   while the queue is enabled, with **GitHub Actions** as the source. Each of the two workflows the
   release gate counts has a pair of its own
6. Keep the bypass list short. A merge that bypasses the required
   checks still gets a push run on `develop`, but that run is what the
   release gate judges, so a broken bypass merge cannot be released

### 5. Verify Sigstore/Rekor Access

No configuration needed. Sigstore is a public service that:

- Signs artifacts using OIDC identity (the GitHub Actions workflow identity)
- Records signatures in a public transparency log (Rekor)
- Requires no key management

Verification can be done by anyone using `cosign` or `gitsign verify`.

## How Releases Work

1. A maintainer pushes a tag matching `v*` (e.g., `v0.5.0`)
2. The `release.yml` workflow triggers
3. The `ci-gate` job checks that the repository's CI workflows
   succeeded on the tagged commit. The workflow builds the package
   only if the gate passed, and then waits for environment approval
4. A required reviewer approves the release in GitHub's UI. The
   reviewer is approving a release whose CI the gate has already
   checked, so the approval is a decision about the release, not a
   substitute for looking at CI. The deployment review page does not
   show CI status, which is why the gate has no override
5. The workflow:
   - Creates a signed git tag using gitsign (Sigstore)
   - Generates Sigstore attestations for the built artifacts
   - Publishes to PyPI using OIDC (no tokens)
   - Creates a GitHub Release with the artifacts

## Verifying Releases

### Verify Git Tag Signature

```bash
# Install gitsign
go install github.com/sigstore/gitsign@latest

# Verify a tag
gitsign verify --certificate-identity-regexp='.*' \
    --certificate-oidc-issuer='https://token.actions.githubusercontent.com' \
    v0.5.0
```

### Verify PyPI Package Attestation

```bash
# PyPI shows attestation status on the package page
# Look for the "Provenance" section
```

### Verify with Cosign

```bash
# Install cosign
go install github.com/sigstore/cosign/v2/cmd/cosign@latest

# Verify artifact attestation
cosign verify-attestation \
    --certificate-identity-regexp='.*' \
    --certificate-oidc-issuer='https://token.actions.githubusercontent.com' \
    occystrap-0.5.0.tar.gz
```

## Troubleshooting

### "Environment not found" Error

Ensure the environment name in the workflow (`release`) exactly matches the
environment created in GitHub Settings.

### "Publisher not found" Error on PyPI

- Verify the workflow filename matches exactly (case-sensitive)
- Verify the environment name matches exactly
- Ensure you're using the correct PyPI account (not TestPyPI)

### Tag Signature Verification Fails

- Ensure you're checking against the correct OIDC issuer
- The certificate identity will be the workflow's identity, not a personal
  email

### Approval Not Requested

- Ensure the tag matches the deployment branch/tag rules (e.g., `v*`)
- Check that required reviewers are configured on the environment

## Security Considerations

- **No long-lived secrets**: Neither GPG keys nor PyPI tokens are stored
- **Audit trail**: All releases are logged in GitHub Actions and Sigstore's
  Rekor transparency log
- **Multi-party approval**: Required reviewers prevent unilateral releases
- **Immutable provenance**: Sigstore attestations cryptographically link
  artifacts to the exact source commit
