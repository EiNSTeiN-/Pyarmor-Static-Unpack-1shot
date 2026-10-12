"""Owned, fail-closed compiler-shape fixtures; no protected inputs are executed."""
import ast
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import preserve_shared_returns


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class SharedReturnTests(unittest.TestCase):
    def recover(self, reference, candidate):
        code = compile_source(reference, 'owned-reference')
        tree, selected = preserve_shared_returns(ast.parse(candidate), code, None)
        source = ast.unparse(tree)
        return code, source, selected

    def test_shared_none_return_preserves_truth_call_order_and_errors(self):
        reference = ('def finish(flag, event):\n'
                     '    if flag:\n        event("called")\n'
                     '    return None if True else None\n')
        code, source, selected = self.recover(reference, reference.rsplit('    return', 1)[0])
        self.assertEqual(selected, ['<module>/finish#1'])
        self.assertTrue(compare(code, compile_source(source, 'candidate'))['all_units_match'])
        original, recovered = {}, {}
        exec(code, original); exec(compile_source(source, 'candidate'), recovered)
        class Truth:
            def __init__(self, calls, result):
                self.calls, self.result = calls, result
            def __bool__(self):
                self.calls.append('truth')
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result
        for result in (False, True, ValueError('owned failure')):
            outcomes = []
            for namespace in (original, recovered):
                calls = []
                try:
                    value = namespace['finish'](Truth(calls, result), lambda message: calls.append(message))
                except ValueError as error:
                    value = (type(error).__name__, str(error))
                outcomes.append((value, calls))
            self.assertEqual(outcomes[1], outcomes[0])

    def test_closures_generators_and_coroutines_keep_full_trees(self):
        reference = '''def outer(flag, event):
    def finish():
        if flag:
            event("nested")
        return None if True else None
    return finish
def values(flag):
    if flag:
        yield 1
    return None if True else None
async def notify(flag, event):
    if flag:
        await event("async")
    return None if True else None
'''
        candidate = '\n'.join(line for line in reference.splitlines()
                              if 'return None if True else None' not in line)
        code, source, selected = self.recover(reference, candidate)
        self.assertEqual(len(selected), 3)
        self.assertTrue(compare(code, compile_source(source, 'candidate'))['all_units_match'])
        original, recovered = {}, {}
        exec(code, original); exec(compile_source(source, 'candidate'), recovered)
        for flag in (False, True):
            outcomes = []
            for namespace in (original, recovered):
                calls = []
                namespace['outer'](flag, calls.append)()
                async def event(message):
                    calls.append(message)
                asyncio.run(namespace['notify'](flag, event))
                outcomes.append((list(namespace['values'](flag)), calls))
            self.assertEqual(outcomes[1], outcomes[0])

    def test_different_constants_signatures_and_handlers_are_not_repaired(self):
        reference = '''def finish(flag, event):
    try:
        if flag:
            event("called")
    except ValueError:
        event("handled")
    return None if True else None
'''
        missing_return = reference.rsplit('    return', 1)[0]
        for candidate in (missing_return.replace('"called"', '"wrong"'),
                          missing_return.replace('finish(flag, event)', 'finish(other, event)').replace('if flag:', 'if other:'),
                          missing_return.replace('except ValueError:', 'except TypeError:')):
            code, source, selected = self.recover(reference, candidate)
            self.assertEqual(selected, [])
            self.assertFalse(compare(code, compile_source(source, 'candidate'))['all_units_match'])
            self.assertNotIn('return None if True else None', source)

    def test_ambiguous_definitions_and_async_generators_are_left_unchanged(self):
        reference = '''def finish(flag):
    if flag:
        pass
    return None if True else None
def finish(flag):
    if flag:
        pass
    return None if True else None
async def stream():
    yield 1
'''
        candidate = reference.replace('    return None if True else None\n', '').replace('yield 1', 'yield 2')
        code, source, selected = self.recover(reference, candidate)
        self.assertEqual(selected, [])
        self.assertNotIn('return None if True else None', source)
        self.assertFalse(compare(code, compile_source(source, 'candidate'))['all_units_match'])

    def test_idempotency_and_existing_matches_are_preserved(self):
        reference = ('def unchanged(value):\n    return value * 2\n'
                     'class Container:\n    def finish(self, flag, event):\n'
                     '        if flag:\n            event(flag)\n'
                     '        return None if True else None\n')
        candidate = reference.rsplit('        return', 1)[0]
        code, source, selected = self.recover(reference, candidate)
        self.assertEqual(selected, ['<module>/Container#1/finish#1'])
        tree, second = preserve_shared_returns(ast.parse(source), code, None)
        self.assertEqual(second, [])
        self.assertEqual(ast.unparse(tree), source)
        self.assertTrue(compare(code, compile_source(source, 'candidate'))['all_units_match'])
