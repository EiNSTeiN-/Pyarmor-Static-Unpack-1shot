"""Static CPython 3.14 source recovery with an optional external decompiler.

Recovered modules are parsed and compiled for validation, never imported/executed.
The sequence's AES material stays local and is never written to this report.
"""

from __future__ import annotations

import ast
from collections import defaultdict
import copy
import dis
import heapq
import importlib.util
import json
import keyword
import marshal
from pathlib import Path
import subprocess
import sys
import types
import warnings

from Crypto.Cipher import AES


def _varint(value: int, marker: bool = False) -> bytes:
    digits = [value & 63]
    while value >> 6:
        value >>= 6
        digits.append(value & 63)
    digits.reverse()
    for index in range(len(digits) - 1):
        digits[index] |= 64
    if marker:
        digits[0] |= 128
    return bytes(digits)


def restore_function_bindings(code: types.CodeType) -> types.CodeType:
    """Recover unambiguous local definition names without changing instructions.

    PyArmor can rename a function's local binding while keeping the nested
    code object's original name. The source backend takes its definition name
    from that binding. Restore only numbered PyArmor locals, never parameters,
    globals, nonlocal cells, class attributes, or conflicting names. Closure
    captures are subsequently resolved by position in ``normalize_code``.
    """
    instructions = [item for item in dis.get_instructions(code)
                    if item.opname not in ('NOP', 'CACHE', 'EXTENDED_ARG', 'NOT_TAKEN')]
    definitions = {}
    for index, item in enumerate(instructions[:-1]):
        if (item.opname != 'LOAD_CONST' or not isinstance(item.argval, types.CodeType)
                or instructions[index + 1].opname != 'MAKE_FUNCTION'):
            continue
        name = item.argval.co_name
        if not name.isidentifier() or keyword.iskeyword(name) or name == '__annotate__':
            continue
        after = index + 2
        while after < len(instructions) and instructions[after].opname == 'SET_FUNCTION_ATTRIBUTE':
            after += 1
        # In CPython 3.14 decorators use the function as CALL 0's implicit
        # argument. Consecutive decorator applications retain this binding.
        while (after < len(instructions) and instructions[after].opname == 'CALL'
               and instructions[after].arg == 0):
            after += 1
        if after >= len(instructions):
            continue
        binding = instructions[after]
        if (binding.opname not in ('STORE_FAST', 'STORE_DEREF')
                or not isinstance(binding.argval, str)
                or not binding.argval.startswith('_var_var_')):
            continue
        definitions.setdefault(binding.argval, set()).add(name)

    parameter_count = code.co_argcount + code.co_kwonlyargcount
    parameter_count += bool(code.co_flags & 4) + bool(code.co_flags & 8)
    parameters = set(code.co_varnames[:parameter_count])
    local_names = set(code.co_varnames) | set(code.co_cellvars)
    occupied = local_names | set(code.co_freevars) | set(code.co_names)
    candidates = {binding: next(iter(names)) for binding, names in definitions.items()
                  if len(names) == 1 and binding in local_names
                  and binding not in parameters and binding not in code.co_freevars}
    counts = {name: sum(other == name for other in candidates.values())
              for name in candidates.values()}
    renames = {binding: name for binding, name in candidates.items()
               if name not in occupied and counts[name] == 1}
    if not renames:
        return code
    return code.replace(co_varnames=tuple(renames.get(name, name) for name in code.co_varnames),
                        co_cellvars=tuple(renames.get(name, name) for name in code.co_cellvars))


def restore_stop_iteration_routes(code, instructions, entries, exits):
    """Bypass a removed runtime rethrow layer, preserving the outer handler.

    In protected generators/coroutines, the body's exception table targets
    PyArmor's exit handler. Its COPY/POP_EXCEPT/RERAISE cleanup then enters the
    compiler's STOPITERATION_ERROR handler. Dropping the first route loses
    that compiler handler's coverage. Only this complete, recognized chain is
    flattened; user handlers and unrecognized runtime layouts stay distinct.
    """
    if not code.co_flags & (32 | 128 | 512):
        return entries
    significant = [item for item in instructions
                   if item.opname not in ('NOP', 'CACHE', 'EXTENDED_ARG', 'NOT_TAKEN')]
    indices = {item.offset: index for index, item in enumerate(significant)}
    routes = {}
    expected = ('PUSH_EXC_INFO', 'LOAD_CONST', 'PUSH_NULL', 'LOAD_CONST', 'CALL',
                'POP_TOP', 'RERAISE', 'COPY', 'POP_EXCEPT', 'RERAISE')
    for head in exits & {entry.target for entry in entries}:
        index = indices.get(head)
        if index is None:
            continue
        handler = significant[index:index + len(expected)]
        if len(handler) != len(expected) or tuple(item.opname for item in handler) != expected:
            continue
        marker = handler[1].argval
        if (not isinstance(marker, str) or not marker.startswith(('__pyarmor_exit_', '__pyarmor_leave_'))
                or handler[4].arg != 1 or handler[6].arg != 0
                or handler[7].arg != 3 or handler[9].arg != 1):
            continue
        cleanup = handler[7].offset
        if not any(entry.start <= head and entry.end == cleanup and entry.target == cleanup
                   and entry.depth == 1 and entry.lasti for entry in entries):
            continue
        outer = []
        for entry in entries:
            if not (entry.start <= cleanup and entry.end >= handler[9].offset + 2
                    and entry.depth == 0 and entry.lasti):
                continue
            target = indices.get(entry.target)
            tail = significant[target:target + 2] if target is not None else []
            if (len(tail) == 2 and tail[0].opname == 'CALL_INTRINSIC_1' and tail[0].arg == 3
                    and tail[1].opname == 'RERAISE' and tail[1].arg == 1):
                outer.append(entry)
        targets = {(entry.target, entry.depth, entry.lasti) for entry in outer}
        if len(targets) == 1:
            routes[head] = next(iter(targets))
    return [entry._replace(target=routes[entry.target][0], depth=routes[entry.target][1],
                           lasti=routes[entry.target][2]) if entry.target in routes else entry
            for entry in entries]


