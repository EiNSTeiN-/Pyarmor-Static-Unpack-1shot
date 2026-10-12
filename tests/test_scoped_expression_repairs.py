"""Owned proof fixtures for evaluation order and loop/call boundaries."""
import ast
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import (restore_expanded_keyword_merges,
                            restore_chained_comprehension_filters,
                            restore_loop_rejection_guards)


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class ScopedExpressionRepairTests(unittest.TestCase):
    def recover(self, source, candidate, repair):
        original = compile_source(source, 'owned-original')
        tree, selected = repair(ast.parse(candidate), original, None)
        recovered = compile_source(ast.unparse(tree), 'owned-recovered')
        return original, recovered, tree, selected

    def test_separate_keyword_merges_keep_duplicate_errors_and_call_order(self):
        source = '''def select(call, first, second):
    return call(7, named=3, **first(), **second())
'''
        candidate = source.replace('named=3, **first(), **second()', "**{'named': 3, **first(), **second()}")
        original, recovered, tree, selected = self.recover(source, candidate, restore_expanded_keyword_merges)
        self.assertEqual(selected, ['<module>/select#1'])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]
        for code, namespace in zip((original, recovered), namespaces): exec(code, namespace)
        for first_values, second_values in (({'a': 1}, {'b': 2}), ({'named': 4}, {}),
                                           ({'a': 1}, {'a': 2}), ({1: 2}, {}),
                                           (None, {}), ({}, None)):
            outputs = []
            for namespace in namespaces:
                calls = []
                def call(*args, **kwargs): calls.append('call'); return args, kwargs
                def first(): calls.append('first'); return first_values
                def second(): calls.append('second'); return second_values
                try: outcome = ('value', namespace['select'](call, first, second))
                except TypeError as error: outcome = ('TypeError', str(error))
                outputs.append((outcome, calls))
            self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(restore_expanded_keyword_merges(tree, original, None)[1], [])

    def test_genuine_dictionary_overwrite_and_wrong_calls_remain_unmodified(self):
        candidate = '''def select(call, first, second):
    return call(7, **{'named': 3, **first(), **second()})
'''
        _, _, tree, selected = self.recover(candidate, candidate, restore_expanded_keyword_merges)
        self.assertEqual(selected, [])
        self.assertEqual(ast.dump(tree), ast.dump(ast.parse(candidate)))
        source = candidate.replace("**{'named': 3, **first(), **second()}", 'named=3, **first(), **second()')
        _, _, tree, selected = self.recover(source, candidate.replace('call(7,', 'call(8,'), restore_expanded_keyword_merges)
        self.assertEqual(selected, [])
        self.assertEqual(ast.dump(tree), ast.dump(ast.parse(candidate.replace('call(7,', 'call(8,'))))

    def test_decorated_scopes_keep_matching_and_unproven_neighbors(self):
        source = '''class Select:
    @staticmethod
    def good(call, first, second):
        return call(7, named=3, **first(), **second())
    @staticmethod
    def wrong(call, first, second):
        return call(7, named=3, **first(), **second())
    @staticmethod
    def existing(call, first, second):
        return call(7, **{'named': 3, **first(), **second()})
'''
        candidate = source.replace('named=3, **first(), **second()', "**{'named': 3, **first(), **second()}")
        candidate = candidate.replace('def wrong(call, first, second):\n        return call(7,',
                                      'def wrong(call, first, second):\n        return call(8,')
        original, recovered, tree, selected = self.recover(source, candidate, restore_expanded_keyword_merges)
        self.assertEqual(selected, ['<module>/Select#1/good#1'])
        matches = {entry['unit']: entry['matched'] for entry in compare(original, recovered)['entries']}
        self.assertTrue(matches['<module>/Select#1/existing#1'])
        self.assertFalse(matches['<module>/Select#1/wrong#1'])
        wrong = next(node for node in tree.body[0].body if isinstance(node, ast.FunctionDef) and node.name == 'wrong')
        self.assertIsInstance(wrong.body[0].value.keywords[0].value, ast.Dict)

    def test_chained_filter_evaluates_middle_call_once(self):
        source = '''def select(values, middle):
    return [value for value in values if 0 <= middle(value) <= 10]
'''
        candidate = source.replace('if 0 <= middle(value) <= 10', 'if 0 <= middle(value) if middle(value) <= 10')
        original, recovered, _, selected = self.recover(source, candidate, restore_chained_comprehension_filters)
        self.assertEqual(selected, ['<module>/select#1'])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        namespaces = [{}, {}]
        for code, namespace in zip((original, recovered), namespaces): exec(code, namespace)
        for failure in (None, 5):
            outputs = []
            for namespace in namespaces:
                calls = []
                def middle(value):
                    calls.append(str(value))
                    if value == failure: raise ValueError('owned middle failed')
                    return value
                try: outcome = namespace['select']([-1, 0, 5, 10, 11, float('nan')], middle)
                except ValueError as error: outcome = (type(error).__name__, str(error))
                outputs.append((outcome, calls))
            self.assertEqual(outputs[1], outputs[0])
        _, _, unchanged, selected = self.recover(candidate, candidate, restore_chained_comprehension_filters)
        self.assertEqual(selected, [])
        self.assertEqual(ast.dump(unchanged), ast.dump(ast.parse(candidate)))

    def test_loop_guard_preserves_success_return_and_skips_missing_records(self):
        source = '''def select(rows):
    for row in rows:
        found = row.get('record')
        if found is None:
            continue
        if found.ready is None:
            continue
        return found
    return None
'''
        candidate = '''def select(rows):
    for row in rows:
        found = row.get('record')
        if found is not None:
            if found.ready is None:
                continue
        return found
    return None
'''
        original, recovered, tree, selected = self.recover(source, candidate, restore_loop_rejection_guards)
        self.assertEqual(selected, ['<module>/select#1'])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        from types import SimpleNamespace
        ready = SimpleNamespace(ready=3)
        pending = SimpleNamespace(ready=None)
        namespaces = [{}, {}]
        for code, namespace in zip((original, recovered), namespaces): exec(code, namespace)
        for rows in ([], [{}], [{}, {'record': pending}, {'record': ready}], [{'record': ready}]):
            self.assertIs(namespaces[1]['select'](rows), namespaces[0]['select'](rows))
        self.assertEqual(restore_loop_rejection_guards(tree, original, None)[1], [])

    def test_existing_accept_guard_nested_loop_continue_and_wrong_lookup_are_retained(self):
        source = '''def select(rows):
    total = 0
    for row in rows:
        if row:
            for value in row:
                if value is None:
                    continue
                total += value
        total += 1
    return total
'''
        original, recovered, tree, selected = self.recover(source, source, restore_loop_rejection_guards)
        self.assertEqual(selected, [])
        self.assertTrue(compare(original, recovered)['all_units_match'])
        self.assertEqual(ast.dump(tree), ast.dump(ast.parse(source)))


if __name__ == '__main__':
    unittest.main()
