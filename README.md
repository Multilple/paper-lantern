# Finding the tests a sqlglot patch breaks

For every patch to sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, the program prints the
tests that fail on it, split over at most 8 workers.

Summary of the approach:

1. The verdicts come from running tests on the patched code inside the scoring run itself.
2. Running is kept cheap: sqlglot and every test module are imported once in a warm process, and each
   commit runs in a forked child of it, so a test costs its own CPU time and nothing more.
3. Maps built from runs of the pinned commit (line coverage, files opened, classes, attributes and table
   keys used, the import itself) only narrow the tests to run: they say which tests *can* be affected.
4. Nothing in this repository maps a patch, or a kind of edit, to failing tests.
5. A test that can be affected is run when a run is expected to cost less than selecting it unrun;
   otherwise it is selected without a verdict. A failing test is always selected.
6. Whenever the program cannot tell what a patch reaches, it selects every test for that commit.

## 1. What happens to one patch

**Planning, without importing sqlglot** (`plan.py`). `patch.py` parses the unified diff and applies it in
memory to the pinned files; anything it does not understand (binary, copies, context that does not
match) makes the plan "select everything". A file added, removed or renamed under `sqlglot/` or `tests/`,
or in a directory a test lists, also selects everything, and so does any commit not planned within 35% of
the run's time budget. For each changed Python file, `codediff.py` compiles the old and the new source
and pairs their code objects. It reports, per code object, whether its own instructions differ (jump
targets and constants compared by value, line numbers ignored) and the old line numbers of the differing
instructions. Nested code objects pair by position and qualified name. When a class body or a module
gains or loses functions, the functions that are identical in both (line numbers aside) are paired by
sequence alignment (`codediff._align`); an old function without a partner counts as changed with all its
lines (with those of any identical twin), and a new one without a partner shows up in the import state.
Outcomes:

A patch that adds, removes or changes a file the test session itself starts from (any `conftest.py`,
`pytest.ini`, `tox.ini`, a `.pth` file, `sitecustomize.py`; `pyproject.toml` or `setup.cfg` when it no
longer parses or its pytest section differs) selects every test: nothing here models a changed session
start (`plan._startup_risk`).

- only function bodies changed: a "swap" commit. Candidates are the tests whose recorded line coverage
  contains one of the changed lines (`maps/lines`).
- a class body or module level changed, the new file does not compile ("syntax"), or the code objects
  cannot be paired at all ("incomparable"): an import-time commit, run with a fresh import.
- a changed function line also ran while sqlglot and the tests were imported (`maps/import.json`): an
  import-time commit too.
- a non-Python file changed: its readers (`maps/opened.json`) are the candidates, run with the patched
  content.

**Function-body edits in the warm process** (`engine.do_swaps`). The child of a swap commit finds every
live function object whose code is the pinned code of a changed function and assigns it the new code
object (docstring included). Closures must keep the same free variables. If the mapping from live
functions to compiled code is not one to one, the commit is sent to the fresh-import path instead.

**Import-time edits** (`engine.run_commit`, mode "fresh"). These children are forked from the parent
before sqlglot is imported (only third-party modules from `maps/preload.json` are loaded there). The
memory limit is set first, then `SourceFileLoader.get_code` is replaced so that changed files compile from
their patched text (once per file: a module that fails to import is not compiled again by each later
importer), then sqlglot and all test modules are imported. A test module that fails to import fails all
its tests. If `sqlglot` itself does not import, if 200 or more tests already fail because their files do
not import, or if more than 800 roots (below) changed, every test is selected at once: such a commit
costs about as much as every test whatever else is found, and looking further only costs CPU. Otherwise `statefp.py` takes a fingerprint of the import state: one digest for every
module global and every class attribute of `sqlglot.*` and `tests.*` (values walked structurally,
functions by a digest of their code, classes by name and bases). Function bodies the patch changed are
digested as their pinned bodies, so only other differences show. The differing keys ("roots") become
candidates, each map answering the roots it covers and passing the rest on:

