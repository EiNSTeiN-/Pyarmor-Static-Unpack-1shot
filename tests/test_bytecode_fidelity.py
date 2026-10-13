"""The static verifier must reject semantic changes, not only syntax errors."""
import dis
import marshal
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source, constant, unused_protection_slot_aliases
from modern_backend import MarkerNormalizer, _varint, unused_future_annotation_offsets


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class FidelityTests(unittest.TestCase):
    def reservation_source(self, name='__assert_armored__'):
        declarations = ''.join(f'    value_{index} = {index}\n' for index in range(61))
        return (f'def choose(flag, convert):\n    if False:\n        {name} = None\n'
                + declarations
                + '    try:\n        if flag:\n            edge = convert(3)\n'
                  '        else:\n            edge = convert(4)\n'
                  '        return edge\n    except UnboundLocalError:\n        return "missing"\n')

    def reservation_code(self, source):
        module = compile_source(source, 'owned-reservation.py')
        return next(value for value in module.co_consts if isinstance(value, types.CodeType))

    def test_unused_protection_slot_can_use_a_neutral_name_without_changing_bytes(self):
        source = self.reservation_source()
        original = self.reservation_code(source)
        for name in ('unused_layout_slot', 'compiled_slot_reservation', 'not_a_magic_spelling'):
            recovered = self.reservation_code(self.reservation_source(name))
            self.assertEqual(original.co_code, recovered.co_code)
            self.assertEqual(original.co_exceptiontable, recovered.co_exceptiontable)
            self.assertEqual(unused_protection_slot_aliases(original, recovered), (2,))
            self.assertTrue(compare(original, recovered)['all_units_match'])
            for flag in (False, True, 0, 1, None):
                self.assertEqual(types.FunctionType(original, {})(flag, int),
                                 types.FunctionType(recovered, {})(flag, int))

    def test_removing_the_physical_slot_still_rejects_changed_load_instructions(self):
        source = self.reservation_source()
        original = self.reservation_code(source)
        deleted = self.reservation_code(source.replace('    if False:\n        __assert_armored__ = None\n', ''))
        self.assertEqual(original.co_varnames.index('edge'), 64)
        self.assertEqual(deleted.co_varnames.index('edge'), 63)
        self.assertNotEqual(original.co_code, deleted.co_code)
        self.assertEqual(unused_protection_slot_aliases(original, deleted), ())
        self.assertFalse(compare(original, deleted)['all_units_match'])

    def test_a_live_or_captured_source_slot_cannot_be_ignored_as_a_reservation(self):
        original = self.reservation_code(self.reservation_source())
        neutral = self.reservation_source('unused_layout_slot')
        for changed in (neutral.replace('    try:\n', '    unused_layout_slot = 7\n    try:\n'),
                        neutral.replace('    try:\n', '    seen = unused_layout_slot\n    try:\n'),
                        neutral.replace('    try:\n', '    def read():\n        return unused_layout_slot\n    try:\n')):
            candidate = self.reservation_code(changed)
            self.assertEqual(unused_protection_slot_aliases(original, candidate), ())
            self.assertFalse(compare(original, candidate)['all_units_match'])

    def test_reservation_name_handling_does_not_allow_changed_values_calls_or_handlers(self):
        original = self.reservation_code(self.reservation_source())
        neutral = self.reservation_source('unused_layout_slot')
        for changed in (neutral.replace('convert(3)', 'convert(4)'),
                        neutral.replace('convert(3)', 'other(3)'),
                        neutral.replace('except UnboundLocalError', 'except ValueError'),
                        neutral.replace('return edge', 'return -edge')):
            self.assertFalse(compare(original, self.reservation_code(changed))['all_units_match'])

    def test_keyword_parameters_never_become_ignored_reservation_aliases(self):
        original = self.reservation_code('def choose(__assert_armored__):\n    return __assert_armored__\n')
        candidate = self.reservation_code('def choose(unused_layout_slot):\n    return unused_layout_slot\n')
        self.assertEqual(unused_protection_slot_aliases(original, candidate), ())
        self.assertFalse(compare(original, candidate)['all_units_match'])

    def test_fused_fast_slot_accesses_disable_reservation_aliasing(self):
        original = self.reservation_code(
            'def choose(flag):\n    if False:\n        __assert_armored__ = None\n'
            '    first = 1\n    second = 2\n    return first + second\n')
        candidate = self.reservation_code(
            'def choose(flag):\n    if False:\n        unused_layout_slot = None\n'
            '    unused_layout_slot = 3\n'
            '    first = 1\n    second = 2\n    return unused_layout_slot + first\n')
        fused = [operation for operation in dis.get_instructions(candidate)
                 if operation.opname == 'LOAD_FAST_LOAD_FAST']
        self.assertTrue(fused)
        self.assertIn(1, (fused[0].arg >> 4, fused[0].arg & 15))
        self.assertEqual(unused_protection_slot_aliases(original, candidate), ())
        self.assertFalse(compare(original, candidate)['all_units_match'])

    def test_coupled_identity_and_truth_jump_inversion_preserves_the_predicate(self):
        code = next(value for value in compile_source(
            'def select(value):\n    if value is True:\n        return 7\n    return 3\n',
            'owned.py').co_consts if isinstance(value, types.CodeType))
        instructions = list(dis.get_instructions(code))
        identity = next(item for item in instructions if item.opname == 'IS_OP')
        branch = next(item for item in instructions if item.opname == 'POP_JUMP_IF_FALSE')
        changed = bytearray(code.co_code)
        changed[identity.offset + 1] = 1
        changed[branch.offset] = dis.opmap['POP_JUMP_IF_TRUE']
        equivalent = code.replace(co_code=bytes(changed))
        self.assertTrue(compare(code, equivalent)['all_units_match'])
        class NoUserComparison:
            def __eq__(self, other):
                raise AssertionError('Identity must not call equality')
            def __bool__(self):
                raise AssertionError('Identity must not call truth conversion')
        for value in (True, False, None, 0, 1, NoUserComparison()):
            self.assertEqual(types.FunctionType(equivalent, {})(value),
                             types.FunctionType(code, {})(value))
        identity_only = bytearray(code.co_code)
        identity_only[identity.offset + 1] = 1
        self.assertFalse(compare(code, code.replace(co_code=bytes(identity_only)))['all_units_match'])
        branch_only = bytearray(code.co_code)
        branch_only[branch.offset] = dis.opmap['POP_JUMP_IF_TRUE']
        self.assertFalse(compare(code, code.replace(co_code=bytes(branch_only)))['all_units_match'])
        # Changing the destination still fails even with the coupled inverse.
        changed[branch.offset + 1] = 0
        self.assertFalse(compare(code, code.replace(co_code=bytes(changed)))['all_units_match'])
    def test_unused_future_annotation_cell_is_only_compiler_bookkeeping(self):
        source = 'from __future__ import annotations\nclass Result:\n    "Documented."\n'
        reference = compile_source(source + '    if False:\n        unused: int\n    amount: int\n', 'reference.py')
        candidate = compile_source(source + '    amount: int\n', 'candidate.py')
        self.assertTrue(compare(reference, candidate)['all_units_match'])
        changed = compile_source(source + '    amount: str\n', 'changed.py')
        self.assertFalse(compare(reference, changed)['all_units_match'])

    def test_observable_conditional_annotation_cells_are_not_removed(self):
        source = 'from __future__ import annotations\nclass Result:\n'
        for body in ('    if False:\n        unused: int\n    saved = __conditional_annotations__\n',
                     '    if False:\n        unused: int\n    __conditional_annotations__ = {"public": 3}\n'):
            module = compile_source(source + body, 'observable.py')
            code = next(item for item in module.co_consts if isinstance(item, types.CodeType))
            self.assertFalse(unused_future_annotation_offsets(code))
        # Deferred PEP 649 annotations capture and read the cell, even when
        # a literal condition lets the compiler fold the executable branch.
        module = compile_source('class Result:\n    if False:\n        unused: int\n', 'deferred.py')
        code = next(item for item in module.co_consts if isinstance(item, types.CodeType))
        self.assertFalse(unused_future_annotation_offsets(code))

    def test_compiler_class_source_line_is_location_metadata(self):
        source = 'class Result:\n    "Documented class."\n    value = 3\n'
        self.assertTrue(self.check(source, '\n\n' + source)['all_units_match'])

    def test_explicit_class_line_assignment_is_not_normalized(self):
        self.assertFalse(self.check('class Result:\n    __firstlineno__ = 50\n',
                                    'class Result:\n    __firstlineno__ = 100\n')['all_units_match'])

    def test_pyarmor_from_import_matches_explicit_bindings(self):
        payload = b'\x04' + marshal.dumps(('example', ('first', 'second'), 1))
        original = compile_source(f'a, b = "__pyarmor_assert_fixture__"({payload!r})\n', 'armored.py')
        candidate = compile_source('from .example import first as a, second as b\n', 'source.py')
        decoder = MarkerNormalizer(bytes(16), bytes(12))
        self.assertTrue(compare(original, candidate, decoder)['all_units_match'])
        changed = compile_source('from .example import first as b, second as a\n', 'wrong.py')
        self.assertFalse(compare(original, changed, decoder)['all_units_match'])

    def test_pyarmor_import_matches_module_binding(self):
        payload = b'\x03example'
        original = compile_source(f'alias = "__pyarmor_assert_fixture__"({payload!r})\n', 'armored.py')
        candidate = compile_source('import example as alias\n', 'source.py')
        decoder = MarkerNormalizer(bytes(16), bytes(12))
        self.assertTrue(compare(original, candidate, decoder)['all_units_match'])

    def test_compilation_does_not_inherit_verifier_future_flags(self):
        plain = compile_source('def select(value: int): return value\n', 'plain.py')
        explicit = compile_source('from __future__ import annotations\n'
                                  'def select(value: int): return value\n', 'future.py')
        self.assertFalse(plain.co_flags & 16777216)
        self.assertTrue(explicit.co_flags & 16777216)

    def check(self, original, recovered):
        return compare(compile(original, 'original.py', 'exec'),
                       compile(recovered, 'recovered.py', 'exec'))

    def test_identical_source_matches_every_unit(self):
        source = 'def select(value):\n    try:\n        return float(value)\n    except ValueError:\n        return None\n'
        self.assertTrue(self.check(source, source)['all_units_match'])

    def test_internal_local_renaming_is_allowed(self):
        original = 'def select(value):\n    obscure = value + 2\n    return obscure * 3\n'
        recovered = 'def select(value):\n    result = value + 2\n    return result * 3\n'
        self.assertTrue(self.check(original, recovered)['all_units_match'])

    def test_keyword_parameter_renaming_is_rejected(self):
        self.assertFalse(self.check('def select(value): return value\n',
                                    'def select(other): return other\n')['all_units_match'])

    def test_changed_constant_is_rejected(self):
        self.assertFalse(self.check('def select(value): return value + 2\n',
                                    'def select(value): return value + 3\n')['all_units_match'])

    def test_changed_branch_is_rejected(self):
        self.assertFalse(self.check('def select(value): return 2 if value else 3\n',
                                    'def select(value): return 3 if value else 2\n')['all_units_match'])

    def test_changed_exception_table_is_rejected(self):
        module = compile('def select(value):\n    try: return float(value)\n'
                         '    except ValueError: return None\n', 'fixture.py', 'exec')
        function = next(value for value in module.co_consts if isinstance(value, types.CodeType))
        changed = function.replace(co_exceptiontable=b'')
        candidate = module.replace(co_consts=tuple(changed if value is function else value for value in module.co_consts))
        report = compare(module, candidate)
        entry = next(entry for entry in report['entries'] if entry['unit'].endswith('/select#1'))
        self.assertFalse(entry['matched'])
        self.assertIn(entry['reason'], ('instructions', 'exception_table'))

    def test_adjacent_identical_handler_ranges_have_identical_dispatch(self):
        module = compile_source('async def select(work):\n    return work()\n', 'fixture.py')
        function = next(value for value in module.co_consts if isinstance(value, types.CodeType))
        entry = next(iter(dis.Bytecode(function).exception_entries))
        split = next(item.offset for item in dis.get_instructions(function) if item.opname == 'CALL')
        def table(rows):
            return b''.join(_varint(row.start // 2, marker=True) + _varint((row.end - row.start) // 2)
                            + _varint(row.target // 2) + _varint((row.depth << 1) | row.lasti)
                            for row in rows)
        divided = function.replace(co_exceptiontable=table([entry._replace(end=split), entry._replace(start=split)]))
        self.assertTrue(compare(function, divided)['all_units_match'])
        # One uncovered CALL changes exception behavior and must still fail.
        call_end = next(item.offset for item in dis.get_instructions(function)
                        if item.offset > split and item.opname != 'CACHE')
        hole = function.replace(co_exceptiontable=table([entry._replace(end=split), entry._replace(start=call_end)]))
        self.assertFalse(compare(function, hole)['all_units_match'])
        # Handler depth/lasti remain significant even for adjacent ranges.
        changed = function.replace(co_exceptiontable=table([entry._replace(end=split),
                                    entry._replace(start=split, lasti=not entry.lasti)]))
        self.assertFalse(compare(function, changed)['all_units_match'])

    def test_changed_docstring_is_rejected(self):
        self.assertFalse(self.check('def select():\n    "First."\n    return 2\n',
                                    'def select():\n    "Second."\n    return 2\n')['all_units_match'])

    def test_opaque_bytes_do_not_leak_to_reports(self):
        payload = b'private_payload'
        self.assertNotIn('private_payload', repr(constant(payload)))


if __name__ == '__main__':
    unittest.main()
