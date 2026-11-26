# spack-cherry-pick

A Spack extension that takes **individual package recipes from a specific commit** of a
package repository (by default the official `spack/spack-packages`). Every other package
keeps coming from `builtin`.

For each package, `spack cherry-pick add` does the following:

1. Fetches only the commit it needs into a small git cache (shallow and blob-less, under
   `~/.spack/cache/cherry_git/`).
2. Copies the package directory (`package.py`, patches and any other files) into a repository
   inside the environment, named after the namespace of the repository the recipe comes from:
   **`cherries_builtin`** for `spack-packages`, `cherries_myorg` for a repository whose
   namespace is `myorg`. That repository declares the package API version of its source
   repository.
3. Registers that repository in the environment's `spack.yaml`, ahead of `builtin`, using a
   relative path so the environment stays relocatable.
4. Records where each recipe came from (git URL, the ref you asked for, the full commit sha)
   in `pins.yaml` in that repository.
5. Checks that the dependencies the new recipe declares can be satisfied (see below).

It has been tested with Spack v1.0.0, v1.1.1 and develop (1.3.0.dev0).

## Install

```bash
spack config add "config:extensions:[/path/to/spack-cherry-pick]"
spack cherry-pick -h
```

## Usage

All commands act on the **active environment** (`spack env activate …` or
`spack -e <env> cherry-pick …`).

### From the command line

```bash
spack cherry-pick add cmake --commit 8ed34337            # full or abbreviated sha
spack cherry-pick add zlib mdspan --commit develop       # several packages; a branch or tag works too
spack cherry-pick add foo --commit 1a2b3c4 --git https://github.com/me/my-spack-packages.git
```

| option | default |
|---|---|
| `-c/--commit` | required: a commit sha (full or abbreviated), branch or tag |
| `-g/--git` | `https://github.com/spack/spack-packages.git` |
| `-p/--path` | auto-detected from `spack-repo-index.yaml`, otherwise `repo.yaml` at the root of the git repo |
| `--no-check` | skip the dependency check |

### From a YAML file

```bash
spack cherry-pick add -f cherries.yaml
```

```yaml
cherries:
- package: cmake
  commit: 8ed34337
- package: zlib
  commit: develop
  git: https://github.com/spack/spack-packages.git   # optional
  path: repos/spack_repo/builtin                      # optional
```

A mapping keyed by package name is also accepted, and the top-level key can be `pins:`
instead of `cherries:`:

```yaml
cherries:
  cmake: {commit: 8ed34337}
  zlib:  {commit: develop}
```

You can safely run the command again with the same file. A package already picked at the same
commit is just refreshed. A package listed with a new commit is moved to that commit.

### Dependency check

After cherry-picking, and whenever you run `spack cherry-pick check`, each `depends_on(...)` of
the cherry-picked recipes is compared with the recipes the environment will actually use. It
reports:

- **missing:** the dependency exists in no repository;
- **version:** no known version of the dependency satisfies the constraint;
- **variant:** the dependency does not declare a requested variant;
- **load:** the recipe itself fails to load, for example because it imports a build-system
  class that `builtin` does not have yet.

When the dependency exists at the same commit, the check prints the command to cherry-pick it.
Nothing is picked automatically.

```
==> Warning: py-scipy (cherry-picked at 731577eee000 (develop)): 2 dependency issue(s)
    py-cython@3.2.0:    when @1.18:  no such version, newest is 3.1.3 (builtin)
    py-pythran@0.18.1:  when @1.18:  no such version, newest is 0.16.1 (builtin)
    -> available at 731577eee000: spack cherry-pick add py-cython py-pythran -c 731577eee000
```

The check reports dependencies that no version can satisfy at all. It cannot predict every
concretization conflict, such as two constraints that are each satisfiable but not together.
`spack spec -N <pkg>` remains the final word.

### Inspect and remove

```bash
spack cherry-pick list
spack cherry-pick rm cmake zlib
spack spec -N cmake          # -N shows namespaces: cherries_builtin.cmake ^builtin.…
```

`list` shows the commit each recipe was taken from, followed by the branch or tag you asked
for when you gave one:

```
PACKAGE   NAMESPACE         COMMIT                  GIT
cmake     cherries_builtin  8ed34337e15d            https://github.com/spack/spack-packages.git
py-scipy  cherries_builtin  731577eee000 (develop)  https://github.com/spack/spack-packages.git
```

Removing the last recipe of a repository also deletes it and its `spack.yaml` entry.

## What it changes in the environment

```yaml
# spack.yaml
spack:
  repos:
    cherries_myorg: cherries/spack_repo/cherries_myorg
    cherries_builtin: cherries/spack_repo/cherries_builtin
```

```
<env>/cherries/spack_repo/cherries_builtin/
├── repo.yaml            # namespace cherries_builtin, API declared by the source repo
├── pins.yaml            # provenance: git, ref, commit, path, source namespace
└── packages/
    ├── cmake/           # package.py + patches, copied from the commit
    └── zlib/
```

### Package API versions

Each `cherries_<ns>` repository declares the package API version of the source repository
(`v2.2` for today's `spack-packages`). When recipes of the same source come from commits
declaring different minor versions, the repository declares the highest one, since minor
versions are backward compatible.

Recipes from an old-layout (API v1) repository are stored in a v1 repository under
`<env>/cherries/v1/`. Only if the same source namespace is used with both majors (for example
`builtin` before Spack 1.0 and today) does the second one get a `_v1` suffix:
`cherries_builtin_v1`. Spack loads v1 and v2 repositories side by side.

### Reproducibility

Commit `cherries/` along with `spack.yaml` to make the environment reproducible, or commit only
your `cherries.yaml` and run `spack cherry-pick add -f cherries.yaml` after cloning.

## Limitations

- Only the recipe directory is copied. Dependencies flagged by the check have to be
  cherry-picked explicitly. If the recipe needs newer build-system classes, pin all of
  `builtin` to that commit in `repos:` instead.
- A package can be cherry-picked only once per environment.
- Specs concretized with a cherry-picked recipe show the `cherries_<ns>` namespace, and the
  recipe content is part of the spec hash, as with any custom repo.