- a table (a dict or set held by a class or module): the tests that asked for a changed key or used
  the table as a whole (`maps/keys`, see section 2);
- a changed `<expression>_sql` method of a generator class (`engine._dispatch_users`): in addition, the
  tests that asked the generator's dispatch table of that class, or of a class inheriting from it, for
  that expression class (the dispatch table is described with `maps/keys`);
- a method that a generator or parser class did not have before, added or renamed
  (`engine._new_methods`): the tests that read the method of that name further up the class's bases,
  through whatever class they read it, plus the tests the attribute recorders could not follow;
- a class attribute: the tests that read that attribute (`maps/attrs.bin`) when the map covers it;
  otherwise the tests that handle an object of that class (`maps/types.json`), plus the tests that read
  any attribute through the class, plus, if no test is recorded for the class, every test that runs a
  line of the file defining it;
- a module global: every test that runs a line of that module, and of every source file that mentions
  the name as a word;
- if an import-time change shows no root and no changed function line is covered, every test that runs a
  line of a changed file.

When only function bodies changed (the commit is here because a body could not be given to the live
function, or because it also runs at import), a root that is nothing but one of those changed functions
(for example its closure changed because `super()` was removed) is dropped: the tests that run it are
candidates through its lines already.

A commit with a syntax error or unpairable code for which nothing was found to fail is not trusted and
selects everything (`main.finish`).

**Fixture edits** (`engine.install_overlay`). The patched text is written under `/tmp`, and `open` (both
`builtins.open` and `io.open`) is replaced by a function that redirects that one absolute path. The test
`test_lazy_load`, which starts `python -c "import sqlglot"` from the working directory, gets a directory
of symlinks with the patched files as real files (`engine.make_farm`) as its working directory.

**The tests' process pool** (`core.InlinePool`). Tests that use `ProcessPoolExecutor` get an in-process
stand-in that runs each task at once. Arguments, results and exceptions still go through pickle at the
places a real pool pickles them, and failures that would break a real pool (unpicklable result read back,
`SystemExit` in a task, a failing initializer) break this one the same way. A task that times out marks
the pool as stuck; later tests of a class that shares a pool (`maps/pools.json`) are then selected.

**Per-test session state** (`core.session_state`, `core.prepare_class`). `setUpClass` runs once per class
per child, as in one pytest process. Before each test the dialect registry is reset to the smallest one
the test can meet in a real session: the dialects pytest's collection loads (`maps/session.json`) for test
files before `tests/test_dialect_entry_points.py`, and an empty registry from that file on (it empties
the registry itself). A failing subtest ends the test at once.

**Output.** The parent collects each child's verdicts over a pipe and packs the selected tests into
workers (`pack.py`). Commits run one at a time: first the swap commits from the warm process, then the
import-time commits (one pinned test alone needs about 1.7 GB and the run has 4 GB).

## 2. The maps (`app/maps`)

All maps come from runs of the pinned commit. Except for `cmp.json`, the runs use our own runner
(`core.run_test`), each test alone in a fork of a warm process, in the environment given in section 7.

