"""Owned return shapes; truth order, returned values and dispatch stay exact."""
import ast
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import restore_compound_return_shapes


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class CompoundReturnTests(unittest.TestCase):
    def recover(self, source, candidate):
        original = compile_source(source, 'owned-original')
        tree, selected = restore_compound_return_shapes(ast.parse(candidate), original, None)
        return original, compile_source(ast.unparse(tree), 'owned-recovered'), tree, selected

    def test_conditional_dictionary_keeps_nested_value_call_and_missing_keys(self):
        source = '''def pick(value):
    return None if not value else {"v": value["v"], "when": str(value["when"]) if value else None}
'''
        candidate = '''def pick(value):
    if not value:
        return
    return {"v": value["v"], "when": str(value["when"]) if value else None}
'''
        original, recovered, tree, selected = self.recover(source, candidate)
        self.assertEqual(selected, [{'unit': '<module>/pick#1', 'shape': 'conditional'}])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]; exec(original, namespaces[0]); exec(recovered, namespaces[1])
        for value in (None, {}, {'v': object(), 'when': 3}, {'v': 1}, {'when': 2}):
            outputs = []
            for namespace in namespaces:
                try: outputs.append(namespace['pick'](value))
                except KeyError as error: outputs.append((type(error).__name__, str(error)))
            self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(restore_compound_return_shapes(tree, original, None)[1], [])

    def test_coroutine_return_keeps_await_failures_and_cancellation(self):
        source = '''async def pick(read):
    value = await read()
    return None if not value else {"v": value["v"], "when": str(value["when"]) if value else None}
'''
        candidate = '''async def pick(read):
    value = await read()
    if not value:
        return
    return {"v": value["v"], "when": str(value["when"]) if value else None}
'''
        original, recovered, _, selected = self.recover(source, candidate)
        self.assertTrue(selected)
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]; exec(original, namespaces[0]); exec(recovered, namespaces[1])
        for value in (None, {'v': 1, 'when': 3}, {}, RuntimeError('owned failure'), asyncio.CancelledError()):
            outputs = []
            for namespace in namespaces:
                events = []
                async def read():
                    events.append('read')
                    if isinstance(value, BaseException): raise value
                    return value
                try: result = asyncio.run(namespace['pick'](read))
                except (RuntimeError, asyncio.CancelledError) as error: result = type(error).__name__
                outputs.append((result, events))
            self.assertEqual(outputs[1], outputs[0])

    def test_short_circuit_rich_comparisons_keep_value_identity_and_truth_order(self):
        source = '''def pick(left, right):
    return (left < 10 and right > 20) or (right < 10 and left > 20)
'''
        candidate = '''def pick(left, right):
    if left < 10:
        if right > 20:
            pass
    return right < 10 and left > 20
'''
        original, recovered, _, selected = self.recover(source, candidate)
        self.assertEqual(selected, [{'unit': '<module>/pick#1', 'shape': 'or'}])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]; exec(original, namespaces[0]); exec(recovered, namespaces[1])
        for values in ((5, 25), (25, 5), (15, 15), (5, 5), (25, 25)):
            outputs = []
            for namespace in namespaces:
                events = []
                class Truth:
                    def __init__(self, value, label): self.value = value; self.label = label
                    def __bool__(self): events.append(('truth', self.label)); return self.value
                class Number:
                    def __init__(self, value, label): self.value = value; self.label = label
                    def __lt__(self, other):
                        label=(self.label,'lt',other); events.append(label)
                        return Truth(self.value < other, label)
                    def __gt__(self, other):
                        label=(self.label,'gt',other); events.append(label)
                        return Truth(self.value > other, label)
                result = namespace['pick'](Number(values[0],'left'), Number(values[1],'right'))
                self.assertIsInstance(result, Truth)
                outputs.append((result.value, result.label, events))
            self.assertEqual(outputs[1], outputs[0])

    def test_changed_values_and_genuine_empty_branches_are_not_accepted(self):
        source = '''def pick(value):
    if value:
        pass
    return value
'''
        original, recovered, _, selected = self.recover(source, source)
        self.assertEqual(selected, [])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        source = '''def pick(value):
    return None if not value else {"v": value["v"], "when": str(value["when"]) if value else None}
'''
        candidate = '''def pick(value):
    if not value:
        return
    return {"v": value["wrong"], "when": str(value["when"]) if value else None}
'''
        original, recovered, _, selected = self.recover(source, candidate)
        self.assertEqual(selected, [])
        self.assertFalse(compare(original, recovered)['all_units_match'])
