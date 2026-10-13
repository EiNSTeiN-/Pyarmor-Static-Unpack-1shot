"""Fail-closed, static comparisons of CPython 3.14 code objects.

This is a stricter check than source compilation or counting functions. It
does not execute either program. A mismatch stays a mismatch; unsupported
PyArmor expression patterns are reported and never treated as equivalent.
"""

from __future__ import annotations

import argparse
import ast
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
import dis
import hashlib
import importlib.util
import json
import marshal
from pathlib import Path
import sys
import types
import warnings

try:
    from .modern_backend import MarkerNormalizer, normalize_code, unused_future_annotation_offsets
except ImportError:
    from modern_backend import MarkerNormalizer, normalize_code, unused_future_annotation_offsets


PADDING = frozenset(('NOP', 'CACHE', 'EXTENDED_ARG', 'NOT_TAKEN', 'RESUME'))
FUSED = {
    'LOAD_FAST_LOAD_FAST': ('LOAD_FAST', 'LOAD_FAST'),
    'LOAD_FAST_BORROW_LOAD_FAST_BORROW': ('LOAD_FAST', 'LOAD_FAST'),
    'STORE_FAST_LOAD_FAST': ('STORE_FAST', 'LOAD_FAST'),
    'STORE_FAST_STORE_FAST': ('STORE_FAST', 'STORE_FAST'),
}


def compile_source(source, filename):
    """The candidate must not inherit this verifier's future-annotations flag."""
    return compile(source, filename, 'exec', dont_inherit=True)


def constant(value):
    """Compare literal values and types, not their constant-pool indexes."""
    if isinstance(value, types.CodeType):
        return ('code', value.co_name)
    if isinstance(value, (tuple, frozenset)):
        values = [constant(item) for item in value]
        if isinstance(value, frozenset):
            values.sort(key=repr)
        return (type(value).__name__, tuple(values))
    if isinstance(value, bytes):
        # Avoid copying potentially confidential opaque payloads into reports.
        return ('bytes', len(value), hashlib.sha256(value).hexdigest())
    if isinstance(value, (str, int, float, complex, bool)) or value is None:
        return (type(value).__name__, repr(value))
    return (type(value).__name__, repr(value))


def local_operand(code, index, reserved_local_indices=()):
    """Alpha-rename internal slots while preserving keyword parameter names."""
    name = code._varname_from_oparg(index)
    locals_ = [value for slot, value in enumerate(code.co_varnames)
               if value != '__assert_armored__' and slot not in reserved_local_indices]
    parameters = code.co_argcount + code.co_kwonlyargcount
    parameters += bool(code.co_flags & 4) + bool(code.co_flags & 8)
    if name in code.co_varnames[:parameters]:
        return ('parameter', name)
    if name in locals_:
        return ('local', locals_.index(name))
    cells = [value for value in code.co_cellvars if value not in code.co_varnames]
    if name in cells:
        return ('cell', cells.index(name))
    if name in code.co_freevars:
        return ('free', code.co_freevars.index(name))
    return ('unresolved_slot', index)


def unused_protection_slot_aliases(reference, candidate):
    """Recognize renamed, unreferenced reservation slots at the same position.

    The protected name already has no canonical local ordinal. Its source
    counterpart may use a neutral name, but it must retain the physical slot
    and cannot contain any instruction or closure that accesses that slot.
    No operation, branch, value or exception region is removed by this check.
    """
    if '__assert_armored__' not in reference.co_varnames:
        return ()
    if len(reference.co_varnames) != len(candidate.co_varnames):
        return ()
    parameters = reference.co_argcount + reference.co_kwonlyargcount
    parameters += bool(reference.co_flags & 4) + bool(reference.co_flags & 8)
    candidate_parameters = candidate.co_argcount + candidate.co_kwonlyargcount
    candidate_parameters += bool(candidate.co_flags & 4) + bool(candidate.co_flags & 8)
    if parameters != candidate_parameters or reference.co_varnames[:parameters] != candidate.co_varnames[:parameters]:
        return ()
    if len(set(candidate.co_varnames)) != len(candidate.co_varnames):
        return ()
    result = []
    operations = tuple(dis.get_instructions(candidate))
    for slot, name in enumerate(reference.co_varnames):
        if name != '__assert_armored__' or slot < parameters or name in reference.co_cellvars:
            continue
        alias = candidate.co_varnames[slot]
        if alias == name or alias in candidate.co_cellvars or alias in candidate.co_freevars:
            continue
        if '__assert_armored__' in candidate.co_varnames:
            continue
        accessed = False
        for operation in operations:
            if operation.opname in FUSED:
                touched = (operation.arg >> 4, operation.arg & 15)
                if slot in touched:
                    accessed = True
                    break
            elif operation.argval == alias:
                accessed = True
                break
        if not accessed:
            result.append(slot)
    return tuple(result)