| File | Contents | Built by | Used for |
|---|---|---|---|
| `index.json` | test ids (the bit order of every bitmap), CPU seconds of each test alone, median `setUpClass` CPU per class, the line-map file of each source file, tests that could not be traced | `trace.py` (coverage run, then an untraced timing run), `build_maps.py` | costs in the run-or-select rule and in time limits; untraced tests are always selected |
| `lines/<n>.bin` | per source file: line -> bitmap of tests that ran it (`sys.monitoring` LINE and PY_START events), and the union | `trace.py`, `build_maps.py` | candidates of changed function lines; "every test that runs a line of this file" fallbacks |
| `opened.json` | path, or `DIR:` path, -> tests that opened or listed it (audit hook on `open`, `os.listdir`, `os.scandir`, `glob.glob`) | `trace.py`, `build_maps.py` | readers of a changed data file; added or removed files in a listed directory select everything |
| `import.json` | lines run and functions called while sqlglot and all test modules are imported; files `import sqlglot` loads (`bare`); data files opened at import | `trace_import.py`, `build_maps.py --bare` | a changed function line that ran at import makes the commit import-time; `bare` decides whether `test_lazy_load` is a candidate |
| `keys/` | per table: tests that asked for each key, asked its length, asked whether it is empty, or used it as a whole | `trace_keys.py`, `build_keys.py` | narrowing table roots (`keymap.narrow`) |
| `types.json` | class -> tests that passed an object of it, of a subclass, or the class itself as one of the first three arguments of any Python call | `trace_types.py`, `build_maps.py --types` | class-attribute roots that `attrs.bin` does not cover |
| `attrs.bin` | which tests read which class attribute, through which lookup class; reads made while test modules were imported; tests the recorders could not follow | `trace_attrs.py`, `build_attrs.py` | narrowing class-attribute roots (`attrmap.narrow`); `attrmap.class_readers` for the broad rule |
| `cmp.json` | per comparison site in `sqlglot/`: the kinds of operand types seen and, for plain values, what the six comparison operators said | `trace_cmp.py` (full pytest sessions) | the comparison-operator rule (section 5) |
| `cmp_tests.json` | per comparison site: which tests evaluated it on which kinds of operands (and what the import evaluated) | `trace_cmp_tests.py` (our runner, every test alone, two hash seeds) | narrowing the candidates of a comparison swap (section 5) |
| `units/*.bin`, `index.pkl` | for 4 giant tests: per unit and phase, lines run, CPU cost, fingerprints of what the phase hands on | `trace_units.py` | unit verdicts (section 4) |
| `pytest_only.json` | lines a few tests run only under pytest (it builds subtest descriptions with `repr`/`str` of sqlglot objects) | not in `tools/` (see section 8) | those tests are selected unrun when such a line changes |
| `preload.json` | names of non-sqlglot modules the tests import | not in `tools/` | imported once in the parent before any fork |
| `session.json` | dialect registry keys after pytest's collection | not in `tools/` | `core.session_state` |
| `pools.json` | test classes whose tests share one process pool (`TestOptimizer`) | written by hand | hung pools (section 6) |

**`attrs.bin`** (`attrmap.narrow`): every entry of every sqlglot class `__dict__` is replaced during
tracing by a recording descriptor that returns the original value. Reads made while the generator fills
its dispatch table (`_build_dispatch` reads every `*_sql` method of the class) are left out: they are
not calls of those methods, and which dispatch entries a test asks for is in `maps/keys`. So for
methods the map holds calls by name only. A changed root `C.attr` is answered by
this map only when `C` is a uniquely named sqlglot class that was wrapped, the attribute had a recorder
(or `C` inherits it from a recorded plain value), no test module read it while being imported, and the
patched value is not a data descriptor unless the pinned one was a property. Tests the recorders could
not follow are added to every narrowed answer. Any other root goes the broad way.

**`keys/`** (`keymap.py`, `tools/trace_keys.py`, `tools/build_keys.py`). sqlglot is table driven
(`TRANSFORMS`, `KEYWORDS`, `FUNCTIONS`, `TYPE_MAPPING`, ...), and nearly every test reads each table, so
"the tests that read the attribute" is nearly all of them. This map answers "the tests that could see a
changed entry".

- *Which tables.* Every plain `dict`, `set` or `frozenset` that a uniquely named sqlglot class or a
  sqlglot module holds under a non-dunder name, and that nothing else refers to (checked by reference
  count). An object held under several names (aliases) is one table. Its keys (or elements) must have
  names that are the same in every process and distinct within the table (`keymap.keyname`: strings,
  numbers, None, bytes, plain classes, Enum members, tuples of these). The shipped map has 3330 tables
  under 3424 names; 2 were refused for outside references.
- *How it records a dict.* Each dict is replaced in all its holders by `RecDict`, a dict subclass whose own
  storage is empty: the entries live in a separate dict and every method reads from there. Per test it
  notes the keys asked for through `[]`, `get` or `in` (found or not), whether `len` was asked, whether
  its truth value was asked, and any other use as "whole table": iteration, `keys`/`values`/`items`,
  copies, `==`, `|`, `repr`, pickling, any write, and a key without a name. Because the storage is
  empty, C code that reads a dict's storage directly instead of calling these methods would get nothing
  and make the test fail in the trace. The builder refuses to build if any test fails or dies under the
  trace; none did.
