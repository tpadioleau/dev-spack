# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""``spack cherry-pick``: take individual package recipes from a given commit of a package repository.

Each cherry-pick copies one recipe (``package.py`` and the files next to it, such as patches)
from a git ref of a package repository (by default the official ``spack/spack-packages``) into
a repository stored inside the active environment. Recipes are grouped by the namespace of the
repository they come from: ``cherries_builtin`` for ``builtin``. Those repositories are
registered in the environment's ``spack.yaml`` ahead of every other repo, so cherry-picked
recipes take precedence while all other packages still come from ``builtin``.
"""

import datetime
import hashlib
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import spack.config
import spack.paths
import spack.repo
import spack.util.git
import spack.util.naming as nm
import spack.util.spack_yaml as syaml
import spack.version as vn
from spack.error import SpackError

try:  # Spack >= 1.2
    from spack.active_environment import active_environment
except ImportError:  # Spack 1.0 / 1.1
    from spack.environment import active_environment

try:  # Spack >= 1.2
    from spack.util import tty
except ImportError:  # Spack 1.0 / 1.1
    try:
        import spack.llnl.util.tty as tty
    except ImportError:
        import llnl.util.tty as tty

description = "use individual package recipes from a specific commit of a package repository"
section = "environments"
level = "long"

#: Default remote, the official Spack package repository
DEFAULT_GIT = "https://github.com/spack/spack-packages.git"
#: Directory, relative to the environment, holding the pin repositories
PINS_DIR = "cherries"
#: Metadata file written in each pin repository
META_FILE = "pins.yaml"


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------
def setup_parser(subparser):
    sp = subparser.add_subparsers(metavar="SUBCOMMAND", dest="cherry_command")

    add = sp.add_parser("add", help="cherry-pick package recipes from a git commit (or branch/tag)")
    add.add_argument("packages", nargs="*", metavar="PACKAGE", help="packages to cherry-pick")
    add.add_argument("-c", "--commit", help="git commit, branch or tag to take the recipes from")
    add.add_argument(
        "-g", "--git", default=None, help=f"git URL of the package repository (default: {DEFAULT_GIT})"
    )
    add.add_argument(
        "-p",
        "--path",
        default=None,
        help="path of the package repository inside the git repo, e.g. repos/spack_repo/builtin "
        "(default: auto-detected)",
    )
    add.add_argument(
        "-f", "--file", default=None, help="YAML file listing the cherry-picks (instead of PACKAGE)"
    )
    add.add_argument(
        "--no-check", action="store_true", help="skip the dependency check after cherry-picking"
    )

    check = sp.add_parser(
        "check", help="check that the dependencies of cherry-picked recipes can be satisfied"
    )
    check.add_argument("packages", nargs="*", metavar="PACKAGE", help="default: all")

    sp.add_parser("list", help="list the cherry-picked recipes of the active environment")

    rm = sp.add_parser("remove", aliases=["rm"], help="remove cherry-picked recipes")
    rm.add_argument("packages", nargs="+", metavar="PACKAGE", help="packages to remove")


def cherry_pick(parser, args):
    env = active_environment()
    if env is None:
        tty.die(
            "`spack cherry-pick` needs an active environment: run `spack env activate <env>` first"
        )

    if args.cherry_command == "add":
        _cmd_add(env, args)
    elif args.cherry_command == "check":
        _cmd_check(env, args.packages)
    elif args.cherry_command == "list":
        _cmd_list(env)
    elif args.cherry_command in ("remove", "rm"):
        _cmd_remove(env, args.packages)
    else:
        parser.print_help()


def _cmd_add(env, args):
    if args.file:
        if args.packages or args.commit:
            tty.die("use either a YAML file (-f) or PACKAGE... --commit, not both")
        requests = read_pin_file(args.file)
    else:
        if not args.packages or not args.commit:
            tty.die("PACKAGE and --commit are required (or use -f FILE)")
        requests = [
            {
                "package": pkg,
                "commit": args.commit,
                "git": args.git,
                "path": args.path,
            }
            for pkg in args.packages
        ]

    for req in requests:
        add_pin(env, **req)

    if not args.no_check:
        _print_report(env, check_pins(env, [r["package"] for r in requests]))

    names = " ".join(r["package"] for r in requests)
    tty.msg(f"Check with: spack spec -N {names}")


def _cmd_check(env, packages):
    pinned = {pkg for _, pkg, _ in list_pins(env)}
    for pkg in packages:
        if pkg not in pinned:
            tty.die(f"{pkg} is not cherry-picked in this environment")
    report = check_pins(env, packages or sorted(pinned))
    if not report:
        tty.msg("All dependencies of the cherry-picked recipes can be satisfied")
    _print_report(env, report)


def _cmd_list(env):
    pins = list_pins(env)
    if not pins:
        tty.msg("No cherry-picked recipes in this environment")
        return
    rows = [("PACKAGE", "NAMESPACE", "COMMIT", "GIT")]
    for ns, pkg, info in pins:
        rows.append(
            (
                pkg,
                ns,
                _commit_label(info),
                str(info.get("git", "")),
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def _commit_label(info: Dict[str, Any]) -> str:
    """Short sha, followed by the ref the user asked for when it is a branch or tag."""
    sha, ref = str(info.get("commit", "")), str(info.get("ref", "") or "")
    if not ref or sha.startswith(ref):
        return sha[:12]
    return f"{sha[:12]} ({ref})"


def _cmd_remove(env, packages):
    for pkg in packages:
        remove_pin(env, pkg)


# ---------------------------------------------------------------------------------------------
# YAML input file
# ---------------------------------------------------------------------------------------------
_ALLOWED_KEYS = {"package", "commit", "git", "path"}


def read_pin_file(path: str) -> List[Dict[str, Optional[str]]]:
    """Read a cherry-pick file. Two equivalent layouts are accepted (``pins:`` also works)::

        cherries:                          cherries:
        - package: zlib                      zlib:
          commit: 1a2b3c4d                     commit: 1a2b3c4d
          git: https://...    # optional       git: https://...   # optional
          path: repos/...     # optional
    """
    with open(path, encoding="utf-8") as f:
        data = syaml.load(f) or {}
    key = next((k for k in ("cherries", "pins") if isinstance(data, dict) and k in data), None)
    if key is None:
        raise SpackError(f"{path}: expected a top-level 'cherries:' (or 'pins:') key")

    entries = data[key]
    if isinstance(entries, dict):
        entries = [{"package": k, **(v or {})} for k, v in entries.items()]
    if not isinstance(entries, list):
        raise SpackError(f"{path}: '{key}' must be a list or a mapping")

    result = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise SpackError(f"{path}: entry #{i + 1} must be a mapping")
        unknown = set(e) - _ALLOWED_KEYS
        if unknown:
            raise SpackError(f"{path}: entry #{i + 1} has unknown keys: {', '.join(sorted(unknown))}")
        for key in ("package", "commit"):
            if not e.get(key):
                raise SpackError(f"{path}: entry #{i + 1} is missing '{key}'")
        result.append({k: (str(e[k]) if e.get(k) is not None else None) for k in _ALLOWED_KEYS})
    return result


# ---------------------------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------------------------
class GitError(SpackError):
    pass


class _Remote:
    """A blob-less local cache of a remote package repository."""

    def __init__(self, url: str):
        self.url = url
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        self.path = os.path.join(spack.paths.user_cache_path, "cherry_git", key)
        self._git = list(spack.util.git.git(required=True).exe)
        if not os.path.isdir(os.path.join(self.path, ".git")):
            os.makedirs(self.path, exist_ok=True)
            self.git("init", "-q")
            self.git("remote", "add", "origin", url)

    def _run(self, *args) -> bytes:
        env = dict(os.environ)
        for var in ("GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_DIR", "GIT_WORK_TREE"):
            env.pop(var, None)
        proc = subprocess.run(
            [*self._git, "-C", self.path, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        if proc.returncode != 0:
            raise GitError(proc.stderr.decode(errors="replace").strip())
        return proc.stdout

    def git(self, *args) -> str:
        return self._run(*args).decode()

    def fetch(self, ref: str) -> str:
        """Fetch ``ref`` (commit, branch or tag) and return the full commit sha."""
        if re.fullmatch(r"[0-9a-fA-F]{4,40}", ref):
            try:  # already known locally: only make sure its tree is available
                sha = self.git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}").strip()
                if not self.exists(sha, ""):
                    self.git("fetch", "-q", "--depth=1", "--filter=blob:none", "origin", sha)
                return sha
            except GitError:
                pass
        try:
            self.git("fetch", "-q", "--depth=1", "--filter=blob:none", "origin", ref)
            return self.git("rev-parse", "FETCH_HEAD^{commit}").strip()
        except GitError:
            pass
        # Servers only accept full shas in a fetch, so resolve an abbreviated sha against the
        # commit history (no trees nor blobs, cached for later calls), then fetch the full sha.
        if not re.fullmatch(r"[0-9a-fA-F]{4,39}", ref):
            raise SpackError(f"cannot fetch '{ref}' from {self.url}")
        tty.msg(f"Resolving abbreviated commit {ref} (first time can take a while)...")
        try:
            shallow = self.git("rev-parse", "--is-shallow-repository").strip() == "true"
            self.git(
                "fetch", "-q", *(["--unshallow"] if shallow else []), "--filter=tree:0",
                "origin", "+refs/heads/*:refs/cherry-heads/*", "+refs/tags/*:refs/tags/*",
            )
            sha = self.git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}").strip()
            self.git("fetch", "-q", "--depth=1", "--filter=blob:none", "origin", sha)
        except GitError as e:
            raise SpackError(f"cannot find commit '{ref}' in {self.url}") from e
        return sha

    def ls(self, sha: str, path: str) -> List[str]:
        out = self.git("ls-tree", "-r", "--name-only", sha, "--", path or ".")
        return [line for line in out.splitlines() if line]

    def read(self, sha: str, path: str) -> bytes:
        # bytes, since patches and other resources may not be text
        return self._run("cat-file", "blob", f"{sha}:{path}")

    def exists(self, sha: str, path: str) -> bool:
        try:
            self.git("cat-file", "-e", f"{sha}:{path}" if path else f"{sha}^{{tree}}")
            return True
        except GitError:
            return False


def _parse_api(value: Any) -> Tuple[int, int]:
    m = re.fullmatch(r"v?(\d+)\.(\d+)", str(value or "v1.0"))
    if not m:
        raise SpackError(f"invalid package api version '{value}'")
    return int(m.group(1)), int(m.group(2))


def _locate_package(remote: _Remote, sha: str, pkg: str, path: Optional[str]):
    """Find ``pkg`` in the git tree. Returns (pkg_dir_in_git, package_api, namespace)."""
    if path is not None:
        candidates = [path.strip("/")]
    elif remote.exists(sha, "spack-repo-index.yaml"):
        index = syaml.load(remote.read(sha, "spack-repo-index.yaml").decode())
        candidates = [p.strip("/") for p in (index.get("repo_index", {}).get("paths") or [])]
    else:
        # no index: any repo.yaml at the root or a few levels below (e.g. spack_repo/<ns>/)
        found = [
            os.path.dirname(f)
            for f in remote.ls(sha, "")
            if os.path.basename(f) == "repo.yaml" and f.count("/") <= 3
        ]
        candidates = sorted(found, key=lambda d: (d.count("/"), d)) or [""]

    for repo_path in candidates:
        repo_yaml = f"{repo_path}/repo.yaml".lstrip("/")
        if not remote.exists(sha, repo_yaml):
            continue
        conf = (syaml.load(remote.read(sha, repo_yaml).decode()) or {}).get("repo", {})
        api = _parse_api(conf.get("api"))
        subdir = conf.get("subdirectory", "packages")
        pkg_dir = "/".join(
            p for p in (repo_path, subdir, nm.pkg_name_to_pkg_dir(pkg, api)) if p
        )
        if remote.exists(sha, f"{pkg_dir}/package.py"):
            namespace = str(conf.get("namespace") or os.path.basename(repo_path) or "builtin")
            return pkg_dir, api, namespace

    where = ", ".join(c or "<root>" for c in candidates)
    raise SpackError(
        f"package '{pkg}' not found in {remote.url} at {sha[:12]} (searched: {where}). "
        "Use --path to give the repository location inside the git repo."
    )


# ---------------------------------------------------------------------------------------------
# Pin repositories in the environment
# ---------------------------------------------------------------------------------------------
#: Recipes are grouped by the repository they come from: recipes from ``builtin`` go to the
#: ``cherries_builtin`` namespace, from ``myorg`` to ``cherries_myorg``.
NAMESPACE_PREFIX = "cherries_"


def _namespace_for(env, source: str, api: Tuple[int, int]) -> str:
    name = NAMESPACE_PREFIX + re.sub(r"[^a-z0-9_]", "_", source.lower())
    # A repository follows a single package API major version, the one declared by the source
    # repository. Only if recipes of the same source namespace come with two API majors (e.g.
    # builtin before and after Spack 1.0), the second one gets a separate repository.
    repo_dir = _repo_dir(env, name)
    if os.path.exists(os.path.join(repo_dir, "repo.yaml")):
        if _parse_api(_read_repo_conf(repo_dir).get("api"))[0] != api[0]:
            return f"{name}_v{api[0]}"
    return name


def _read_repo_conf(repo_dir: str) -> Dict[str, Any]:
    with open(os.path.join(repo_dir, "repo.yaml"), encoding="utf-8") as f:
        return dict((syaml.load(f) or {}).get("repo", {}))


def _ensure_repo(env, source: str, api: Tuple[int, int]) -> Tuple[str, str, Dict[str, Any]]:
    """Create, if needed, the repository receiving recipes of the ``source`` namespace and make
    sure its declared API version is at least ``api``. Returns (namespace, repo_dir, repo.yaml
    content)."""
    namespace = _namespace_for(env, source, api)
    _check_namespace_free(env, namespace)
    repo_dir = _repo_dir(env, namespace, api)
    if not os.path.exists(os.path.join(repo_dir, "repo.yaml")):
        root = _pins_root(env) if api[0] >= 2 else repo_dir
        spack.repo.create_repo(root, namespace, package_api=api)
        _write_meta(repo_dir, {})
    conf = _read_repo_conf(repo_dir)
    declared = _parse_api(conf.get("api"))
    if declared[0] != api[0]:
        raise SpackError(
            f"repository '{namespace}' declares package API v{declared[0]}.{declared[1]}, "
            f"incompatible with v{api[0]}.{api[1]}"
        )
    if declared < api:
        # the declared API is the one of the source repository; with recipes taken from
        # several commits, the highest one, since minor versions are backward compatible
        conf["api"] = f"v{api[0]}.{api[1]}"
        with open(os.path.join(repo_dir, "repo.yaml"), "w", encoding="utf-8") as f:
            syaml.dump({"repo": conf}, stream=f)
    return namespace, repo_dir, conf


def _pkg_path(repo_dir: str, conf: Dict[str, Any], package: str) -> str:
    api = _parse_api(conf.get("api"))
    return os.path.join(
        repo_dir, conf.get("subdirectory", "packages"), nm.pkg_name_to_pkg_dir(package, api)
    )


def _remove_repo_dir(env, repo_dir: str) -> None:
    shutil.rmtree(repo_dir)
    # drop now-empty parent directories, up to the environment
    parent = os.path.dirname(repo_dir)
    while parent.startswith(_pins_root(env)) and not os.listdir(parent):
        os.rmdir(parent)
        parent = os.path.dirname(parent)


def _pins_root(env) -> str:
    return os.path.join(env.path, PINS_DIR)


def _repo_dir(env, namespace: str, api: Optional[Tuple[int, int]] = None) -> str:
    v2 = os.path.join(_pins_root(env), "spack_repo", *namespace.split("."))
    v1 = os.path.join(_pins_root(env), "v1", namespace)
    if api is None:  # existing repository: find where it is
        return v1 if os.path.exists(os.path.join(v1, "repo.yaml")) else v2
    return v2 if api[0] >= 2 else v1


def _config_entry(env, namespace: str) -> str:
    # Relative paths in spack.yaml are resolved against the manifest directory, which keeps
    # the environment relocatable.
    return os.path.relpath(_repo_dir(env, namespace), env.path).replace(os.sep, "/")


def _read_meta(repo_dir: str) -> Dict[str, Any]:
    path = os.path.join(repo_dir, META_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return dict((syaml.load(f) or {}).get("pins") or {})


def _write_meta(repo_dir: str, pins: Dict[str, Any]) -> None:
    with open(os.path.join(repo_dir, META_FILE), "w", encoding="utf-8") as f:
        f.write("# Generated by `spack cherry-pick`, do not edit by hand.\n")
        syaml.dump({"pins": pins}, stream=f)


def _env_repos(env) -> Dict[str, Any]:
    return dict(spack.config.CONFIG.get("repos", default={}, scope=env.scope_name) or {})


def _register(env, namespace: str) -> None:
    repos = _env_repos(env)
    entry = _config_entry(env, namespace)
    if repos.get(namespace) == entry:
        return
    repos.pop(namespace, None)
    # Prepend: highest priority within the environment, which is itself above other scopes
    spack.config.CONFIG.set(
        "repos", syaml.syaml_dict({namespace: entry, **repos}), scope=env.scope_name
    )
    tty.msg(f"Registered repository '{namespace}' in {env.manifest_path}")


def _unregister(env, namespace: str) -> None:
    repos = _env_repos(env)
    if repos.pop(namespace, None) is not None:
        spack.config.CONFIG.set("repos", syaml.syaml_dict(repos), scope=env.scope_name)
        tty.msg(f"Unregistered repository '{namespace}'")


def _check_namespace_free(env, namespace: str) -> None:
    """Refuse namespaces used by a repository that ``spack cherry-pick`` does not manage."""
    repo_dir = _repo_dir(env, namespace)
    if os.path.exists(os.path.join(repo_dir, META_FILE)):
        return
    for repo in spack.repo.PATH.repos:
        if repo.namespace == namespace:
            raise SpackError(
                f"namespace '{namespace}' is already used by {repo.root}"
            )
    if os.path.exists(repo_dir) and os.listdir(repo_dir):
        raise SpackError(f"{repo_dir} exists and was not created by `spack cherry-pick`")


def add_pin(
    env,
    package: str,
    commit: str,
    git: Optional[str] = None,
    path: Optional[str] = None,
) -> None:
    """Cherry-pick ``package`` from ``commit`` (a sha, branch or tag)."""
    git = git or DEFAULT_GIT
    remote = _Remote(git)
    tty.msg(f"Fetching {package} recipe from {git} at {commit}")
    sha = remote.fetch(commit)
    src_dir, api, source = _locate_package(remote, sha, package, path)

    # A package is cherry-picked once per environment: it changes repository when the new
    # recipe comes from another source namespace or follows another package API major version.
    namespace = _namespace_for(env, source, api)
    moved: Dict[str, Any] = {}
    for ns, pkg, info in list_pins(env):
        if pkg == package and ns != namespace:
            tty.msg(f"Moving {package} from namespace '{ns}' to '{namespace}'")
            moved = info
            remove_pin(env, package, quiet=True)

    namespace, repo_dir, conf = _ensure_repo(env, source, api)
    meta = _read_meta(repo_dir)
    old = meta.get(package) or moved

    dest = _pkg_path(repo_dir, conf, package)
    shutil.rmtree(dest, ignore_errors=True)
    for f in remote.ls(sha, src_dir):
        target = os.path.join(dest, os.path.relpath(f, src_dir))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as out:
            out.write(remote.read(sha, f))

    meta[package] = {
        "git": git,
        "ref": commit,
        "commit": sha,
        "path": os.path.dirname(os.path.dirname(src_dir)) if path is None else path,
        "source_namespace": source,
        "pinned": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _write_meta(repo_dir, meta)
    _register(env, namespace)

    label = _commit_label(meta[package])
    if old and old.get("commit") == sha:
        tty.msg(f"{package} already cherry-picked at {label}, recipe refreshed")
    elif old:
        tty.msg(f"{package}: {str(old.get('commit'))[:12]} -> {label}")
    else:
        tty.msg(f"Cherry-picked {package} at {label} into namespace '{namespace}'")


def list_pins(env) -> List[Tuple[str, str, Dict[str, Any]]]:
    result = []
    for ns in _env_repos(env):
        repo_dir = _repo_dir(env, ns)
        for pkg, info in sorted(_read_meta(repo_dir).items()):
            result.append((ns, pkg, dict(info)))
    return result


def remove_pin(env, package: str, quiet: bool = False) -> None:
    """Remove the cherry-picked recipe of ``package``."""
    for ns, pkg, _ in list_pins(env):
        if pkg != package:
            continue
        repo_dir = _repo_dir(env, ns)
        shutil.rmtree(_pkg_path(repo_dir, _read_repo_conf(repo_dir), package), ignore_errors=True)
        meta = _read_meta(repo_dir)
        meta.pop(package, None)
        if meta:
            _write_meta(repo_dir, meta)
        else:
            _unregister(env, ns)
            _remove_repo_dir(env, repo_dir)
        if not quiet:
            tty.msg(f"Removed cherry-picked {package}")
        return
    raise SpackError(f"{package} is not cherry-picked in this environment")


# ---------------------------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------------------------
class Issue(NamedTuple):
    package: str  #: cherry-picked package having the issue
    dep: str  #: dependency that cannot be satisfied (== package for load errors)
    kind: str  #: "load", "missing", "version" or "variant"
    constraint: str  #: e.g. "py-cython@3.2.0:"
    when: str  #: condition of the depends_on, "" if unconditional
    detail: str


#: Variants that every package accepts, they are not declared with ``variant()``
_SPECIAL_VARIANTS = {"patches", "dev_path", "commit"}


def _env_repositories(env) -> List["spack.repo.Repo"]:
    """Repositories as the environment will see them, including recipes picked in this run."""
    # Spack caches the content of package directories for the whole process: drop the entries
    # of our repositories, whose content may have changed since.
    cache = getattr(getattr(spack.repo, "FastPackageChecker", None), "_paths_cache", None)
    if isinstance(cache, dict):
        root = _pins_root(env) + os.sep
        for key in [k for k in cache if str(k).startswith(root)]:
            del cache[key]
    ours = []
    for ns in _env_repos(env):
        repo_dir = _repo_dir(env, ns)
        if os.path.exists(os.path.join(repo_dir, META_FILE)):
            ours.append(spack.repo.from_path(repo_dir))
    names = {r.namespace for r in ours}
    return ours + [r for r in spack.repo.PATH.repos if r.namespace not in names]


def _when_str(pkg: str, when) -> str:
    text = str(when).strip()
    if text.startswith(pkg):
        text = text[len(pkg):].strip()
    return text


def _dep_issues(path, pkg: str, name: str, spec, when) -> List[Issue]:
    when_s = _when_str(pkg, when)
    constraint = str(spec)
    if path.is_virtual(name):
        return []
    if not path.exists(name):
        return [Issue(pkg, name, "missing", constraint, when_s, "no recipe in any repository")]
    try:
        dep_cls = path.get_pkg_class(name)
    except Exception as e:  # noqa: B902 -- any error while importing a recipe
        return [Issue(pkg, name, "load", constraint, when_s, f"recipe fails to load: {e}")]

    issues = []
    versions = spec.versions
    if versions != vn.any_version:
        known = list(dep_cls.versions)
        if known and not any(v.intersects(versions) for v in known):
            newest = max(known)
            issues.append(
                Issue(pkg, name, "version", constraint, when_s,
                      f"no such version, newest is {newest} ({dep_cls.namespace})")
            )
    declared = set(dep_cls.variant_names())
    missing = sorted(v for v in spec.variants if v not in declared and v not in _SPECIAL_VARIANTS)
    if missing:
        issues.append(
            Issue(pkg, name, "variant", constraint, when_s,
                  f"unknown variant(s) {', '.join(missing)} ({dep_cls.namespace})")
        )
    return issues


def check_pins(env, packages: List[str]) -> Dict[str, List[Issue]]:
    """Check that the dependencies declared by cherry-picked recipes can be satisfied by the
    recipes the environment will use."""
    report: Dict[str, List[Issue]] = {}
    with spack.repo.use_repositories(*_env_repositories(env)) as path:
        for pkg in packages:
            try:
                pkg_cls = path.get_pkg_class(pkg)
            except Exception as e:  # noqa: B902
                report[pkg] = [Issue(pkg, pkg, "load", pkg, "", f"recipe fails to load: {e}")]
                continue
            seen = set()
            issues = []
            for when, deps in pkg_cls.dependencies.items():
                for name, dep in deps.items():
                    for issue in _dep_issues(path, pkg, name, dep.spec, when):
                        key = (issue.dep, issue.kind, issue.constraint, issue.when)
                        if key not in seen:
                            seen.add(key)
                            issues.append(issue)
            if issues:
                report[pkg] = issues
    return report


def _print_report(env, report: Dict[str, List[Issue]]) -> None:
    pins = {pkg: (ns, info) for ns, pkg, info in list_pins(env)}
    for pkg, issues in report.items():
        ns, info = pins.get(pkg, ("?", {}))
        sha = str(info.get("commit", ""))
        tty.warn(f"{pkg} (cherry-picked at {_commit_label(info)}): {len(issues)} dependency issue(s)")
        rows = [(i.constraint, f"when {i.when}" if i.when else "always", i.detail) for i in issues]
        widths = [max(len(r[k]) for r in rows) for k in range(2)]
        for r in rows:
            print(f"    {r[0].ljust(widths[0])}  {r[1].ljust(widths[1])}  {r[2]}")

        fixable, unfixable = [], []
        remote = _Remote(str(info.get("git") or DEFAULT_GIT)) if sha else None
        for dep in dict.fromkeys(i.dep for i in issues if i.dep != pkg):
            if dep in pins:
                unfixable.append(f"{dep} (already cherry-picked at {pins[dep][1].get('commit', '')[:12]})")
                continue
            try:
                _locate_package(remote, sha, dep, info.get("path"))
                fixable.append(dep)
            except SpackError:
                unfixable.append(f"{dep} (not in {sha[:12]} either)")
        if fixable:
            print(f"    -> available at {sha[:12]}: spack cherry-pick add {' '.join(fixable)} "
                  f"-c {sha[:12]}")
        for u in unfixable:
            print(f"    -> cannot fix automatically: {u}")
        if any(i.dep == pkg for i in issues):
            print("    -> the recipe itself does not load with the current repositories; it may "
                  "need a newer `builtin` (build systems), consider pinning `builtin` instead")
