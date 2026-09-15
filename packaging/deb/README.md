# Debian packaging

Builds door-sync as a `.deb` whose runtime dependencies are **real Debian
packages**, not a bundled virtualenv.

```sh
packaging/deb/build-deb.sh                      # version from pyproject.toml
packaging/deb/build-deb.sh --version 0.3.0      # must match pyproject
```

Requires `dpkg-deb`, `python3` and `uv`, so it runs on a Debian-ish host or in
CI — not on macOS. `.github/workflows/release.yml` builds it on every published
release and attaches the `.deb` to the release. The same workflow's image job
then installs that exact package into the appliance image.

It does **not** run on pull requests. To exercise a packaging change before
merging, dispatch the workflow against the branch (Actions -> Release -> Run
workflow): that builds and verifies the package without uploading anything,
because the publish job is gated on a release event.

## Why a package rather than a venv

The rpi-image-gen appliance build (`deploy/rpi-image-gen/`) bakes door-sync into
an immutable root. `image-rota` sells that root on the claim that "executing
software matches the manifest exactly" — and a pip-installed venv breaks it,
because dpkg knows nothing about the code inside, so neither does any SBOM built
from the package database.

Declaring `python3-httpx`, `python3-flask` and `python3-waitress` as Debian
dependencies puts every runtime component in the package database, where an SBOM
can see it. It also means security updates arrive through `apt` like everything
else on the machine, rather than needing a rebuild.

## Architecture: all

The wheel is `py3-none-any` with `Root-Is-Purelib: true`, so one package serves
the arm64 Pi and an amd64 CI container alike. That is why CI can meaningfully
verify on an amd64 runner a package destined for a Pi.

## The flask floor

`pyproject.toml` previously required `flask>=3.1.3`. Debian trixie ships
**3.1.1-1+deb13u1**, so that floor made the package uninstallable on the target
OS. The floor is now `>=3.1.1`.

This is safe: the only Flask API door-sync uses is `Flask`, `Response`,
`request`, `app.config[...]`, `@app.get` and `@app.post` — all present since
Flask 2.0. The 3.1.3 floor came from `uv add` picking the newest release, not
from a requirement. The `+deb13u1` suffix means Debian has already applied
stable updates to its 3.1.1 build.

Lowering a floor only widens what is acceptable; `uv.lock` still resolves to the
newest Flask, so development is unchanged. Verified: 404 tests pass and the venv
still gets 3.1.3.

`httpx` (0.28.1) and `waitress` (3.0.2) match their floors exactly in trixie, so
there is no margin there — check both when bumping either.

The `Depends` line is **generated from `pyproject.toml`**, so the two cannot
drift. A dependency with no Debian counterpart fails the build rather than
shipping a package that cannot satisfy its own imports.

## adduser

The maintainer scripts use `addgroup`/`adduser`/`deluser`, so the package
depends on `adduser`. It is Priority: important, so most Debian systems already
have it — but minimal images do not, including the `debian:trixie-slim` that CI
verifies against, where `postinst` fails with `addgroup: not found`. Caught by
CI's install test rather than at release time -- which is the argument for
running that test at all, and for dispatching this workflow manually when you
touch the dependency list.

## Deliberate choices

**The service is installed but not started.** `door-sync.service` has no leading
dash on `EnvironmentFile`, so without `/etc/door-sync/env` it would restart-loop
— correct fail-secure behaviour, but a poor thing to trigger from `apt install`.
Provision config and secrets from `/usr/share/door-sync/`, then enable it.

**`apt purge` keeps `/var/log/door-sync`.** `audit.jsonl` is the record of who
had door access when; purging a package should not silently destroy it.
`/var/lib/door-sync` (state) is removed. CI asserts both.

**The service account is created only if missing**, so an image build that pins
the uid wins and `postinst` becomes a no-op. That matters for the appliance
image, where `/persistent` outlives any single build and a shifting uid would
leave the audit log owned by the wrong user.

## Gaps

- **No declared licence.** The repo has no `LICENSE` and no `project.license`,
  so `/usr/share/doc/door-sync/copyright` says so rather than asserting terms
  nobody has chosen. Debian policy requires the file to exist; it does not
  require it to be honest, but it should be. Fix upstream, then fix the file.
- **Not lintian-clean.** The package is hand-assembled with `dpkg-deb` rather
  than built through `debhelper`, which is the right trade for one pure-Python
  app a volunteer has to maintain, but it means no changelog, no source package,
  and lintian will have opinions.
