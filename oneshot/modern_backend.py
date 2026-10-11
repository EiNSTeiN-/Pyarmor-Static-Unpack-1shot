"""Static CPython 3.14 source recovery with an optional external decompiler.

Recovered modules are parsed and compiled for validation, never imported/executed.
The sequence's AES material stays local and is never written to this report.
"""

from __future__ import annotations

import ast
import dis
import importlib.util
import json
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


def normalize_code(code: types.CodeType) -> types.CodeType:
    """Remove only identified PyArmor entry/exit plumbing; retain user handlers."""
    if sys.version_info[:2] != (3, 14):
        raise ValueError('Modern source recovery currently requires CPython 3.14')
    instructions = list(dis.get_instructions(code))
    bytecode = bytearray(code.co_code)
    constants = tuple(normalize_code(value) if isinstance(value, types.CodeType)
                      else value for value in code.co_consts)
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
            # Jump targets can precede the marker by interpreter housekeeping.
            exits.update(item.offset for item in instructions[max(0, index - 4):index + 1])
        if name.startswith('__pyarmor_enter_'):
            end = next((item.offset for item in instructions[index + 1:index + 16]
                        if item.opname == 'POP_TOP'), None)
            if end is not None:
                for offset in range(instruction.offset, end + 2, 2):
                    bytecode[offset:offset + 2] = bytes((dis.opmap['NOP'], 0))
    for instruction in instructions:
        if instruction.opname == 'JUMP_FORWARD' and instruction.argval in exits:
            bytecode[instruction.offset:instruction.offset + 2] = bytes((dis.opmap['RETURN_VALUE'], 0))
    entries = [entry for entry in dis.Bytecode(code).exception_entries if entry.target not in exits]
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
        tree = ast.fix_missing_locations(DeadTailNormalizer().visit(transformer.visit(tree)))
        source = ast.unparse(tree) + '\n'
        with warnings.catch_warnings(record=True) as found_warnings:
            warnings.simplefilter('always', SyntaxWarning)
            compile(source, str(output), 'exec') # Validation only. Never execute.
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
