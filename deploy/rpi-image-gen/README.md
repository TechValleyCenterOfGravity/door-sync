# rpi-image-gen appliance build (draft)

Builds door-sync as an immutable A/B appliance image using
[rpi-image-gen](https://github.com/raspberrypi/rpi-image-gen) with the
`image-rota` layer: read-only root, two system slots, rollback by flipping the
slot, and all writable state on a shared persistent partition.

**Status: draft, never built.** Authored against upstream docs and layer
sources; it has not been run. Lint it (`ig` metadata lint, per upstream's
`layer/LAYER_BEST_PRACTICES`) and build it before trusting any of it.

## Files

| File | Purpose |
| --- | --- |
| `layer/door-sync.yaml` | The application layer: account, the door-sync `.deb`, slot-shared state |
| `layer/cloudflared.yaml` | Cloudflare Tunnel daemon: pinned binary and service account only |
| `config/door-sync-ab.yaml` | Image config: includes `trixie-minbase-ab`, overrides sizes, hostname, version |
| `RUNBOOK.md` | Operating a flashed device: first boot, config changes, health, updates |

## door-sync is installed as a package

The layer installs the `.deb` from `packaging/deb/` rather than staging a
virtualenv. That is what makes the SBOM honest: `image-rota` justifies the
immutable root partly on "executing software matches the manifest exactly", and
dpkg cannot see inside a venv, so neither can an SBOM built from the package
database. Installing a package puts door-sync *and* flask, httpx and waitress in
that database.

It also deletes a lot of this layer. The package supplies the module,
`/usr/bin/door-sync`, `door-sync.service`, the logrotate config and the examples
under `/usr/share/door-sync/`, and its `postinst` creates `/etc/door-sync` and
the state directories. The layer no longer builds a venv, rewrites `ExecStart`,
or hand-places units and example config — and no longer needs a path to the
repo's `deploy/` directory at all, only the `.deb`.

The service account is still created by the layer, deliberately, and *before*
the package: `postinst` creates it only when missing, so doing it first pins the
uid and makes `postinst` a no-op there. `/persistent` outlives any single image,
and a uid that shifted between builds would leave the audit log owned by the
wrong user after a reflash.

Runtime dependencies are declared in the layer's `packages:` so mmdebstrap
installs them from the Debian mirror and `dpkg -i` needs no resolution. If that
list ever drifts from the package's own `Depends`, `dpkg -i` fails at build time
rather than shipping something broken.

## Build

rpi-image-gen wants a **Debian Bookworm/Trixie arm64** host with `CAP_SYS_ADMIN`
(bdebstrap, mmdebstrap, genimage, podman), plus `curl` for the cloudflared fetch.
The same host can build the `.deb`; `packaging/deb/build-deb.sh` needs `dpkg-deb`,
`python3` and `uv`. On an Apple Silicon Mac an arm64
Debian VM runs natively; building on a Pi also works. QEMU is not formally
supported upstream.

```sh
packaging/deb/build-deb.sh --output dist      # or download a release asset
rpi-image-gen build -S ./deploy/rpi-image-gen/ -c door-sync-ab.yaml -- \
  IGconf_doorsync_deb=$PWD/dist/door-sync_0.2.0_all.deb
```

`-S` sets the source directory, so `config/` and `layer/` are found beneath it;
layer variables are passed after `--`. The build emits
`door-sync-ab.update.tar.zst` next to the disk image — that bundle is the OTA
artefact, not the `.img`.

## Decisions worth reviewing

**Slot-shared state is the important part.** `image-rota` makes `/var` *per
slot* — upstream is explicit that a layer's state under `/var` "will be wiped or
reset on the next slot flip unless you add a `slot-shared.d` entry for it".
Without the entry in this layer, `audit.jsonl` — the record of who had door
access when — silently restarts on every OTA, and `state.json`'s last-success
timestamp resets, which would likely trip staleness alerting after each update.

The safety guards are unaffected either way: `active_baseline` is computed live
from the UniFi user list each cycle, never read from `state.json`. So a slot
flip cannot weaken the mass-change guards.

**One consequence to keep in mind:** upstream shares state opt-in precisely
because a newer slot can write state an older slot cannot read, which breaks
rollback. For door-sync that means `state.json` must stay backward-readable —
add fields, never repurpose or remove them — or a rollback lands on a state file
the older build mis-parses. `audit.jsonl` is append-only and never read back, so
it is safe.

**Secrets are not in the image, and config is not either.** `/etc/door-sync` is
declared slot-shared, which makes it writable on a read-only root and persists
it across flips. The image ships only `config.example.toml` and `env.example`
under `/usr/share/door-sync/`; the operator provisions the real files on first
boot.

The alternative is baking `config.toml` into the image and letting OTA manage
it. That works — upstream pushes baked files into the shared location — but
files are pushed whenever they *differ*, so an on-device edit to `config.toml`
would be silently reverted on the next boot. Given the config carries safety
thresholds an operator may want to tune without rebuilding an image, device-
provisioned is the safer default. Flip it if you would rather config be
image-managed.

Until `/etc/door-sync/env` exists, `door-sync.service` will restart-loop —
`EnvironmentFile=` has no leading `-`, so a missing secrets file is a hard
failure. That is the fail-secure behaviour you want, but it means a freshly
flashed card is loudly broken until provisioned. The provisioning procedure is
in `RUNBOOK.md`.

**UIDs are pinned (900/901), not allocated.** The persistent partition outlives
any single image. If a later build allocated different system uids, the existing
audit log and state would come back owned by the wrong user after a reflash.

**The unit comes from the package**, which points `ExecStart` at
`/usr/bin/door-sync`. The hardening, `ReadWritePaths`, and
`After=time-sync.target` all carry over unchanged from `deploy/`. The layer requires `systemd-timesyncd` for
the same reason that ordering exists: the Pi has no RTC, and the webhook rejects
signatures outside `max_skew_seconds`.

It deliberately does **not** require `fake-hwclock`, despite the missing RTC.
fake-hwclock restores the clock to the last shutdown time, which on a Pi that
has been off for a day is wrong by a day — and a clock that is wrong by a day
fails a 300-second freshness window exactly as surely as one that is unset. Only
`After=time-sync.target` actually protects the webhook. Requiring it also breaks
the build: something in this image stack masks `fake-hwclock.service`, so
upstream's layer fails when it tries to enable it.

## cloudflared

**A separate layer**, on the seam between binary and configuration.
`layer/cloudflared.yaml` installs the daemon and its service account and nothing
else — no unit, no tunnel config, no opinion about what it connects to.
`layer/door-sync.yaml` requires it and supplies the rest, because which tunnel
runs and where it points (`127.0.0.1:8787`, the webhook port) is entirely
door-sync's business.

Splitting it follows upstream's own idiom — small single-purpose layers composed
through `X-Env-Layer-Requires`, the way `systemd-timesyncd` requires
`systemd-min` — and it keeps the variable namespaces honest: `cfver`/`cfsha`
under a `doorsync` prefix described cloudflared, not door-sync. They are now
`IGconf_cloudflared_version` and `IGconf_cloudflared_sha`. It also means bumping
cloudflared, which releases roughly monthly, never touches the door-sync layer.

Upstream ships no cloudflared layer of its own, so this one is ours.

### Why not `cloudflared service install`

Cloudflare's documented path is `cloudflared service install`, which generates
the systemd unit itself and expects config at `$HOME/.cloudflared/config.yml`.
This layer ships a unit instead, deliberately:

- `service install` is an imperative step that writes a unit at run time. A
  read-only root cannot do that at all, and an immutable image wants its units
  baked, reviewed, and in version control.
- The generated unit runs the tunnel as **root**. Ours runs it as a dedicated
  `cloudflared` account under `NoNewPrivileges`, `ProtectSystem=strict` and
  `ProtectHome`. For an outbound-only tunnel on a door controller that is a
  meaningful difference, and cloudflared needs no privileges to make an outbound
  connection.
- Passing `--config /etc/cloudflared/config.yml` is Cloudflare's own documented
  override for the `$HOME` default, so nothing here is unsupported — just
  declarative rather than generated.

The trade is that our unit can drift if cloudflared changes its expected
invocation. Re-read Cloudflare's service docs when bumping `version`.

### /etc/cloudflared must be slot-shared

That directory holds `config.yml` **and the tunnel credentials JSON**, both
provisioned per device. Two consequences on an immutable A/B root: it has to be
a writable bind mount at all, and it has to be slot-shared or the first OTA flip
loses the credentials and the tunnel silently stops connecting — the tunnel goes
down, the webhook becomes unreachable, and door-sync's own logs say nothing,
because the request never arrives. The layer declares it, mode 0750 owned by the
`cloudflared` account so it can read its own credentials.

Each layer declares its own shared paths, which is why this sits here rather
than in `door-sync.yaml` alongside the audit log and state.

Installed from a **pinned `.deb` with a SHA256 check**, not Cloudflare's apt
repo. The repo floats, so two builds of the same config could produce different
images; and the read-only root blocks on-device `apt` anyway, which would make
an apt source in the image dead weight plus an extra trust anchor. dpkg records
the install, so like door-sync itself it appears in the SBOM.

Pinned at **2026.9.1**. Verified against the real artifact rather than the
release notes: checksum matches, the package declares **no `Depends`** (static
Go binary, so plain `dpkg -i` resolves nothing), and it ships **no systemd unit**
of its own -- `./usr/bin/cloudflared` is the only binary in it.

That last detail surfaced a bug outside this directory. The `.deb` installs to
`/usr/bin/cloudflared`, but `deploy/cloudflared.service` said
`/usr/local/bin/cloudflared` — and nothing in `docs/usage.rst` ever tells you to
install cloudflared at all, so that path was an unexamined default rather than a
documented convention. It is wrong for the official `.deb`, which is the normal
way to install cloudflared on Debian, so it was wrong for a hand-built Pi too,
not just for an image. **`deploy/cloudflared.service` is fixed at source** rather
than rewritten during the build. Left unfixed, the tunnel never starts and the
webhook is unreachable with nothing in door-sync's logs to explain it.

To bump the version, change `version` and `sha` together — the checksum is
per-release and the build fails closed if they disagree. For an offline or
air-gapped build, pre-download the `.deb` and point `deb` at it; the checksum
is still verified.

## Updates: Raspberry Pi Connect

Delivery follows upstream's `examples/ota`: the layer requires `rpi-connect-lite`
and `rpi-connect-ota`, the build emits an OTA bundle named after the image, and
Raspberry Pi Connect pushes it to the device, which writes the inactive slot and
flips. `artefact.version` is what an operator sees when choosing whether to
deploy or roll back, so keep it in step with the door-sync release it carries.

**Decided: adopted.** Two costs were weighed and accepted:

- Raspberry Pi describes the remote update capability as **experimental**.
- It puts a **vendor remote-management channel on a door controller**. That is
  the same objection raised against balena's control plane, and it applies here
  too. The difference is that the rest of the stack stays plain Debian and
  systemd, so the channel is removable without redesigning the deployment. If
  the trade later proves unacceptable, drop the two `rpi-connect-*` layers:
  `image-rota` still gives immutable A/B roots and rollback, and updates become
  reflash-or-bring-your-own-transport.

**Still open: first-boot sign-in.** Either a per-device identity (Connect for
Organisations, no credentials in the image) or an embedded single-use auth key
passed at build time. Prefer the former — an auth key baked into an image is a
secret living in an artefact you may later want to rebuild or hand to someone
else. With a single device, interactive `rpi-connect signin` at provisioning
time is a third option worth checking: it needs console access once and puts no
secret in the image at all.

## Building it in CI

`.github/workflows/release.yml` builds the image and OTA bundle on
`ubuntu-24.04-arm`, which is what upstream uses for its own images — native
arm64, no container, no QEMU (which upstream does not formally support). It runs
on published releases and on manual dispatch, and deliberately **not** on pull
requests: the build takes minutes on a scarce arm64 runner, and a paths filter
fired on documentation living beside the image definition as readily as on the
definition itself.

To validate a packaging or image change before merging, dispatch the workflow
against the branch (Actions -> Release -> Run workflow). That builds the package
and the image and skips the publish job, which is gated on a release event. The
trade is that nothing forces that check — an image change can merge unbuilt, so
run it when the change is more than cosmetic.

The image job takes the `.deb` from the package job in the same workflow rather
than rebuilding it, so a release ships the byte-identical artefact that was
verified against Debian trixie.

rpi-image-gen is pinned to a commit. It is under active development, and the
image that opens a door should not change because upstream moved. Override it
for a one-off with the `workflow_dispatch` input.

`SOURCE_DATE_EPOCH` comes from the commit being built, which pins the
bootstrapped rootfs timestamps to the revision rather than to whenever CI ran.
That is **not** bit-for-bit reproducibility, and the distinction is worth
keeping straight: upstream's `builtin/hooks/cleanup01` writes
`$(date +%Y-%m-%d)` into `/etc/rpi-issue`, so two builds of the same commit on
different days still differ. Fixing that needs a change upstream.

Artefacts are **discovered** rather than read from a hardcoded path: upstream
does not document its deploy directory and is free to move it, so the workflow
searches for `door-sync-ab*` and fails loudly if nothing turns up. The disk image
is zstd-compressed, which takes a mostly-empty multi-GB image well under the
2 GiB per-asset limit on releases; anything still over that is skipped with a
warning rather than failing the run.

For releases the workflow asserts that `artefact.version` in the config matches
the tag. A bundle labelled with the wrong version is worse than a failed build
when the thing being updated is a door controller.

## Known gaps

- **`docs/usage.rst` never documents installing cloudflared.** Independent of
  this directory: the manual deployment path ships a unit for a binary the docs
  never tell you to install. Worth a paragraph pointing at the official `.deb`.
- **Partition sizes are estimates.** 1G per system slot and 4G shared are
  starting guesses, not measurements.