- *Reads at import.* A table read while a test module is imported has left its effect in that module, so
  it counts as used whole by every test of the files that are, or import, the reading module (the
  `import` statements of the test sources, followed from module to module, so plain values taken from
  another test module count too). A table read at that time by code that is not a test module's is not
  covered.
- *The generator's dispatch table.* `sqlglot/generator.py` copies `TRANSFORMS` into a per-class dict
  the first time a generator class is used (`_build_dispatch`, kept in `_DISPATCH_CACHE`) and looks
  expressions up in the copy. Without a hook every test would count as using `TRANSFORMS` whole (the
  copy) and lookups in the copy would not be noted. The trace replaces `_build_dispatch`: the copy is
  made unnoted, and the copy is itself wrapped so that its lookups are noted as lookups of the
  `TRANSFORMS` table it came from. Each entry of the copy depends only on the same key of `TRANSFORMS`
  (and on methods of the class), so a lookup in the copy sees a change of exactly that key.
- *How it records a set.* A set subclass is not enough. C code that is handed a set (a subclass
  included) reads its storage directly (`d.keys() - S`, `set(S)`, `a.issubset(S)`), so no method of the
  subclass is called. A first attempt with an empty-storage set subclass showed exactly that: one test
  failed on `kwargs.keys() - SUPPORTED_SETTINGS`. A set or frozenset is therefore replaced by a stand-in
  that is not a set at all (`RecSet` / `RecFrozen`, elements in a separate set). Handed this object, the
  same C code has to iterate it or call its methods, and every one of those is noted: `in` as a key
  lookup, `len` and truth value as for dicts, anything else (iteration, set operators and methods,
  comparisons, hashing, copies, pickling, writes) as whole-table use. `isinstance(x, set)` still answers
  True, through `__class__`. All 1620 tests pass under this trace (`tools/trace_keys.py` with
  `BR_KEY_SETS=1`).
- *At run time.* The warm process sends, for every covered table, one digest per key of the pinned value
  (`statefp.all_key_digests`). A fresh child computes the same for the patched table
  (`statefp.key_digests`, with changed function bodies digested as their pinned bodies). For a changed
  table root, `keymap.narrow` selects the whole-table users, the tests that asked for any key whose
  digest differs or that exists on one side only, the `len` askers if the size changed, and the truth
  value askers if emptiness changed. For a set only membership counts: an element added or removed.
  A root whose table is not covered, or whose patched value is no
  longer a table with named keys, goes on to the other maps.
- *Why it is sound.* Run a test on the patched and on the pinned import side by side. Until the test
  first reads a changed entry, the two runs are the same, so up to that point it makes the reads the
  trace recorded. The first read of a changed entry is therefore a recorded key, a recorded `len` or
  truth value whose answer changed, or a recorded whole-table use, and the test is selected. A test that
  never asked for a changed key and never used the table as a whole gets from the patched table exactly
  what it got from the pinned one. If the runs first diverge through some other change, that change's
  own root or changed lines select the test.
- *Two hash seeds.* The order in which a set is walked depends on the hash seed and can steer which keys
  a test asks for. The shipped map merges two traces, with `PYTHONHASHSEED` 0 and 1 (`build_keys.py`
  takes several traces: a test asked for a key if it did in any of them, and a table refused or read at
  import in any trace is so in the map). The per-test lookups were the same in both traces.

## 3. Run or select, and worker packing

Cost of a commit is the largest worker load plus 2 s per worker, plus 300 s if a failing test is missed,
and every CPU second of the run counts too. Selecting a test unrun costs its recorded seconds in the
packing; running it costs our CPU seconds for it, priced at a fixed factor of 1.4 (`main.CPU_PRICE`).

