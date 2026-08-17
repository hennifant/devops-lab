# 0006. Compiled requirements with uv

Date: 2026-08-17
Status: Accepted

## Context

`requirements.txt` declared four packages with no versions:

```
fastapi
uvicorn
psycopg[binary]
prometheus-fastapi-instrumentator
```

Twenty-five packages were actually installed. Twenty-one of them were transitive and
entirely unconstrained. Building the same commit on two different days could therefore
produce two different images, which contradicts the stated goal that a commit SHA
identifies what runs. The `:sha` image tag promised a reproducibility that did not exist.

## Decision

Split intent from lock:

```
requirements.in       →  requirements.txt
requirements-dev.in   →  requirements-dev.txt
```

The `.in` files hold direct dependencies, loosely. The `.txt` files are compiled, fully
pinned, and never edited by hand:

```bash
uv pip compile --universal requirements.in     -o requirements.txt
uv pip compile --universal requirements-dev.in -o requirements-dev.txt
```

CI installs with `uv pip install --system -r requirements-dev.txt`.

## Consequences

- A commit now pins all 25 packages, not 4.
- Upgrading is a deliberate act: `uv pip compile --upgrade`, review the diff, commit.
  Automated by Renovate — see [0008](0008-renovate-as-an-action.md).
- Two extra files, and a rule that `requirements.txt` is generated. Editing it by hand
  is now a mistake, and nothing enforces that beyond the header comment.
- Adding a dependency is a two-step operation: edit the `.in`, recompile. Forgetting the
  second step means the dependency is not installed anywhere.
- The Dockerfile still installs with `pip`. Nothing is resolved there — the file is fully
  pinned — so the installer choice only affects speed inside the image build.

## Background

**Why not `pip freeze`.** Freeze dumps whatever happens to be installed in the current
environment. It records no distinction between what you asked for and what got dragged
in, so six months later `h11` and `fastapi` look identical in the file. Upgrading then
means guessing which lines are safe to touch. The `.in`/`.txt` split keeps the question
answerable: the `.in` is the contract, the `.txt` is the solution to it.

**Why uv rather than pip.** pip cannot compile a lock file at all; the classic answer is
`pip-tools` (`pip-compile`). uv implements the same interface — `uv pip compile` is a
drop-in for `pip-compile` — and is already installed on this machine. It is a Rust
implementation with parallel downloads, a PubGrub resolver, and a global
content-addressed cache that hardlinks into environments instead of copying, which is why
it is typically 10–100× faster. For this project's 25 packages that is roughly 15 s
versus 1–2 s; the speed is a bonus, the lock-file capability is the reason.

**Why `--universal`.** Without it, uv resolves for the machine doing the compiling —
here aarch64, Linux, CPython 3.14. CI runs on amd64 and the image builds for two
architectures. A universal resolution emits environment markers instead of assuming one
platform:

```
colorama==0.4.6 ; sys_platform == 'win32'
psycopg-binary==3.3.4 ; implementation_name != 'pypy'
```

so a single file installs correctly everywhere.

**Hashes were deliberately left out.** `--generate-hashes` additionally protects against
a compromised index serving different content under an existing version. It also forces
every install to run in hash-checking mode, which means both files must be hashed and ad
hoc `pip install` stops working without extra flags. That belongs with the Phase 4 supply
chain work, next to image scanning, not here.

- [uv pip compile](https://docs.astral.sh/uv/pip/compile/)
- [pip-tools, the original of this workflow](https://pip-tools.readthedocs.io/)