def normalize_code(code: types.CodeType) -> types.CodeType:
    """Remove only identified PyArmor entry/exit plumbing; retain user handlers."""
    if sys.version_info[:2] != (3, 14):
        raise ValueError('Modern source recovery currently requires CPython 3.14')
    code = restore_function_bindings(code)
    instructions = list(dis.get_instructions(code))
    bytecode = bytearray(code.co_code)
    # PyArmor's native entry hook replaces the ordinary initial RESUME.
    # Without that hook, the frame can remain incomplete: zero-argument
    # super() then consults the caller's frame. Restore only the known NOP
    # slot after closure/cell setup and an optional generator-entry pair.
    if any(item.opname == 'LOAD_CONST' and isinstance(item.argval, str)
           and item.argval.startswith('__pyarmor_enter_') for item in instructions):
        initial = [item for item in instructions if item.opname != 'EXTENDED_ARG']
        index = 0
        while index < len(initial) and initial[index].opname in ('COPY_FREE_VARS', 'MAKE_CELL'):
            index += 1
        if (index + 1 < len(initial) and initial[index].opname == 'RETURN_GENERATOR'
                and initial[index + 1].opname == 'POP_TOP'):
            index += 2
        if index < len(initial) and initial[index].opname == 'NOP':
            offset = initial[index].offset
            bytecode[offset:offset + 2] = bytes((dis.opmap['RESUME'], 0))
    # A closure's cells are bound by tuple position, not by co_freevars text.
    # PyArmor can rename parent cells without renaming a nested code object's
    # metadata. Resolve the actual capture names before descending into it.
    significant = [item for item in instructions
                   if item.opname not in ('NOP', 'CACHE', 'EXTENDED_ARG', 'NOT_TAKEN')]
    captures = {}
    for index, item in enumerate(significant):
        if item.opname != 'LOAD_CONST' or not isinstance(item.argval, types.CodeType):
            continue
        child = item.argval
        if not child.co_freevars or index < 1:
            continue
        tail = significant[index + 1:index + 3]
        if [next_item.opname for next_item in tail] != ['MAKE_FUNCTION', 'SET_FUNCTION_ATTRIBUTE']:
            continue
        if tail[1].arg != 8 or significant[index - 1].opname != 'BUILD_TUPLE':
            continue
        count = significant[index - 1].arg
        loads = significant[index - count - 1:index - 1]
        if (count != len(child.co_freevars) or len(loads) != count
                or any(load.opname not in ('LOAD_FAST', 'LOAD_FAST_BORROW', 'LOAD_DEREF', 'LOAD_CLOSURE')
                       or not isinstance(load.argval, str) for load in loads)):
            continue
        names = tuple(load.argval for load in loads)
        if item.arg in captures and captures[item.arg] != names:
            raise ValueError('One code object has inconsistent closure capture names')
        captures[item.arg] = names
    constants = tuple(normalize_code(value.replace(co_freevars=captures.get(index, value.co_freevars)))
                      if isinstance(value, types.CodeType) else value
                      for index, value in enumerate(code.co_consts))
    exits = set()
    forced_jumps = {}
    for index, instruction in enumerate(instructions):
        # Python 3.14 inlines any/all/tuple over generators behind an identity
        # guard. The original slow path calls the same callable and preserves
        # shadowed builtins; select it to avoid backend fast-path stack bugs.
        if (instruction.opname != 'LOAD_COMMON_CONSTANT' or instruction.arg not in (2, 3, 4)
                or index < 1 or index + 2 >= len(instructions)):
            continue
        copy, compare, jump = instructions[index - 1], instructions[index + 1], instructions[index + 2]
        if (copy.opname != 'COPY' or copy.arg != 1 or compare.opname != 'IS_OP'
                or compare.arg != 0 or jump.opname != 'POP_JUMP_IF_FALSE'):
            continue
        for offset in range(copy.offset, jump.offset + 4, 2):
            bytecode[offset:offset + 2] = bytes((dis.opmap['NOP'], 0))
        arg = (jump.argval - jump.offset - 2) // 2
        digits = [arg & 255]
        while arg >> 8:
            arg >>= 8
            digits.append(arg & 255)
        if len(digits) > (jump.offset - copy.offset) // 2 + 1:
            raise ValueError('Optimized builtin guard exceeds available jump width')
        for count, digit in enumerate(reversed(digits[1:])):
            offset = jump.offset - 2 * (len(digits) - 1 - count)
            bytecode[offset:offset + 2] = bytes((dis.opmap['EXTENDED_ARG'], digit))
        bytecode[jump.offset:jump.offset + 2] = bytes((dis.opmap['JUMP_FORWARD'], digits[0]))
        forced_jumps[jump.offset] = jump.argval
    for index, instruction in enumerate(instructions):
        if instruction.opname != 'LOAD_CONST' or not isinstance(instruction.argval, str):
            continue
        name = instruction.argval
        if name.startswith(('__pyarmor_exit_', '__pyarmor_leave_')):
            # Only padding and the exit handler's own PUSH_EXC_INFO belong
            # to this marker. COPY/POP_EXCEPT/RERAISE immediately before it
            # can be the genuine user's exception cleanup, not PyArmor's.
            exits.add(instruction.offset)
            previous = index - 1
            while previous >= 0 and instructions[previous].opname in ('NOP', 'EXTENDED_ARG'):
                exits.add(instructions[previous].offset)
                previous -= 1
            if previous >= 0 and instructions[previous].opname == 'PUSH_EXC_INFO':
                exits.add(instructions[previous].offset)
        if name.startswith('__pyarmor_enter_'):
            end = next((item.offset for item in instructions[index + 1:index + 16]
                        if item.opname == 'POP_TOP'), None)
            if end is not None:
                for offset in range(instruction.offset, end + 2, 2):
                    bytecode[offset:offset + 2] = bytes((dis.opmap['NOP'], 0))
    for index, instruction in enumerate(instructions):
        if instruction.opname == 'JUMP_FORWARD' and instruction.argval in exits:
            bytecode[instruction.offset:instruction.offset + 2] = bytes((dis.opmap['RETURN_VALUE'], 0))
            # RETURN_VALUE has no operand. A wide exit jump's prefix must
            # not survive as an orphan argument extension before the return.
            # Keep all addresses stable so jumps and handlers still align.
            previous = index - 1
            while previous >= 0 and instructions[previous].opname == 'EXTENDED_ARG':
                offset = instructions[previous].offset
                bytecode[offset:offset + 2] = bytes((dis.opmap['NOP'], 0))
                previous -= 1
    entries = restore_stop_iteration_routes(code, instructions,
                                           list(dis.Bytecode(code).exception_entries), exits)
    entries = [entry for entry in entries if entry.target not in exits]
    # NOP unreachable cleanup blocks without moving any instruction addresses.
    # This keeps jump offsets and genuine exception-table regions intact.
    by_offset = {instruction.offset: index for index, instruction in enumerate(instructions)}
    pending, reachable = [0], set()
    while pending:
        offset = pending.pop()
        if offset in reachable or offset not in by_offset:
            continue
        reachable.add(offset)
        index = by_offset[offset]
        instruction = instructions[index]
        for entry in entries:
            if entry.start <= offset < entry.end:
                pending.append(entry.target)
        if bytecode[offset] == dis.opmap['RETURN_VALUE'] or instruction.opname in ('RETURN_CONST', 'RAISE_VARARGS', 'RERAISE'):
            continue
        if offset in forced_jumps:
            pending.append(forced_jumps[offset])
            continue
        if instruction.opname in ('JUMP_FORWARD', 'JUMP_BACKWARD', 'JUMP_BACKWARD_NO_INTERRUPT'):
            pending.append(instruction.argval)
            continue
        if (instruction.opname.startswith(('POP_JUMP', 'JUMP_IF'))
                or instruction.opname in ('FOR_ITER', 'SEND')):
            pending.append(instruction.argval)
        if index + 1 < len(instructions):
            pending.append(instructions[index + 1].offset)
    for index, instruction in enumerate(instructions):
        if instruction.offset not in reachable:
            end = instructions[index + 1].offset if index + 1 < len(instructions) else len(bytecode)
            for offset in range(instruction.offset, end, 2):
                bytecode[offset:offset + 2] = bytes((dis.opmap['NOP'], 0))
    # Once the selected slow path's intervening fast implementation is
    # entirely masked, its synthetic jump can fall through that padding.
    # A source parser must not mistake this implementation-only jump for
    # an else boundary in an enclosing short-circuit expression. Never
    # erase a jump over any remaining meaningful instruction.
    for offset, target in forced_jumps.items():
        if target > offset and all(bytecode[index] == dis.opmap['NOP']
                                   for index in range(offset + 2, target, 2)):
            bytecode[offset:offset + 2] = bytes((dis.opmap['NOP'], 0))
            previous = offset - 2
            while previous >= 0 and bytecode[previous] == dis.opmap['EXTENDED_ARG']:
                bytecode[previous:previous + 2] = bytes((dis.opmap['NOP'], 0))
                previous -= 2
    table = bytearray()
    for entry in entries:
        if entry.target not in reachable:
            continue
        table.extend(_varint(entry.start // 2, marker=True))
        table.extend(_varint((entry.end - entry.start) // 2))
        table.extend(_varint(entry.target // 2))
        table.extend(_varint((entry.depth << 1) | entry.lasti))
    return code.replace(co_code=bytes(bytecode), co_consts=constants,
                        co_exceptiontable=bytes(table))


def unused_future_annotation_offsets(code: types.CodeType) -> tuple[int, ...]:
    """Identify an inaccessible, unused compiler cell in a future-annotated class.

    Conditional annotations can leave a set cell even when the compiler drops
    their guarded stores. It is not a class attribute. Keep every observable
    use, capture, additional cell, and explicit namespace binding significant.
    """
    if (code.co_name == '<module>' or code.co_flags & 1
            or not code.co_flags & 16777216 or code.co_freevars
            or code.co_cellvars != ('__conditional_annotations__',)):
        return ()
    instructions = [item for item in dis.get_instructions(code)
                    if item.opname not in ('NOP', 'CACHE', 'EXTENDED_ARG', 'NOT_TAKEN', 'RESUME')]
    uses = [(index, item) for index, item in enumerate(instructions)
            if item.argval == '__conditional_annotations__']
    if len(uses) != 2:
        return ()
    (make_index, make), (store_index, store) = uses
    if (make_index != 0 or make.opname != 'MAKE_CELL'
            or store.opname != 'STORE_DEREF' or store_index == 0):
        return ()
    initializer = instructions[store_index - 1]
    if initializer.opname != 'BUILD_SET' or initializer.arg != 0:
        return ()
    return make.offset, initializer.offset, store.offset


class ClassAnnotationNormalizer(ast.NodeTransformer):
    """Remove only class-cell initializers proven private and unused by bytecode."""

    def __init__(self, code: types.CodeType):
        self.qualifier = ''
        candidates = {}

        def collect(unit):
            candidates.setdefault(unit.co_qualname, []).append(unit)
            for value in unit.co_consts:
                if isinstance(value, types.CodeType):
                    collect(value)

        collect(code)
        self.unused = {name for name, values in candidates.items()
                       if len(values) == 1 and unused_future_annotation_offsets(values[0])}

    def qualified(self, name):
        return self.qualifier + name

    def visit_FunctionDef(self, node):
        previous = self.qualifier
        self.qualifier = self.qualified(node.name) + '.<locals>.'
        node = self.generic_visit(node)
        self.qualifier = previous
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        previous = self.qualifier
        name = self.qualified(node.name)
        self.qualifier = name + '.'
        node = self.generic_visit(node)
        self.qualifier = previous
        if name in self.unused and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Assign) and len(first.targets) == 1
                    and isinstance(first.targets[0], ast.Name)
                    and first.targets[0].id == '__conditional_annotations__'
                    and isinstance(first.value, (ast.Dict, ast.Set))
                    and not (first.value.keys if isinstance(first.value, ast.Dict) else first.value.elts)):
                node.body.pop(0)
        return node


class MarkerNormalizer(ast.NodeTransformer):
    def __init__(self, key: bytes, nonce: bytes):
        self.key, self.nonce = key, nonce
        self.remaining = []

    def decode(self, value: ast.expr) -> ast.expr:
        if not isinstance(value, ast.Constant) or not isinstance(value.value, bytes):
            return value
        raw = value.value
        if not raw or raw[0] & 127 not in (1, 2, 3, 4):
            raise ValueError('Unrecognized PyArmor mixed constant')
        kind, data = raw[0] & 127, raw[1:]
        if raw[0] & 128:
            data = AES.new(self.key, AES.MODE_CTR, nonce=self.nonce,
                           initial_value=2).decrypt(data)
        if kind == 1:
            return ast.Constant(data.decode('utf-8'))
        if kind == 2:
            value = marshal.loads(data)
            # Convert literal containers to source without evaluating them.
            return ast.parse(repr(value), mode='eval').body
        if kind == 3:
            return ast.Call(ast.Name('_pyarmor_import_marker', ast.Load()),
                            [ast.Constant(data.decode('utf-8'))], [])
        module, names, level = marshal.loads(data)
        return ast.Call(ast.Name('_pyarmor_from_import_marker', ast.Load()),
                        [ast.Constant(module), ast.Constant(tuple(names)), ast.Constant(level)], [])

    def visit_Call(self, node):
        node = self.generic_visit(node)
        if not isinstance(node.func, ast.Constant) or not isinstance(node.func.value, str):
            return node
        name = node.func.value
        if not name.startswith('__pyarmor_'):
            return node
        if name.startswith(('__pyarmor_enter_', '__pyarmor_exit_', '__pyarmor_leave_')):
            return ast.copy_location(ast.Constant(None), node)
        if name.startswith('__pyarmor_assert_') and len(node.args) == 1:
            value = node.args[0]
            if isinstance(value, ast.Tuple) and len(value.elts) in (2, 3):
                attr = self.decode(value.elts[1])
                if not isinstance(attr, ast.Constant) or not isinstance(attr.value, str):
                    raise ValueError('Recovered attribute is not a string')
                dest = ast.Attribute(value.elts[0], attr.value, ast.Load())
                if len(value.elts) == 3:
                    return ast.Call(ast.Name('_pyarmor_store_marker', ast.Load()),
                                    [dest, value.elts[2]], [])
                return ast.copy_location(dest, node)
            return ast.copy_location(self.decode(value), node)
        self.remaining.append(name)
        return node

    def visit_Expr(self, node):
        node = self.generic_visit(node)
        if isinstance(node.value, ast.Constant) and node.value.value is None:
            return None
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            call = node.value
            if call.func.id == '_pyarmor_store_marker':
                target = call.args[0]
                target.ctx = ast.Store()
                return ast.copy_location(ast.Assign([target], call.args[1]), node)
            if call.func.id == '_pyarmor_import_marker':
                return ast.copy_location(ast.Import([ast.alias(call.args[0].value)]), node)
        return node

    def visit_Assign(self, node):
        node = self.generic_visit(node)
        # The source backend can join a marker initializer and ordinary
        # stores into one parallel assignment. Remove only the paired marker
        # literal. Keep the remaining parallel assignment so every RHS still
        # runs before any target store, including observable attribute writes.
        if (len(node.targets) == 1 and isinstance(node.targets[0], (ast.Tuple, ast.List))
                and isinstance(node.value, (ast.Tuple, ast.List))
                and len(node.targets[0].elts) == len(node.value.elts)):
            pairs = list(zip(node.targets[0].elts, node.value.elts))
            remaining = [(target, value) for target, value in pairs
                         if not (isinstance(target, ast.Name) and target.id == '__assert_armored__'
                                 and isinstance(value, ast.Constant) and isinstance(value.value, str)
                                 and value.value.startswith('__pyarmor_assert_'))]
            if len(remaining) != len(pairs):
                if not remaining:
                    return None
                targets, values = zip(*remaining)
                if len(remaining) == 1:
                    return ast.copy_location(ast.Assign([targets[0]], values[0]), node)
                return ast.copy_location(ast.Assign([ast.Tuple(list(targets), ast.Store())],
                                                    ast.Tuple(list(values), ast.Load())), node)
        if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == '__assert_armored__'
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and node.value.value.startswith('__pyarmor_assert_')):
            return None
        if not isinstance(node.value, ast.Call) or not isinstance(node.value.func, ast.Name):
            return node
        call = node.value
        if len(node.targets) != 1:
            return node
        target = node.targets[0]
        if call.func.id == '_pyarmor_import_marker' and isinstance(target, ast.Name):
            return ast.copy_location(ast.Import([ast.alias(call.args[0].value, target.id)]), node)
        if call.func.id == '_pyarmor_from_import_marker' and isinstance(target, (ast.Tuple, ast.List)):
            names = call.args[1].value
            if len(names) != len(target.elts) or not all(isinstance(value, ast.Name) for value in target.elts):
                raise ValueError('Recovered from-import target does not match names')
            aliases = [ast.alias(name, dest.id if dest.id != name else None)
                       for name, dest in zip(names, target.elts)]
            return ast.copy_location(ast.ImportFrom(call.args[0].value, aliases, call.args[2].value), node)
        return node


class DeadTailNormalizer(ast.NodeTransformer):
    """Drop unreachable statements after a terminator in the same source suite."""
    def generic_visit(self, node):
        node = super().generic_visit(node)
        for field in ('body', 'orelse', 'finalbody'):
            statements = getattr(node, field, None)
            if not isinstance(statements, list):
                continue
            for index, statement in enumerate(statements):
                if isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                    setattr(node, field, statements[:index + 1])
                    break
        required_body = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                         ast.If, ast.For, ast.AsyncFor, ast.While, ast.With,
                         ast.AsyncWith, ast.Try, ast.TryStar, ast.ExceptHandler,
                         ast.match_case)
        if isinstance(node, required_body) and not node.body:
            # Removing runtime-only statements can leave an otherwise valid
            # empty class/function/suite. `pass` has no application behavior.
            node.body = [ast.copy_location(ast.Pass(), node)]
        return node

    def visit_Module(self, node):
        node = self.generic_visit(node)
        future = [statement for statement in node.body if isinstance(statement, ast.ImportFrom)
                  and statement.module == '__future__']
        if future:
            other = [statement for statement in node.body if statement not in future]
            # A generated annotation-initialization store may precede the
            # original module docstring in the decompiler's output.
            doc = next((statement for statement in other[:2] if isinstance(statement, ast.Expr)
                        and isinstance(statement.value, ast.Constant)
                        and isinstance(statement.value.value, str)), None)
            if doc:
                other.remove(doc)
            node.body = ([doc] if doc else []) + future + other
        return node


class FutureAnnotationNormalizer(ast.NodeTransformer):
    """Restore expression text serialized by future-annotations bytecode.

    CPython's annotation function stores ``Any`` as the string 'Any'. Treating
    that string as a quoted annotation in source changes it to "'Any'" on
    recompilation. Parsing the stored expression also preserves annotations
    that were explicitly quoted in the original source.
    """

    @staticmethod
    def expression(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return ast.copy_location(ast.parse(node.value, mode='eval').body, node)
            except SyntaxError:
                return node
        return node

    def visit_arg(self, node):
        node.annotation = self.expression(node.annotation)
        return self.generic_visit(node)

    def visit_FunctionDef(self, node):
        node.returns = self.expression(node.returns)
        return self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node):
        node.annotation = self.expression(node.annotation)
        return self.generic_visit(node)


def restore_module_annotation_binding(tree, reference, decoder):
    """Remove a duplicate guessed map only after the module unit matches.

    Python 3.14 creates the module's conditional-annotation set implicitly.
    Rendering that setup as an explicit empty dict overwrites its real binding.
    Recompilation must recreate the exact set setup and all namespace stores;
    this pass never ignores the binding in bytecode comparison.
    """
    if reference.co_name != '<module>':
        return tree, False
    candidates = [statement for statement in tree.body
                  if isinstance(statement, ast.Assign) and len(statement.targets) == 1
                  and isinstance(statement.targets[0], ast.Name)
                  and statement.targets[0].id == '__conditional_annotations__'
                  and isinstance(statement.value, ast.Dict) and not statement.value.keys]
    if not candidates:
        return tree, False
    try:
        from .bytecode_fidelity import compare, compile_source
    except ImportError:
        from bytecode_fidelity import compare, compile_source

    def report():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            candidate = compile_source(ast.unparse(tree), '<static-module-candidate>')
        return compare(reference, candidate, decoder)

    before = report()
    if before['entries'][0]['matched']:
        return tree, False
    matched_before = {entry['unit'] for entry in before['entries'] if entry['matched']}
    for statement in candidates:
        index = tree.body.index(statement)
        tree.body.pop(index)
        after = report()
        matched_after = {entry['unit'] for entry in after['entries'] if entry['matched']}
        if after['entries'][0]['matched'] and matched_before.issubset(matched_after):
            return tree, True
        tree.body.insert(index, statement)
    return tree, False


def restore_capture_name_order(tree, reference, decoder):
    """Choose private capture spellings that preserve the proven cell order.

    CPython sorts nonparameter cells and free-variable names lexically. PyArmor
    may rename those names while retaining their original tuple positions.
    Compile a constraint-derived spelling trial, never permute verifier slots.
    Public parameters, global names and ambiguous ordering remain unchanged.
    """
    try:
        from .bytecode_fidelity import compare, compile_source, units
    except ImportError:
        from bytecode_fidelity import compare, compile_source, units

    def check(candidate_tree):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            candidate = compile_source(ast.unparse(ast.fix_missing_locations(candidate_tree)),
                                       '<capture-name-candidate>')
        return compare(reference, candidate, decoder)

    before = check(tree)
    if before['all_units_match']:
        return tree, []
    codes = [code for _, code in units(normalize_code(reference))]
    occupied = set().union(*(set(code.co_names) | set(code.co_varnames) |
                            set(code.co_freevars) | set(code.co_cellvars) for code in codes))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            occupied.add(node.id)
        elif isinstance(node, ast.arg):
            occupied.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            occupied.add(node.name)
        elif isinstance(node, ast.alias):
            occupied.add(node.asname or node.name.split('.')[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            occupied.add(node.name)
    forbidden = set()
    graph = defaultdict(set)
    for code in codes:
        parameter_count = code.co_argcount + code.co_kwonlyargcount
        parameter_count += bool(code.co_flags & 4) + bool(code.co_flags & 8)
        parameters = code.co_varnames[:parameter_count]
        forbidden.update(code.co_names)
        forbidden.update(parameters)
        for sequence in (code.co_freevars,
                         tuple(name for name in code.co_cellvars if name not in parameters)):
            for name in sequence:
                graph[name]
            for left, right in zip(sequence, sequence[1:]):
                graph[left].add(right)
    private = {name for name in graph if name.startswith('_var_var_') and name not in forbidden}
    if not private:
        return tree, []
    fixed = sorted(set(graph) - private)
    for left, right in zip(fixed, fixed[1:]):
        graph[left].add(right)
    incoming = {name: 0 for name in graph}
    for targets in graph.values():
        for name in targets:
            incoming[name] += 1
    queue = [(name not in private, name) for name, count in incoming.items() if count == 0]
    heapq.heapify(queue)
    ordered = []
    while queue:
        _, name = heapq.heappop(queue)
        ordered.append(name)
        for target in graph[name]:
            incoming[target] -= 1
            if incoming[target] == 0:
                heapq.heappush(queue, (target not in private, target))
    if len(ordered) != len(graph):
        return tree, []
    names = {}
    previous = None
    anchor = None
    for index, name in enumerate(ordered):
        if name not in private:
            previous = anchor = name
            continue
        next_fixed = next((other for other in ordered[index + 1:] if other not in private), None)
        if anchor is None:
            candidates = [f'A_recovered_capture_{index:04d}', f'_recovered_capture_{index:04d}',
                          f'a_recovered_capture_{index:04d}']
        else:
            candidates = [f'{anchor}__recovered_capture_{index:04d}',
                          f'{anchor}0_recovered_capture_{index:04d}']
        chosen = next((value for value in candidates if value not in occupied
                       and (previous is None or previous < value)
                       and (next_fixed is None or value < next_fixed)), None)
        if chosen is None:
            return tree, []
        names[name] = chosen
        occupied.add(chosen)
        previous = chosen

    class Rename(ast.NodeTransformer):
        def visit_Name(self, node):
            node.id = names.get(node.id, node.id)
            return node
        def visit_ExceptHandler(self, node):
            node.name = names.get(node.name, node.name)
            return self.generic_visit(node)
        def visit_alias(self, node):
            node.asname = names.get(node.asname, node.asname)
            return node
        def visit_Nonlocal(self, node):
            node.names = [names.get(name, name) for name in node.names]
            return node

    # Parse a new tree rather than mutating the original before proof is known.
    trial = Rename().visit(ast.parse(ast.unparse(tree)))
    after = check(trial)
    old_matches = {entry['unit'] for entry in before['entries'] if entry['matched']}
    new_matches = {entry['unit'] for entry in after['entries'] if entry['matched']}
    if not old_matches.issubset(new_matches) or not new_matches - old_matches:
        return tree, []
    return trial, sorted(new_matches - old_matches)


def _prove_function_edits(tree, reference, decoder, edit):
    """Accept scoped AST edits only after complete unit and retention proof.

    The caller returns attribute undo records, without traversing nested code
    scopes. A failed neighbor is rolled back before the final whole-tree
    comparison; a matched child can never be sacrificed for its parent.
    """
    try:
        from .bytecode_fidelity import compare, compile_source, units
    except ImportError:
        from bytecode_fidelity import compare, compile_source, units

    def checked():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            compiled = compile_source(ast.unparse(ast.fix_missing_locations(tree)), '<scoped-edit-trial>')
        matches = {entry['unit'] for entry in compare(reference, compiled, decoder)['entries']
                   if entry['matched']}
        return compiled, matches

    tree = ast.parse(ast.unparse(ast.fix_missing_locations(tree)))
    definitions = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            line = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            definitions[node.name, line].append(node)
    compiled, before = checked()
    attempted = {}
    for path, code in units(compiled):
        owners = definitions[code.co_name, code.co_firstlineno]
        if path in before or len(owners) != 1:
            continue
        records = edit(owners[0])
        if records:
            attempted[path] = records
    if not attempted:
        return tree, []
    def undo(records):
        for node, field, previous in reversed(records):
            setattr(node, field, previous)
    try:
        _, tentative = checked()
        selected = {}
        for path, records in attempted.items():
            if path in tentative:
                selected[path] = records
            else:
                undo(records)
        _, after = checked()
        if not (before | selected.keys()).issubset(after):
            for records in reversed(list(selected.values())):
                undo(records)
            return tree, []
        return tree, sorted(selected)
    except (SyntaxError, ValueError):
        for records in reversed(list(attempted.values())):
            undo(records)
        return tree, []


def _function_nodes(owner):
    """Walk only this function's code scope, including inline comprehensions."""
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.GeneratorExp)
    for child in ast.iter_child_nodes(owner):
        if not isinstance(child, scopes):
            yield child
            yield from _function_nodes(child)


def _statement_suites(owner):
    """Yield statement lists in one code scope, including handler/else suites."""
    for node in (owner, *_function_nodes(owner)):
        for field, value in ast.iter_fields(node):
            if isinstance(value, list) and value and all(isinstance(item, ast.stmt) for item in value):
                yield node, field, value


def _fold_value(value, depth):
    value = value if value is not None else ast.Constant(None)
    for _ in range(depth):
        value = ast.IfExp(ast.Constant(True), value, ast.Constant(None))
    return value


def _fold_return_statements(owner, depth, selected=None):
    """Retain statement return edges without moving their values into a try."""
    returns = [node for node in _function_nodes(owner) if isinstance(node, ast.Return)]
    wanted = {id(node) for index, node in enumerate(returns) if selected is None or index == selected}
    for parent, field, suite in reversed(list(_statement_suites(owner))):
        replacement = []
        for statement in suite:
            if id(statement) in wanted:
                for _ in range(depth):
                    statement = ast.If(ast.Constant(True), [statement], [])
            replacement.append(statement)
        setattr(parent, field, replacement)


def _prove_function_trials(tree, reference, decoder, trials):
    """Prove bounded single-scope shape trials, then retain the whole module.

    Only source shapes change. Each accepted owner and every nested unit must
    match the original instructions AND exception dispatch. Failed trials are
    restored before the next one; a final module comparison protects matching
    neighbors and capture users. The comparator's rules are never relaxed.
    """
    try:
        from .bytecode_fidelity import compare, compile_source, units
    except ImportError:
        from bytecode_fidelity import compare, compile_source, units

    tree = ast.parse(ast.unparse(ast.fix_missing_locations(tree)))
    def compiled():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            # Compilation accepts an AST directly. Retain its scope and
            # location context rather than repeatedly unparsing an unchanged
            # large module for each bounded trial. Proof rules are identical.
            return compile_source(ast.fix_missing_locations(tree), '<shape-trial>')

    initial = compiled()
    before = {entry['unit'] for entry in compare(reference, initial, decoder)['entries'] if entry['matched']}
    references = dict(units(normalize_code(reference)))
    definitions = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            line = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            definitions[node.name, line].append(node)
    accepted = []
    undo = []
    settled = set(before)
    for path, code in units(initial):
        owners = definitions[code.co_name, code.co_firstlineno]
        if path in settled or path not in references or len(owners) != 1:
            continue
        owner = owners[0]
        original_body = owner.body
        seen = set()
        try:
            for label, body in trials(owner, references[path], code):
                fingerprint = ast.dump(ast.Module(body=body, type_ignores=[]))
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                owner.body = body
                try:
                    candidate = dict(units(compiled())).get(path)
                    if candidate is not None and compare(references[path], candidate, decoder)['all_units_match']:
                        undo.append((owner, original_body))
                        accepted.append({'unit': path, 'shape': label})
                        settled.update(unit_path for unit_path, _ in units(candidate, path))
                        break
                except (SyntaxError, ValueError):
                    pass
                finally:
                    if not accepted or accepted[-1]['unit'] != path:
                        owner.body = original_body
        except BaseException:
            owner.body = original_body
            for changed, body in reversed(undo):
                changed.body = body
            raise
    if not accepted:
        return tree, []
    after = {entry['unit'] for entry in compare(reference, compiled(), decoder)['entries'] if entry['matched']}
    if not (before | {entry['unit'] for entry in accepted}).issubset(after):
        for owner, body in reversed(undo):
            owner.body = body
        return tree, []
    return tree, accepted


def restore_context_boundaries(tree, reference, decoder):
    """Close an async context before a swallowed suffix, only by exact proof.

    The original dispatch map decides which work is protected. No operation,
    predicate, or value is introduced, and genuine transaction bodies remain
    inside their contexts. Bounded suffix trials also retain undefined-local
    behavior when a context suppresses failure during setup.
    """
    def trials(owner, original, candidate):
        contexts = [(suite_index, index, len(statement.body))
                    for suite_index, (_, _, suite) in enumerate(_statement_suites(owner))
                    for index, statement in enumerate(suite)
                    if isinstance(statement, ast.AsyncWith) and len(statement.body) > 1]
        for suite_index, index, length in contexts:
            for cut in range(max(1, length - 16), length):
                clone = copy.deepcopy(owner)
                parent, field, suite = list(_statement_suites(clone))[suite_index]
                context = suite[index]
                suffix = context.body[cut:]
                context.body = context.body[:cut]
                setattr(parent, field, suite[:index + 1] + suffix + suite[index + 1:])
                yield f'async-context-suffix-{cut}', clone.body
    if not any(isinstance(node, ast.AsyncWith) and len(node.body) > 1 for node in ast.walk(tree)):
        return tree, []
    return _prove_function_trials(tree, reference, decoder, trials)


def preserve_individual_return_edges(tree, reference, decoder):
    """Prove individual shared returns and legal async-generator exit shapes."""
    try:
        from .bytecode_fidelity import canonical
    except ImportError:
        from bytecode_fidelity import canonical

    def trials(owner, original, candidate):
        if candidate.co_flags & 0x200:
            for callable_values in (False, True):
                clone = copy.deepcopy(owner)
                clone.body.append(ast.If(ast.Constant(True), [ast.Return()], []))
                if callable_values:
                    instructions = canonical(original, decoder).instructions
                    needed = {item[1][0] for item, following in zip(instructions, instructions[1:])
                              if item[0] == 'LOAD_ATTR' and item[1][1] == 0 and following[0] == 'PUSH_NULL'}
                    changed = False
                    for node in _function_nodes(clone):
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in needed:
                            node.func = ast.IfExp(ast.Constant(True), node.func, ast.Constant(None))
                            changed = True
                    if not changed:
                        continue
                yield 'async-generator-bare-exit' + ('-callable-values' if callable_values else ''), clone.body
            return
        returns = [node for node in _function_nodes(owner) if isinstance(node, ast.Return)]
        for index, _ in enumerate(returns):
            for depth in range(1, 5):
                clone = copy.deepcopy(owner)
                statement = [node for node in _function_nodes(clone) if isinstance(node, ast.Return)][index]
                statement.value = _fold_value(statement.value, depth)
                yield f'individual-return-{index}-fold-{depth}', clone.body
            for depth in (1, 2):
                clone = copy.deepcopy(owner)
                _fold_return_statements(clone, depth, index)
                yield f'individual-statement-return-{index}-fold-{depth}', clone.body
        for depth in (1, 2):
            clone = copy.deepcopy(owner)
            clone.body.append(ast.Return(_fold_value(None, depth)))
            yield f'shared-implicit-none-{depth}', clone.body
        # The false-arm form can preserve a shared dict-return edge that the
        # true-arm form duplicates. Its value and evaluation order are unchanged.
        for index, statement in enumerate(returns):
            if not isinstance(statement.value, ast.Dict):
                continue
            for implicit in (False, True):
                clone = copy.deepcopy(owner)
                returned = [node for node in _function_nodes(clone) if isinstance(node, ast.Return)][index]
                returned.value = ast.IfExp(ast.Constant(False), ast.Constant(None), returned.value)
                if implicit:
                    clone.body.append(ast.Return(_fold_value(None, 1)))
                yield f'false-arm-dict-return-{index}-implicit-{implicit}', clone.body
    return _prove_function_trials(tree, reference, decoder, trials)


def restore_coupled_statement_shapes(tree, reference, decoder):
    """Reuse existing statements in bounded structural/return-edge trials.

    Context, handler and value-expression reconstruction can need two source
    changes together. Neither partial trial is accepted. Shapes below only
    reuse existing tests/work and implicit None; full unit proof selects the
    original short circuit, return scope and dispatch, including NaN behavior.
    """
    def structural(owner):
        loop_owned = {id(node) for loop in _function_nodes(owner)
                      if isinstance(loop, (ast.For, ast.AsyncFor, ast.While))
                      for node in _function_nodes(loop)}
        for suite_index, (parent, field, suite) in enumerate(_statement_suites(owner)):
            for index, statement in enumerate(suite):
                if (isinstance(statement, ast.Try) and not statement.finalbody
                        and index + 1 < len(suite)):
                    # Existing trailing work can be an omitted finally copy.
                    # Reuse that exact operation, including handler copies;
                    # the original dispatch map must prove guaranteed cleanup.
                    cleanup = suite[index + 1]
                    if isinstance(cleanup, (ast.Assign, ast.Expr)):
                        clone = copy.deepcopy(owner)
                        target, target_field, block = list(_statement_suites(clone))[suite_index]
                        region = block[index]
                        region.finalbody = [block[index + 1]]
                        fingerprint = ast.dump(block[index + 1])
                        for handler in region.handlers:
                            handler.body = [n for n in handler.body if ast.dump(n) != fingerprint] or [ast.Pass()]
                        setattr(target, target_field, block[:index + 1] + block[index + 2:])
                        yield 'existing-trailing-finally', clone
                    if statement.handlers and not statement.orelse and len(suite) - index - 1 <= 8:
                        clone = copy.deepcopy(owner)
                        target, target_field, block = list(_statement_suites(clone))[suite_index]
                        region = block[index]
                        region.body = [n for n in region.body if not isinstance(n, ast.Pass)] + block[index + 1:]
                        setattr(target, target_field, block[:index + 1])
                        yield 'hoisted-protected-suffix', clone
                if isinstance(statement, ast.Try) and index + 1 < len(suite) and isinstance(suite[index + 1], ast.Return):
                    clone = copy.deepcopy(owner)
                    target, target_field, block = list(_statement_suites(clone))[suite_index]
                    block[index].body = [n for n in block[index].body if not isinstance(n, ast.Pass)] + [block[index + 1]]
                    setattr(target, target_field, block[:index + 1] + block[index + 2:])
                    yield 'one-try-return-scope', clone
                if isinstance(statement, ast.If) and not statement.orelse:
                    if len(statement.body) == 1 and isinstance(statement.body[0], ast.If) and not statement.body[0].orelse:
                        inner = statement.body[0]
                        if inner.body and (all(isinstance(n, (ast.Return, ast.Raise)) for n in inner.body)
                                           or (id(statement) in loop_owned and isinstance(inner.body[-1], ast.Continue))):
                            clone = copy.deepcopy(owner)
                            branch = list(_statement_suites(clone))[suite_index][2][index]
                            branch.test = ast.BoolOp(ast.Or(), [ast.UnaryOp(ast.Not(), branch.test), branch.body[0].test])
                            branch.body = branch.body[0].body
                            yield 'short-circuit-rejection-return', clone
                    if id(statement) in loop_owned and not all(isinstance(n, ast.Continue) for n in statement.body):
                        clone = copy.deepcopy(owner)
                        target, target_field, block = list(_statement_suites(clone))[suite_index]
                        branch = block[index]
                        setattr(target, target_field, block[:index] + [ast.If(ast.UnaryOp(ast.Not(), branch.test), [ast.Continue()], [])]
                                + branch.body + block[index + 1:])
                        yield 'loop-rejection-suffix', clone
                    if index + 1 < len(suite) and isinstance(suite[index + 1], ast.Return):
                        tail = suite[index + 1]
                        replacement = None
                        if len(statement.body) == 1 and isinstance(statement.body[0], ast.Return):
                            replacement = ast.IfExp(statement.test, statement.body[0].value or ast.Constant(None), tail.value or ast.Constant(None))
                        elif (len(statement.body) == 1 and isinstance(statement.body[0], ast.Pass)
                              and isinstance(tail.value, ast.IfExp)):
                            value = tail.value
                            replacement = ast.IfExp(ast.BoolOp(ast.And(), [statement.test, value.test]), value.body, value.orelse)
                        if replacement is not None:
                            clone = copy.deepcopy(owner)
                            target, target_field, block = list(_statement_suites(clone))[suite_index]
                            setattr(target, target_field, block[:index] + [ast.Return(copy.deepcopy(replacement))] + block[index + 2:])
                            yield 'compound-conditional-return', clone
                    if (isinstance(parent, ast.Try) and field == 'orelse' and len(suite) == 1
                            and len(statement.body) == 1 and isinstance(statement.body[0], ast.Return)):
                        clone = copy.deepcopy(owner)
                        target, target_field, block = list(_statement_suites(clone))[suite_index]
                        branch = block[0]
                        setattr(target, target_field, [ast.Return(ast.IfExp(branch.test, branch.body[0].value or ast.Constant(None), ast.Constant(None)))])
                        yield 'try-else-conditional-return', clone
        comprehensions = [node for node in _function_nodes(owner) if isinstance(node, ast.comprehension) and len(node.ifs) > 1]
        for index in range(len(comprehensions)):
            clone = copy.deepcopy(owner)
            node = [n for n in _function_nodes(clone) if isinstance(n, ast.comprehension) and len(n.ifs) > 1][index]
            node.ifs = [ast.BoolOp(ast.Or(), node.ifs)]
            yield 'comprehension-or-filter', clone
        # Merging AND guards can be coupled to a shared implicit return.
        clone = copy.deepcopy(owner)
        changed = False
        for node in _function_nodes(clone):
            if (isinstance(node, ast.If) and not node.orelse and len(node.body) == 1
                    and isinstance(node.body[0], ast.If) and not node.body[0].orelse):
                inner = node.body[0]
                node.test = ast.BoolOp(ast.And(), [node.test, inner.test])
                node.body = inner.body
                changed = True
        if changed:
            yield 'nested-and-guards', clone

    def trials(owner, original, candidate):
        if candidate.co_flags & 0x200:
            return
        for label, clone in structural(owner):
            for depth, implicit in ((0, False), (1, False), (2, False), (3, False),
                                    (4, False), (0, True), (1, True), (2, True)):
                trial = copy.deepcopy(clone)
                if depth:
                    for node in _function_nodes(trial):
                        if isinstance(node, ast.Return):
                            node.value = _fold_value(node.value, depth)
                if implicit:
                    trial.body.append(ast.Return(_fold_value(None, 1)))
                yield f'{label}-fold-{depth}-implicit-{implicit}', trial.body
            returns = [node for node in _function_nodes(clone) if isinstance(node, ast.Return)]
            if len(returns) <= 8:
                for index in range(len(returns)):
                    for statement_form in (False, True):
                        trial = copy.deepcopy(clone)
                        if statement_form:
                            _fold_return_statements(trial, 1, index)
                        else:
                            returned = [node for node in _function_nodes(trial) if isinstance(node, ast.Return)][index]
                            returned.value = _fold_value(returned.value, 1)
                        yield f'{label}-single-return-{index}-statement-{statement_form}', trial.body
            for depth in (1, 2):
                trial = copy.deepcopy(clone)
                _fold_return_statements(trial, depth)
                yield f'{label}-all-statement-return-fold-{depth}', trial.body
    return _prove_function_trials(tree, reference, decoder, trials)



def restore_residual_statement_shapes(tree, reference, decoder):
    """Prove coupled handler, predicate and terminal shapes from existing work.

    No business expression or exception type is invented. Structural trials
    reuse each scope's AST, while constant return edges and implicit None only
    retain compiler control-flow forms. A complete owner/child comparison and
    the final module retention gate arbitrate every bounded trial.
    """
    def same_handlers(region):
        if not isinstance(region, ast.Try) or not region.handlers or region.orelse or region.finalbody:
            return None
        return ast.dump(ast.Module(body=region.handlers, type_ignores=[]))

    def handler_continues(owner):
        loop_owned = {id(node) for loop in _function_nodes(owner)
                      if isinstance(loop, (ast.For, ast.AsyncFor, ast.While))
                      for node in _function_nodes(loop)}
        handlers = [node for node in _function_nodes(owner)
                    if isinstance(node, ast.ExceptHandler) and id(node) in loop_owned
                    and node.body and not isinstance(node.body[-1],
                        (ast.Return, ast.Raise, ast.Break, ast.Continue))]
        if len(handlers) > 4:
            return
        for mask in range(1, 1 << len(handlers)):
            trial = copy.deepcopy(owner)
            all_handlers = [node for node in _function_nodes(trial) if isinstance(node, ast.ExceptHandler)]
            original_handlers = [node for node in _function_nodes(owner) if isinstance(node, ast.ExceptHandler)]
            for index, handler in enumerate(handlers):
                if mask & (1 << index):
                    all_handlers[original_handlers.index(handler)].body.append(ast.Continue())
            yield mask, trial

    def shapes(owner):
        # Reunite all adjacent fragments with identical lexical handlers.
        # Existing independently authored tries are protected by full proof.
        merged = copy.deepcopy(owner)
        changed = False
        for _, _, suite in reversed(list(_statement_suites(merged))):
            index = len(suite) - 1
            while index > 0:
                key = same_handlers(suite[index])
                if key is not None and key == same_handlers(suite[index - 1]):
                    suite[index - 1].body.extend(suite[index].body)
                    del suite[index]
                    changed = True
                index -= 1
        if changed:
            yield 'merged-handler-fragments', merged
            for mask, trial in handler_continues(merged):
                yield f'merged-handler-loop-continue-{mask}', trial
            if len(merged.body) > 1 and isinstance(merged.body[-1], ast.Return) and isinstance(merged.body[-2], ast.Try):
                trial = copy.deepcopy(merged)
                trial.body[-2].body.append(trial.body.pop())
                yield 'merged-handler-protected-return', trial
            trial = copy.deepcopy(merged)
            removed = False
            for _, _, suite in _statement_suites(trial):
                for statement in suite:
                    if isinstance(statement, ast.Try) and statement.body and isinstance(statement.body[-1], ast.Return) and statement.body[-1].value is None:
                        statement.body.pop()
                        statement.body = statement.body or [ast.Pass()]
                        removed = True
            if removed:
                yield 'merged-handler-without-bare-exit', trial

        for suite_index, (parent, field, suite) in enumerate(_statement_suites(owner)):
            for index, statement in enumerate(suite):
                if isinstance(statement, ast.Try):
                    if (same_handlers(statement) is not None and index + 2 < len(suite)
                            and same_handlers(suite[index + 1]) is not None
                            and isinstance(suite[index + 2], ast.Return)
                            and len(statement.handlers) == len(suite[index + 1].handlers)):
                        returned = suite[index + 2]
                        pairs = zip(statement.handlers, suite[index + 1].handlers)
                        missing_terminal = all(
                            (ast.dump(first.type) if first.type is not None else None)
                                == (ast.dump(second.type) if second.type is not None else None)
                            and first.name == second.name
                            and first.body and isinstance(first.body[-1], ast.Return)
                            and ast.dump(first.body[-1]) == ast.dump(returned)
                            and ast.dump(ast.Module(body=first.body[:-1], type_ignores=[]))
                                == ast.dump(ast.Module(body=second.body, type_ignores=[]))
                            for first, second in pairs)
                        if missing_terminal:
                            trial = copy.deepcopy(owner)
                            target, name, block = list(_statement_suites(trial))[suite_index]
                            region = block[index]
                            region.body.extend(block[index + 1].body)
                            region.body.append(block[index + 2])
                            setattr(target, name, block[:index + 1] + block[index + 3:])
                            yield 'handler-prefix-with-existing-terminal-return', trial
                    if (statement.handlers and not statement.finalbody and not statement.orelse
                            and index + 1 < len(suite) and isinstance(suite[index + 1], ast.Return)):
                        trial = copy.deepcopy(owner)
                        target, name, block = list(_statement_suites(trial))[suite_index]
                        region, returned = block[index], block[index + 1]
                        region.body = [node for node in region.body if not isinstance(node, ast.Pass)] + [copy.deepcopy(returned)]
                        for handler in region.handlers:
                            handler.body = [node for node in handler.body if not isinstance(node, ast.Pass)] + [copy.deepcopy(returned)]
                        setattr(target, name, block[:index + 1] + block[index + 2:])
                        yield 'try-and-handler-result-return', trial
                    if (statement.orelse and not statement.finalbody and statement.handlers
                            and all(handler.body and isinstance(handler.body[-1], (ast.Return, ast.Raise)) for handler in statement.handlers)):
                        trial = copy.deepcopy(owner)
                        target, name, block = list(_statement_suites(trial))[suite_index]
                        region = block[index]
                        suffix = region.orelse
                        region.orelse = []
                        setattr(target, name, block[:index + 1] + suffix + block[index + 1:])
                        yield 'exiting-handler-success-suffix', trial
                    if (not statement.orelse and not statement.finalbody
                            and all(isinstance(node, ast.Pass) for node in statement.body)
                            and all(all(isinstance(node, ast.Pass) for node in handler.body) for handler in statement.handlers)):
                        trial = copy.deepcopy(owner)
                        target, name, block = list(_statement_suites(trial))[suite_index]
                        setattr(target, name, block[:index] + block[index + 1:] or [ast.Pass()])
                        yield 'empty-handler-artifact', trial
                if not isinstance(statement, ast.If) or statement.orelse:
                    continue
                # Lost positive predicates can precede their rejection guard
                # or the conditional value assignment they used to constrain.
                end = index
                predicates = []
                while end < len(suite) and isinstance(suite[end], ast.If) and not suite[end].orelse and len(suite[end].body) == 1 and isinstance(suite[end].body[0], ast.Pass):
                    predicates.append(suite[end].test)
                    end += 1
                if predicates and end < len(suite):
                    following = suite[end]
                    if isinstance(following, ast.If) and not following.orelse:
                        trial = copy.deepcopy(owner)
                        target, name, block = list(_statement_suites(trial))[suite_index]
                        branch = block[end]
                        branch.test = ast.BoolOp(ast.Or(), [ast.UnaryOp(ast.Not(), copy.deepcopy(test)) for test in predicates] + [branch.test])
                        setattr(target, name, block[:index] + [branch] + block[end + 1:])
                        yield 'empty-positive-rejection-prefix', trial
                    if isinstance(following, (ast.Assign, ast.AnnAssign)) and isinstance(following.value, ast.IfExp):
                        trial = copy.deepcopy(owner)
                        target, name, block = list(_statement_suites(trial))[suite_index]
                        assignment = block[end]
                        assignment.value.test = ast.BoolOp(ast.And(), copy.deepcopy(predicates) + [assignment.value.test])
                        setattr(target, name, block[:index] + [assignment] + block[end + 1:])
                        yield 'empty-positive-conditional-value', trial
                if len(statement.body) == 1 and isinstance(statement.body[0], ast.If):
                    inner = statement.body[0]
                    if inner.orelse:
                        trial = copy.deepcopy(owner)
                        branch = list(_statement_suites(trial))[suite_index][2][index]
                        branch.test = ast.BoolOp(ast.And(), [branch.test, branch.body[0].test])
                        branch.orelse = branch.body[0].orelse
                        branch.body = branch.body[0].body
                        yield 'nested-acceptance-with-existing-else', trial
                    elif inner.body:
                        # Optional positive filters reject only when supplied
                        # AND their inner acceptance fails; keep unfiltered work.
                        trial = copy.deepcopy(owner)
                        target, name, block = list(_statement_suites(trial))[suite_index]
                        branch = block[index]
                        rejection = ast.If(ast.BoolOp(ast.And(), [branch.test, ast.UnaryOp(ast.Not(), branch.body[0].test)]), [ast.Continue()], [])
                        setattr(target, name, block[:index] + [rejection] + branch.body[0].body + block[index + 1:])
                        yield 'optional-loop-filter-prefix', trial
                        # Rebuild the original comparison chain; proving it
                        # rejects independently evaluated middle expressions.
                        if (isinstance(statement.test, ast.Compare) and len(statement.test.ops) == 1
                                and isinstance(inner.test, ast.UnaryOp) and isinstance(inner.test.op, ast.Not)
                                and isinstance(inner.test.operand, ast.Compare) and len(inner.test.operand.ops) == 1
                                and ast.dump(statement.test.comparators[0]) == ast.dump(inner.test.operand.left)):
                            trial = copy.deepcopy(owner)
                            branch = list(_statement_suites(trial))[suite_index][2][index]
                            second = branch.body[0].test.operand
                            chain = ast.Compare(branch.test.left, branch.test.ops + second.ops, branch.test.comparators + second.comparators)
                            branch.test = ast.UnaryOp(ast.Not(), chain)
                            branch.body = branch.body[0].body
                            yield 'original-negated-comparison-chain', trial

        for mask, trial in handler_continues(owner):
            yield f'existing-loop-handler-continue-{mask}', trial

    def trials(owner, original, candidate):
        if candidate.co_flags & 0x200:
            return
        for label, base in shapes(owner):
            for fold, tail in ((0, 'none'), (1, 'none'), (2, 'none'),
                               (0, 'implicit'), (1, 'implicit'),
                               (0, 'statement'), (1, 'statement')):
                trial = copy.deepcopy(base)
                if fold:
                    for node in _function_nodes(trial):
                        if isinstance(node, ast.Return): node.value = _fold_value(node.value, fold)
                if tail == 'implicit': trial.body.append(ast.Return(_fold_value(None, 1)))
                elif tail == 'statement': trial.body.append(ast.If(ast.Constant(True), [ast.Return()], []))
                yield f'{label}-fold-{fold}-tail-{tail}', trial.body
            trial = copy.deepcopy(base)
            _fold_return_statements(trial, 1)
            yield label + '-statement-return-edges', trial.body
        # Some shared exit tails need a single existing bare return wrapped
        # together with the corresponding implicit statement return outside.
        returns = [node for node in _function_nodes(owner) if isinstance(node, ast.Return)]
        for index, returned in enumerate(returns):
            if returned.value is not None: continue
            trial = copy.deepcopy(owner)
            _fold_return_statements(trial, 1, index)
            trial.body.append(ast.If(ast.Constant(True), [ast.Return()], []))
            yield f'bare-return-{index}-and-shared-statement-tail', trial.body
    return _prove_function_trials(tree, reference, decoder, trials)


def restore_expanded_keyword_merges(tree, reference, decoder):
    """Recover CALL_FUNCTION_EX merge boundaries without losing duplicate checks.

    A call with several keyword expansions can be emitted as one dictionary
    literal. DICT_UPDATE in that literal overwrites duplicates, whereas the
    original call's DICT_MERGE raises TypeError. Reuse its existing ordered
    expressions and prove the whole unit before splitting the literal.
    Genuine dictionary-literal arguments keep their original shape.
    """
    def eligible(node):
        return (isinstance(node, ast.Call) and any(
            keyword.arg is None and isinstance(keyword.value, ast.Dict)
            and any(key is None for key in keyword.value.keys)
            for keyword in node.keywords))
    if not any(eligible(node) for node in ast.walk(tree)):
        return tree, []
    def edit(owner):
        records = []
        for node in _function_nodes(owner):
            if not eligible(node):
                continue
            expanded = []
            for argument in node.keywords:
                value = argument.value
                if argument.arg is not None or not isinstance(value, ast.Dict) or not any(key is None for key in value.keys):
                    expanded.append(argument)
                    continue
                named = set()
                for key, expression in zip(value.keys, value.values):
                    if key is None:
                        expanded.append(ast.keyword(None, expression))
                    elif (isinstance(key, ast.Constant) and isinstance(key.value, str)
                          and key.value.isidentifier() and not keyword.iskeyword(key.value)
                          and key.value not in named):
                        expanded.append(ast.keyword(key.value, expression))
                        named.add(key.value)
                    else:
                        expanded.append(ast.keyword(None, ast.Dict([key], [expression])))
            # Duplicate explicit names would be invalid source. Keep this
            # candidate untouched rather than inventing grouping boundaries.
            names = [keyword.arg for keyword in expanded if keyword.arg is not None]
            if len(names) != len(set(names)):
                continue
            records.append((node, 'keywords', node.keywords))
            node.keywords = expanded
        return records
    return _prove_function_edits(tree, reference, decoder, edit)


def restore_chained_comprehension_filters(tree, reference, decoder):
    """Rejoin split comparison filters only with exact evaluation proof."""
    def edit(owner):
        records = []
        for node in _function_nodes(owner):
            if not isinstance(node, ast.comprehension):
                continue
            filters = list(node.ifs)
            for index in range(len(filters) - 2, -1, -1):
                left, right = filters[index:index + 2]
                if (isinstance(left, ast.Compare) and isinstance(right, ast.Compare)
                        and ast.dump(left.comparators[-1]) == ast.dump(right.left)):
                    filters[index:index + 2] = [ast.Compare(
                        left.left, left.ops + right.ops, left.comparators + right.comparators)]
            if len(filters) != len(node.ifs):
                records.append((node, 'ifs', node.ifs))
                node.ifs = filters
        return records
    if not any(isinstance(node, ast.comprehension) and len(node.ifs) > 1 for node in ast.walk(tree)):
        return tree, []
    return _prove_function_edits(tree, reference, decoder, edit)


def restore_loop_rejection_guards(tree, reference, decoder):
    """Recover an inverted continue guard, reusing its existing predicate/body.

    No new predicate or work is supplied. Continuations owned by nested loops
    or definitions do not qualify; the exact instruction and dispatch proof
    decides whether later statements belonged outside the accepting branch.
    """
    def has_continue(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.For, ast.AsyncFor, ast.While, ast.FunctionDef,
                                  ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Continue) or has_continue(child):
                return True
        return False
    def edit(owner):
        records = []
        def visit(node, in_loop=False):
            for _, block in ast.iter_fields(node):
                if not isinstance(block, list) or not block or not isinstance(block[0], ast.stmt):
                    continue
                previous = list(block)
                changed = []
                for statement in previous:
                    nested = in_loop or isinstance(statement, (ast.For, ast.AsyncFor, ast.While))
                    if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        visit(statement, nested)
                    if (in_loop and isinstance(statement, ast.If) and not statement.orelse
                            and not all(isinstance(item, ast.Continue) for item in statement.body)
                            and has_continue(statement)):
                        changed.append(ast.If(ast.UnaryOp(ast.Not(), statement.test), [ast.Continue()], []))
                        changed.extend(statement.body)
                    else:
                        changed.append(statement)
                if changed != previous:
                    # Store the owning field rather than mutating the old
                    # list: nested-suite undo remains independent.
                    for field, value in ast.iter_fields(node):
                        if value is block:
                            records.append((node, field, block))
                            setattr(node, field, changed)
                            break
        visit(owner)
        return records
    if not any(isinstance(node, ast.Continue) for node in ast.walk(tree)):
        return tree, []
    return _prove_function_edits(tree, reference, decoder, edit)


def restore_compound_return_shapes(tree, reference, decoder):
    """Restore a return expression only when its entire code unit is proven.

    Structured decompilation can split a conditional value into two returns or
    leave a short-circuit arm as empty nested if statements. Reuse the existing
    tests and values; never infer a missing literal, call or business predicate.
    """
    try:
        from .bytecode_fidelity import compare, compile_source, units
    except ImportError:
        from bytecode_fidelity import compare, compile_source, units

    class Definitions(ast.NodeVisitor):
        def __init__(self):
            self.prefix = ''
            self.nodes = defaultdict(list)
        def visit_ClassDef(self, node):
            old = self.prefix
            self.prefix += node.name + '.'
            self.generic_visit(node)
            self.prefix = old
        def visit_FunctionDef(self, node):
            name = self.prefix + node.name
            self.nodes[name].append(node)
            old = self.prefix
            self.prefix = name + '.<locals>.'
            self.generic_visit(node)
            self.prefix = old
        visit_AsyncFunctionDef = visit_FunctionDef

    def check():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            candidate = compile_source(ast.unparse(ast.fix_missing_locations(tree)),
                                       '<compound-return-candidate>')
        return candidate, compare(reference, candidate, decoder)

    def replacement(body, shape):
        if (len(body) < 2 or not isinstance(body[-1], ast.Return)
                or not isinstance(body[-2], ast.If)):
            return None
        branch, tail = body[-2:]
        if branch.orelse:
            return None
        value = tail.value or ast.Constant(None)
        if shape == 'conditional':
            if len(branch.body) != 1 or not isinstance(branch.body[0], ast.Return):
                return None
            return ast.Return(ast.IfExp(branch.test, branch.body[0].value or ast.Constant(None), value))
        conditions = []
        node = branch
        while isinstance(node, ast.If) and not node.orelse and len(node.body) == 1:
            conditions.append(node.test)
            node = node.body[0]
        if not conditions or not isinstance(node, ast.Pass):
            return None
        condition = conditions[0] if len(conditions) == 1 else ast.BoolOp(ast.And(), conditions)
        values = [condition] + (value.values if isinstance(value, ast.BoolOp)
                                and isinstance(value.op, ast.Or) else [value])
        return ast.Return(ast.BoolOp(ast.Or(), values))

    accepted = []
    for shape in ('conditional', 'or'):
        candidate, baseline = check()
        matching = {entry['unit'] for entry in baseline['entries'] if entry['matched']}
        by_name = defaultdict(list)
        for path, code in units(candidate):
            by_name[code.co_qualname].append((path, code))
        definitions = Definitions()
        definitions.visit(tree)
        attempted = {}
        for name, nodes in definitions.nodes.items():
            if len(nodes) != 1 or len(by_name[name]) != 1:
                continue
            path, code = by_name[name][0]
            if path in matching or code.co_flags & 0x200:
                continue
            node = nodes[0]
            statement = replacement(node.body, shape)
            if statement is None:
                continue
            original = node.body[-2:]
            node.body[-2:] = [statement]
            attempted[path] = (node, original)
        if not attempted:
            continue
        _, tentative = check()
        tested = {entry['unit'] for entry in tentative['entries'] if entry['matched']}
        selected = {}
        for path, (node, original) in attempted.items():
            if path in tested:
                selected[path] = (node, original)
            else:
                node.body[-1:] = original
        _, final = check()
        after = {entry['unit'] for entry in final['entries'] if entry['matched']}
        if not matching.issubset(after) or not set(selected).issubset(after):
            for node, original in selected.values():
                node.body[-1:] = original
        else:
            accepted.extend({'unit': path, 'shape': shape} for path in sorted(selected))
    return tree, accepted


def restore_shared_try_fragments(tree, reference, decoder):
    """Reunite duplicate lexical handlers only after complete unit proof.

    Early returns split an exception table's protected body. A structured
    backend can emit those fragments as consecutive tries with copied
    handlers. Identical source handlers alone do not establish equivalence:
    a trial must match the original instructions and dispatch map and retain
    every previously matched unit. Else/finally suites and intervening work
    are not candidates for reunion. A reunited body can also need its existing
    final return moved back under the handler; prove those shapes together.
    """
    try:
        from .bytecode_fidelity import compare, compile_source, units
    except ImportError:
        from bytecode_fidelity import compare, compile_source, units

    class Definitions(ast.NodeVisitor):
        def __init__(self):
            self.prefix = ''
            self.nodes = defaultdict(list)
        def visit_ClassDef(self, node):
            previous = self.prefix
            self.prefix += node.name + '.'
            self.generic_visit(node)
            self.prefix = previous
        def visit_FunctionDef(self, node):
            name = self.prefix + node.name
            self.nodes[name].append(node)
            previous = self.prefix
            self.prefix = name + '.<locals>.'
            self.generic_visit(node)
            self.prefix = previous
        visit_AsyncFunctionDef = visit_FunctionDef

    def check():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            candidate = compile_source(ast.unparse(ast.fix_missing_locations(tree)),
                                       '<shared-try-fragments-candidate>')
        return candidate, compare(reference, candidate, decoder)

    def blocks(node):
        # Inner suites must be processed before an enclosing concatenation
        # copies their statements. Definitions have their own trial scope.
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef, ast.Lambda)):
                yield from blocks(child)
        for _, value in ast.iter_fields(node):
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                yield value

    def handler_key(node):
        if not isinstance(node, ast.Try) or not node.handlers or node.orelse or node.finalbody:
            return None
        return ast.dump(ast.Module(node.handlers, []), include_attributes=False)

    def revert(edits):
        for block, index, original in reversed(edits):
            block[index:index + 1] = original

    # Avoid a whole-tree comparison when there is no adjacent candidate.
    if not any(handler_key(first) is not None and handler_key(first) == handler_key(second)
               for owner in ast.walk(tree) for _, block in ast.iter_fields(owner)
               if isinstance(block, list)
               for first, second in zip(block, block[1:])):
        return tree, []

    candidate, baseline = check()
    matching = {entry['unit'] for entry in baseline['entries'] if entry['matched']}
    by_name = defaultdict(list)
    for path, code in units(candidate):
        by_name[code.co_qualname].append(path)
    definitions = Definitions()
    definitions.visit(tree)
    attempted = {}
    owners = {}
    for name, nodes in definitions.nodes.items():
        if len(nodes) != 1 or len(by_name[name]) != 1 or by_name[name][0] in matching:
            continue
        edits = []
        for block in blocks(nodes[0]):
            index = len(block) - 1
            while index > 0:
                key = handler_key(block[index])
                if key is None:
                    index -= 1
                    continue
                first = index
                while first > 0 and handler_key(block[first - 1]) == key:
                    first -= 1
                if first < index:
                    original = block[first:index + 1]
                    body = [statement for region in original for statement in region.body]
                    block[first:index + 1] = [ast.Try(body, original[0].handlers, [], [])]
                    edits.append((block, first, original))
                index = first - 1
        if edits:
            attempted[by_name[name][0]] = edits
            owners[by_name[name][0]] = nodes[0]
    if not attempted:
        return tree, []
    _, tentative = check()
    tested = {entry['unit'] for entry in tentative['entries'] if entry['matched']}
    hoisted = {}
    for path, owner in owners.items():
        if path in tested:
            continue
        edits = []
        for block in blocks(owner):
            if (len(block) >= 2 and isinstance(block[-1], ast.Return)
                    and handler_key(block[-2]) is not None):
                region, statement = block[-2:]
                edits.append((block, len(block) - 1, region, statement))
                block.pop()
                region.body.append(statement)
        if edits:
            hoisted[path] = edits
    def restore_hoisted(edits):
        for block, index, region, statement in reversed(edits):
            region.body.remove(statement)
            block.insert(index, statement)
    if hoisted:
        _, joint = check()
        tested = {entry['unit'] for entry in joint['entries'] if entry['matched']}
        for path, edits in list(hoisted.items()):
            if path not in tested:
                restore_hoisted(edits)
                del hoisted[path]
    selected = {}
    for path, edits in attempted.items():
        if path in tested:
            selected[path] = edits
        else:
            revert(edits)
    if not selected:
        return tree, []
    _, final = check()
    after = {entry['unit'] for entry in final['entries'] if entry['matched']}
    if not matching.issubset(after) or not set(selected).issubset(after):
        for edits in reversed(list(hoisted.values())):
            restore_hoisted(edits)
        for edits in reversed(list(selected.values())):
            revert(edits)
        return tree, []
    return tree, sorted(selected)


def restore_try_return_scope(tree, reference, decoder):
    """Restore hoisted block-tail returns only after exact dispatch proof."""
    try:
        from .bytecode_fidelity import compare, compile_source, units
    except ImportError:
        from bytecode_fidelity import compare, compile_source, units

    class Definitions(ast.NodeVisitor):
        def __init__(self):
            self.prefix = ''
            self.nodes = defaultdict(list)
        def visit_ClassDef(self, node):
            previous = self.prefix
            self.prefix += node.name + '.'
            self.generic_visit(node)
            self.prefix = previous
        def visit_FunctionDef(self, node):
            name = self.prefix + node.name
            self.nodes[name].append(node)
            previous = self.prefix
            self.prefix = name + '.<locals>.'
            self.generic_visit(node)
            self.prefix = previous
        visit_AsyncFunctionDef = visit_FunctionDef

    def check():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            candidate = compile_source(ast.unparse(ast.fix_missing_locations(tree)), '<try-scope-candidate>')
        return candidate, compare(reference, candidate, decoder)

    candidate, before = check()
    matched_before = {entry['unit'] for entry in before['entries'] if entry['matched']}
    # A hoisted return can leave only ``pass`` in the try body. CPython may
    # optimize that handler away, so these cases also fail instruction proof.
    # The tentative repair still needs full unit proof and preserves old matches.
    failed = {entry['unit'] for entry in before['entries']
              if entry['reason'] in ('exception_table', 'instructions')}
    if not failed:
        return tree, []
    by_name = defaultdict(list)
    for path, code in units(candidate):
        by_name[code.co_qualname].append(path)
    definitions = Definitions()
    definitions.visit(tree)
    def statement_blocks(node):
        for _, value in ast.iter_fields(node):
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                yield value
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            yield from statement_blocks(child)

    def revert(edits):
        for block, index, region, statement in reversed(edits):
            region.body.remove(statement)
            block.insert(index, statement)

    attempted = {}
    for name, nodes in definitions.nodes.items():
        if len(nodes) != 1 or len(by_name[name]) != 1 or by_name[name][0] not in failed:
            continue
        edits = []
        for block in statement_blocks(nodes[0]):
            if (len(block) < 2 or not isinstance(block[-1], ast.Return)
                    or not isinstance(block[-2], ast.Try)):
                continue
            region = block[-2]
            handled = region.handlers and not region.orelse and not region.finalbody
            empty_finally = (not region.handlers and not region.orelse and region.finalbody
                             and len(region.body) == 1 and isinstance(region.body[0], ast.Pass))
            if handled or empty_finally:
                edits.append((block, len(block)-1, region, block[-1]))
        if edits:
            for block, index, region, statement in edits:
                block.remove(statement)
                region.body.append(statement)
            attempted[by_name[name][0]] = edits
    if not attempted:
        return tree, []
    _, tentative = check()
    matching = {entry['unit'] for entry in tentative['entries'] if entry['matched']}
    selected = {}
    for path, edits in attempted.items():
        if path in matching:
            selected[path] = edits
        else:
            revert(edits)
    if not selected:
        return tree, []
    _, final = check()
    after = {entry['unit'] for entry in final['entries'] if entry['matched']}
    if not matched_before.issubset(after) or not set(selected).issubset(after):
        for edits in selected.values():
            revert(edits)
        return tree, []
    return tree, sorted(selected)


def preserve_shared_returns(tree, reference, decoder):
    """Retain a compiler shared-return shape only after exact unit proof.

    CPython can duplicate terminal returns. A constant-folded expression at the
    end of the source instead keeps the shared edge found in some recovered
    functions. Preserve an existing return's value expression exactly; implicit
    returns still produce None. This supplies no missing work and runs no new
    predicate. Never accept it on syntax or behavior tests alone.
    """
    # Import lazily: the verifier itself imports this normalizer. Both tools
    # are fully initialized by the time source reconstruction reaches here.
    try:
        from .bytecode_fidelity import compare, compile_source, units
    except ImportError:
        from bytecode_fidelity import compare, compile_source, units

    class Definitions(ast.NodeVisitor):
        def __init__(self):
            self.prefix = ''
            self.nodes = defaultdict(list)

        def visit_ClassDef(self, node):
            prefix = self.prefix
            self.prefix += node.name + '.'
            self.generic_visit(node)
            self.prefix = prefix

        def visit_FunctionDef(self, node):
            name = self.prefix + node.name
            self.nodes[name].append(node)
            prefix = self.prefix
            self.prefix = name + '.<locals>.'
            self.generic_visit(node)
            self.prefix = prefix

        visit_AsyncFunctionDef = visit_FunctionDef

    def checked_compile():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            return compile_source(ast.unparse(ast.fix_missing_locations(tree)), '<static-source-candidate>')

    def matched(candidate):
        return {entry['unit'] for entry in compare(reference, candidate, decoder)['entries']
                if entry['matched']}

    candidate = checked_compile()
    before = matched(candidate)
    by_qualname = defaultdict(list)
    for path, code in units(candidate):
        by_qualname[code.co_qualname].append((path, code))
    definitions = Definitions()
    definitions.visit(tree)
    attempted = {}
    for name, nodes in definitions.nodes.items():
        if len(nodes) != 1 or len(by_qualname[name]) != 1:
            continue
        path, code = by_qualname[name][0]
        # Async generators cannot have a value-bearing return expression.
        if path in before or code.co_flags & 0x200:
            continue
        node = nodes[0]
        previous = node.body[-1] if node.body and isinstance(node.body[-1], ast.Return) else None
        value = previous.value if previous is not None and previous.value is not None else ast.Constant(None)
        replacement = ast.Return(ast.IfExp(ast.Constant(True), value, ast.Constant(None)))
        if previous is None:
            node.body.append(replacement)
        else:
            node.body[-1] = replacement
        attempted[path] = (node, previous)
    if not attempted:
        return tree, []
    tested = matched(checked_compile())
    selected = {}
    def revert(node, previous):
        if previous is None:
            node.body.pop()
        else:
            node.body[-1] = previous

    for path, (node, previous) in attempted.items():
        if path in tested:
            selected[path] = (node, previous)
        else:
            revert(node, previous)
    if not selected:
        return tree, []
    after = matched(checked_compile())
    if not before.issubset(after) or not set(selected).issubset(after):
        # Removing an unproven neighboring change must not invalidate either
        # an old match or an accepted new one. Fall back without any edits.
        for node, previous in selected.values():
            revert(node, previous)
        return tree, []
    return tree, sorted(selected)


def restore_attribute_callable_values(tree, reference, decoder):
    """Preserve callable-value loads only after full unit proof.

    A folded condition retains LOAD_ATTR without the method-call optimization.
    Reuse only existing attribute/argument expressions. Some functions also
    need their shared return block preserved; try these two shapes together
    before rejecting either. Every accepted trial must match the full unit
    and retain every previously matched unit.
    """
    try:
        from .bytecode_fidelity import canonical, compare, compile_source, units
    except ImportError:
        from bytecode_fidelity import canonical, compare, compile_source, units

    def checked():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            compiled = compile_source(ast.unparse(ast.fix_missing_locations(tree)), '<attribute-trial>')
        return compiled, compare(reference, compiled, decoder)
    # Reparse once so AST/code line locations share a single source rendering.
    tree = ast.parse(ast.unparse(ast.fix_missing_locations(tree)))
    candidate, before = checked()
    matching = {e['unit'] for e in before['entries'] if e['matched']}
    references = dict(units(normalize_code(reference)))
    candidates = dict(units(candidate))
    nodes = defaultdict(list)
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.GeneratorExp)
    for node in ast.walk(tree):
        if isinstance(node, scopes):
            name = ('<lambda>' if isinstance(node, ast.Lambda) else
                    '<genexpr>' if isinstance(node, ast.GeneratorExp) else node.name)
            # Decorated code objects start at the first decorator, not ``def``.
            decorators = getattr(node, 'decorator_list', ())
            line = decorators[0].lineno if decorators else node.lineno
            nodes[name, line].append(node)
    def direct_calls(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, scopes): continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute): yield child
            yield from direct_calls(child)
    def direct_returns(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, scopes): continue
            if isinstance(child, ast.Return): yield child
            else: yield from direct_returns(child)
    attempted = {}
    return_edits = {}
    for path, code in candidates.items():
        if path in matching or path not in references: continue
        scope_nodes = nodes[code.co_name, code.co_firstlineno]
        if len(scope_nodes) != 1: continue
        original = canonical(references[path], decoder).instructions
        rebuilt = canonical(code, decoder).instructions
        needed = {instruction[1][0] for instruction, after in zip(original, original[1:])
                  if instruction[0] == 'LOAD_ATTR' and instruction[1][1] == 0
                  and after[0] == 'PUSH_NULL'}
        optimized = {instruction[1][0] for instruction in rebuilt
                     if instruction[0] == 'LOAD_ATTR' and instruction[1][1] == 1}
        wanted = needed & optimized
        edits = []
        for call in direct_calls(scope_nodes[0]):
            if call.func.attr not in wanted: continue
            previous = call.func
            call.func = ast.IfExp(ast.Constant(True), previous, ast.Constant(None))
            edits.append((call, previous))
        if edits:
            attempted[path] = edits
            if not code.co_flags & 0x200:
                return_edits[path] = [(statement, statement.value)
                                      for statement in direct_returns(scope_nodes[0])]
    if not attempted: return tree, []
    _, tentative = checked()
    proved = {e['unit'] for e in tentative['entries'] if e['matched']}
    selected = {}
    for path, edits in attempted.items():
        if path in proved: selected[path] = edits
    folded_returns = {}
    # Attribute-only and return-only changes can both fail while their joint
    # shape matches. Keep the callable trial in place for this bounded search;
    # each failed return trial is restored before trying the next depth.
    for depth in range(1, 5):
        pending = {path: edits for path, edits in return_edits.items()
                   if path not in selected and edits}
        if not pending: break
        for edits in pending.values():
            for statement, previous in edits:
                value = previous if previous is not None else ast.Constant(None)
                for _ in range(depth):
                    value = ast.IfExp(ast.Constant(True), value, ast.Constant(None))
                statement.value = value
        _, tentative = checked()
        proved = {e['unit'] for e in tentative['entries'] if e['matched']}
        for path, edits in pending.items():
            if path in proved:
                selected[path] = attempted[path]
                folded_returns[path] = edits
            else:
                for statement, previous in edits: statement.value = previous
    for path, edits in attempted.items():
        if path not in selected:
            for call, previous in edits: call.func = previous
    _, final = checked()
    after = {e['unit'] for e in final['entries'] if e['matched']}
    if not matching.issubset(after) or not set(selected).issubset(after):
        for edits in selected.values():
            for call, previous in edits: call.func = previous
        for edits in folded_returns.values():
            for statement, previous in edits: statement.value = previous
        return tree, []
    return tree, sorted(after-matching)