`policy.decide_next` is called before every test run. It models a commit as "nothing in the candidate set
fails" with probability `pi`, otherwise every candidate fails independently with one rate `q ~ Beta(a, b)`
(`engine.POLICY_MODEL`; separate parameters for sets of at most 2). These are commit-level counts, never
per-test or per-edit figures. At each step it prices plans of the form "run the L tests with the most
recorded seconds per CPU second, cheapest first, select the rest" for L on a grid up to all of them,
using `pack.bound` (the least any split can cost) for the selected part and the posterior of `q` after
each observed pass or failure. The cheapest test of the best plan runs if that plan beats stopping now;
otherwise the remaining candidates are selected unrun. A `setUpClass` cost is charged once per class.
Tests that fail without a run (their file does not import) count as observed failures.

`pack.pack` tries every worker count from 1 to 8, places tests longest first on the least loaded worker,
then moves and swaps tests out of the fullest worker while that lowers the maximum, and keeps the
cheapest count.

## 4. Giant tests decided unit by unit (`units.py`)

Four tests cost most of the suite's CPU: `TestExecutor.test_execute_tpch`, `test_execute_tpcds`,
`test_optimized_tpch` and `TestParser.test_max_nodes`. Each is a loop over independent units (one TPC
query, one statement group). `trace_units.py` records for every unit and phase the lines it runs, its CPU
cost, and a fingerprint of what the phase hands on (for example the parsed tree after pickle, the tree
handed to `Plan`, the result table, the DuckDB text). It traces each unit alone in a fresh fork, in order
in one process, and in reverse order; a fingerprint is kept only when all runs agree, and lines that run
only after other units ran are marked as order dependent.

On a patch, `units.decide` returns "pass", "fail" or "unknown":

- a unit that runs no changed line is left out: it does what it did on the pinned commit;
- other units run their sqlglot phases for real; when a phase hands on the pinned fingerprint, later
  phases that run no changed line (the Python executor, the DuckDB query) are cut;
- anything that differs runs with the test's own code, cheapest unit first; one failing unit fails the
  test; shared data the units read must keep its pinned fingerprint afterwards.

"Unknown" is returned whenever this reasoning is incomplete: a test file changed, a data file the test
reads changed in a way not followed, an import-time change, the map does not match the live class, the
hash seed differs from the one the map was built with, a DuckDB text differs (`test_optimized_tpch`), and
more. The engine uses units only when the estimate is below 0.9 of a full run. On "unknown" the test runs
whole, or, if it costs more than 3 s, is selected without a run.

### A rule that was measured and is switched off: `test_execute_tpcds` behind many small tests

Everything in this program selects a test unless it was seen to pass or a map says the patch cannot
reach it. One exception was built, measured, and left off (`engine.SENTINEL_MIN = 0`).

When changed lines are in code that every TPC-DS query runs (the Python executor, the planner), the unit
verdict has to execute all 57 queries again. On the scoring machine that is about 28 CPU seconds, more
than the 23.5 s that selecting the test costs, so the test is selected without a verdict. The rule would
instead run the other candidates when more than 10 cheap tests run the changed lines, and leave the
TPC-DS test out if all of them pass.

How often that would be wrong was measured: 632 single-token mutants of function bodies (swapped
comparison, `and`/`or`, dropped `not`, flipped boolean, integer plus one) on lines the TPC-DS test runs,
labeled with every candidate run to the first failure and the TPC-DS test by its full unit verdict. In
456 of them the TPC-DS test was a candidate of a function-body change. A cheaper test failed in 377. In
the 79 where every other candidate passed, the TPC-DS test passed in 78; with 10 or more other tests, in
all 54 where its verdict is the dear kind. That is not proof, a miss costs 300 s, and in 9 cases of 10
the rule only pays for the executor class's set-up before a small test fails. On the practice set it
ran once, a small test failed, and nothing was saved. So it stays off (`BR_SENTINEL_MIN=10` turns it on).

The same experiment for other dear tests (the big optimizer fixture tests) said no much more loudly:
when the cheap candidates all pass, a dear one still fails about 1 time in 4.

