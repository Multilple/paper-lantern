"""Fetch sqlglot at the pinned commit from GitHub's archive URL, check its content, unpack it to a dir.

Used by the Dockerfile at build time (python:3.13-slim has no curl or git, so this is plain Python).

    python fetch_sqlglot.py DEST            download, check, unpack into DEST
    python fetch_sqlglot.py DEST --from F   same, from a local tarball F (for testing)
    python fetch_sqlglot.py --hash DIR      print the content hash of an unpacked tree
    python fetch_sqlglot.py --hash-git DIR  print the content hash of the commit in git checkout DIR,
                                            counting only tracked files (git ls-tree of the commit = git ls-files)

The content hash covers every tracked regular file and symlink: sorted path, kind (f = file,
x = executable file, l = symlink) and sha256 of the bytes (of the link target for a symlink).
Directories are not hashed. The one gitlink at the pinned commit (sqlglot-integration-tests, a
submodule) is not a file: git ls-files lists it, GitHub's archive has it as an empty directory,
and neither side hashes it.
"""
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = "https://github.com/tobymao/sqlglot/archive/%s.tar.gz" % COMMIT
# Computed with `python fetch_sqlglot.py --hash-git /home/claude/br/repo` on the pinned checkout.
EXPECTED_FILES = 364
EXPECTED_HASH = "76d14c6ea131eab430feb9110a084a6d30904be28b9d1843e255b7ad687f00f1"


def _digest(entries):
    h = hashlib.sha256()
    for path, kind, sha in sorted(entries):
        h.update(("%s\0%s\0%s\n" % (path, kind, sha)).encode("utf-8", "surrogateescape"))
    return len(entries), h.hexdigest()


def tree_hash(root):
    entries = []
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for name in dirs + files:
            p = os.path.join(d, name)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                entries.append((rel, "l", hashlib.sha256(os.fsencode(os.readlink(p))).hexdigest()))
            elif stat.S_ISREG(st.st_mode):
                with open(p, "rb") as f:
                    sha = hashlib.sha256(f.read()).hexdigest()
                entries.append((rel, "x" if st.st_mode & 0o100 else "f", sha))
    return _digest(entries)


def git_hash(repo):
    # The tracked files of the pinned commit itself (same list as `git ls-files` on a clean checkout
    # of it; read from the commit so local edits or a dirty index cannot change the answer).
    out = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "-z", "--full-tree", COMMIT],
                         check=True, capture_output=True).stdout
    entries = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, _, obj = meta.split()
        kind = {b"100644": "f", b"100755": "x", b"120000": "l"}.get(mode)
        if kind is None:          # 160000 = submodule: no content in the commit
            continue
        blob = subprocess.run(["git", "-C", repo, "cat-file", "blob", obj.decode()],
                              check=True, capture_output=True).stdout
        entries.append((path.decode("utf-8", "surrogateescape"), kind, hashlib.sha256(blob).hexdigest()))
    return _digest(entries)


def download():
    last = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(URL, timeout=120) as r:
                return r.read()
        except Exception as e:  # network hiccup at build time: wait and try again
            last = e
            print("download failed (%r), retrying" % (e,), file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    raise SystemExit("could not download %s: %r" % (URL, last))


def unpack(data, dest):
    tmp = dest + ".unpack"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        tf.extractall(tmp, filter="data")
    tops = os.listdir(tmp)
    if len(tops) != 1:
        raise SystemExit("archive has %d top-level entries, expected 1" % len(tops))
    top = os.path.join(tmp, tops[0])
    n, h = tree_hash(top)
    if (n, h) != (EXPECTED_FILES, EXPECTED_HASH):
        raise SystemExit("content check failed: %d files, hash %s; expected %d files, hash %s"
                         % (n, h, EXPECTED_FILES, EXPECTED_HASH))
    if os.path.isdir(dest):
        os.rmdir(dest)            # must be empty
    os.rename(top, dest)
    os.rmdir(tmp)
    print("sqlglot %s: %d files, content hash %s OK" % (COMMIT, n, h), file=sys.stderr)


def main(argv):
    if argv[:1] == ["--hash"]:
        print(*tree_hash(argv[1]))
    elif argv[:1] == ["--hash-git"]:
        print(*git_hash(argv[1]))
    elif len(argv) == 3 and argv[1] == "--from":
        with open(argv[2], "rb") as f:
            unpack(f.read(), argv[0])
    elif len(argv) == 1:
        unpack(download(), argv[0])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