def preserve_shared_branch_returns(tree, reference, decoder):
    """Keep shared short return blocks with bounded, fully proved folding.

    CPython's flow-graph optimizer can inline an exit block of at most four
    instructions. Constant-folded expression edges can retain the original
    shared block. Try one through four layers around existing return values;
    no predicate, argument, call or handler is supplied or removed. Accept
    only full-unit matches and retain all earlier matches after reverting
    unsuccessful neighboring trials. Never traverse a nested definition from
    its parent's return collector.
    """
    try:
        from .bytecode_fidelity import compare, compile_source, units
    except ImportError:
        from bytecode_fidelity import compare, compile_source, units

    def checked():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            compiled = compile_source(ast.unparse(ast.fix_missing_locations(tree)), '<return-block-trial>')
        return compiled, {entry['unit'] for entry in compare(reference, compiled, decoder)['entries']
                          if entry['matched']}

    # Match source scopes to code objects using this exact source rendering.
    tree = ast.parse(ast.unparse(ast.fix_missing_locations(tree)))
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.GeneratorExp)
    nodes = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            line = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            nodes[node.name, line].append(node)

    def returns(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, scopes):
                continue
            if isinstance(child, ast.Return):
                yield child
            else:
                yield from returns(child)

    candidate, before = checked()
    candidates = dict(units(candidate))
    eligible = {}
    for path, code in candidates.items():
        definitions = nodes[code.co_name, code.co_firstlineno]
        if path in before or code.co_flags & 0x200 or len(definitions) != 1:
            continue
        statements = list(returns(definitions[0]))
        if statements:
            eligible[path] = [(statement, statement.value) for statement in statements]
    if not eligible:
        return tree, []

    selected = {}
    for depth in range(1, 5):
        attempted = {path: edits for path, edits in eligible.items() if path not in selected}
        if not attempted:
            break
        for edits in attempted.values():
            for statement, previous in edits:
                value = previous if previous is not None else ast.Constant(None)
                for _ in range(depth):
                    value = ast.IfExp(ast.Constant(True), value, ast.Constant(None))
                statement.value = value
        _, tentative = checked()
        newly_selected = {}
        for path, edits in attempted.items():
            if path in tentative:
                newly_selected[path] = edits
            else:
                for statement, previous in edits:
                    statement.value = previous
        if not newly_selected:
            continue
        _, after = checked()
        required = before | selected.keys() | newly_selected.keys()
        if not required.issubset(after):
            for edits in newly_selected.values():
                for statement, previous in edits:
                    statement.value = previous
            continue
        selected.update(newly_selected)
    _, final = checked()
    if not (before | selected.keys()).issubset(final):
        for edits in selected.values():
            for statement, previous in edits:
                statement.value = previous
        return tree, []
    return tree, sorted(selected)