## 5. The comparison-operator rule (`cmpeq.py`, `maps/cmp.json`)

`trace_cmp.py` copies the pinned tree, rewrites every single-operator comparison `is`, `is not`, `==`,
`!=`, `<`, `<=`, `>`, `>=` in `sqlglot/` into a call that records the operand types and then performs the
same comparison, and runs the real pytest session. The shipped map merges three such sessions with
different hash seeds. For each site (line and column in the pinned source) it holds the pairs of kinds
seen:

- `N` None, `B` bool, `V` exactly int/float/complex/str/bytes/tuple/list/dict/set/frozenset;
- `O` a type whose `__eq__` and `__ne__` are `object`'s (`==` is identity);
- `E:<class>` a member of an Enum compared by its str or int value;
- `X` any other type with its own `__eq__`; `Z` such a value that, compared with `None` at that site,
  answered `==` False and `!=` True;
- for two plain values of one sort (numbers, or strings, or short tuples of them), a 6-bit record of what
  `== != < <= > >=` said about them.

At run time a changed `sqlglot/` file counts as unchanged when its syntax tree differs from the pinned
one only in single-operator comparisons, every such site was recorded, and for every pair seen there:

- for `is` <-> `==` or `is not` <-> `!=`: the pair is one where both operators agree for any values of
  those kinds (O with anything except X/Z; N with anything except X; two bools; two members of one Enum
  class; Z only with N);
- for one of `== != < <= > >=` swapped for another: every pair has a bit record and the two operators
  gave the same answer in all of them (so `<` for `<=` passes only where the two sides were never equal).

Swaps inside `__eq__`, `__ne__`, `__hash__`, `__bool__` and the ordering methods are refused, and so is a
file that tests read as data or that is opened at import (`plan.py`).

Why a test cannot change: run the patched and the pinned code side by side. Up to the first evaluation
of a swapped comparison that gives a different answer, both executions are the same, so the operands at
that evaluation are values the pinned run also met at that site, and their kinds (or, for ordering
swaps, their recorded answers) are in the map. For those, the two operators give the same answer, so
there is no first difference, and every test does what it did on the pinned commit. The built-in
comparisons used for these kinds have no side effects and cannot raise, so the swap does not add or
remove any other effect.

When the swap is the only change, the commit selects nothing and runs nothing. When the same commit has
other changes, the rule is still used if every swap is inside function bodies and nothing in the commit
needs a fresh import (`plan.make_plan`). The swapped functions are then patched in like any other
function edit, but bring no candidates of their own. The argument above still holds for every test the
other changes do not reach, since such a test does what it did on the pinned commit up to every
swapped comparison. The tests the other changes do reach are candidates anyway and run with the whole
patch, swaps included. A swap at class or module level is used only when it is the only change; next to
anything else, or next to a change that needs a fresh import, the commit is planned without the rule.

**Test by test** (`cmpeq.narrow`, `maps/cmp_tests.json`). When a swap is not harmless for every test,
the same argument is applied to each test on its own. `trace_cmp_tests.py` rewrites the comparisons the
same way and runs every test alone in a fork with our runner (two hash seeds, merged), noting what that
test evaluated at every site, and what the import itself evaluated. For a file that differs from the
pinned one only by swapped operators inside functions, a candidate from line coverage is kept only if
its own trace holds, at one of the swapped sites, operands for which the old and the new operator
differ (or could: any pair the rule above does not accept). The other candidates ran the line but
either never evaluated the comparison or only on operands where both operators agree, so they do what
they did on the pinned commit. Nothing is left out when a swapped site is not in the trace at all,
when the import evaluated it on telling operands, or for a test missing from a trace. The per-test
trace reaches exactly the 1888 sites of the three pytest sessions.

Checked on 186 comparison-swap mutants of our own: the rule narrowed 23 of them (candidates from 7919
to 5546 over the set). For those 23 every candidate was then run without the rule: every failing test
was inside the narrowed set (for example 3 failing of 3 kept out of 52, 14 of 15 kept out of 66, 47 of
48 kept out of 51).

