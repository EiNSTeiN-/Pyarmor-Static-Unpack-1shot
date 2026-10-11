"""The static verifier must reject semantic changes, not only syntax errors."""
import dis
import marshal
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source, constant
from modern_backend import MarkerNormalizer


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class FidelityTests(unittest.TestCase):
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

    def test_changed_docstring_is_rejected(self):
        self.assertFalse(self.check('def select():\n    "First."\n    return 2\n',
                                    'def select():\n    "Second."\n    return 2\n')['all_units_match'])

    def test_opaque_bytes_do_not_leak_to_reports(self):
        payload = b'private_payload'
        self.assertNotIn('private_payload', repr(constant(payload)))


if __name__ == '__main__':
    unittest.main()
