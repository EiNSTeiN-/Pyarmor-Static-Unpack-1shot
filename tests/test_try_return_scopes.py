"""Owned scope fixtures; raising operations and dispatch stay significant."""
import ast
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import restore_try_return_scope


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class TryReturnScopeTests(unittest.TestCase):
    def recover(self, source, candidate):
        reference = compile_source(source, 'owned-reference')
        tree, selected = restore_try_return_scope(ast.parse(candidate), reference, None)
        recovered = compile_source(ast.unparse(tree), 'owned-recovered')
        return reference, recovered, tree, selected

    def test_dict_return_catches_access_errors_inside_original_scope(self):
        reference = '''def summary(fetch):
    try:
        data = fetch()
        return {"value": data["value"]}
    except KeyError:
        return {}
'''
        candidate = '''def summary(fetch):
    try:
        data = fetch()
    except KeyError:
        return {}
    return {"value": data["value"]}
'''
        original, recovered, tree, selected = self.recover(reference, candidate)
        self.assertEqual(selected, ['<module>/summary#1'])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]; exec(original, namespaces[0]); exec(recovered, namespaces[1])
        for value in ({'value': 3}, {}, None):
            outcomes = []
            for namespace in namespaces:
                calls = []
                def fetch():
                    calls.append('fetch'); return value
                try:
                    result = namespace['summary'](fetch)
                except TypeError as error:
                    result = (type(error).__name__, str(error))
                outcomes.append((result, calls))
            self.assertEqual(outcomes[1], outcomes[0])
        _, second = restore_try_return_scope(tree, original, None)
        self.assertEqual(second, [])

    def test_coroutine_result_conversion_keeps_errors_and_cancellation(self):
        reference = '''async def summary(fetch):
    try:
        data = await fetch()
        return {"value": data["value"]}
    except Exception:
        return {}
'''
        candidate = '''async def summary(fetch):
    try:
        data = await fetch()
    except Exception:
        return {}
    return {"value": data["value"]}
'''
        original, recovered, _, selected = self.recover(reference, candidate)
        self.assertEqual(selected, ['<module>/summary#1'])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]; exec(original, namespaces[0]); exec(recovered, namespaces[1])
        for value in ({'value': 3}, {}, ValueError('owned failure'), asyncio.CancelledError()):
            outputs = []
            for namespace in namespaces:
                async def fetch():
                    if isinstance(value, BaseException):
                        raise value
                    return value
                try:
                    result = asyncio.run(namespace['summary'](fetch))
                except asyncio.CancelledError:
                    result = 'cancelled'
                outputs.append(result)
            self.assertEqual(outputs[1], outputs[0])

    def test_changed_calls_literals_and_handler_types_are_not_accepted(self):
        reference = '''def summary(fetch):
    try:
        data = fetch()
        return {"value": data["value"]}
    except KeyError:
        return {}
'''
        candidate = '''def summary(fetch):
    try:
        data = fetch()
    except KeyError:
        return {}
    return {"value": data["value"]}
'''
        for wrong in (candidate.replace('data["value"]', 'data["wrong"]'),
                      candidate.replace('KeyError', 'ValueError'),
                      candidate.replace('data = fetch()', 'data = fetch(1)')):
            original, recovered, _, selected = self.recover(reference, wrong)
            self.assertEqual(selected, [])
            self.assertFalse(compare(original, recovered)['all_units_match'])

    def test_matching_outside_scope_and_finally_are_left_unchanged(self):
        source = '''def summary(fetch):
    try:
        data = fetch()
    except KeyError:
        return {}
    return {"value": data["value"]}
'''
        original, recovered, _, selected = self.recover(source, source)
        self.assertEqual(selected, [])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        source = '''def summary(fetch):
    try:
        data = fetch()
    finally:
        fetch("cleanup")
    return data
'''
        original, recovered, _, selected = self.recover(source, source)
        self.assertEqual(selected, [])
        self.assertTrue(compare(original, recovered)['all_units_match'])