What it does not cover: comparisons no test ran; chained comparisons; any type with its own `__eq__`
except the Z-with-None case (and there the argument assumes that `__eq__` with `None` has no side
effect); `V` with `V` for `is`/`==`; plain values compared with a value of another sort; files under
`tests/`. The argument also assumes the scoring run executes like the traced pytest sessions, which ran
with rewritten comparison code and other hash seeds than the run's; code whose path depends on hash
order, time or addresses is covered only as far as the three traced sessions met it.

## 6. Safety nets

- **Per test**: a CPU timer of `max(2.5, min(15c, 2c + 4)) x 1.4` s, where `c` is the test's recorded
  CPU plus its class set-up, and never more than its usual time again over its usual time (4 x 1.4 s for
  a short test); a wall timer of 4 times that plus 2 s. A test that swallows the timeout is interrupted
  again every 0.03 CPU s. A timed-out test is selected, and the commit stops there: the patch makes code
  hang, every further hang would cost seconds of CPU, so the remaining candidates are selected unrun.
  A limit that is too tight can only select a test that would have passed, never leave one out.
- **Per commit**: tests stop once the commit has used 12 x 1.4 s of CPU beyond the recorded cost of what
  it ran; the rest are selected unrun. A fresh import has its own 2.5 x 1.4 s CPU limit (the pinned
  import takes about 0.3 s).
- **Whole run**: after 4.6 s per commit of wall time, remaining commits select their candidates (swap)
  or everything (import-time) without running; no test runs past 5.2 s per commit minus 1 s.
- **Memory**: `RLIMIT_DATA` of 3200 MB is set in every child before any patched code runs (a fresh
  import included), so one huge allocation becomes a `MemoryError`. A profiling timer reads the resident
  size every 0.2 CPU s and stops a test above 1300 MB (2900 MB for `test_max_nodes`). A stopped test
  counts as failing.
- **Isolation**: the warm process and every commit child close every inherited descriptor except their
  own report pipe, so patched code cannot write to the real output or to another process's pipe.
- **Watchdog**: the parent waits on the child's pipe with a deadline (20 s, or 30 s for a fresh import,
  until the first test starts; then 3 times the running test's limit plus 10 s), never later than 2 s
  past the run's hard deadline. Report lines are parsed defensively: a line that is not one of ours, or
  names a test number out of range, is ignored, and megabytes without a line end end the child. If a
  child dies or hangs, it is killed with its process group; the test it was running is selected, and so
  are all candidates without a verdict. If the candidates were not known yet, everything is selected.
- **Main process**: if anything fails in the main process, it still prints one line per commit: the
  answers decided so far and every test for the others (`main._emit`), or every test for every commit
  if even that fails (`main._fallback`).
- **Unknown means everything**: a patch that cannot be parsed or applied, a planning error or timeout,
  a lost warm process (its commits select their candidates), no pinned fingerprint, a package that does
  not import, an unrecognisable import state, or an untrusted empty answer selects every test of that
  commit.

## 7. Rebuilding the maps and running

The maps are committed as plain files. To rebuild them on the pinned checkout (run from it, with
`BR_ROOT` pointing at it, `SKIP_INTEGRATION=0 PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1`, and the kit's
locked packages):

```
python tools/trace.py tests.json trace0.jsonl 0 2           # and part 1 of 2: coverage, files opened
python tools/trace.py tests.json time0.jsonl 0 2 notrace    # and part 1: untraced CPU costs
python tools/trace_import.py tests.json import.json
python tools/trace_types.py tests.json types0.jsonl 0 2     # and part 1
python tools/build_maps.py tests.json import.json app/maps trace0.jsonl trace1.jsonl \
    --types types0.jsonl types1.jsonl --timing time0.jsonl time1.jsonl --bare bare.json
python tools/trace_attrs.py tests.json attrs0.jsonl 0 2     # and part 1
python tools/build_attrs.py tests.json app/maps/attrs.bin attrs0.jsonl attrs1.jsonl
BR_KEY_SETS=1 python tools/trace_keys.py tests.json keys0.jsonl 0 2   # and part 1; again with PYTHONHASHSEED=1
python tools/build_keys.py tests.json app/maps/keys keys*.jsonl     # merges the traces; empties app/maps/keys first
BR_SEED=<n> python tools/trace_cmp.py REPO /tmp/cmptree cmp_<n>.json        # for three seeds, then merge
python tools/trace_units.py raw /tmp/units
python tools/trace_units.py build /tmp/units app/maps
```

