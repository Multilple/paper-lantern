# for each sqlglot patch, find the tests it breaks without running the whole suite.
#
# Layout inside the image
#   /repo   sqlglot at the pinned commit, with tests/ (the code the patches apply to)
#   /app    our program (app/ in this repository); /app/maps holds the coverage and import maps
#   /tools  the offline map builders (tools/), kept so the maps can be rebuilt and checked; not run here
#
# The run is read-only, offline, user nobody, 2 cores. So everything Python will ever compile is
# compiled here, at build time, and every file is made readable by nobody.

# The exact image the organizers labeled the tests in (CPython 3.13.18), pinned by digest.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The environment the tests were labeled in, and the one our program expects:
# - SKIP_INTEGRATION=0 turns sqlglot's executor tests on (they were labeled that way).
# - No .pyc writes at run time (the disk is read-only), a fixed hash seed, unbuffered logs.
# - One thread per numeric library: the run has 2 cores and every CPU second is scored.
ENV SKIP_INTEGRATION=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    VECLIB_MAXIMUM_THREADS=1 \
    BR_ROOT=/repo \
    BR_DATA=/data \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# 1. Python packages the tests import, at the kit's locked versions. Wheels only, checked against
#    the sha256 in requirements.txt, no dependency resolution (the list is already complete).
COPY requirements.txt /build/requirements.txt
RUN python -m pip install --no-deps --require-hashes --only-binary=:all: -r /build/requirements.txt

# 2. sqlglot at the pinned commit, from GitHub's archive of that commit. The helper (plain Python:
#    this image has no curl or git) hashes every unpacked file and stops the build unless the tree is
#    exactly the commit's tracked files.
COPY build/fetch_sqlglot.py /build/fetch_sqlglot.py
RUN python /build/fetch_sqlglot.py /repo

# 3. Precompile the standard library, site-packages and sqlglot (src and tests) to .pyc.
#    unchecked-hash: Python loads the .pyc without even looking at the source's timestamp, which is
#    the fastest import there is, and the same way the maps' reference import time was measured.
#    The base image ships without .pyc files, so this covers the stdlib too.
#    A few files in those trees are not valid Python on purpose (test data, templates);
#    compileall reports them and they stay uncompiled, which is harmless, so failures are listed, not fatal.
RUN python -m compileall -q -f -j 4 --invalidation-mode unchecked-hash \
        /usr/local/lib/python3.13 /repo > /build/compile-base.log 2>&1 \
    || (echo "files compileall could not compile (expected, not used at run time):"; \
        grep -E "^\*\*\*|Error" /build/compile-base.log | head -50)

# 4. Our program and its maps (copied as whole directories), compiled the same way.
COPY app/ /app/
COPY tools/ /tools/
RUN python -m compileall -q -f --invalidation-mode unchecked-hash /app /tools

# 5. Everything readable (and directories enterable) by any user, writable by none but root.
#    The run uses user 65534 (nobody) on a read-only disk. Only files that need it are changed
#    (a blanket chmod -R would copy every file into a new layer); normally that is none.
RUN find /repo /app /tools /usr/local/lib/python3.13 \
        \( -type f ! -perm -444 \) -o \( -type d ! -perm -555 \) -o \( ! -type l -perm /022 \) \
        -print -exec chmod a+rX,go-w {} + \
    && rm -rf /build

# The tests' own subprocess (test_lazy_load runs `python -c "import sqlglot"`) imports sqlglot from
# the working directory, as when the kit labeled from the checkout.
WORKDIR /repo
USER 65534:65534
ENV HOME=/tmp

# Reads /data/commits/*.patch and /data/tests.json, prints one JSON line per commit on stdout.
# Logs go to stderr only.
CMD ["python", "/app/main.py"]