def recover(sequence: Path, backend: Path) -> dict:
    """Use the native export beside sequence and write validated source + status."""
    prefix = Path(str(sequence).removesuffix('.seq'))
    exported = Path(str(prefix) + '.recovered.pyc')
    normalized = Path(str(prefix) + '.normalized.pyc')
    output = Path(str(prefix) + '.cdc.py')
    report_path = Path(str(prefix) + '.recovery.json')
    report = {'backend': 'external', 'syntax_valid': False, 'complete': False,
              'behavioral_equivalence_verified': False}
    try:
        data = exported.read_bytes()
        if data[:4] != importlib.util.MAGIC_NUMBER:
            raise ValueError('Run this recovery using the same Python version as the exported bytecode')
        code = normalize_code(marshal.loads(data[16:]))
        normalized.write_bytes(data[:16] + marshal.dumps(code))
        process = subprocess.run([str(backend), str(normalized)], capture_output=True,
                                 text=True, timeout=120)
        Path(str(prefix) + '.modern.raw.py').write_text(process.stdout)
        Path(str(prefix) + '.modern.log').write_text(process.stderr)
        report['backend_exit_code'] = process.returncode
        report['backend_incomplete'] = 'incomplete' in process.stderr.lower()
        if process.returncode:
            raise ValueError('External decompiler failed; see the local .modern.log')
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            tree = ast.parse(process.stdout)
        metadata = sequence.read_bytes()[:34]
        if len(metadata) != 34 or metadata[0] != 0xa1 or metadata[17] != 0xa2:
            raise ValueError('Missing AES metadata in sequence')
        transformer = MarkerNormalizer(metadata[1:17], metadata[18:30])
        tree = ast.fix_missing_locations(DeadTailNormalizer().visit(
            ClassAnnotationNormalizer(code).visit(transformer.visit(tree))))
        if any(isinstance(statement, ast.ImportFrom) and statement.module == '__future__'
               and any(alias.name == 'annotations' for alias in statement.names)
               for statement in tree.body):
            tree = ast.fix_missing_locations(FutureAnnotationNormalizer().visit(tree))
        tree, report['verified_module_annotation_binding'] = restore_module_annotation_binding(tree, code, transformer)
        tree, report['verified_shared_return_shapes'] = preserve_shared_returns(tree, code, transformer)
        tree, report['verified_try_return_scopes'] = restore_try_return_scope(tree, code, transformer)
        tree, report['verified_capture_name_order'] = restore_capture_name_order(tree, code, transformer)
        tree, report['verified_compound_return_shapes'] = restore_compound_return_shapes(tree, code, transformer)
        tree, report['verified_attribute_callable_values'] = restore_attribute_callable_values(tree, code, transformer)
        tree, report['verified_shared_branch_return_shapes'] = preserve_shared_branch_returns(tree, code, transformer)
        tree, report['verified_shared_try_fragments'] = restore_shared_try_fragments(tree, code, transformer)
        tree, report['verified_expanded_keyword_merges'] = restore_expanded_keyword_merges(tree, code, transformer)
        tree, report['verified_chained_comprehension_filters'] = restore_chained_comprehension_filters(tree, code, transformer)
        tree, report['verified_loop_rejection_guards'] = restore_loop_rejection_guards(tree, code, transformer)
        tree, report['verified_context_boundaries'] = restore_context_boundaries(tree, code, transformer)
        tree, report['verified_coupled_statement_shapes'] = restore_coupled_statement_shapes(tree, code, transformer)
        tree, report['verified_individual_return_edges'] = preserve_individual_return_edges(tree, code, transformer)
        tree, report['verified_residual_statement_shapes'] = restore_residual_statement_shapes(tree, code, transformer)
        source = ast.unparse(tree) + '\n'
        if report['verified_shared_return_shapes']:
            lines = []
            for line in source.splitlines():
                if line.lstrip().startswith('return ') and line.rstrip().endswith(' if True else None'):
                    indent = line[:len(line) - len(line.lstrip())]
                    lines.append(indent + '# Folded expression preserves the verified shared-return edge.')
                lines.append(line)
            source = '\n'.join(lines) + '\n'
        with warnings.catch_warnings(record=True) as found_warnings:
            warnings.simplefilter('always', SyntaxWarning)
            compile(source, str(output), 'exec', dont_inherit=True) # Validation only. Never execute.
        report['syntax_warnings'] = [str(warning.message) for warning in found_warnings]
        unresolved_imports = any(isinstance(node, ast.Name) and node.id.startswith('_pyarmor_')
                                 for node in ast.walk(tree))
        report.update(syntax_valid=True, unresolved_markers=transformer.remaining,
                      unresolved_imports=unresolved_imports,
                      functions=sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree)),
                      classes=sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree)))
        report['complete'] = not (report['backend_incomplete'] or transformer.remaining
                                  or unresolved_imports or report['syntax_warnings'])
        output.write_text('# Recovered statically; behavioral equivalence requires separate verification.\n' + source)
    except Exception as error:
        report['error'] = str(error)
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    return report
