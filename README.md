# Papra Ollama OCR adapter

Standalone source and GitHub Actions publishing starter, version 1.0.3.
Based on the HEIC-capable 1.0.2 adapter. The only runtime source change is a
generic default Ollama URL (`http://ollama:11434`); the TrueNAS configuration
already supplies `OLLAMA_URL` explicitly.

This repository builds a small HTTP adapter for Papra's custom-http text
extraction strategy. It renders PDF pages and decodes PNG/JPEG/TIFF/WebP/BMP
and HEIC/HEIF photos, sends PNG images to a local Ollama OCR model, and returns
`{"text":"..."}`. It does not embed OCR layers into uploaded files.

The source includes a Dockerfile, pinned Python dependencies, 13 tests, and
`.github/workflows/publish.yml`. No NAS credentials, document archive, model
weights, or full Papra Compose configuration are included. Existing dependencies
retain their own licenses; choose a source license before sharing this project
publicly if you want others to have clear reuse permissions.

## Create the repository and first release

1. Create an empty GitHub repository named `papra-ollama-ocr` under your account.
   Its name determines the GHCR image name. A public repository plus public
   GHCR package is the simplest setup for TrueNAS image pulls and update checks.
2. Clone that empty repository to your computer and extract this ZIP's contents
   directly into its root, including `.github/`, `.dockerignore`, and `.gitignore`.
   Do not upload just the visible files and omit `.github/`.
3. Commit the files on the `main` branch and push:

   ```bash
   git add .
   git commit -m "Add Ollama OCR adapter and GHCR publishing"
   git push -u origin main
   ```

   If your local branch has another name, rename it to `main` before pushing.
   The first main push tests the adapter and publishes an `edge` image.
4. Push the first release tag, matching the included VERSION file:

   ```bash
   git tag v1.0.3
   git push origin v1.0.3
   ```

5. On GitHub, open Actions and confirm the release-tag workflow succeeds.
   If Actions is disabled by account/organization policy, enable it for this
   repository. The workflow requests `contents: read` and `packages: write`;
   it authenticates to GHCR with the automatically supplied `GITHUB_TOKEN`.
   No manually created publishing token or repository secret is required for
   a normal new package in the same account/organization.
6. After publishing, open your GitHub profile's Packages page, select the
   package, then Package settings. Change the **package** visibility to Public
   for anonymous TrueNAS pulls. New packages start private; making the source
   repository public does not automatically make its container package public.
7. Test downloading the image on TrueNAS before changing the running app:

   ```bash
   sudo docker --config /etc/docker pull ghcr.io/YOUR_GITHUB_USERNAME/papra-ollama-ocr:stable
   ```

Replace YOUR_GITHUB_USERNAME with your actual lowercase account name. If you
used another repository name, use its lowercase name too. The workflow derives
the image name from the repository and requires no username edits.

## Image tags and update behavior

| Trigger | Published tags |
| --- | --- |
| Push to main / manual run on main | `edge`, `sha-<short-commit>` |
| Push release tag `v1.0.3` | `1.0.3`, `stable`, `latest`, `sha-<short-commit>` |
| Pull request | Tests only; no image publication |

`stable` and `latest` advance only on a release-tag push, not every main push.
The workflow accepts plain release versions such as `v1.0.3`; prerelease tags
are intentionally not supported. Keep release tags and their published images
unchanged; create a new version for a new release.

To release an update, edit the code and VERSION (for example, 1.0.4), commit and
push main, then push the matching `v1.0.4` Git tag. Passing tests gate publication.
Release pushes publish automatically; a GitHub Releases page entry is optional.
The manual workflow button on main refreshes `edge`, not `stable`.

## Switch the existing TrueNAS app to GHCR

In your existing Papra Compose, replace only the OCR adapter's image and pull
policy:

```yaml
services:
  ocr-adapter:
    image: ghcr.io/YOUR_GITHUB_USERNAME/papra-ollama-ocr:stable
    pull_policy: missing
```

This is a **partial edit**, not a complete replacement Compose. Preserve the
existing adapter environment, security settings, health check, resource limits,
and Papra's `http://ocr-adapter:8080/extract` URL. In particular, retain:

```yaml
OLLAMA_URL: http://192.168.10.12:11434
```

When using your Include configuration, edit:
`/mnt/BrockNetPoolSSD/AppConfigs/Papra/docker-compose.yaml`, then redeploy the
app through TrueNAS. Pull the public image successfully before redeploying.

The next TrueNAS app upgrade can pull both Papra and the adapter from their
registries. This fixes the local-repository access-denied error without
splitting the app. No on-NAS adapter build is needed for future published
releases. Ollama models and documents remain on the NAS; GitHub builds only
the adapter's software image.

For explicit version selection, use `:1.0.3` instead of `:stable`. TrueNAS does
not automatically change a fixed tag to `:1.0.4`; you would edit Compose when
choosing the next release. Using `:stable` is appropriate when you want the
TrueNAS update workflow to follow each version you release.

If Papra is still pinned to a specific version, moving the adapter to GHCR
does not change Papra's version-selection policy.

## Private image alternative

You can keep the GHCR package private. Configure credentials for `ghcr.io`
in TrueNAS's Docker registry settings, using your GitHub username and a classic
personal access token with `read:packages` and access to the package. Organization
SSO policies may require additional token authorization. Do not put the token
in the Compose image URL or commit it to the repository.

Private-image pull credentials and upstream update detection are separate
concerns; older TrueNAS versions can have limitations checking private registry
updates. Public GHCR packages avoid authentication for both pull and manifest
checks, which is why they are the recommended initial setup here.

## Local testing

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=pillow-heif -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
docker build -t papra-ollama-ocr:test .
```

The tests generate PDF/image fixtures and call a local mock Ollama service.
They cover HEIC decoding, orientation, primary-image selection, multi-page
documents, and error handling. They do not measure recognition accuracy or
connect to a real Ollama model.

The image is Linux AMD64 for your x86 TrueNAS host, runs as UID/GID 3009 by
default, and listens on port 8080. Retain the existing Compose `user:` override
if you use a different numeric account. `/health` is liveness only.

The adapter keeps the existing 25 MiB / 50-page limits, serial OCR, bounded
rendering, and deadline behavior. It supports HEIC/HEIF primary photos, not
video/Live Photo processing. It does not add Word document parsing. Papra's
extraction strategy, ingestion, and reprocessing settings remain configured
in your existing Papra Compose.

## Validation and scope

The starter's 13 tests were run locally and the workflow YAML and action commit
references were checked. GitHub Actions itself and the Docker image build must
run in your repository; no repository or GHCR package has been created for you.
Actions are pinned to release commit SHAs with readable version comments.

## References

- https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images
- https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry
- https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility
