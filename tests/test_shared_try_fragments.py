"""Owned fragment repairs; all meaningful handlers remain verifier-significant."""
import ast
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import restore_shared_try_fragments


REFERENCE = '''def select(read, choose):
    try:
        value = read()
        if value > 10:
            return 'high'
        if choose(value):
            return 'chosen'
        if value < 0:
            return 'low'
    except (TypeError, ValueError) as error:
        return 'fallback', str(error)
    return 'ordinary'
'''
FRAGMENTED = '''def select(read, choose):
    try:
        value = read()
        if value > 10:
            return 'high'
    except (TypeError, ValueError) as error:
        return 'fallback', str(error)
    try:
        if choose(value):
            return 'chosen'
    except (TypeError, ValueError) as error:
        return 'fallback', str(error)
    try:
        if value < 0:
            return 'low'
    except (TypeError, ValueError) as error:
        return 'fallback', str(error)
    return 'ordinary'
'''


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class SharedTryFragmentTests(unittest.TestCase):
    def recover(self, source, candidate):
        original = compile_source(source, 'owned-original')
        tree, selected = restore_shared_try_fragments(ast.parse(candidate), original, None)
        recovered = compile_source(ast.unparse(tree), 'owned-recovered')
        return original, recovered, tree, selected

    def test_consecutive_fragments_keep_return_order_and_exact_handler_routes(self):
        original, recovered, tree, selected = self.recover(REFERENCE, FRAGMENTED)
        self.assertEqual(selected, ['<module>/select#1'])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]
        for code, namespace in zip((original, recovered), namespaces): exec(code, namespace)
        for number in (-2, 0, 5, 10, 11, float('nan')):
            for failure in ('', 'read', 'choose', 'unhandled', 'base'):
                outputs = []
                for namespace in namespaces:
                    calls = []
                    def read():
                        calls.append('read')
                        if failure == 'read': raise ValueError('read failed')
                        if failure == 'base': raise KeyboardInterrupt('read interrupted')
                        return number
                    def choose(value):
                        calls.append(('choose', value))
                        if failure == 'choose': raise TypeError('choice failed')
                        if failure == 'unhandled': raise KeyError('choice failed')
                        return value == 5
                    try: outcome = ('value', namespace['select'](read, choose))
                    except (KeyError, KeyboardInterrupt) as error:
                        outcome = (type(error).__name__, str(error))
                    outputs.append((outcome, calls))
                self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(restore_shared_try_fragments(tree, original, None)[1], [])

    def test_coroutines_keep_await_errors_and_cancellation_inside_original_scope(self):
        source = REFERENCE.replace('def select(', 'async def select(').replace('value = read()', 'value = await read()').replace('if choose(value):', 'if await choose(value):')
        candidate = FRAGMENTED.replace('def select(', 'async def select(').replace('value = read()', 'value = await read()').replace('if choose(value):', 'if await choose(value):')
        original, recovered, _, selected = self.recover(source, candidate)
        self.assertEqual(selected, ['<module>/select#1'])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]
        for code, namespace in zip((original, recovered), namespaces): exec(code, namespace)
        for number in (-2, 0, 5, 11):
            for failure in ('', 'read', 'choose', 'cancel-read', 'cancel-choose'):
                outcomes = []
                for namespace in namespaces:
                    calls = []
                    async def read():
                        calls.append('read'); await asyncio.sleep(0)
                        if failure == 'read': raise ValueError('read failed')
                        if failure == 'cancel-read': raise asyncio.CancelledError('read cancelled')
                        return number
                    async def choose(value):
                        calls.append(('choose', value)); await asyncio.sleep(0)
                        if failure == 'choose': raise TypeError('choice failed')
                        if failure == 'cancel-choose': raise asyncio.CancelledError('choice cancelled')
                        return value == 5
                    try: result = ('value', asyncio.run(namespace['select'](read, choose)))
                    except asyncio.CancelledError as error: result = ('cancelled', str(error))
                    outcomes.append((result, calls))
                self.assertEqual(outcomes[1], outcomes[0])

    def test_nested_blocks_and_definitions_have_independent_proven_trials(self):
        source = '''def factory():
    def select(read, choose):
        if read is not None:
            try:
                value = read()
                if value > 10:
                    return 'high'
                if choose(value):
                    return 'chosen'
            except ValueError:
                return 'fallback'
        return 'ordinary'
    return select
'''
        candidate = source.replace("                if choose(value):\n", "            except ValueError:\n                return 'fallback'\n            try:\n                if choose(value):\n")
        original, recovered, _, selected = self.recover(source, candidate)
        self.assertEqual(selected, ['<module>/factory#1/select#1'])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]
        for code, namespace in zip((original, recovered), namespaces): exec(code, namespace)
        for value in (-2, 0, 5, 11):
            functions = [namespace['factory']() for namespace in namespaces]
            self.assertEqual(functions[1](lambda: value, lambda item: item == 5),
                             functions[0](lambda: value, lambda item: item == 5))

    def test_matching_units_and_unproven_neighboring_edits_are_retained(self):
        source = REFERENCE + FRAGMENTED.replace('def select(', 'def independent(') + REFERENCE.replace('def select(', 'def wrong(')
        candidate = FRAGMENTED + FRAGMENTED.replace('def select(', 'def independent(') + FRAGMENTED.replace('def select(', 'def wrong(').replace("return 'high'", "return 'changed'")
        original, recovered, tree, selected = self.recover(source, candidate)
        self.assertEqual(selected, ['<module>/select#1'])
        report = compare(original, recovered)
        self.assertTrue(next(e for e in report['entries'] if e['unit'] == '<module>/independent#1')['matched'])
        self.assertFalse(next(e for e in report['entries'] if e['unit'] == '<module>/wrong#1')['matched'])
        wrong = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'wrong')
        self.assertEqual(sum(isinstance(n, ast.Try) for n in wrong.body), 3)

    def test_distinct_handlers_else_finally_and_intervening_work_are_not_guessed(self):
        variants = (
            FRAGMENTED.replace("    try:\n        if choose(value):", "    choose('between')\n    try:\n        if choose(value):"),
            FRAGMENTED.replace('except (TypeError, ValueError) as error:', 'except KeyError as error:', 1),
            FRAGMENTED.replace("        return 'fallback', str(error)\n    try:", "        return 'fallback', str(error)\n    else:\n        choose('else')\n    try:", 1),
            FRAGMENTED.replace("        return 'fallback', str(error)\n    try:", "        return 'fallback', str(error)\n    finally:\n        choose('finally')\n    try:", 1),
            FRAGMENTED.replace('value = read()', 'value = read(1)'),
        )
        for candidate in variants:
            original, recovered, tree, selected = self.recover(REFERENCE, candidate)
            self.assertEqual(selected, [])
            self.assertFalse(compare(original, recovered)['all_units_match'])
            self.assertEqual(ast.dump(tree, include_attributes=False),
                             ast.dump(ast.parse(candidate), include_attributes=False))


if __name__ == '__main__':
    unittest.main()
