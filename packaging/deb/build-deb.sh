#!/usr/bin/env bash
# Build door-sync as a Debian binary package.
#
# door-sync is pure Python (the wheel is py3-none-any), so the package is
# Architecture: all -- one .deb serves the arm64 Pi and an amd64 test container
# alike, with no cross-build.
#
# Runtime dependencies are declared as real Debian packages rather than vendored
# into a venv. That is the whole point: a venv is invisible to dpkg, and so to
# any SBOM built from the package database.
#
# Usage:
#   packaging/deb/build-deb.sh [--version X.Y.Z] [--output DIR]
#
# --version defaults to project.version in pyproject.toml. When given, it must
# match -- a release tag that disagrees with the source is a packaging bug, not
# something to paper over.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/dist"
VERSION=""

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="${2:?--version needs a value}"; shift 2 ;;
    --output)  OUTPUT_DIR="${2:?--output needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

for tool in dpkg-deb python3 uv; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 1; }
done

PROJECT_VERSION="$(python3 - "$REPO_ROOT/pyproject.toml" <<'PY'
import sys, tomllib
print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])
PY
)"

if [ -z "$VERSION" ]; then
  VERSION="$PROJECT_VERSION"
elif [ "$VERSION" != "$PROJECT_VERSION" ]; then
  echo "version mismatch: asked for ${VERSION}, pyproject says ${PROJECT_VERSION}" >&2
  echo "bump project.version before tagging the release" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ROOT="$WORK/root"

echo "==> building wheel"
uv build --wheel --out-dir "$WORK/wheel" >/dev/null
WHEEL="$(echo "$WORK"/wheel/door_sync-*.whl)"
[ -f "$WHEEL" ] || { echo "no wheel produced" >&2; exit 1; }

echo "==> staging $ROOT"
install -d "$ROOT/usr/lib/python3/dist-packages" \
           "$ROOT/usr/bin" \
           "$ROOT/usr/lib/systemd/system" \
           "$ROOT/etc/logrotate.d" \
           "$ROOT/usr/share/door-sync/examples" \
           "$ROOT/usr/share/doc/door-sync" \
           "$ROOT/DEBIAN"

# The wheel is purelib, so its contents drop straight into dist-packages. The
# .dist-info is kept: it is what importlib.metadata reads, and dh-python keeps
# it too.
python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
  "$WHEEL" "$ROOT/usr/lib/python3/dist-packages"
rm -rf "$ROOT/usr/lib/python3/dist-packages"/*.dist-info/RECORD

# Console entry point. The wheel's own script shebangs the build venv, which
# does not exist on the target, so write our own against the system python.
cat > "$ROOT/usr/bin/door-sync" <<'EOF'
#!/usr/bin/python3
import sys

from door_sync.__main__ import main

sys.exit(main())
EOF
chmod 0755 "$ROOT/usr/bin/door-sync"

install -m0644 "$REPO_ROOT/deploy/door-sync.service" "$ROOT/usr/lib/systemd/system/"
install -m0644 "$REPO_ROOT/deploy/door-sync.logrotate" "$ROOT/etc/logrotate.d/door-sync"
install -m0644 "$REPO_ROOT/config.example.toml" "$ROOT/usr/share/door-sync/"
install -m0644 "$REPO_ROOT/.env.example" "$ROOT/usr/share/door-sync/env.example"
# cloudflared is a separate daemon with its own package; ship its unit and tunnel
# config as examples rather than depending on something we do not install.
install -m0644 "$REPO_ROOT/deploy/cloudflared.service" \
               "$REPO_ROOT/deploy/cloudflared-config.yml" \
               "$ROOT/usr/share/door-sync/examples/"
install -m0644 "$REPO_ROOT/packaging/deb/copyright" "$ROOT/usr/share/doc/door-sync/copyright"

# The unit ships with ExecStart pointing at a manual install prefix; the package
# puts the entry point on PATH.
sed -i'' -e 's#^ExecStart=/usr/local/bin/door-sync#ExecStart=/usr/bin/door-sync#' \
  "$ROOT/usr/lib/systemd/system/door-sync.service"

echo "==> resolving Debian dependencies from pyproject"
DEPENDS="$(python3 - "$REPO_ROOT/pyproject.toml" <<'PY'
import sys, tomllib
from packaging.requirements import Requirement

# PyPI distribution name -> Debian binary package. Anything not listed is a
# build failure rather than a silent omission: a dependency added to pyproject
# without a Debian counterpart would otherwise ship broken.
DEBIAN = {"flask": "python3-flask", "httpx": "python3-httpx", "waitress": "python3-waitress"}

deps = ["python3:any", "python3 (>= 3.11)"]
unmapped = []
for raw in tomllib.load(open(sys.argv[1], "rb"))["project"]["dependencies"]:
    req = Requirement(raw)
    deb = DEBIAN.get(req.name.lower())
    if deb is None:
        unmapped.append(req.name)
        continue
    floors = [s.version for s in req.specifier if s.operator in (">=", "==")]
    deps.append(f"{deb} (>= {floors[0]})" if floors else deb)
if unmapped:
    sys.exit(f"no Debian package mapped for: {', '.join(unmapped)} -- "
             f"add it to DEBIAN in build-deb.sh, or it will not be installed")
print(", ".join(deps))
PY
)"
echo "    Depends: $DEPENDS"

INSTALLED_SIZE="$(du -sk "$ROOT" | cut -f1)"

cat > "$ROOT/DEBIAN/control" <<EOF
Package: door-sync
Version: ${VERSION}
Architecture: all
Maintainer: Tech Valley Center of Gravity <noreply@techvalleycog.org>
Depends: ${DEPENDS}
Section: net
Priority: optional
Homepage: https://github.com/TechValleyCenterOfGravity/door-sync
Installed-Size: ${INSTALLED_SIZE}
Description: CiviCRM to UniFi Access reconciliation daemon
 Reconciles CiviCRM membership records against UniFi Access users so that door
 access follows membership state. Runs as a long-lived systemd service with an
 optional HMAC-authenticated webhook receiver for CiviCRM-triggered syncs.
 .
 Safety guards halt the cycle rather than apply a suspicious diff, and every
 applied or halted diff is written to an append-only JSONL audit log.
EOF

echo "/etc/logrotate.d/door-sync" > "$ROOT/DEBIAN/conffiles"

install -m0755 "$REPO_ROOT/packaging/deb/postinst" "$ROOT/DEBIAN/postinst"
install -m0755 "$REPO_ROOT/packaging/deb/prerm"    "$ROOT/DEBIAN/prerm"
install -m0755 "$REPO_ROOT/packaging/deb/postrm"   "$ROOT/DEBIAN/postrm"

( cd "$ROOT" && find . -type f ! -path './DEBIAN/*' -printf '%P\0' \
  | xargs -0 md5sum > DEBIAN/md5sums )

install -d "$OUTPUT_DIR"
DEB="${OUTPUT_DIR}/door-sync_${VERSION}_all.deb"
echo "==> packing $DEB"
dpkg-deb --root-owner-group --build "$ROOT" "$DEB" >/dev/null

echo "==> done"
dpkg-deb --info "$DEB" | sed 's/^/    /'
echo "$DEB"
