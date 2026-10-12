# Pyarmor Static Unpack One-Shot Tool

This fork adds CPython 3.14 recovery and an optional modern source backend.
Native changes include borrowed local loads, small integer literals, fused
stores, function defaults, cache-aware jumps, await expressions, formatted
strings, and inline list/set/dict comprehensions. `--export-pyc` exports a
standard decrypted object tree; `--export-only` avoids native AST reconstruction.

For modern source recovery, build [EiNSTeiN-/pycdc-modern](https://github.com/EiNSTeiN-/pycdc-modern)
and run this tool using CPython 3.14:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cmake -S pycdc -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel 4
.venv/bin/python oneshot/shot.py /path/to/input \
  -r /path/to/pyarmor_runtime.so -e build/pyarmor-1shot \
  --modern-backend /path/to/pycdc-modern/target/release/pycdc \
  -o /path/to/output
```

The modern path removes identified runtime entry/exit plumbing, decodes mixed
constants, preserves user exception handlers, and validates emitted source
without importing it. Per-module `.recovery.json` reports distinguish syntax
validation from structural warnings. `complete: true` means no structural issue
was reported; it does **not** establish behavioral equivalence. Keep `.das`,
`.recovered.pyc`, and `.modern.raw.py` for comparison. Sequence files contain
runtime AES material and should remain private.
The pinned backend also preserves independent for-loop rejection guards, so a
return cannot escape the guard that assigned its value. Owned bytecode/behavior
checks include invalid rows, None and false values, getter order and identity
tests that must not invoke user truth conversion.
Padded try returns also keep their existing calculated value. The backend
requires a previously recognized padding/return tail and a single live
expression, without a with protocol. Native export, modern reconstruction and
whole-unit verification are tested together, including call/getter failures,
fallback paths and base exceptions. Comparison and normalization rules stay
unchanged.
Early-return exception-table fragments can also leave duplicated consecutive
`try` blocks. Source reconstruction reunites only adjacent suites with
identical handlers and no else/finally, then requires the complete original
code unit to match and every earlier unit match to survive. Independent tries,
intervening work and unproved candidates keep their existing code. Nested
suites are processed before enclosing concatenations, and definitions have
separate trial scopes. Owned sync, coroutine and property fixtures check
handled errors, cancellation, return/getter order and rejected scope changes.
The lookup repair requires an unprotected suite, the assigned local's later
None guard and a return of that same local. Other guard and handler layouts keep
their existing handling; broad guard inversion failed corpus-retention checks.
Negated comparison guards retain their original operator and invert the truth
jump instead; NaN and user rich comparisons must not be changed by folding
`not (value < limit)` into `value >= limit`.

The normalizer resolves closure cells by capture position, preserves genuine
exception cleanup directly before an exit marker, and reconstructs future
annotation expressions without extra quoting.

Numbered local function bindings are restored from their nested code object's
original name when the binding is unambiguous. This metadata change keeps the
instruction bytes and exception table intact; closure captures are resolved
by position afterward. It also supports CPython 3.14 decorator applications.
Parameters, public bindings, nonlocal cells, colliding names and ambiguous
definitions are retained. This fixes generated nested helper names without
relaxing comparison of their bodies or callers.

When a compiler's optimized builtin-generator path is replaced with its
ordinary callable path, the synthetic forward jump is removed only if all
intervening words have already been masked as unreachable padding. This
preserves an enclosing short-circuit guard without inventing an `else` boundary.
Instruction addresses stay fixed. Owned tests check captured generators,
shadowed callables, lazy call order, computed rejection returns and idempotency.

Removing a recognized runtime exit/rethrow layer preserves its route into
the compiler's StopIteration handler. The normalizer validates the complete
rethrow, cleanup, and outer-handler chain before reconnecting it. Owned
coroutine, generator and async-generator fixtures compare normal results,
errors and causes before and after normalization, including real user handlers.
Exception comparisons merge only adjacent ranges with identical dispatch;
a missing covered call or a changed depth/lasti still fails comparison.
The standard initial `RESUME 0` is restored at its known entry slot when the
native entry hook is removed. This makes ordinary frame lookup and
zero-argument `super()` work in the recovered method; an inheritance fixture
checks the constructor against its plain original code.
When a wide jump to an exit marker becomes an operandless return, its argument
prefixes are removed too. Instruction addresses remain stable. A fixture
executes the resulting return, and the corpus audit checks nested-unit recovery.

Source cleanup retains ordinary stores joined with a runtime marker initializer
in a parallel assignment. Only the paired marker literal is removed; remaining
RHS expressions still run before target stores. Owned tests check attribute
write ordering and a recovered constructor's complete bytecode tree.

A separate static verifier compares nested code units and keeps unsupported
marker patterns as failures:

```bash
.venv/bin/python oneshot/bytecode_fidelity.py reference.recovered.pyc candidate.py \
  --sequence reference.seq --report fidelity.json
```

It compares signatures, meaningful instructions, jump destinations, exception
tables and docstrings after a narrow documented normalization of compiler and
PyArmor bookkeeping. A zero exit status requires all matched code units to
pass. A nonzero exit status or an unsupported marker is not proof of equivalence.
Keyword parameter names, global/attribute names, constants and branch semantics
remain significant. The verifier never imports either candidate program.
Compiler-generated class `__firstlineno__` values are location metadata, just
like code-object line numbers. Only the recognized class header is normalized;
an explicit assignment in the class body remains significant and is covered
by a negative regression test.
A sole future-annotation class cell is compiler bookkeeping only when its
complete bytecode has no reads, captures or public namespace binding and only
the recognized empty-set initialization. Its three setup instructions and cell
count can then be omitted from comparison, and the source initializer is
removed using the original class metadata. This restores the class docstring
and avoids inventing a public attribute. Used cells and explicit attributes
remain significant; owned tests cover dataclass fields and namespace behavior.

The verifier permits one coupled predicate spelling variance: `IS_OP 1`
immediately consumed by a truth jump is canonicalized to `IS_OP 0` with the
opposite truth jump, retaining the destination and fallthrough. Identity tests
produce a builtin bool, so this cannot call user equality or truth methods.
Changing either operation alone, its target, or intervening meaningful work
still fails. An owned behavior test includes objects whose truth/equality
methods raise, and negative tests enforce those boundaries.

Source reconstruction can preserve a shared terminal return with a
constant-folded `return value if True else None` expression. An existing final
return keeps its exact value expression; implicit returns use None. This also
preserves shared boolean-return paths through handled errors. The tool accepts this
only when the entire function unit (including signature, branches and exception
dispatch) then matches the recovered bytecode. It reverts unproven attempts,
skips ambiguous definitions and async generators, and checks that no previously
matched unit is lost. No verifier rule is weakened. Owned tests compare truth
hooks, closures, generators and coroutines and reject different constants,
parameters and handler types. The recovery report lists exactly the accepted
unit paths in `verified_shared_return_shapes`.

The existing jump-to-bare-return comparison also accounts for the original
forward jump's own non-`lasti` exception-table slot. That slot contains no
fallible evaluation. Coverage over real returns, calls, other instructions,
backward jumps and cleanup/StopIteration routes remains significant. Owned
bytecode fixtures preserve callback/identity/truth/error behavior and coroutine
cancellation, and reject changed call coverage, handler types, stack depths,
literals and intervening work. See Python's [forward/backward jump documentation](https://docs.python.org/3.14/library/dis.html#opcode-JUMP_FORWARD).
The source scope repair now also finds hoisted returns at nested block tails;
each whole function must match before any move is retained, and nested
definitions are proved independently. Nested row fallback/getter and loop
fixtures cover these cases. This does not permit ignoring a handler over a
call or moving arbitrary work between handlers.

The related branch-return pass tries at most four constant-folded layers around
existing returns, including returns inside rejection guards. This preserves
some shared keyword-call or dictionary tails that one folded layer cannot
retain. CPython 3.14's [flow-graph optimizer](https://github.com/python/cpython/blob/v3.14.2/Python/flowgraph.c)
can copy small exit blocks with a four-instruction limit. Each trial still has
to match the complete code unit, including exception dispatch, and retain every
earlier match after unsuccessful neighboring edits are reverted. Nested
definitions are collected independently; async generators are skipped.
`verified_shared_branch_return_shapes` records accepted paths. Owned tests
cover descriptors, arguments, chained rich comparisons, dictionaries, nested
closures, handled coroutine errors and cancellation; changed contracts remain
failures. These folded expressions supply no runtime predicate or missing work.

Attribute callable values can retain their original unoptimized lookup using
`(target.method if True else None)(...)`. The folded condition performs no
runtime test. The source pass reuses existing attributes and arguments and
accepts a trial only after its whole unit matches and every prior match survives.
It preserves the distinction between `LOAD_ATTR` plus `PUSH_NULL` and the method
call optimization; the verifier does not normalize that distinction away.
Owned tests cover descriptors, argument/call order and errors, bound methods,
lazy closures, coroutine handlers and cancellation. Altered attributes,
arguments and mixed call shapes remain unproven. Accepted units are listed in
`verified_attribute_callable_values` in the recovery report.

The callable-value pass also tries one through four constant-folded layers
around existing returns when an attribute-only trial does not match. Some
coroutines need both the callable-value lookup and their shared exit block
restored together; neither isolated change passes full-unit comparison. The
joint trial reuses existing expressions, keeps every handler significant and
restores all failed neighboring edits. Owned fixtures verify reconnect versus
creation, descriptor lookup, argument order, errors, cancellation and nested
scope isolation. Wrong arguments, receivers or branch conditions still fail.

Source-scope lookup uses the first decorator's line for decorated functions,
matching CPython's code-object location. Using the ``def`` line silently skipped
property, static and class methods in the callable and branch-return passes.
Owned fixtures check descriptors, stacked decorators, coroutine cancellation,
duplicate method names, unproven neighbors and full native-export recovery.
The mapping correction does not change comparison or exception-handler rules.

Python 3.14 may also create a module's conditional-annotation set implicitly.
An explicit guessed empty dictionary in generated source would overwrite that
real binding. The source pass removes it only when the entire module unit then
matches and no earlier unit match is lost. Actual namespace stores remain
significant in the verifier. Owned tests compare the compiler set, annotation
dictionary and other exported bindings and keep genuine explicit assignments.
The recovery report records `verified_module_annotation_binding`.

A result-building final return can also be hoisted past the handler that
originally covered it. The scope repair moves it back into the preceding try
body only after the complete code unit matches, including every exception
range, target, stack depth and lasti flag. It rejects unproven changes and
checks that no old match is lost. Existing outside returns and finally suites
are preserved. Owned fixtures exercise handled access/conversion errors and
coroutine cancellation. Accepted paths appear in `verified_try_return_scopes`.
The same proof-gated trial covers empty try bodies whose handlers were optimized
away after the return was hoisted. These cases can initially fail instruction
comparison; they still require a full code-unit match after repair. Owned
fixtures retain handled missing attributes and unhandled property failures.

Private captured names can also disrupt CPython's lexical closure-cell order.
The source pass derives ordering constraints from the recovered capture tuples
and chooses nonconflicting private spellings. It preserves public parameter and
global names, skips incompatible constraints and accepts a trial only when new
units match and every prior match survives. Parent capture tuples and child
slot uses both remain significant in the verifier. Owned tests compare closure
values, generators, nonlocal mutations and late bound attributes, and reject
changed capture uses. Newly proven paths appear in `verified_capture_name_order`.

Conditional return values split into separate statements, and lost boolean
short-circuit arms left as empty branches, are reconstructed from their existing
tests and values. Full-unit comparison and preservation of every old match gate
each accepted shape. Legitimate empty branches and changed business values are
retained or rejected rather than guessed away. Owned fixtures check nested
value construction, rich comparison results/truth order, await failures and
cancellation. Accepted unit/shape pairs appear in `verified_compound_return_shapes`.

Run the original-fixture regression tests with:

CI pins modern backend **0.6.12-einstein.1**. Its chained inline-comprehension
repair distinguishes an accepting jump into the next guard from a skip edge;
the unpacker has an end-to-end regression for lists, sets and dictionaries,
positive/negative predicates, conjunctions and successive filters. Every
generated fixture receives complete code-tree comparison and value checks.
This repair changes source generation, without adding a comparison allowance.
An additional whole-tree regression covers shared raises with a chained
comparison cleanup, formatted message and explicit cause, checking call order,
error metadata and both valid and rejected inputs.

```bash
PYARMOR_MODERN_BACKEND=/path/to/pycdc-modern/target/release/pycdc \
  .venv/bin/python -B -m unittest discover -s tests -v
```

The native backend's context-manager test is an expected failure; the optional
modern backend covers that case. The fork includes no recovered third-party
code, runtime binaries, keys, or protected test fixtures.

[![GitHub Downloads (all assets, all releases)](https://img.shields.io/github/downloads/Lil-House/Pyarmor-Static-Unpack-1shot/total)](https://github.com/Lil-House/Pyarmor-Static-Unpack-1shot/releases)
[![GitHub Repo stars](https://img.shields.io/github/stars/Lil-House/Pyarmor-Static-Unpack-1shot?style=flat)](https://github.com/Lil-House/Pyarmor-Static-Unpack-1shot/stargazers)
[![GitHub Release](https://img.shields.io/github/v/release/Lil-House/Pyarmor-Static-Unpack-1shot)](https://github.com/Lil-House/Pyarmor-Static-Unpack-1shot/releases)
[![GitHub commits since latest release](https://img.shields.io/github/commits-since/Lil-House/Pyarmor-Static-Unpack-1shot/latest)](https://github.com/Lil-House/Pyarmor-Static-Unpack-1shot/commits/main/)
[![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/Lil-House/Pyarmor-Static-Unpack-1shot/regtest.yml?branch=main&label=test)](https://github.com/Lil-House/Pyarmor-Static-Unpack-1shot/actions)

[Pyarmor](https://github.com/dashingsoft/pyarmor) is a popular tool to protect Python source code. It turns Python scripts into binary data, which can be regarded as an encrypted variant of pyc files. They can be decrypted by a shared library (pyarmor_runtime) and then executed by Python interpreter.

This project aims to convert armored data back to bytecode assembly and (experimentally) source code. We forked the awesome [Decompyle++](https://github.com/zrax/pycdc) (aka pycdc), and added some processes on it like modifying abstract syntax tree.

> [!IMPORTANT]
>
> This tool should only be used on scripts you own or have permission to analyze. Please respect software licenses and terms of service. The author is not responsible for any misuse or damage caused by this tool.

> [!NOTE]
>
> Like other decompilers, this tool is intended for professional users. You should have a basic understanding of Python bytecode. If not, you may need to ask for help from someone who does.

> [!WARNING]
>
> **Disassembly results are accurate, but decompiled code can be incomplete and incorrect.** [See issue #3](https://github.com/Lil-House/Pyarmor-Static-Unpack-1shot/issues/3)

## Features

### Static

You don't need to execute the encrypted script. We decrypt them using the same algorithm as pyarmor_runtime. This is useful when the scripts cannot be trusted.

### Universal

Currently we are trying to support Pyarmor 8.0 to 9.2.x (latest), Python 3.7 - 3.13, on all operating systems, with obfuscating options as many as possible. (However, we only have limited tests.)

You can run this tool in any environment, no need to be the same with obfuscated scripts or runtime.

> [!NOTE]
>
> If the data starts with `PY` followed by six digits, it is supported. Otherwise, if it starts with `PYARMOR`, it is generated by Pyarmor 7 or earlier, and is not supported.

### Easy to use

The only thing you need to do is specifying where your obfuscated scripts are. The tool does everything like detecting armored data, parsing, disassembling, and decompiling. See "Usage" section below.

## Build

```bash
mkdir build
cd build
cmake ../pycdc
cmake --build .
cmake --install .
```

You can also download prebuilt binary files on [releases page](https://github.com/Lil-House/Pyarmor-Static-Unpack-1shot/releases).

## Usage

```bash
python /path/to/oneshot/shot.py /path/to/scripts
```

Before running `shot.py`, make sure the executable `pyarmor-1shot` (`pyarmor-1shot.exe` on Windows) exists in `oneshot` directory.

You only need to specify the directory that contains all armored data and `pyarmor_runtime`. The tool finds and handles them recursively as much as possible.

When necessary, specify a `pyarmor_runtime` executable with `-r path/to/pyarmor_runtime[.pyd|.so|.dylib]`.

All files generated from this tool have a `.1shot.` in file names. If you want to save them in another directory instead of in-place, use `-o another/path/`. Folder structure will remain unchanged.

Note:

- Subdirectories will not be touched if the folder name is exactly `__pycache__` or `site-packages` or it directly contains a file named `.no1shot`, and symbolic links will not be followed, to avoid repeat or forever loop and save time. If you really need them, run the script later in these directories and specify the runtime.
- Archives, executables generated by PyInstaller and so on, must be unpacked by other tools before decrypting, or you will encounter undefined behavior.

## Feedback

Feel free to open an issue if you have any questions, suggestions, or problems. Don't forget to attach the armored data and the `pyarmor_runtime` executable if possible.

## Project Status

### Current

**Bug fixes and minor improvements.**

The author is trying to design and implement a new pyc decompiler. After that, we can rewrite this tool with the new decompiler.

We will discard the current codebase based on pycdc, and after the new code becomes stable, expected in 2027, we will reach version 1.0.0.

### Before v1.0.0

**The project is already functioning well, no need to wait for v1.0.0.** The public API is still subject to change.

These are the features we are planning to implement before v1.0.0:

- Rewriting the codebase based on a new pyc decompiler. It will be more stable, maintainable, and extendable.
- Wiki about the Pyarmor decryption algorithm, and how we analyzed it and implemented it in this tool.
- The last piece of the puzzle: analysis tool for the native part of BCC Mode (the hardest obfuscating option in Pyarmor).
- Packaging and distribution of the tool, for installation from package managers like pip.

Maybe there are not many things to do after v1.0.0. We will maintain the project and fix bugs if necessary.

## Original Intention

There are not many code protection tools for Python that are publicly available, and Pyarmor is one of the most popular ones. However, when I (Lil-Ran) was analyzing some specific Pyarmor-protected scripts, I found it not so difficult as I had imagined.

Just encrypting the code objects is not as irreversible as the project claims. To be honest, Python bytecode based protection is much less secure than native code based protection.

We definitely do not make profits by cracking. We ourselves had no other uses for this tool, so we published it, hoping to let people know that Pyarmor with default configuration is not as secure as they think.

The Pyarmor algorithm has not been changed, even a year after this unpacking tool is released. Perhaps it is due to considerations of backward compatibility.

We guarantee that once Pyarmor project changes the algorithm, we will **not** update this tool to support the new algorithm. Currently existing code generated by supported versions of Pyarmor can always be decrypted by this tool.

### For CTF Challenge Makers

Please, don't make CTF (Capture The Flag) **REVERSE** challenges that are about finding and using existing **TOOLS**. It is not fun for anyone. It is more like a game of hide-and-seek, rather than a test of skill and ingenuity.

It is more meaningful to understand how Pyarmor or this tool works, and then try to improve them.

## Star Chart

[![Stargazers over time](https://starchart.cc/Lil-House/Pyarmor-Static-Unpack-1shot.svg?variant=adaptive)](https://starchart.cc/Lil-House/Pyarmor-Static-Unpack-1shot)

### Coupled source-shape repairs

Adjacent identical handler fragments sometimes need both reunion and relocation
of an existing final return into the reunited try. The fork trials those repairs
atomically: either the complete function's meaningful instructions and dispatch
map match, or both edits are rolled back. Intervening work, different handlers,
and else/finally suites are excluded. An empty try/finally may similarly require
its existing await-return to move before cleanup; owned ContextVar tests check
success, failure, cancellation, and restoration timing.

Other narrowly scoped AST trials restore separate CALL_FUNCTION_EX keyword
merges, split chained comprehension filters, and inverted loop continuation
guards. Each reuses only expressions already present in the candidate. Keyword
trials retain Python's duplicate-key TypeError instead of silently overwriting
inside a dict literal; genuine dict-literal calls are not changed. Filter trials
retain a chained comparison's single middle evaluation. Loop trials retain
continue ownership and success-return scope, excluding nested-loop continuations.
These trials require full unit proof and preservation of every prior matched
unit after unsuccessful neighboring edits are rolled back. They do not widen
the bytecode verifier's normalization rules.

The 0.4.24 CI pin selects backend 0.6.15, which restores unpacked
comprehension iterator slots. The complete corpus retains every previous match
and gains 97 units with the preceding source repairs. The initial 0.6.14
blanket-cleanup trial was rejected after five regressions despite passing its
initial tests. Source verification rules remain unchanged.

### Bounded statement-scope and return-edge trials

Version 0.4.25 adds single-function shape trials selected by full instruction
and exception-dispatch proof. Failed candidates are rolled back, nested code
units must match with their owner, and a final whole-module check protects all
earlier matches. This changes source reconstruction rather than comparison
rules, signature contracts or exception-coverage tolerances.

The bounded candidates reuse existing statements and expressions to close
async contexts before swallowed suffixes; restore a return to one protected
body; retain omitted finally copies; recover OR comprehension filters and
short-circuit rejection guards; and restore conditional returns in try-else
suites. Structural repairs are tried together with shared return edges when
neither change proves alone. Per-site expression and constant-statement return
forms preserve distinct compiler dispatch boundaries. Async generators use a
legal constant If with a bare return, optionally together with reference-proved
callable-value loads; no value-bearing generator return is introduced.

Owned validation passes 167 cases, with one existing legacy expected failure.
The new cases check context exit order and exception suppression, genuine
transaction protection, wrong-call/cleanup rejection, predicate evaluation
order, OR versus AND selection, cancellation cleanup, and async-generator
normal completion/close/throw. CI pins backend 0.6.16. The independently audited
application coverage remains recorded by the component's recovery manifests;
passing these tests alone does not establish a complete recovered application.


The 0.4.26 source trials add coupled shared-handler/return reconstruction,
existing handler-prefix terminal returns, exiting-handler success suffixes,
empty positive predicate restoration, exact negated comparison chains, and
reference-proved loop handler continues. Only existing work and compiler return
shapes are reused. The owner, all descendants and every previous module match
must still pass the unchanged comparator. Direct AST compilation avoids repeatedly
unparsing large modules during bounded trials; final emitted source is compared
separately. Owned tests include true independent tries, meaningful fallthrough,
duplicate middle calls, wrong handlers and real calls that must be retained.
Full protected-corpus retention remains a separate publication requirement.

### Existing value and binding reconstruction

Version 0.4.27 restores lost inplace assignment operators, singleton tuple
boundaries around optional expanded rows, carried container return values,
top-package bindings for unaliased dotted imports, and compound conditional
values. Generator filters incorrectly paired with OR may be restored as
consecutive conjunctive filters. These are bounded trials over existing
expressions; a genuine assignment, expansion, alias, discarded container or
disjunction remains unchanged unless the original complete subtree proves the
replacement. An already matching function may still need its generator child
repaired, so trial eligibility checks the whole descendant set.

The seven new owned tests cover single target evaluation and mutable identity,
empty and populated expansions, handler boundaries and call order, import
binding, short-circuit predicates, nested captures, and meaningful negative
controls. All 183 unpacker cases pass, including one existing expected failure.
CI pins backend 0.6.19, whose independently tested native changes preserve
ordered keyword expansions, chained comparisons within comprehension values,
and constant early-return cleanup in async contexts. Full application coverage
and zero-loss retention must still be audited separately before either tool is
advanced to its stable branch.
