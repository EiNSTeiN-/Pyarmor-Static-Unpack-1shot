"""Owned compiler-slot boundaries, retention and full recovery controls."""
import ast
import dis
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source, units
from modern_backend import restore_physical_local_slots


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class PhysicalLocalSlots(unittest.TestCase):
    def source(self, fillers=61, marker=True, docstring=False, nested=False):
        lines = ['def work(rows):']
        if docstring:
            lines.append('    """Select the first valid cost."""')
        lines.append('    if False:')
        if marker:
            lines.append('        __assert_armored__ = None')
        lines.extend(f'        dead_{index} = None' for index in range(fillers))
        lines.extend([
            '    for row in rows:',
            "        cost = row.get('cost')",
            '        if row and cost is not None:',
            '            return cost',
            '    return None',
        ])
        if nested:
            lines.insert(-1, '    unused = lambda: 7')
        return '\n'.join(lines) + '\n'

    def restore(self, original, candidate):
        reference = compile_source(original, 'original')
        tree, selected = restore_physical_local_slots(ast.parse(candidate), reference, None)
        code = compile_source(ast.unparse(tree), 'rebuilt')
        return reference, code, tree, selected

    def test_slot_64_crossing_is_repaired_and_nearby_slots_stay_untouched(self):
        for fillers in range(59, 64):
            original = self.source(fillers)
            candidate = self.source(fillers, marker=False)
            reference, code, _, selected = self.restore(original, candidate)
            self.assertTrue(compare(reference, code)['all_units_match'])
            self.assertEqual(bool(selected), fillers == 61)
            before = dict(units(reference))['<module>/work#1']
            self.assertEqual(before.co_varnames.index('cost'), fillers + 3)
            spaces = [{}, {}]
            for compiled, space in zip((reference, code), spaces):
                exec(compiled, space)
            for rows in ([], [{}], [{'cost': None}], [{'cost': 0}],
                         [{'cost': 4}], [{}, {'cost': 8}]):
                self.assertEqual(spaces[0]['work'](rows), spaces[1]['work'](rows))

    def test_docstring_and_existing_descendant_are_preserved(self):
        original = self.source(docstring=True, nested=True)
        candidate = self.source(marker=False, docstring=True, nested=True)
        reference, code, tree, selected = self.restore(original, candidate)
        self.assertTrue(selected)
        self.assertTrue(compare(reference, code)['all_units_match'])
        self.assertEqual(ast.get_docstring(tree.body[0]), 'Select the first valid cost.')
        self.assertIsInstance(tree.body[0].body[1], ast.If)
        self.assertIs(tree.body[0].body[1].test.value, False)
        for instructions in (dis.get_instructions(dict(units(code))['<module>/work#1']),):
            self.assertFalse(any(item.argval == '__assert_armored__' for item in instructions))

    def test_already_matching_original_marker_is_not_reclaimed(self):
        original = self.source()
        reference, code, _, selected = self.restore(original, original)
        self.assertFalse(selected)
        self.assertTrue(compare(reference, code)['all_units_match'])

    def test_original_without_marker_never_gains_an_invented_local(self):
        original = self.source(marker=False)
        reference, code, tree, selected = self.restore(original, original)
        self.assertFalse(selected)
        self.assertNotIn('__assert_armored__', ast.unparse(tree))
        self.assertTrue(compare(reference, code)['all_units_match'])

    def test_marker_cannot_hide_changed_work_or_exception_coverage(self):
        original = self.source()
        candidates = [
            self.source(marker=False).replace('return cost', 'return cost + 1'),
            self.source(marker=False).replace("cost = row.get('cost')",
                "try:\n            cost = row.get('cost')\n        except ValueError:\n            return 0"),
            self.source(marker=False).replace('    for row in rows:',
                '    if False:\n        invented = None\n    for row in rows:'),
        ]
        # An extra ordinary local is significant even if its declaration is
        # dead; only the original hidden marker has the canonical exception.
        for candidate in candidates:
            reference, code, tree, selected = self.restore(original, candidate)
            self.assertFalse(selected)
            self.assertFalse(compare(reference, code)['all_units_match'])
            self.assertNotIn('__assert_armored__', ast.unparse(tree))


if __name__ == '__main__':
    unittest.main()