@dataclass(frozen=True)
class CanonicalCode:
    signature: tuple
    instructions: tuple
    exceptions: tuple
    unresolved: tuple
    # Record erased transfer-site provenance separately from the already
    # established shared-return operation abstraction. This is diagnostic,
    # not an assertion that opcode tracing observes the same physical event.
    return_alias_provenance: tuple = field(default=(), compare=False)


def canonical(code, decoder=None, reserved_local_indices=()):
    """Apply an explicit, narrow whitelist; keep control flow and handlers."""
    original = [item for item in dis.get_instructions(code) if item.opname not in PADDING]
    removed, replacements, unresolved = set(), {}, []
    # A sole private compiler annotation cell with no reads/captures cannot
    # affect the class namespace or annotation values. The source must not
    # invent a public attribute for it. Used cells and public stores remain.
    unused_annotation = set(unused_future_annotation_offsets(code))
    removed.update(index for index, item in enumerate(original) if item.offset in unused_annotation)

    # CPython 3.13+ stores class source locations in the compiler header as
    # well as co_firstlineno. Formatting changes may alter only that header
    # value. An explicit user assignment later in the body stays significant.
    if code.co_name != '<module>' and not code.co_flags & 1:
        for index, item in enumerate(original):
            if (index >= 4 and index + 1 < len(original)
                    and item.opname in ('LOAD_CONST', 'LOAD_SMALL_INT')
                    and isinstance(item.argval, int)
                    and original[index - 4].opname == 'LOAD_NAME'
                    and original[index - 4].argval == '__name__'
                    and original[index - 3].opname == 'STORE_NAME'
                    and original[index - 3].argval == '__module__'
                    and original[index - 2].opname == 'LOAD_CONST'
                    and original[index - 1].opname == 'STORE_NAME'
                    and original[index - 1].argval == '__qualname__'
                    and original[index + 1].opname == 'STORE_NAME'
                    and original[index + 1].argval == '__firstlineno__'):
                replacements[index] = ('LOAD_CONST', ('compiler_class_line',))

    def store(item):
        if item.opname == 'STORE_FAST':
            return (item.opname, local_operand(code, item.arg, reserved_local_indices))
        if item.opname in ('STORE_NAME', 'STORE_GLOBAL'):
            return (item.opname, item.argval)
        return None

    # Ordinary import bytecode and PyArmor's positional tuple importer use
    # different plumbing for the same explicit binding contract. Keep the
    # module, relative level, imported names, and every destination significant.
    for index, item in enumerate(original):
        if (index in removed or item.opname not in ('LOAD_CONST', 'LOAD_SMALL_INT')
                or not isinstance(item.argval, int)):
            continue
        tail = original[index + 1:index + 3]
        if len(tail) != 2 or [value.opname for value in tail] != ['LOAD_CONST', 'IMPORT_NAME']:
            continue
        names, module = tail[0].argval, tail[1].argval
        if names is None and index + 3 < len(original) and store(original[index + 3]):
            replacements[index] = ('IMPORT', (module, store(original[index + 3]), item.argval))
            removed.update(range(index + 1, index + 4))
        elif isinstance(names, tuple) and names and all(isinstance(name, str) for name in names):
            imports = original[index + 3:index + 3 + 2 * len(names)]
            end = index + 3 + len(imports)
            if (len(imports) == 2 * len(names) and end < len(original)
                    and original[end].opname == 'POP_TOP'
                    and all(imports[2 * count].opname == 'IMPORT_FROM'
                            and imports[2 * count].argval == name
                            and store(imports[2 * count + 1]) for count, name in enumerate(names))):
                bindings = tuple((name, store(imports[2 * count + 1])) for count, name in enumerate(names))
                replacements[index] = ('FROM_IMPORT', (module, bindings, item.argval))
                removed.update(range(index + 1, end + 1))

    for index, item in enumerate(original):
        if index in removed:
            continue
        if (item.opname == 'LOAD_CONST' and isinstance(item.argval, str)
                and item.argval.startswith('__pyarmor_assert_')
                and index + 1 < len(original)
                and original[index + 1].opname in ('STORE_FAST', 'STORE_NAME', 'STORE_GLOBAL')
                and original[index + 1].argval == '__assert_armored__'):
            removed.update((index, index + 1))
            continue
        is_assert = ((item.opname == 'LOAD_CONST' and isinstance(item.argval, str)
                      and item.argval.startswith('__pyarmor_assert_'))
                     or (item.opname in ('LOAD_FAST', 'LOAD_FAST_BORROW', 'LOAD_NAME', 'LOAD_GLOBAL')
                         and item.argval == '__assert_armored__'))
        if not is_assert:
            continue
        # Literal-load form: assert_callable, NULL, encrypted literal, CALL 1.
        tail = original[index + 1:index + 4]
        if (decoder is not None and len(tail) == 3
                and [value.opname for value in tail] == ['PUSH_NULL', 'LOAD_CONST', 'CALL']
                and tail[2].arg == 1 and isinstance(tail[1].argval, bytes)):
            try:
                value = decoder.decode(ast.Constant(tail[1].argval))
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                    after = index + 4
                    if (value.func.id == '_pyarmor_import_marker' and after < len(original)
                            and store(original[after])):
                        replacements[index] = ('IMPORT', (value.args[0].value, store(original[after]), 0))
                        removed.update(range(index + 1, after + 1))
                        continue
                    if (value.func.id == '_pyarmor_from_import_marker'
                            and after < len(original) and original[after].opname == 'UNPACK_SEQUENCE'):
                        module, names, level = (argument.value for argument in value.args)
                        count = len(names)
                        stores = original[after + 1:after + 1 + count]
                        if original[after].arg == count and len(stores) == count and all(store(target) for target in stores):
                            bindings = tuple((name, store(target)) for name, target in zip(names, stores))
                            replacements[index] = ('FROM_IMPORT', (module, bindings, level))
                            removed.update(range(index + 1, after + 1 + count))
                            continue
                if not isinstance(value, ast.Constant):
                    raise ValueError('Non-scalar marker requires a separate lowering rule')
                replacements[index] = ('LOAD_CONST', constant(value.value))
                removed.update(range(index + 1, index + 4))
                continue
            except (ValueError, SyntaxError, TypeError):
                pass
        unresolved.append(item.offset)

    ops, offsets = [], []
    for index, item in enumerate(original):
        if index in removed:
            continue
        if index in replacements:
            offsets.append(item.offset)
            ops.append(replacements[index])
            continue
        name, arg = item.opname, item.arg
        if name in FUSED:
            for opname, slot in zip(FUSED[name], (arg >> 4, arg & 15)):
                offsets.append(item.offset)
                ops.append((opname, local_operand(code, slot, reserved_local_indices)))
            continue
        offsets.append(item.offset)
        if name == 'LOAD_FAST_BORROW':
            name = 'LOAD_FAST'
        if name == 'LOAD_SMALL_INT':
            name, operand = 'LOAD_CONST', constant(arg)
        elif name == 'LOAD_CONST':
            operand = constant(item.argval)
        elif name in ('LOAD_FAST', 'LOAD_FAST_CHECK', 'LOAD_FAST_AND_CLEAR', 'STORE_FAST',
                      'DELETE_FAST', 'LOAD_DEREF', 'STORE_DEREF', 'DELETE_DEREF',
                      'LOAD_CLOSURE', 'MAKE_CELL'):
            operand = local_operand(code, arg, reserved_local_indices)
        elif item.opcode in dis.hasjump:
            # Direction is determined by the target; NO_INTERRUPT stays distinct.
            if name in ('JUMP_FORWARD', 'JUMP_BACKWARD'):
                name = 'JUMP'
            operand = ('target_offset', item.argval)
        elif name == 'LOAD_GLOBAL':
            operand = (item.argval, arg & 1)
        elif name == 'LOAD_ATTR':
            operand = (item.argval, arg & 1)
        elif item.opcode in dis.hasname:
            operand = item.argval
        else:
            operand = arg
        ops.append((name, operand))

    def position(offset):
        return bisect_left(offsets, offset)

    for index, (name, operand) in enumerate(ops):
        if isinstance(operand, tuple) and operand[:1] == ('target_offset',):
            ops[index] = (name, ('target', position(operand[1])))
    # Identity tests always produce a builtin bool. Inverting both IS_OP
    # and the immediately consuming truth jump leaves exactly the same
    # predicate, target and fallthrough; no user truth/equality method runs.
    # Keep either inversion alone, intervening work and destinations strict.
    for index in range(len(ops) - 1):
        if ops[index] != ('IS_OP', 1):
            continue
        branch, destination = ops[index + 1]
        inverse = {'POP_JUMP_IF_TRUE': 'POP_JUMP_IF_FALSE',
                   'POP_JUMP_IF_FALSE': 'POP_JUMP_IF_TRUE'}.get(branch)
        if inverse:
            ops[index] = ('IS_OP', 0)
            ops[index + 1] = (inverse, destination)
    # Complete the existing shared-return alias rule before comparing handler
    # ranges. A forward jump directly to a bare RETURN_VALUE contains no user
    # operation or backward-jump interrupt check. Its own non-lasti dispatch
    # slot is transfer plumbing, not the effective RETURN operation. Keep
    # every real RETURN slot and all lasti-covered alias routes significant.
    # If the target is an intrinsic-covered coroutine return, the substituted
    # operation must carry that target's route rather than a fabricated hole.
    return_aliases = set()
    alias_targets = {}
    for index, (name, operand) in enumerate(ops):
        if name == 'JUMP' and operand[0] == 'target':
            target = operand[1]
            if target < len(ops) and ops[target][0] == 'RETURN_VALUE':
                ops[index] = ('RETURN_VALUE', None)
                if index < target:
                    return_aliases.add(index)
                    alias_targets[index] = target
    entries = tuple(dis.Bytecode(code).exception_entries)
    projected_routes, alias_provenance = [], []
    physical = {item.offset: item for item in original}
    if code.co_flags & 128:  # Only native coroutine return cleanup is proved.
        for index, target in sorted(alias_targets.items()):
            transfer_op = physical[offsets[index]]
            terminal_op = physical[offsets[target]]
            if transfer_op.opname != 'JUMP_FORWARD' or terminal_op.opname != 'RETURN_VALUE':
                continue
            transfer = [row for row in entries if row.start <= offsets[index] < row.end]
            terminal = [row for row in entries if row.start <= offsets[target] < row.end]
            if len(transfer) != 1 or len(terminal) != 1:
                continue
            source_route, target_route = transfer[0], terminal[0]
            if source_route.lasti or source_route.depth != 0:
                continue
            handler = position(target_route.target)
            if (target_route.depth != 0 or not target_route.lasti
                    or handler >= len(ops) or ops[handler] != ('CALL_INTRINSIC_1', 3)):
                continue
            projected_routes.append((index, index + 1, handler, 0, True))
            alias_provenance.append((index, target,
                                     (position(source_route.target), source_route.depth, source_route.lasti),
                                     (handler, target_route.depth, target_route.lasti)))
    exceptions = []
    for entry in entries:
        region = (position(entry.start), position(entry.end), position(entry.target), entry.depth, entry.lasti)
        segments = []
        start = region[0]
        for index in sorted(return_aliases):
            if entry.lasti or not region[0] <= index < region[1]:
                continue
            if start < index:
                segments.append((start, index, *region[2:]))
            start = index + 1
        if start < region[1]:
            segments.append((start, region[1], *region[2:]))
        for region in segments:
            # Adjacent ranges with the same handler have exactly the same
            # dispatch map. Never merge across a meaningful instruction or a
            # different handler; the alias slot remains a deliberate gap.
            if exceptions and exceptions[-1][1] == region[0] and exceptions[-1][2:] == region[2:]:
                previous = exceptions[-1]
                exceptions[-1] = (previous[0], region[1], *region[2:])
            else:
                exceptions.append(region)
    if projected_routes:
        # Every eligible alias had one removed non-lasti transfer route, so
        # adding its effective return route cannot overlap another region.
        merged = []
        for region in sorted(exceptions + projected_routes):
            if merged and merged[-1][1] == region[0] and merged[-1][2:] == region[2:]:
                previous = merged[-1]
                merged[-1] = (previous[0], region[1], *region[2:])
            else:
                merged.append(region)
        exceptions = merged
    exceptions = tuple(exceptions)
    parameters = code.co_argcount + code.co_kwonlyargcount
    parameters += bool(code.co_flags & 4) + bool(code.co_flags & 8)
    docstring = constant(code.co_consts[0]) if code.co_flags & 67108864 else None
    signature = (code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
                 code.co_flags, tuple(code.co_varnames[:parameters]),
                 len(code.co_freevars), len(code.co_cellvars) - bool(unused_annotation), docstring)
    return CanonicalCode(signature, tuple(ops), exceptions, tuple(unresolved), tuple(alias_provenance))


