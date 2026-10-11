"""Optional backend round trips of original fixtures, including normalization."""
import importlib.util
import marshal
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_python314 as native

BINARY, ROOT = native.BINARY, native.ROOT

sys.path.insert(0, str(ROOT / 'oneshot'))
from modern_backend import (DeadTailNormalizer, FutureAnnotationNormalizer,
                            MarkerNormalizer, _varint, normalize_code, recover,
                            restore_function_bindings)

BACKEND = os.environ.get('PYARMOR_MODERN_BACKEND')


@unittest.skipUnless(BACKEND and sys.version_info[:2] == (3, 14),
                     'Set PYARMOR_MODERN_BACKEND and use CPython 3.14')
class ModernBackendTests(native.Python314Tests):
    def recover(self, source):
        return self.recover_code(compile(source, 'fixture.py', 'exec', dont_inherit=True))

    def recover_code(self, code):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.pyc'
            path.write_bytes(importlib.util.MAGIC_NUMBER + bytes(12) + marshal.dumps(code))
            process = subprocess.run([str(BINARY), '--pyc', '--export-only', str(path)],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            exported = Path(str(path) + '.recovered.pyc')
            # Compare exported constants/code without importing the file.
            reconstructed = marshal.loads(exported.read_bytes()[16:])
            self.assertEqual(reconstructed.co_code, code.co_code)
            prefix = Path(directory) / 'fixture.1shot'
            exported.rename(Path(str(prefix) + '.recovered.pyc'))
            sequence = Path(str(prefix) + '.seq')
            sequence.write_bytes(b'\xa1' + bytes(16) + b'\xa2' + bytes(12) + b'\xf0\xff\x00\x00')
            report = recover(sequence, Path(BACKEND).resolve())
            self.assertTrue(report['syntax_valid'], report)
            self.assertTrue(report['complete'], report)
            source = Path(str(prefix) + '.cdc.py').read_text()
            self.last_generated = source
            original, reconstructed = {}, {}
            # The code comes only from the literal fixtures in these tests.
            exec(code, original)
            exec(compile(source, 'recovered_fixture.py', 'exec'), reconstructed)
            return original, reconstructed

    def test_named_nested_helpers_keep_names_and_closure_bindings(self):
        import types
        code = compile('def select(values):\n'
                       '    def rank(value):\n        return value + len(values)\n'
                       '    def count(value):\n        return rank(value) * 2\n'
                       '    return count(values[0]), rank.__name__, count.__name__\n',
                       'fixture.py', 'exec', dont_inherit=True)
        outer = next(value for value in code.co_consts if isinstance(value, types.CodeType))
        mapping = {'rank': '_var_var_8', 'count': '_var_var_9'}
        changed = outer.replace(
            co_varnames=tuple(mapping.get(name, name) for name in outer.co_varnames),
            co_cellvars=tuple(mapping.get(name, name) for name in outer.co_cellvars))
        code = code.replace(co_consts=tuple(changed if value is outer else value for value in code.co_consts))
        original, reconstructed = self.recover_code(code)
        for values in ([1], [3, 4], [-5, 0, 2]):
            self.assertEqual(reconstructed['select'](values), original['select'](values))
        self.assertIn('def rank(', self.last_generated)
        self.assertIn('def count(', self.last_generated)
        self.assertNotIn('_var_var_', self.last_generated)
        from bytecode_fidelity import compare
        self.assertTrue(compare(code, compile(self.last_generated, 'candidate.py', 'exec',
                                             dont_inherit=True))['all_units_match'])

    def test_decorated_nested_helper_keeps_original_name(self):
        import types
        code = compile('def select(value, decorator):\n'
                       '    @decorator\n    def adjust(amount=2):\n        return value + amount\n'
                       '    return adjust(), adjust.__name__\n', 'fixture.py', 'exec', dont_inherit=True)
        outer = next(value for value in code.co_consts if isinstance(value, types.CodeType))
        changed = outer.replace(co_varnames=tuple('_var_var_22' if name == 'adjust' else name
                                                 for name in outer.co_varnames))
        code = code.replace(co_consts=tuple(changed if value is outer else value for value in code.co_consts))
        original, reconstructed = self.recover_code(code)
        for value in (-2, 0, 5):
            calls = []
            def decorator(function):
                calls.append(function.__name__)
                return function
            self.assertEqual(reconstructed['select'](value, decorator), original['select'](value, decorator))
            self.assertEqual(calls, ['adjust', 'adjust'])
        self.assertIn('def adjust(', self.last_generated)

    def test_recursive_nested_helper_keeps_its_self_capture(self):
        import types
        code = compile('def select(value):\n    def factorial(number):\n'
                       '        return 1 if number < 2 else number * factorial(number - 1)\n'
                       '    return factorial(value)\n', 'fixture.py', 'exec', dont_inherit=True)
        outer = next(value for value in code.co_consts if isinstance(value, types.CodeType))
        changed = outer.replace(co_cellvars=tuple('_var_var_31' if name == 'factorial' else name
                                                 for name in outer.co_cellvars))
        code = code.replace(co_consts=tuple(changed if value is outer else value for value in code.co_consts))
        original, reconstructed = self.recover_code(code)
        for value in (0, 1, 2, 5, 8):
            self.assertEqual(reconstructed['select'](value), original['select'](value))
        self.assertIn('def factorial(', self.last_generated)

    def test_context_manager(self):
        # The optional backend supports the native backend's documented gap.
        super().test_context_manager()

    def test_annotated_class_preserves_docstring_and_annotations(self):
        original, reconstructed = self.recover(
            'from __future__ import annotations\n'
            'class Result:\n    "Documented result."\n    amount: int\n    valid: bool = True\n')
        self.assertEqual(reconstructed['Result'].__doc__, original['Result'].__doc__)
        self.assertEqual(reconstructed['Result'].__annotations__, original['Result'].__annotations__)

    def test_generator_builtin_fast_paths(self):
        for name in ('all', 'any', 'tuple'):
            original, reconstructed = self.recover(
                f'def select(values):\n    return {name}(value > 0 for value in values)\n')
            for values in ([], [-1, 0, 2], [1, 2, 3]):
                self.assertEqual(reconstructed['select'](values), original['select'](values))

    def test_shadowed_builtin_fast_path(self):
        original, reconstructed = self.recover(
            'def select(values, any):\n    return any(value > 0 for value in values)\n')
        replacement = lambda values: tuple(values)
        self.assertEqual(reconstructed['select']([-1, 2], replacement),
                         original['select']([-1, 2], replacement))

    def test_nested_dictionary_comprehension_in_generator(self):
        original, reconstructed = self.recover(
            'def select(rows):\n    return tuple({key: row[key] for key in row} for row in rows)\n')
        rows = [{'a': 2, 'b': 3}, {'x': 5}]
        self.assertEqual(reconstructed['select'](rows), original['select'](rows))

    def test_expanded_kwargs_in_generator(self):
        original, reconstructed = self.recover(
            'def select(rows, factory):\n    return tuple(factory(**dict(row)) for row in rows)\n')
        rows, factory = [{'a': 2}, {'x': 5}], lambda **values: values
        self.assertEqual(reconstructed['select'](rows, factory), original['select'](rows, factory))

    def test_async_context_with_guard_and_finally(self):
        import asyncio
        from contextlib import nullcontext
        original, reconstructed = self.recover(
            'async def select(manager, calculate):\n    async with manager:\n        if calculate is None:\n            return 3\n        try:\n            return await calculate()\n        finally:\n            last = False\n')
        async def calculate():
            return 7
        for function in (None, calculate):
            self.assertEqual(asyncio.run(reconstructed['select'](nullcontext(), function)),
                             asyncio.run(original['select'](nullcontext(), function)))

    def test_short_circuit_inside_comprehension_element(self):
        original, reconstructed = self.recover(
            'def select(rows):\n    return [max(0.0, float(row.get("x") or 0.0)) for row in rows]\n')
        rows = [{'x': 7}, {'x': None}, {'x': -2}, {}]
        self.assertEqual(reconstructed['select'](rows), original['select'](rows))

    def test_large_dictionary_literal_inside_comprehension(self):
        literal = ', '.join(f'"k{i}": row + {i}' for i in range(20))
        original, reconstructed = self.recover(
            f'def select(rows):\n    return [{{{literal}}} for row in rows]\n')
        self.assertEqual(reconstructed['select']([1, 3]), original['select']([1, 3]))


class NormalizationTests(unittest.TestCase):
    @staticmethod
    def binding_fixture(source, names):
        import types
        code = compile(source, 'fixture.py', 'exec', dont_inherit=True)
        outer = next(value for value in code.co_consts if isinstance(value, types.CodeType))
        iterator = iter(names)
        return outer.replace(co_consts=tuple(value.replace(co_name=next(iterator))
                                            if isinstance(value, types.CodeType) else value
                                            for value in outer.co_consts))

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_function_binding_restoration_is_instruction_preserving_and_idempotent(self):
        code = self.binding_fixture('def select():\n    def _var_var_1():\n        return 7\n'
                                    '    return _var_var_1()\n', ['calculate'])
        normalized = restore_function_bindings(code)
        self.assertIn('calculate', normalized.co_varnames)
        self.assertNotIn('_var_var_1', normalized.co_varnames)
        self.assertEqual(code.co_code, normalized.co_code)
        self.assertEqual(code.co_exceptiontable, normalized.co_exceptiontable)
        self.assertEqual(restore_function_bindings(normalized), normalized)

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_function_binding_restoration_preserves_parameters(self):
        code = self.binding_fixture('def select(_var_var_1):\n'
                                    '    def _var_var_1():\n        return 7\n'
                                    '    return _var_var_1()\n', ['calculate'])
        self.assertIs(restore_function_bindings(code), code)

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_function_binding_restoration_avoids_local_and_global_collisions(self):
        for statement in ('calculate = 3', 'print(calculate)'):
            with self.subTest(statement=statement):
                code = self.binding_fixture('def select():\n    ' + statement + '\n'
                                            '    def _var_var_1():\n        return 7\n'
                                            '    return _var_var_1()\n', ['calculate'])
                self.assertIs(restore_function_bindings(code), code)

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_function_binding_restoration_rejects_ambiguous_definitions(self):
        code = self.binding_fixture('def select(flag):\n    if flag:\n'
                                    '        def _var_var_1():\n            return 1\n'
                                    '    else:\n        def _var_var_1():\n            return 2\n'
                                    '    return _var_var_1()\n', ['first', 'second'])
        self.assertIs(restore_function_bindings(code), code)
        code = self.binding_fixture('def select():\n    def _var_var_1():\n        return 1\n'
                                    '    def _var_var_2():\n        return 2\n'
                                    '    return _var_var_1(), _var_var_2()\n', ['same', 'same'])
        self.assertIs(restore_function_bindings(code), code)

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_function_binding_restoration_preserves_nonlocal_and_public_bindings(self):
        import types
        code = compile('def outer():\n    _var_var_1 = None\n    def inner():\n'
                       '        nonlocal _var_var_1\n        def _var_var_1():\n            return 7\n',
                       'fixture.py', 'exec', dont_inherit=True)
        outer = next(value for value in code.co_consts if isinstance(value, types.CodeType))
        inner = next(value for value in outer.co_consts if isinstance(value, types.CodeType))
        child = next(value for value in inner.co_consts if isinstance(value, types.CodeType))
        inner = inner.replace(co_consts=tuple(child.replace(co_name='calculate') if value is child else value
                                             for value in inner.co_consts))
        self.assertIs(restore_function_bindings(inner), inner)
        for source in ('def _var_var_1():\n    return 7\n',
                       'class Public:\n    def _var_var_1():\n        return 7\n'):
            public = compile(source, 'fixture.py', 'exec', dont_inherit=True)
            def renamed(value):
                if not isinstance(value, types.CodeType):
                    return value
                return value.replace(co_name='calculate' if value.co_name == '_var_var_1' else value.co_name,
                                     co_consts=tuple(renamed(child) for child in value.co_consts))
            public = renamed(public)
            self.assertIs(restore_function_bindings(public), public)
            for value in public.co_consts:
                if isinstance(value, types.CodeType) and value.co_name == 'Public':
                    self.assertIs(restore_function_bindings(value), value)

    def test_runtime_only_suites_remain_syntactically_valid(self):
        import ast
        source = 'class Result:\n    __assert_armored__ = "__pyarmor_assert_fixture__"\n'
        tree = MarkerNormalizer(bytes(16), bytes(12)).visit(ast.parse(source))
        tree = ast.fix_missing_locations(DeadTailNormalizer().visit(tree))
        cleaned = ast.unparse(tree)
        compile(cleaned, 'fixture.py', 'exec', dont_inherit=True)
        self.assertIn('pass', cleaned)

    def test_future_annotations_preserve_expression_and_quoted_forward_reference(self):
        import ast
        tree = ast.parse('from __future__ import annotations\n'
                         'def select(value: "Any", explicit: "\\\'Later\\\'") -> "float | None":\n    pass\n')
        normalized = ast.fix_missing_locations(FutureAnnotationNormalizer().visit(tree))
        functions = {}
        exec(compile(normalized, 'fixture.py', 'exec'), functions)
        self.assertEqual(functions['select'].__annotations__,
                         {'value': 'Any', 'explicit': "'Later'", 'return': 'float | None'})

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_exit_marker_preserves_adjacent_user_exception_cleanup(self):
        import dis
        import types
        module = compile('def select(value):\n    try:\n        return float(value)\n'
                         '    except (TypeError, ValueError):\n        return None\n', 'fixture.py', 'exec')
        code = next(value for value in module.co_consts if isinstance(value, types.CodeType))
        marker = len(code.co_consts)
        start = len(code.co_code)
        suffix = bytes((dis.opmap['PUSH_EXC_INFO'], 0, dis.opmap['LOAD_CONST'], marker,
                        dis.opmap['PUSH_NULL'], 0, dis.opmap['LOAD_CONST'], 0,
                        dis.opmap['CALL'], 1)) + bytes(6) + bytes((
                            dis.opmap['POP_TOP'], 0, dis.opmap['RERAISE'], 0))
        # An outer PyArmor handler follows the original COPY/POP_EXCEPT/RERAISE.
        outer = (_varint(0, marker=True) + _varint(1) + _varint(start // 2) + _varint(0))
        protected = code.replace(co_code=code.co_code + suffix,
                                 co_consts=code.co_consts + ('__pyarmor_exit_fixture__',),
                                 co_exceptiontable=code.co_exceptiontable + outer)
        normalized = normalize_code(protected)
        self.assertEqual(list(dis.Bytecode(normalized).exception_entries),
                         list(dis.Bytecode(code).exception_entries))

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_extended_exit_marker_identifies_handler_prefix(self):
        import dis
        import types
        module = compile('def select(value):\n    return value + 2\n', 'fixture.py', 'exec')
        code = next(value for value in module.co_consts if isinstance(value, types.CodeType))
        constants = code.co_consts + (None,) * (300 - len(code.co_consts))
        start = len(code.co_code)
        suffix = bytes((dis.opmap['PUSH_EXC_INFO'], 0,
                        dis.opmap['EXTENDED_ARG'], 1, dis.opmap['LOAD_CONST'], 44,
                        dis.opmap['PUSH_NULL'], 0, dis.opmap['LOAD_CONST'], 0,
                        dis.opmap['CALL'], 1)) + bytes(6) + bytes((
                            dis.opmap['POP_TOP'], 0, dis.opmap['RERAISE'], 0))
        outer = (_varint(0, marker=True) + _varint(1) + _varint(start // 2) + _varint(0))
        protected = code.replace(co_code=code.co_code + suffix,
                                 co_consts=constants + ('__pyarmor_exit_wide__',),
                                 co_exceptiontable=outer)
        normalized = normalize_code(protected)
        self.assertEqual(list(dis.Bytecode(normalized).exception_entries), [])

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_closure_capture_names_follow_cells_not_child_metadata(self):
        import types
        module = compile('def select(rows):\n    values = rows\n'
                         '    return tuple(value for value in values if values)\n', 'fixture.py', 'exec')
        outer = next(value for value in module.co_consts if isinstance(value, types.CodeType))
        child = next(value for value in outer.co_consts if isinstance(value, types.CodeType))
        changed = child.replace(co_freevars=('previous_name',))
        outer = outer.replace(co_consts=tuple(changed if value is child else value for value in outer.co_consts))
        normalized = normalize_code(outer)
        normalized_child = next(value for value in normalized.co_consts if isinstance(value, types.CodeType))
        self.assertEqual(normalized_child.co_freevars, ('values',))

    def test_dead_tail_does_not_remove_sibling_branch(self):
        import ast
        tree = ast.parse('def f(x):\n    if x:\n        return 1\n        print("dead")\n    return 2\n')
        source = ast.unparse(DeadTailNormalizer().visit(tree))
        self.assertNotIn('dead', source)
        self.assertIn('return 2', source)

    def test_marker_constant_decoding(self):
        import ast
        source = "'__pyarmor_assert_1__'(b'\\x01example')"
        node = MarkerNormalizer(bytes(16), bytes(12)).visit(ast.parse(source))
        self.assertEqual(ast.get_docstring(node), 'example')

    def test_future_import_after_generated_annotation_store(self):
        import ast
        source = '__conditional_annotations__ = {}\n"Documentation"\nfrom __future__ import annotations\n'
        node = DeadTailNormalizer().visit(ast.parse(source))
        compile(ast.unparse(node), 'fixture.py', 'exec')
        self.assertEqual(ast.get_docstring(node), 'Documentation')
