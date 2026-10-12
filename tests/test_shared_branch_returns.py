"""Owned fixtures for shared exit blocks; protected modules are not executed."""
import ast
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import preserve_shared_branch_returns


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class SharedBranchReturnTests(unittest.TestCase):
    def recover(self, reference, candidate):
        code = compile_source(reference, 'owned-reference')
        tree, selected = preserve_shared_branch_returns(ast.parse(candidate), code, None)
        source = ast.unparse(tree)
        return code, source, selected

    def test_keyword_call_tail_preserves_properties_arguments_and_errors(self):
        plain = '''def describe(owner, first, second):
    data = owner.data
    return data.format(first=first if first is not None else 0,
                       second=second if second is not None else 7)
'''
        folded = plain.replace('return data.format(', 'return ((data.format(').replace(
            'else 7)', 'else 7)) if True else None) if True else None')
        code, source, selected = self.recover(folded, plain)
        self.assertEqual(selected, ['<module>/describe#1'])
        self.assertTrue(compare(code, compile_source(source, 'candidate'))['all_units_match'])
        original, recovered = {}, {}
        exec(code, original); exec(compile_source(source, 'candidate'), recovered)
        for first, second in ((None, None), (0, 0), ('a', 'b')):
            for fails in (None, 'data', 'format', 'call'):
                outcomes = []
                for namespace in (original, recovered):
                    calls = []
                    sentinel = object()
                    class Data:
                        @property
                        def format(self):
                            calls.append('format')
                            if fails == 'format': raise ValueError('owned format failure')
                            def call(**values):
                                calls.append(('call', values))
                                if fails == 'call': raise ValueError('owned call failure')
                                return sentinel
                            return call
                    class Owner:
                        @property
                        def data(self):
                            calls.append('data')
                            if fails == 'data': raise ValueError('owned data failure')
                            return Data()
                    try:
                        result = namespace['describe'](Owner(), first, second)
                        self.assertIs(result, sentinel)
                        result = 'sentinel'
                    except ValueError as error:
                        result = (type(error).__name__, str(error))
                    outcomes.append((result, calls))
                self.assertEqual(outcomes[1], outcomes[0])

    def test_chained_guard_return_preserves_rich_comparison_order(self):
        plain = '''def bounded(value, minimum, maximum):
    if value is None or not minimum <= value <= maximum:
        return None
    return value
'''
        reference = plain.replace('return None', 'return None if True else None').replace(
            'return value', 'return value if True else None')
        code, source, selected = self.recover(reference, plain)
        self.assertEqual(selected, ['<module>/bounded#1'])
        self.assertTrue(compare(code, compile_source(source, 'candidate'))['all_units_match'])
        original, recovered = {}, {}
        exec(code, original); exec(compile_source(source, 'candidate'), recovered)
        for value in (None, -1, 0, 5, 11, float('nan')):
            self.assertIs(recovered['bounded'](value, 0, 10), original['bounded'](value, 0, 10))
        for first, second in ((False, True), (True, False), (True, True), (ValueError('owned'), True)):
            outcomes = []
            for namespace in (original, recovered):
                calls = []
                class Bound:
                    def __init__(self, result): self.result = result
                    def __le__(self, other):
                        calls.append('lower')
                        if isinstance(self.result, Exception): raise self.result
                        return self.result
                class Value:
                    def __le__(self, other): calls.append('upper'); return second
                    def __bool__(self): raise AssertionError('value truth must not run')
                value = Value()
                try:
                    result = namespace['bounded'](value, Bound(first), object())
                    result = 'value' if result is value else result
                except ValueError as error:
                    result = (type(error).__name__, str(error))
                outcomes.append((result, calls))
            self.assertEqual(outcomes[1], outcomes[0])

    def test_dictionary_tail_preserves_key_value_order_and_identity(self):
        plain = '''def mapping(flag, value, record):
    return {'a': record('a'), 'b': value if flag else None,
            'c': record('c'), 'd': record('d')}
'''
        reference = plain.replace("return {'a'", "return (({'a'").replace(
            "record('d')}", "record('d')}) if True else None) if True else None")
        code, source, selected = self.recover(reference, plain)
        # Other surrounding instructions can already keep the exit shared.
        self.assertTrue(compare(code, compile_source(source, 'candidate'))['all_units_match'])
        original, recovered = {}, {}
        exec(code, original); exec(compile_source(source, 'candidate'), recovered)
        for flag in (False, True):
            outcomes = []
            for namespace in (original, recovered):
                calls = []
                sentinel = object()
                result = namespace['mapping'](flag, sentinel, lambda key: calls.append(key) or key)
                self.assertIs(result['b'], sentinel if flag else None)
                result['b'] = 'sentinel' if flag else None
                outcomes.append((result, calls))
            self.assertEqual(outcomes[1], outcomes[0])

    def test_coroutine_handler_and_cancellation_remain_significant(self):
        plain = '''async def finish(flag, callback):
    try:
        value = await callback()
        return value if flag else None
    except ValueError:
        return 'handled'
'''
        reference = plain.replace('return value if flag else None',
                                  'return ((value if flag else None) if True else None) if True else None')
        code, source, selected = self.recover(reference, plain)
        self.assertTrue(compare(code, compile_source(source, 'candidate'))['all_units_match'])
        original, recovered = {}, {}
        exec(code, original); exec(compile_source(source, 'candidate'), recovered)
        for flag in (False, True):
            for failure in (None, ValueError('owned'), asyncio.CancelledError('owned')):
                outcomes = []
                for namespace in (original, recovered):
                    calls = []
                    async def callback():
                        calls.append('callback')
                        if failure is not None: raise failure
                        return 7
                    try: result = asyncio.run(namespace['finish'](flag, callback))
                    except asyncio.CancelledError as error: result = (type(error).__name__, str(error))
                    outcomes.append((result, calls))
                self.assertEqual(outcomes[1], outcomes[0])

    def test_mismatched_contracts_are_rejected_and_prior_matches_survive(self):
        plain = '''def unchanged(value):
    return value * 2
def describe(callback, value):
    return callback(value=value if value is not None else 7)
'''
        reference = plain.replace('return callback(', 'return ((callback(').replace(
            'else 7)', 'else 7)) if True else None) if True else None')
        code, source, selected = self.recover(reference, plain)
        self.assertTrue(compare(code, compile_source(source, 'candidate'))['all_units_match'])
        tree, repeated = preserve_shared_branch_returns(ast.parse(source), code, None)
        self.assertEqual(repeated, [])
        self.assertEqual(ast.unparse(tree), source)
        for candidate in (plain.replace('else 7', 'else 8'),
                          plain.replace('callback(value=', 'callback(other='),
                          plain.replace('describe(callback, value)', 'describe(callback, other)').replace(
                              'value=value if value is not None', 'value=other if other is not None')):
            _, rejected, selected = self.recover(reference, candidate)
            self.assertEqual(selected, [])
            report = compare(code, compile_source(rejected, 'candidate'))
            self.assertFalse(report['all_units_match'])
            self.assertTrue(next(e['matched'] for e in report['entries']
                                 if e['unit'] == '<module>/unchanged#1'))

    def test_nested_definitions_are_proved_independently_and_async_generators_skipped(self):
        plain = '''def outer(flag):
    def inner(callback, value):
        return callback(value=value if flag else None)
    return inner
async def stream():
    yield 1
'''
        reference = plain.replace('return callback(', 'return ((callback(').replace(
            'else None)', 'else None)) if True else None) if True else None')
        code, source, selected = self.recover(reference, plain)
        self.assertTrue(compare(code, compile_source(source, 'candidate'))['all_units_match'])
        self.assertEqual(selected, ['<module>/outer#1/inner#1'])
        _, rejected, _ = self.recover(reference, plain.replace('yield 1', 'yield 2'))
        self.assertFalse(compare(code, compile_source(rejected, 'candidate'))['all_units_match'])


if __name__ == '__main__':
    unittest.main()
