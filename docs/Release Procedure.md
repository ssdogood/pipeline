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

   Expect all suites passing and 7 deselected (integration tests — they
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

## 4. Publish

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
certified by receivers. The receipt travels two formal routes: uploaded
as the fifth release asset, and committed to the `release-checksums/`
ledger in the tagged tree (step 6 below). `dist/` is git-ignored by
design: releases are artifacts, not source.

## 5. Receiver flow (what you tell whoever deploys)

The fabricated `README-FIRST.txt` inside every bundle is the authority;
in short: verify SHA256SUMS → build the conda env from the shipped
`environment.yml` → certify via the companion test kit in a throwaway
directory → expect GREEN (FERNET-ONLY) → delete the certification room
→ deploy a fresh unpack, pointing `DPL_HOME` at it.

## 6. Release Ceremony (tag, publish, verify)

Order matters. The checksums ledger is committed **before** the tag, so
the tagged tree carries its own receipt, and `release-checksums/` is
default-denied in the bundle.

**6.1 Ledger, commit, tag, push** (point of no return: after the push,
the tag is public and never moves):

```
mkdir -p release-checksums
cp dist/SHA256SUMS release-checksums/SHA256SUMS-v<version>
git add -A                       # working tree must contain ONLY intended changes
git status --short               # verify the stage list BEFORE committing
git commit -m "Release v<version>: <summary>"
git tag -a v<version> -m "Release <version>: <one-liner>"
git push origin main --follow-tags
```

Push output must echo the tag name. Use `--follow-tags` (annotated tags
reachable from pushed commits), never `--tags` (which pushes every tag
in the repo).

**6.2 Release notes — ALWAYS a file, never inline.** Long `--notes`
text on a command line invites shell trouble (v0.1.0 lesson: zsh
globbed `.[test]` inside the notes and refused the whole command).
Write notes with a quoted heredoc so brackets are taken literally:

```
cat > /tmp/release-notes-v<version>.md <<'EOF'
# dpl <version>

## In this release
...
EOF
```

Notes structure: In this release / Installing / Certifying /
Known issues / Roadmap. Describe what a user sees, not what you did —
"refactored build helpers" is a commit message, not a release note.

**6.3 Publish** (uploads the five dist/ files; prints the release URL):

```
gh release create v<version> dist/* \
    --title "dpl <version>" \
    --notes-file /tmp/release-notes-v<version>.md
```

**6.4 Receiver's-chair verification** — download as a receiver and
prove delivery. Run this OUTSIDE the repo, so `--repo` is required
(v0.1.0 lesson: `gh` locates the project via `.git`, which a scratch
directory lacks):

```
mkdir -p /tmp/relcheck && cd /tmp/relcheck
gh release download v<version> --repo ssdogood/pipeline --clobber
shasum -a 256 -c SHA256SUMS                                       # expect 4 OK
diff SHA256SUMS <repo>/release-checksums/SHA256SUMS-v<version>     # expect silence
tar -xzf dpl-<version>.tar.gz && head -8 dpl-<version>/environment.yml
rm -rf /tmp/relcheck
```

(The shasum check lists only the four archives — `SHA256SUMS` does not
list itself — hence four `OK` lines, not five.)

**6.5 Log the ceremony** — add a row to the Ceremony Log (below),
commit, push.

## 7. Failure cheat-sheet

| Symptom | Meaning |
|---|---|
| Clean room FULL-STRENGTH green | Binaries leaked into the bundle — do not ship; inspect `bin/` staging |
| Clean room RED | Archive damaged or doctrine violation — read the transcript tail in the FATAL |
| PREFLIGHT FAIL | `dpl` imported from outside the unpacked tree (editable-install shadowing) — investigate `PYTHONPATH`/site-packages |
| Determinism check differs | Tree changed between builds, or an unlisted nondeterministic source — re-run from a clean tree |
| shasum -c fails for a receiver | Corrupted transfer, or the published sums do not match the published archive |
| `zsh: no matches found` at publish | Glob characters (e.g. `[...]`) in notes text — use `--notes-file` (6.2) |
| `fatal: not a git repository` at download | Ran `gh` outside the repo without `--repo` (6.4) |

## 8. After the release

Confirm the tag and the pushed artifacts match the local SHA256SUMS
(done in 6.4). Then return to the working branch and continue
development — `pyproject.toml` on main now carries the released
version, which is correct: the working tree is always "the release that
will be", and the build script stamps whatever it reads.

**Once published, the tag never moves.** Any post-publication fix
becomes `v<next>`, never a retag.

## Ceremony Log

| Version | Date | Result | Notes |
|---|---|---|---|
| v0.1.0 | 2026-11-07 | GREEN (FERNET-ONLY), 136 passed / 25 skipped; 4 OK, diff silent | First release. Lessons folded into §6: zsh glob on inline notes → `--notes-file`; `gh` outside repo → `--repo`. |
| v0.1.1 | 2026-11-09 | GREEN (FERNET-ONLY), 136 passed / 25 skipped; 4 OK, diff silent | Cleanup release. pyrefly 47→0, ruff 36→0, mkstemp descriptor leak fixed, executable bits normalized, pyasn1>=0.6.5 floor (CVE-2026-119194). Publish hiccups fixed post-tag: stale v0.1.0 assets swept in by wildcard assets; §6.3 now mandates explicit asset names; hostname placeholder in notes eaten by renderer. |
| v0.2.0 | 2026-11-13 | GREEN (FERNET-ONLY), 326 passed / 25 skipped; 5 assets, diff silent | Feature release: email notification service (tiered events, audited sends, twin HTML bodies); UTC-Z timestamps with conformance verifier. |