`tests.json` is the kit's test list. `build_keys.py` reads the test order from `index.json` next to its
output directory, so it runs after `build_maps.py`. The `Dockerfile` installs the kit's locked wheels by
hash, fetches sqlglot at the pinned commit and checks every file against it, precompiles everything, and
runs `python /app/main.py` as user nobody. It reads `/data/commits/*.patch` and `/data/tests.json` and
prints one JSON line per commit. Without Docker, from the pinned checkout:

```
BR_ROOT=$PWD BR_DATA=<dir with commits/ and tests.json> SKIP_INTEGRATION=0 \
PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 BR_LOG=1 python /path/to/app/main.py > out.jsonl
```

`BR_LOG=1` writes a log to stderr; `BR_DUMP=<dir>` writes candidates and verdicts per commit;
`BR_NOKEYS=1` turns the table-key map off.

## 8. Known limits

- Coverage, types, attribute and key maps come from one traced run of each test with our runner. A test
  that reaches changed code only through a path it did not take on the pinned commit (for example a
  branch decided by a changed constant elsewhere) is found only if some other map names it.
- The key map relies on every read of a table going through the recording methods. A C-level read of the
  empty storage that did not make any test fail in the trace would go unnoticed.
- The key map follows reads at import time from one test module to another through their `import`
  statements; a test module reached only through a dynamic import is not followed.
- In the key trace a set is replaced by an object that is not a set. Code that checks `type(x) is set`,
  or C code that requires a real set, takes another path there than in a real run; all tests still
  passed under the trace, but such a path would be recorded as it ran in the trace.
- `engine._new_methods` assumes that nothing looks a method up by a name no class had before (a
  `getattr` with a default, `hasattr`, a scan of `dir()`) other than the generator's dispatch filler,
  which `_dispatch_users` covers. In the pinned sqlglot the only other name-built lookup
  (`getattr(self, f"{node.key}_sql")` in the generator) has no default.
- The global-name rule for module globals is textual and broad; it can select far more than needed.
- The runner is not pytest. Behaviour that only pytest produces is handled by `pytest_only.json`
  (selected unrun) and by the registry reset; anything else pytest-specific is not modelled.
- The in-process pool runs tasks in the test's own process: a task that changes global state would be
  isolated in a real worker and is not here.
- CPU is priced at a fixed 1.4 per recorded second. On the practice board a price of 2.5 scored worse
  (fewer small tests run, more selected), although one CPU second of test running there costs about
  2 recorded seconds: the pass rates in the model are rough.
- When the unit verdict of `test_execute_tpcds` costs more than selecting it (changes in code every
  TPC-DS query runs), the test is selected without a verdict even if it would pass: 23.5 s.
- The three comparison traces were merged by hand, outside `tools/`; `trace_cmp.py` writes one session
  per call and has no merge step.
- The builders of `pytest_only.json`, `preload.json`, `session.json` and the `bare.json` input of
  `build_maps.py` are not in `tools/`; `pools.json` is written by hand.
- Unit verdicts exist for 4 tests only; all others run whole or are selected.

How the rules were checked, beyond the sample patches:

- Comparison rule: 793 `is`/`==` swaps and 620 ordering swaps that the rule accepts, applied all at
  once to the pinned tree; the real test session passes.
- Table keys: 93 labeled single-entry edits of dicts and sets (labeled by real pytest and by an
  in-process run of every test, which agreed on every patch labeled by both): 0 misses.
- 32 patches of other kinds (deleted, added and renamed methods, changed defaults, base classes,
  decorators, slices, return values, fixtures, test helpers): 0 misses.
