# Release Procedure

How to cut and publish a release of the dpl data pipeline. Follow this
end to end; every step exists because skipping it has a name: shipping
something unverified.

Audience: the person building the release (builder-side). Receiver-side
instructions live in `README-FIRST.txt`, fabricated fresh into every
release bundle at build time.

---

## 1. Preconditions (all must be true before you build)

1. **You are on `main`** (or a branch cut from it) with a clean working
   tree: `git status --short` prints nothing.
2. **The full suite is green** at full strength on the build machine:

   ```
   pytest -v
   ```

   Expect all suites passing and 6 deselected (integration tests — they
   need live servers and are not part of the offline certification).
3. **The pinned SOPS/age binaries are present in `bin/`** so the
   builder-side run exercises the binary-dependent suites too. The
   release itself never ships them; presence here only makes YOUR local
   verification stronger.

## 2. Decide the version

The version stamped on every artifact is read from `pyproject.toml`
(single source of truth — the script refuses to build without it).

- Patch release (bug fixes): `0.1.0` → `0.1.1`
- Feature release (new capability): `0.1.0` → `0.2.0`

Edit `[project] version` in `pyproject.toml` and commit that bump alone,
before building. A release whose archive names disagree with its
pyproject is a packaging bug.

## 3. Build and self-certify

```
python scripts/build_release.py --dry-run
python scripts/build_release.py --build
```

**Inspect the dry run first.** The two SHIP inventories must contain
everything you intend to publish and nothing else — the bundle (code,
configs, docs, scripts, environment.yml, manifest-only bin/) and the
test kit (tests + green scripts). Anything unexpected goes on the
exclude list with a reason; anything *missing* is a stopped build.

**Then build.** One command does everything: stages both trees,
re-verifies doctrine on the staged copies, writes four archives plus
`SHA256SUMS`, then **clean-room certifies** — it unpacks the bundle
fresh, overlays the test kit, and runs the kit's own green script
against the unpacked code. The build may only be trusted if the report
ends:

```
clean : ... GREEN (FERNET-ONLY), N passed, M skipped
```

`N + M` must equal the total test count of that release. The
FERNET-ONLY label is required on purpose: the bundle deliberately
contains no SOPS/age executables, so a full-strength GREEN in the clean
room means binaries leaked into the bundle — that is a doctrine breach
and the build must be investigated, not shipped.

Any FATAL stops the release. Nothing in `dist/` from a failed build is
publishable.

**Determinism check:** run `--build` a second time and confirm all four
SHA256 hashes are identical to the first run. Byte-identical rebuilds
are your tamper-evidence anchor: anyone can rebuild the same tree later
and compare.

## 4. Publish `dist/`

The complete release is exactly five files in `dist/`:

| File | Audience |
|---|---|
| `dpl-<version>.tar.gz` | production tree (macOS/Linux) |
| `dpl-<version>.zip` | production tree (Windows) |
| `dpl-<version>-testkit.tar.gz` | certification overlay (macOS/Linux) |
| `dpl-<version>-testkit.zip` | certification overlay (Windows) |
| `SHA256SUMS` | integrity receipt for all four |

Publish them together, never separately — a kit without its bundle
certifies nothing, a bundle without a downloadable kit cannot be
certified by receivers. Publish the SHA256SUMS contents (e.g. in the
release announcement) so receivers can verify without trusting the
download channel. `dist/` is git-ignored by design: releases are
artifacts, not source.

Tag the release:

```
git tag -a v<version> -m "Release <version>"
git push origin main --tags
```

## 5. Receiver flow (what you tell whoever deploys)

The fabricated `README-FIRST.txt` inside every bundle is the authority;
in short: verify SHA256SUMS → build the conda env from the shipped
`environment.yml` → certify via the companion test kit in a throwaway
directory → expect GREEN (FERNET-ONLY) → delete the certification room
→ deploy a fresh unpack, pointing `DPL_HOME` at it.

## 6. Failure cheat-sheet

| Symptom | Meaning |
|---|---|
| Clean room FULL-STRENGTH green | Binaries leaked into the bundle — do not ship; inspect `bin/` staging |
| Clean room RED | Archive damaged or doctrine violation — read the transcript tail in the FATAL |
| PREFLIGHT FAIL | `dpl` imported from outside the unpacked tree (editable-install shadowing) — investigate `PYTHONPATH`/site-packages |
| Determinism check differs | Tree changed between builds, or an unlisted nondeterministic source — re-run from a clean tree |
| shasum -c fails for a receiver | Corrupted transfer, or the published sums do not match the published archive |

## 7. After the release

Confirm the tag and the pushed artifacts match the local SHA256SUMS.
Then return to the working branch and continue development — `pyproject.toml`
on main now carries the released version, which is correct: the working
tree is always "the release that will be", and the build script stamps
whatever it reads.
