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
    def test_protected_reader_return_survives_nested_normal_exit(self):
        import asyncio
        from bytecode_fidelity import compare, compile_source
        sources = (
            '''async def work(owner):
    try:
        async with owner:
            async with owner.query("row0") as query:
                rows = await query.read()
            values = []
            for row in rows:
                try:
                    values.append(owner.build(row))
                except Exception as error:
                    owner.record(error)
                    continue
            return values
    except Exception as error:
        owner.record(error)
        return []
''',
            '''async def work(owner):
    try:
        async with owner:
            async with owner.query("row0") as query:
                first = await query.read()
            async with owner.query("row1") as query:
                second = await query.read()
            return owner.build(first, second)
    except Exception as error:
        owner.record(error)
        return []
''',
        )
        for source in sources:
            code = compile_source(source, 'owned-protected-reader')
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'fixture.pyc'
                path.write_bytes(importlib.util.MAGIC_NUMBER + bytes(12) + marshal.dumps(code))
                process = subprocess.run([str(BINARY), '--pyc', '--export-only', str(path)],
                                         capture_output=True, text=True, timeout=10)
                self.assertEqual(process.returncode, 0, process.stderr)
                prefix = Path(directory) / 'fixture.1shot'
                Path(str(path) + '.recovered.pyc').rename(Path(str(prefix) + '.recovered.pyc'))
                sequence = Path(str(prefix) + '.seq')
                sequence.write_bytes(b'\xa1' + bytes(16) + b'\xa2' + bytes(12) + b'\xf0\xff\x00\x00')
                report = recover(sequence, Path(BACKEND).resolve())
                self.assertTrue(report['syntax_valid'])
                candidate = Path(str(prefix) + '.cdc.py').read_text()
            # A native structural warning can precede a fully proved source
            # postpass. Accept only the complete instruction/exception proof.
            self.assertTrue(compare(code, compile_source(candidate, 'owned-reader-candidate'))['all_units_match'])
            original, reconstructed = {}, {}
            exec(code, original)
            exec(compile_source(candidate, 'owned-reader-candidate'), reconstructed)
            for rows in ([], [1, 2], [0, 'bad', 3], ['cancel', 4]):
                for fault in ('', 'outer-enter', 'query-enter', 'read', 'read-cancel',
                              'query-exit', 'outer-exit', 'build', 'build-cancel'):
                    for suppression in ('', 'outer', 'query'):
                        def exercise(function):
                            events = []
                            def fail(stage):
                                if fault == stage:
                                    raise ValueError('owned ' + stage)
                                if fault == stage + '-cancel':
                                    raise asyncio.CancelledError('owned ' + stage)
                            class Query:
                                def __init__(self, owner, name): self.owner, self.name = owner, name
                                async def __aenter__(self):
                                    events.append(('query-enter', self.name)); fail('query-enter')
                                    return self
                                async def read(self):
                                    events.append(('read', self.name)); fail('read')
                                    return list(rows)
                                async def __aexit__(self, kind, error, traceback):
                                    events.append(('query-exit', self.name, kind.__name__ if kind else None))
                                    fail('query-exit'); return suppression == 'query'
                            class Owner:
                                active = False
                                async def __aenter__(self):
                                    events.append('outer-enter'); fail('outer-enter')
                                    self.active = True; return self
                                async def __aexit__(self, kind, error, traceback):
                                    events.append(('outer-exit', kind.__name__ if kind else None))
                                    self.active = False; fail('outer-exit')
                                    return suppression == 'outer'
                                def query(self, name):
                                    events.append(('query', name)); return Query(self, name)
                                def build(self, value, other=None):
                                    events.append(('build', self.active, value, other)); fail('build')
                                    if value == 'bad': raise LookupError('owned row')
                                    if value == 'cancel': raise asyncio.CancelledError('owned row')
                                    return value + other if other is not None else value * 2
                                def record(self, error): events.append(('record', type(error).__name__, str(error)))
                            owner = Owner()
                            try: result = ('return', asyncio.run(function(owner)))
                            except BaseException as error: result = ('raise', type(error).__name__, str(error))
                            return result, events, owner.active
                        with self.subTest(rows=rows, fault=fault, suppression=suppression):
                            self.assertEqual(exercise(original['work']), exercise(reconstructed['work']))

    def test_original_dead_marker_slot_survives_full_export_pipeline(self):
        from bytecode_fidelity import compare, compile_source
        source = ('def work(rows):\n'
                  '    """Select the first valid cost."""\n'
                  '    if False:\n        __assert_armored__ = None\n')
        source += ''.join(f'    local_{index} = None\n' for index in range(61))
        source += ("    for row in rows:\n        cost = row.get('cost')\n"
                   '        if cost is not None:\n            return cost\n'
                   '    return None\n')
        original, reconstructed = self.recover(source)
        self.assertTrue(compare(compile_source(source, 'original'),
                                compile_source(self.last_generated, 'reconstructed'))['all_units_match'])
        self.assertIn('if False:\n        __assert_armored__ = None', self.last_generated)
        self.assertEqual(reconstructed['work'].__doc__, original['work'].__doc__)
        for rows in ([], [{}], [{'cost': None}], [{'cost': 0}], [{}, {'cost': 8}]):
            self.assertEqual(original['work'](rows), reconstructed['work'](rows))

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

    def test_decorated_shared_dictionary_returns_match_through_full_export_pipeline(self):
        import ast
        from bytecode_fidelity import compare, compile_source
        plain = '''def retain(method):
    return method
class Owner:
    def __init__(self, ready):
        self.ready = ready
    @property
    def details(self):
        return {'ready': self.ready, 'label': 'ready' if self.ready else 'pending'}
    @staticmethod
    @retain
    def describe(ready):
        return {'label': 'ready' if ready else 'pending'}
'''
        tree = ast.parse(plain)
        for owner in tree.body:
            if not isinstance(owner, ast.ClassDef): continue
            for method in owner.body:
                if not method.decorator_list: continue
                statement = method.body[0]
                for _ in range(3):
                    statement.value = ast.IfExp(ast.Constant(True), statement.value, ast.Constant(None))
        source = ast.unparse(ast.fix_missing_locations(tree))
        original, recovered = self.recover(source)
        self.assertTrue(compare(compile_source(source, 'original'),
                                compile_source(self.last_generated, 'recovered'))['all_units_match'])
        for ready in (False, True, None, 0, 1, 'yes'):
            self.assertEqual(recovered['Owner'](ready).details, original['Owner'](ready).details)
            self.assertEqual(recovered['Owner'].describe(ready), original['Owner'].describe(ready))

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

    def test_shared_try_fragments_preserve_property_getters_and_comparison_errors(self):
        from bytecode_fidelity import compare, compile_source
        source = ('class Sensor:\n'
                  '    @property\n    def icon(self):\n'
                  '        try:\n            trend = self.trend\n'
                  '            if trend > 10:\n                return "high"\n'
                  '            if trend > 5:\n                return "rising"\n'
                  '            if trend < -10:\n                return "low"\n'
                  '            if trend < -5:\n                return "falling"\n'
                  '        except Exception:\n            return "fallback"\n'
                  '        return "ordinary"\n')
        original, recovered = self.recover(source)
        self.assertTrue(compare(compile_source(source, 'owned-original'),
                                compile_source(self.last_generated, 'owned-recovered'))['all_units_match'])
        def exercise(sensor_class, number, failure):
            calls = []
            class Value:
                def __gt__(self, limit):
                    calls.append(('gt', limit))
                    if failure == 'comparison': raise ValueError('owned comparison')
                    return number > limit
                def __lt__(self, limit):
                    calls.append(('lt', limit))
                    if failure == 'comparison': raise ValueError('owned comparison')
                    return number < limit
            class Reading(sensor_class):
                @property
                def trend(self):
                    calls.append('getter')
                    if failure == 'getter': raise AttributeError('owned getter')
                    if failure == 'base': raise KeyboardInterrupt('owned interrupted getter')
                    return Value()
            try: outcome = ('value', Reading().icon)
            except KeyboardInterrupt as error: outcome = ('KeyboardInterrupt', str(error))
            return outcome, calls
        for number in (-11, -10, -6, -5, 0, 5, 6, 10, 11, float('nan')):
            for failure in ('', 'getter', 'comparison', 'base'):
                self.assertEqual(exercise(recovered['Sensor'], number, failure),
                                 exercise(original['Sensor'], number, failure))
        self.assertEqual(exercise(recovered['Sensor'], 0, '')[0], ('value', 'ordinary'))

    def test_chained_inline_filters_keep_each_positive_and_negative_guard(self):
        from bytecode_fidelity import compare, compile_source
        for kind in ('list', 'set', 'dict'):
            for guard in ('isinstance(value, str)', 'not isinstance(value, str)'):
                for join in (' and ', ' if '):
                    opening, closing = ('[', ']') if kind == 'list' else ('{', '}')
                    element = 'value: value' if kind == 'dict' else 'value'
                    source = ('def select(values):\n'
                              f'    return {opening}{element} for value in values '
                              f'if {guard}{join}value{closing}\n')
                    with self.subTest(kind=kind, guard=guard, join=join):
                        original, reconstructed = self.recover(source)
                        self.assertTrue(compare(compile_source(source, 'owned-reference'),
                                                compile_source(self.last_generated, 'owned-candidate'))
                                        ['all_units_match'])
                        for values in ([], ['', 'kept', None, 0, 2, 'again'],
                                       ['repeat', 'repeat', False, True]):
                            self.assertEqual(reconstructed['select'](values),
                                             original['select'](values))

    def test_shared_raise_guard_keeps_formatted_message_chain_cleanup_and_cause(self):
        from bytecode_fidelity import compare, compile_source
        source = ('def select(value, read, invalid, make_error, make_cause):\n'
                  '    if value is None or not 0 <= read(value) <= 10 or invalid(value):\n'
                  '        raise make_error(f"invalid {value!r}") from make_cause(value)\n'
                  '    return value\n')
        original, reconstructed = self.recover(source)
        self.assertTrue(compare(compile_source(source, 'owned-reference'),
                                compile_source(self.last_generated, 'owned-candidate'))['all_units_match'])
        def exercise(function, value, rejects):
            calls = []
            def read(item):
                calls.append(('read', item)); return item
            def invalid(item):
                calls.append(('invalid', item)); return rejects
            def make_error(message):
                calls.append(('error', message)); return ValueError(message)
            def make_cause(item):
                calls.append(('cause', item)); return RuntimeError('cause')
            try: result = ('value', function(value, read, invalid, make_error, make_cause))
            except ValueError as error:
                result = ('ValueError', str(error), type(error.__cause__).__name__,
                          str(error.__cause__), error.__suppress_context__)
            return result, calls
        for value in (None, -1, 0, 5, 10, 11):
            for rejects in (False, True):
                self.assertEqual(exercise(reconstructed['select'], value, rejects),
                                 exercise(original['select'], value, rejects))

    def test_annotated_class_preserves_docstring_and_annotations(self):
        original, reconstructed = self.recover(
            'from __future__ import annotations\n'
            'class Result:\n    "Documented result."\n    amount: int\n    valid: bool = True\n')
        self.assertEqual(reconstructed['Result'].__doc__, original['Result'].__doc__)
        self.assertEqual(reconstructed['Result'].__annotations__, original['Result'].__annotations__)

    def test_unused_future_annotation_cell_preserves_dataclass_namespace(self):
        original, reconstructed = self.recover(
            'from __future__ import annotations\n'
            'from dataclasses import dataclass\n'
            '@dataclass\nclass Result:\n    "Documented result."\n'
            '    if False:\n        discarded: int\n'
            '    amount: int\n    valid: bool = True\n')
        for name in ('__doc__', '__annotations__', '__match_args__'):
            self.assertEqual(getattr(reconstructed['Result'], name), getattr(original['Result'], name))
        self.assertEqual(reconstructed['Result'](3).__dict__, original['Result'](3).__dict__)
        self.assertNotIn('__conditional_annotations__', reconstructed['Result'].__dict__)
        self.assertNotIn('__conditional_annotations__', self.last_generated)
        from bytecode_fidelity import compare
        reference = compile('from __future__ import annotations\nfrom dataclasses import dataclass\n'
                            '@dataclass\nclass Result:\n    "Documented result."\n'
                            '    if False:\n        discarded: int\n'
                            '    amount: int\n    valid: bool = True\n',
                            'fixture.py', 'exec', dont_inherit=True)
        self.assertTrue(compare(reference, compile(self.last_generated, 'candidate.py', 'exec',
                                                  dont_inherit=True))['all_units_match'])

    def test_explicit_annotation_bookkeeping_attribute_is_preserved(self):
        original, reconstructed = self.recover(
            'from __future__ import annotations\n'
            'class Result:\n    "Documented result."\n'
            '    __conditional_annotations__ = {"public": 4}\n    amount: int\n')
        self.assertEqual(reconstructed['Result'].__conditional_annotations__,
                         original['Result'].__conditional_annotations__)

    def test_joined_runtime_initializer_keeps_the_user_store(self):
        source = 'class Result:\n    def __init__(self):\n'
        source += '        __assert_armored__ = "__pyarmor_assert_fixture__"\n        self.samples = {}\n'
        original, reconstructed = self.recover(source)
        self.assertEqual(reconstructed['Result']().__dict__, original['Result']().__dict__)
        self.assertNotIn('__assert_armored__', self.last_generated)
        from bytecode_fidelity import compare
        self.assertTrue(compare(compile(source, 'reference.py', 'exec', dont_inherit=True),
                                compile(self.last_generated, 'candidate.py', 'exec',
                                        dont_inherit=True))['all_units_match'])

    def test_generator_builtin_fast_paths(self):
        for name in ('all', 'any', 'tuple'):
            original, reconstructed = self.recover(
                f'def select(values):\n    return {name}(value > 0 for value in values)\n')
            for values in ([], [-1, 0, 2], [1, 2, 3]):
                self.assertEqual(reconstructed['select'](values), original['select'](values))

    def test_builtin_generator_in_shared_rejection_guard_keeps_computed_return(self):
        source = ('def select(values, build):\n'
                  '    if not values or not all(value > 0 for value in values):\n'
                  '        return build("reject", amount=0)\n'
                  '    return build("accept", amount=sum(values))\n')
        original, reconstructed = self.recover(source)
        for values in ([], [-1, 2], [0], [1, 2], [float('nan')]):
            outputs = []
            for namespace in (original, reconstructed):
                calls = []
                def build(kind, **options):
                    calls.append((kind, options))
                    return kind, options
                outputs.append((namespace['select'](values, build), calls))
            self.assertEqual(outputs[1], outputs[0])
        from bytecode_fidelity import compare
        self.assertTrue(compare(compile(source, 'reference.py', 'exec', dont_inherit=True),
                                compile(self.last_generated, 'candidate.py', 'exec',
                                        dont_inherit=True))['all_units_match'])

    def test_guarded_generator_keeps_closure_capture_and_shadowed_callable(self):
        original, reconstructed = self.recover(
            'def select(values, threshold, all):\n'
            '    if not values or not all(value > threshold for value in values):\n'
            '        return "reject"\n'
            '    return "accept"\n')
        for values in ([], [0, 2], [3, 4]):
            results = []
            for namespace in (original, reconstructed):
                calls = []
                def replacement(items):
                    items = tuple(items)
                    calls.append(items)
                    return bool(items) and items[0]
                results.append((namespace['select'](values, 1, replacement), calls))
            self.assertEqual(results[1], results[0])

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
    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_selected_builtin_padding_is_idempotent_and_keeps_shadowed_call_order(self):
        import types
        module = compile('def select(values):\n'
                         '    if not values or not all(value > 0 for value in values):\n'
                         '        return "reject"\n'
                         '    return "accept"\n', 'fixture.py', 'exec', dont_inherit=True)
        function = next(value for value in module.co_consts if isinstance(value, types.CodeType))
        normalized = normalize_code(function)
        repeated = normalize_code(normalized)
        self.assertEqual(repeated.co_code, normalized.co_code)
        self.assertEqual(repeated.co_exceptiontable, normalized.co_exceptiontable)
        for values in ([], [0, 2], [1, 2]):
            outputs = []
            for code in (function, normalized):
                calls = []
                def shadowed(items):
                    result = tuple(items)
                    calls.append(result)
                    return all(result)
                result = types.FunctionType(code, {'all': shadowed})(values)
                outputs.append((result, calls))
            self.assertEqual(outputs[1], outputs[0])
    def test_parallel_marker_removal_preserves_rhs_then_target_order(self):
        import ast
        source = ('target.first, __assert_armored__, target.second = '
                  '(read("first"), "__pyarmor_assert_fixture__", read("second"))\n')
        tree = ast.fix_missing_locations(MarkerNormalizer(bytes(16), bytes(12)).visit(ast.parse(source)))
        observed = []
        class Target:
            def __setattr__(self, name, value):
                observed.append(('store', name, value))
        def read(name):
            observed.append(('read', name))
            return name
        exec(compile(tree, 'owned.py', 'exec'), {'target': Target(), 'read': read})
        self.assertEqual(observed, [('read', 'first'), ('read', 'second'),
                                    ('store', 'first', 'first'), ('store', 'second', 'second')])
        self.assertNotIn('__assert_armored__', ast.unparse(tree))
        untouched = 'target.first, __assert_armored__ = (read("first"), "user value")\n'
        tree = MarkerNormalizer(bytes(16), bytes(12)).visit(ast.parse(untouched))
        self.assertIn('__assert_armored__', ast.unparse(tree))
    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_wide_exit_jump_drops_only_its_argument_prefix(self):
        import dis
        import types
        code = compile('def select():\n    return 7\n', 'fixture.py', 'exec', dont_inherit=True)
        function = next(value for value in code.co_consts if isinstance(value, types.CodeType))
        ret = next(item for item in dis.get_instructions(function) if item.opname == 'RETURN_VALUE')
        # More than 255 words to the recognized runtime exit requires a
        # prefix. The original value stays on the stack across that jump.
        padding = bytes((dis.opmap['NOP'], 0)) * 300
        suffix = bytes((dis.opmap['LOAD_CONST'], len(function.co_consts),
                        dis.opmap['PUSH_NULL'], 0, dis.opmap['LOAD_CONST'], 0,
                        dis.opmap['CALL'], 1)) + bytes(6) + bytes((dis.opmap['POP_TOP'], 0,
                            dis.opmap['RETURN_VALUE'], 0))
        jump = bytes((dis.opmap['EXTENDED_ARG'], 1, dis.opmap['JUMP_FORWARD'], 44))
        protected = function.replace(co_code=function.co_code[:ret.offset] + jump + padding + suffix,
                                     co_consts=function.co_consts + ('__pyarmor_exit_fixture__',))
        normalized = normalize_code(protected)
        instructions = list(dis.get_instructions(normalized))
        self.assertEqual(normalized.co_code[ret.offset:ret.offset+2], bytes((dis.opmap['NOP'], 0)))
        self.assertEqual(next(item for item in instructions if item.opname == 'RETURN_VALUE').arg, None)
        self.assertEqual(len(normalized.co_code), len(protected.co_code))
        self.assertEqual(types.FunctionType(normalized, {})(), 7)

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_runtime_entry_restores_frame_visibility_for_zero_argument_super(self):
        import builtins
        import dis
        import types
        from bytecode_fidelity import compare
        module = compile('class Child(Base):\n    def __init__(self, value):\n'
                         '        super().__init__(value)\n', 'fixture.py', 'exec', dont_inherit=True)
        body = next(value for value in module.co_consts if isinstance(value, types.CodeType))
        method = next(value for value in body.co_consts if isinstance(value, types.CodeType))
        resume = next(item for item in dis.get_instructions(method) if item.opname == 'RESUME')
        marker = len(method.co_consts)
        entry = bytes((dis.opmap['LOAD_CONST'], marker, dis.opmap['PUSH_NULL'], 0,
                       dis.opmap['LOAD_CONST'], marker + 1, dis.opmap['CALL'], 1)) + bytes(6) + bytes((dis.opmap['POP_TOP'], 0))
        protected = method.replace(co_code=method.co_code[:resume.offset] + bytes((dis.opmap['NOP'], 0))
                                   + entry + method.co_code[resume.offset + 2:],
                                   co_consts=method.co_consts + ('__pyarmor_enter_fixture__', b'owned_context'))
        normalized = normalize_code(protected)
        self.assertTrue(compare(normalized, method)['all_units_match'])
        self.assertEqual(next(item for item in dis.get_instructions(normalized)
                              if item.opname == 'RESUME').arg, 0)
        class Base:
            def __init__(self, value):
                self.value = value
        for child_method in (method, normalized):
            class_code = body.replace(co_consts=tuple(child_method if value is method else value
                                                     for value in body.co_consts))
            cls = builtins.__build_class__(types.FunctionType(class_code, {}), 'Child', Base)
            self.assertEqual(cls(7).value, 7)

    @staticmethod
    def exit_layer_fixture(source, *, valid_outer=True):
        """Wrap owned bytecode in the observed runtime rethrow/cleanup shape."""
        import dis
        import types
        class ExitMarker(str):
            def __call__(self, context):
                return None
        module = compile(source, 'fixture.py', 'exec', dont_inherit=True)
        code = next(value for value in module.co_consts if isinstance(value, types.CodeType))
        ops = {item.offset: item for item in dis.get_instructions(code)}
        entries = list(dis.Bytecode(code).exception_entries)
        implicit = [entry for entry in entries if ops[entry.target].opname == 'CALL_INTRINSIC_1'
                    and ops[entry.target].arg == 3]
        if not implicit:
            raise AssertionError('Fixture needs a compiler StopIteration handler')
        head, marker = len(code.co_code), len(code.co_consts)
        suffix = bytes((dis.opmap['PUSH_EXC_INFO'], 0, dis.opmap['LOAD_CONST'], marker,
                        dis.opmap['PUSH_NULL'], 0, dis.opmap['LOAD_CONST'], marker + 1,
                        dis.opmap['CALL'], 1)) + bytes(6) + bytes((
                            dis.opmap['POP_TOP'], 0, dis.opmap['RERAISE'], 0,
                            dis.opmap['COPY'], 3, dis.opmap['POP_EXCEPT'], 0,
                            dis.opmap['RERAISE'], 1))
        cleanup = head + len(suffix) - 6
        rows = [entry._replace(target=head, depth=0, lasti=False) if entry in implicit else entry
                for entry in entries]
        row_type = type(implicit[0])
        rows.extend((row_type(head, cleanup, cleanup, 1, True),
                     row_type(cleanup, head + len(suffix), implicit[0].target, 0, valid_outer)))
        table = b''.join(_varint(row.start // 2, marker=True) + _varint((row.end - row.start) // 2)
                         + _varint(row.target // 2) + _varint((row.depth << 1) | row.lasti)
                         for row in sorted(rows))
        protected = code.replace(co_code=code.co_code + suffix, co_exceptiontable=table,
                                 co_stacksize=max(8, code.co_stacksize + 4),
                                 co_consts=code.co_consts + (ExitMarker('__pyarmor_exit_fixture__'), b'owned_context'))
        return code, protected

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_exit_layer_preserves_coroutine_generator_and_async_generator_errors(self):
        import asyncio
        import types
        from bytecode_fidelity import compare
        for source, kind in (('async def select(work):\n    return work()\n', 'coroutine'),
                             ('def select(work):\n    yield work()\n', 'generator'),
                             ('async def select(work):\n    yield work()\n', 'async_generator')):
            plain, protected = self.exit_layer_fixture(source)
            normalized = normalize_code(protected)
            self.assertTrue(compare(normalized, plain)['all_units_match'])
            def run(code, error):
                def work():
                    if error is not None:
                        raise error('owned failure')
                    return 7
                instance = types.FunctionType(code, {})(work)
                try:
                    if kind == 'coroutine':
                        # Drive directly so asyncio's Task cannot hide a
                        # wrongly escaping StopIteration as a successful result.
                        try:
                            instance.send(None)
                        except StopIteration as completed:
                            return ('result', completed.value)
                    elif kind == 'generator':
                        return ('result', next(instance))
                    else:
                        return ('result', asyncio.run(anext(instance)))
                except Exception as caught:
                    return ('error', type(caught), str(caught), type(caught.__cause__))
                finally:
                    if kind != 'async_generator':
                        instance.close()
                    else:
                        asyncio.run(instance.aclose())
            for error in (None, ValueError, StopIteration, StopAsyncIteration):
                with self.subTest(kind=kind, error=error):
                    self.assertEqual(run(normalized, error), run(protected, error))
                    self.assertEqual(run(normalized, error), run(plain, error))

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_exit_layer_preserves_real_nested_exception_handlers(self):
        import types
        from bytecode_fidelity import compare
        plain, protected = self.exit_layer_fixture('async def select(work):\n    try:\n        return work()\n'
                                                  '    except ValueError:\n        return 8\n')
        normalized = normalize_code(protected)
        self.assertTrue(compare(normalized, plain)['all_units_match'])
        for code in (plain, protected, normalized):
            def work():
                raise ValueError('handled by user')
            coroutine = types.FunctionType(code, {})(work)
            with self.assertRaises(StopIteration) as finished:
                coroutine.send(None)
            self.assertEqual(finished.exception.value, 8)

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
    def test_exit_layer_does_not_invent_an_unrecognized_outer_route(self):
        from bytecode_fidelity import compare
        plain, protected = self.exit_layer_fixture('async def select(work):\n    return work()\n', valid_outer=False)
        self.assertFalse(compare(protected, plain)['all_units_match'])

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
