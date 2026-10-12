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
