"""Owned strict proofs for lost operands, inplace updates and import binding."""
import ast
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import restore_substantive_value_shapes


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class SubstantiveValueShapes(unittest.TestCase):
    def repaired(self, source, candidate, reference=None):
        reference = reference or compile_source(source, '<owned-reference>')
        tree, selected = restore_substantive_value_shapes(ast.parse(candidate), reference, None)
        compiled = compile_source(ast.unparse(tree), '<owned-rebuilt>')
        self.assertTrue(compare(reference, compiled)['all_units_match'])
        spaces = [{}, {}]
        for code, space in zip((reference, compiled), spaces):
            exec(code, space)
        return spaces, tree, selected

    def unchanged(self, source):
        spaces, tree, selected = self.repaired(source, source)
        self.assertEqual(selected, [])
        return spaces, tree

    def test_inplace_updates_keep_one_target_evaluation_and_mutable_identity(self):
        source = '''def select(rows, index, delta):
    rows[index()] += delta()
    return rows
'''
        candidate = source.replace('rows[index()] += delta()',
                                   'rows[index()] = rows[index()] + delta()')
        spaces, _, selected = self.repaired(source, candidate)
        self.assertTrue(selected)
        for fail in (False, True):
            outputs = []
            for space in spaces:
                events = []
                class Number:
                    value = 2
                    def __iadd__(self, value):
                        events.append(('inplace', value))
                        self.value += value
                        return self
                    def __add__(self, value):
                        raise AssertionError('must retain inplace operation')
                value = Number()
                rows = [value]
                def index():
                    events.append('index')
                    return 0
                def delta():
                    events.append('delta')
                    if fail:
                        raise ValueError('owned delta')
                    return 3
                try:
                    space['select'](rows, index, delta)
                    outcome = 'complete'
                except ValueError:
                    outcome = 'failed'
                outputs.append((outcome, rows[0] is value, value.value, events))
            self.assertEqual(outputs[1], outputs[0])
            self.assertEqual(outputs[1][-1].count('index'), 1)
        _, tree = self.unchanged(candidate)
        self.assertTrue(any(isinstance(node, ast.Assign) for node in ast.walk(tree)))

    def test_singleton_starred_tuple_preserves_rows_and_empty_branch(self):
        source = '''def select(value, present):
    return [('base', value), *((('battery', value),) if present else ())]
'''
        candidate = source.replace("(('battery', value),)", "('battery', value)")
        spaces, _, selected = self.repaired(source, candidate)
        self.assertTrue(selected)
        for present in (False, True):
            self.assertEqual(spaces[1]['select'](3, present), spaces[0]['select'](3, present))
        self.unchanged(candidate)

    def test_carried_dictionary_results_keep_handler_and_call_order(self):
        source = '''def select(read, failed):
    try:
        return {'value': read()}
    except ValueError:
        return {'failed': failed()}
'''
        candidate = source.replace("return {'value': read()}", "{'value': read()}\n        return").replace(
            "return {'failed': failed()}", "{'failed': failed()}\n        return")
        spaces, _, selected = self.repaired(source, candidate)
        self.assertTrue(selected)
        for error in (None, ValueError, RuntimeError):
            results = []
            for space in spaces:
                events = []
                def read():
                    events.append('read')
                    if error:
                        raise error('owned read')
                    return 7
                def failed():
                    events.append('failed')
                    return True
                try:
                    value = space['select'](read, failed)
                except RuntimeError:
                    value = 'unprotected'
                results.append((value, events))
            self.assertEqual(results[1], results[0])
        self.unchanged(candidate)

    def test_unaliased_dotted_import_keeps_top_package_binding(self):
        source = '''def select():
    import xml.etree
    return xml.etree.__name__
'''
        reference = compile_source(source, '<owned-dotted-import>')
        reference = reference.replace(co_consts=tuple(
            item.replace(co_varnames=('_owned_import',)) if isinstance(item, types.CodeType) else item
            for item in reference.co_consts))
        candidate = source.replace('import xml.etree', 'import xml.etree as _owned_import').replace(
            'return xml.etree', 'return _owned_import.etree')
        spaces, _, selected = self.repaired(source, candidate, reference)
        self.assertTrue(selected)
        self.assertEqual(spaces[1]['select'](), spaces[0]['select']())
        self.unchanged('''def select():
    import xml.etree as actual_alias
    return actual_alias.__name__
''')

    def test_compound_conditional_values_retain_short_circuit_and_work(self):
        source = '''def select(first, second, work):
    return work(first * second) if first is not None and second is not None else None
'''
        candidate = '''def select(first, second, work):
    return first if first is not None else work(first * second) if second is not None else None
'''
        spaces, _, selected = self.repaired(source, candidate)
        self.assertTrue(selected)
        for first in (None, 0, 2):
            for second in (None, 0, 3):
                outcomes = []
                for space in spaces:
                    calls = []
                    def work(value):
                        calls.append(value)
                        return value + 1
                    outcomes.append((space['select'](first, second, work), calls))
                self.assertEqual(outcomes[1], outcomes[0])
        self.unchanged(candidate)

    def test_generator_conjunctions_repair_children_of_matching_owner(self):
        source = '''def select(rows, first, second):
    key = lambda row: row['value']
    return list(key(row) for row in rows if first(row) if second(row))
'''
        candidate = source.replace('if first(row) if second(row)',
                                   'if first(row) or second(row)')
        before = compare(compile_source(source, '<reference>'),
                         compile_source(candidate, '<candidate>'))
        self.assertTrue(next(item for item in before['entries']
                             if item['unit'] == '<module>/select#1')['matched'])
        spaces, _, selected = self.repaired(source, candidate)
        self.assertTrue(selected)
        for fail in (None, 1, 2):
            outcomes = []
            for space in spaces:
                events = []
                def first(row):
                    events.append(('first', row['value']))
                    if fail == 1 and row['value'] == 2:
                        raise ValueError('first predicate')
                    return row['value'] > 0
                def second(row):
                    events.append(('second', row['value']))
                    if fail == 2 and row['value'] == 2:
                        raise ValueError('second predicate')
                    return row['value'] % 2 == 0
                try:
                    result = space['select']([{'value': v} for v in (-1, 0, 1, 2, 3)], first, second)
                except ValueError as error:
                    result = str(error)
                outcomes.append((result, events))
            self.assertEqual(outcomes[1], outcomes[0])
        self.unchanged(candidate)

    def test_mixed_real_or_filter_is_not_changed_to_a_conjunction(self):
        source = '''def select(rows):
    return [row for row in rows if row > 0 or row == -2 if row % 2 == 0]
'''
        spaces, tree = self.unchanged(source)
        self.assertEqual(spaces[1]['select']([-2, -1, 0, 1, 2]), [-2, 2])
        self.assertTrue(any(isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
                            for node in ast.walk(tree)))


if __name__ == '__main__':
    unittest.main()