def units(code, path='<module>'):
    """Use names and occurrence numbers to match separately compiled units."""
    yield path, code
    counts = Counter()
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            counts[value.co_name] += 1
            yield from units(value, f'{path}/{value.co_name}#{counts[value.co_name]}')


def compare(reference, candidate, decoder=None):
    expected = dict(units(normalize_code(reference)))
    actual = dict(units(normalize_code(candidate)))
    entries = []
    for path in sorted(expected.keys() | actual.keys()):
        entry = {'unit': path, 'matched': False}
        if path not in expected or path not in actual:
            entry['reason'] = 'missing_reference_unit' if path not in expected else 'missing_source_unit'
        else:
            reserved = unused_protection_slot_aliases(expected[path], actual[path])
            left = canonical(expected[path], decoder)
            right = canonical(actual[path], decoder, reserved)
            if left.return_alias_provenance or right.return_alias_provenance:
                entry['return_alias_provenance'] = {
                    'reference': left.return_alias_provenance,
                    'candidate': right.return_alias_provenance,
                }
            if left.unresolved or right.unresolved:
                entry.update(reason='unsupported_marker_pattern', unresolved_marker_count=len(left.unresolved) + len(right.unresolved))
            elif left.signature != right.signature:
                entry.update(reason='signature', reference=left.signature, candidate=right.signature)
            elif left.instructions != right.instructions:
                first = next((index for index, (a, b) in enumerate(zip(left.instructions, right.instructions))
                              if a != b), min(len(left.instructions), len(right.instructions)))
                entry.update(reason='instructions', first_difference=first,
                             reference=left.instructions[max(0, first - 2):first + 3],
                             candidate=right.instructions[max(0, first - 2):first + 3],
                             reference_instruction_count=len(left.instructions),
                             candidate_instruction_count=len(right.instructions))
            elif left.exceptions != right.exceptions:
                entry.update(reason='exception_table', reference=left.exceptions, candidate=right.exceptions)
            else:
                entry.update(matched=True, reason='normalized_bytecode_match')
        entries.append(entry)
    return {'reference_units': len(expected), 'source_units': len(actual),
            'matched_units': sum(entry['matched'] for entry in entries),
            'all_units_match': all(entry['matched'] for entry in entries),
            'behavioral_equivalence_verified': False, 'entries': entries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', type=Path)
    parser.add_argument('source', type=Path)
    parser.add_argument('--sequence', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 14):
        parser.error('Run with CPython 3.14')
    data = args.reference.read_bytes()
    if data[:4] != importlib.util.MAGIC_NUMBER:
        parser.error('Reference magic does not match this interpreter')
    reference = marshal.loads(data[16:])
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)
        candidate = compile_source(args.source.read_text(), str(args.source))
    decoder = None
    if args.sequence:
        header = args.sequence.read_bytes()[:34]
        if len(header) != 34 or header[0] != 0xa1 or header[17] != 0xa2:
            parser.error('Invalid sequence metadata')
        decoder = MarkerNormalizer(header[1:17], header[18:30])
    report = compare(reference, candidate, decoder)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'entries'}))
    return 0 if report['all_units_match'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
