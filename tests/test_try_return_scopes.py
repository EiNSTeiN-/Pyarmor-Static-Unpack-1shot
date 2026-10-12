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

    def test_empty_finally_body_retains_context_through_await_and_cancellation(self):
        from contextvars import ContextVar
        source = '''async def write(writer, origin):
    token = origin.set('owned')
    try:
        return await writer()
    finally:
        origin.reset(token)
'''
        candidate = source.replace('        return await writer()', '        pass') + '    return await writer()\n'
        original, recovered, tree, selected = self.recover(source, candidate)
        self.assertEqual(selected, ['<module>/write#1'])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]
        for code, namespace in zip((original, recovered), namespaces): exec(code, namespace)
        for failure in ('', 'error', 'cancel'):
            outputs = []
            for namespace in namespaces:
                origin = ContextVar('owned', default='default')
                calls = []
                async def writer():
                    calls.append(origin.get())
                    await asyncio.sleep(0)
                    calls.append(origin.get())
                    if failure == 'error': raise ValueError('write failed')
                    if failure == 'cancel': raise asyncio.CancelledError('write cancelled')
                    return 42
                async def run():
                    try: outcome = ('value', await namespace['write'](writer, origin))
                    except (ValueError, asyncio.CancelledError) as error:
                        outcome = (type(error).__name__, str(error))
                    return outcome, origin.get(), calls
                outputs.append(asyncio.run(run()))
            self.assertEqual(outputs[1], outputs[0])
            self.assertEqual(outputs[0][1:], ('default', ['owned', 'owned']))
        self.assertEqual(restore_try_return_scope(tree, original, None)[1], [])
        for wrong in (candidate.replace('await writer()', 'await writer(1)'),
                      candidate.replace('origin.reset(token)', 'origin.reset(None)')):
            _, _, rejected, selections = self.recover(source, wrong)
            self.assertEqual(selections, [])
            self.assertEqual(ast.dump(rejected), ast.dump(ast.parse(wrong)))

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

    def test_empty_try_retains_fallible_attribute_lookup_in_original_handler(self):
        reference = '''def read(value):
    try:
        return value.active
    except AttributeError:
        return False
'''
        candidate = '''def read(value):
    try:
        pass
    except AttributeError:
        return False
    return value.active
'''
        original, recovered, tree, selected = self.recover(reference, candidate)
        self.assertEqual(selected, ['<module>/read#1'])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]
        exec(original, namespaces[0]); exec(recovered, namespaces[1])

        class Value:
            def __init__(self, mode):
                self.mode = mode
            @property
            def active(self):
                if self.mode == 'missing':
                    raise AttributeError('owned missing telemetry')
                if self.mode == 'unhandled':
                    raise ValueError('owned unhandled failure')
                return self.mode

        for mode in (True, False, 'missing', 'unhandled'):
            outputs = []
            for namespace in namespaces:
                try:
                    outputs.append(namespace['read'](Value(mode)))
                except ValueError as error:
                    outputs.append((type(error).__name__, str(error)))
            self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(restore_try_return_scope(tree, original, None)[1], [])
