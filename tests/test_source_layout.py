"""Owned compiler-layout witnesses with strict whole-tree retention proofs."""
import ast
import importlib.util
import marshal
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import preserve_multiline_return_shapes, recover


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class SourceLayoutTests(unittest.TestCase):
    JOINT_SOURCE = '''def work(rows, convert):
    """Retain shared iterator cleanup after choosing a value."""
    for row in rows:
        return (
            ((convert(row) if row else (0 if True else None)) if True else None) if True else None
        )
    return 1
'''
    SOURCE = '''def work(rows, convert):
    """Select the first eligible row."""
    for row in rows:
        if row is None:
            continue
        try:
            return (
                convert(row) if row else None
            )
        except (TypeError, ValueError):
            return None
'''

    def exercise(self, source, rows, failure):
        namespace, events = {}, []
        exec(compile_source(source, 'owned-layout-execution'), namespace)

        def convert(value):
            events.append(('convert', value))
            errors = {'type': TypeError, 'value': ValueError, 'other': LookupError}
            if failure:
                raise errors[failure]('owned ' + failure)
            return float(value)

        try:
            result = ('return', namespace['work'](rows, convert))
        except BaseException as error:
            result = ('raise', type(error).__name__, str(error))
        return result, events

    def check_behavior(self, candidate, reference_source=None):
        for rows in ([], [None], [0], [2], [-3], ['bad'], [None, 2], [0, 2]):
            for failure in ('', 'type', 'value', 'other'):
                with self.subTest(rows=rows, failure=failure):
                    self.assertEqual(self.exercise(reference_source or self.SOURCE, rows, failure),
                                     self.exercise(candidate, rows, failure))

    def test_same_ast_layout_restores_the_shared_return(self):
        reference = compile_source(self.SOURCE, 'owned-original')
        dense = ast.unparse(ast.parse(self.SOURCE)) + '\n'
        self.assertFalse(compare(reference, compile_source(dense, 'owned-dense'))['all_units_match'])
        fixed, selected = preserve_multiline_return_shapes(dense, reference, None)
        self.assertEqual(selected, ['<module>/work#1'])
        self.assertTrue(compare(reference, compile_source(fixed, 'owned-fixed'))['all_units_match'])
        self.assertEqual(ast.dump(ast.parse(dense)), ast.dump(ast.parse(fixed)))
        self.assertIn('# Keep this line boundary', fixed)
        self.assertEqual(preserve_multiline_return_shapes(fixed, reference, None), (fixed, []))
        self.check_behavior(fixed)

    def test_displaced_try_return_requires_coupled_layout(self):
        tree = ast.parse(self.SOURCE)
        loop = tree.body[0].body[1]
        region = loop.body[-1]
        statement = region.body.pop()
        region.body = [ast.Pass()]
        loop.body.append(statement)
        displaced = ast.unparse(ast.fix_missing_locations(tree)) + '\n'
        reference = compile_source(self.SOURCE, 'owned-original')
        self.assertNotEqual(self.exercise(self.SOURCE, [2], 'value'),
                            self.exercise(displaced, [2], 'value'))
        fixed, selected = preserve_multiline_return_shapes(displaced, reference, None)
        self.assertEqual(selected, ['<module>/work#1'])
        self.assertTrue(compare(reference, compile_source(fixed, 'owned-fixed'))['all_units_match'])
        self.check_behavior(fixed)

    def test_changed_values_and_handlers_are_rejected(self):
        reference = compile_source(self.SOURCE, 'owned-original')
        dense = ast.unparse(ast.parse(self.SOURCE)) + '\n'
        for changed in (dense.replace('convert(row)', 'convert(row) + 1'),
                        dense.replace('if row else None', 'if row else 0'),
                        dense.replace('(TypeError, ValueError)', 'TypeError')):
            with self.subTest(source=changed):
                fixed, selected = preserve_multiline_return_shapes(changed, reference, None)
                self.assertEqual(selected, [])
                self.assertEqual(fixed, changed)
                self.assertFalse(compare(reference, compile_source(fixed, 'owned-rejected'))['all_units_match'])

    def check_joint_behavior(self, candidate):
        for rows in ([], [0], [2], [-3], [None], [None, 2], [2, 3]):
            for failure in ('', 'type', 'value', 'other'):
                with self.subTest(rows=rows, failure=failure):
                    self.assertEqual(self.exercise(self.JOINT_SOURCE, rows, failure),
                                     self.exercise(candidate, rows, failure))

    def test_asymmetric_join_retains_the_for_only_shared_cleanup(self):
        reference = compile_source(self.JOINT_SOURCE, 'owned-original')
        tree = ast.parse(self.JOINT_SOURCE)
        returned = tree.body[0].body[1].body[0]
        returned.value = returned.value.body.body
        returned.value.orelse = returned.value.orelse.body
        dense = ast.unparse(ast.fix_missing_locations(tree)) + '\n'
        self.assertFalse(compare(reference, compile_source(dense, 'owned-dense'))['all_units_match'])
        fixed, selected = preserve_multiline_return_shapes(dense, reference, None)
        self.assertEqual(selected, ['<module>/work#1'])
        self.assertTrue(compare(reference, compile_source(fixed, 'owned-fixed'))['all_units_match'])
        self.assertEqual(preserve_multiline_return_shapes(fixed, reference, None), (fixed, []))
        self.check_joint_behavior(fixed)

    def test_asymmetric_join_does_not_accept_different_work_or_results(self):
        reference = compile_source(self.JOINT_SOURCE, 'owned-original')
        dense = ast.unparse(ast.parse(self.JOINT_SOURCE)) + '\n'
        for changed in (dense.replace('convert(row)', 'convert(row) + 1'),
                        dense.replace('0 if True else None', '1 if True else None'),
                        dense.replace('if row else', 'if not row else')):
            with self.subTest(source=changed):
                fixed, selected = preserve_multiline_return_shapes(changed, reference, None)
                self.assertEqual(selected, [])
                self.assertEqual(fixed, changed)
                self.assertFalse(compare(reference, compile_source(fixed, 'owned-negative'))['all_units_match'])

    @unittest.skipUnless(os.environ.get('PYARMOR_MODERN_BACKEND'),
                         'Set PYARMOR_MODERN_BACKEND for the complete pipeline')
    def test_asymmetric_join_survives_the_complete_source_pipeline(self):
        reference = compile_source(self.JOINT_SOURCE, 'owned-original')
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / 'joint.1shot'
            Path(str(prefix) + '.recovered.pyc').write_bytes(
                importlib.util.MAGIC_NUMBER + bytes(12) + marshal.dumps(reference))
            sequence = Path(str(prefix) + '.seq')
            sequence.write_bytes(b'\xa1' + bytes(16) + b'\xa2' + bytes(12) + b'\xf0\xff\x00\x00')
            report = recover(sequence, Path(os.environ['PYARMOR_MODERN_BACKEND']).resolve())
            self.assertTrue(report['syntax_valid'], report)
            source = Path(str(prefix) + '.cdc.py').read_text()
            self.assertTrue(compare(reference, compile_source(source, 'owned-joint-pipeline'))['all_units_match'])
            self.assertEqual(report['verified_multiline_return_shapes'], ['<module>/work#1'])
            self.check_joint_behavior(source)

    def test_decorated_owner_descendants_and_neighbors_are_retained(self):
        source = '''def keep(function):
    return function
class Owner:
    @staticmethod
    @keep
    def work(rows, convert):
        """Build the first group."""
        for row in rows:
            try:
                return (
                    tuple(convert(value) for value in row) if row else None
                )
            except (TypeError, ValueError):
                return None
def neighbor(value):
    return value + 1
'''
        reference = compile_source(source, 'owned-original')
        dense = ast.unparse(ast.parse(source)) + '\n'
        changed = dense.replace('return value + 1', 'return value + 2')
        before = compare(reference, compile_source(changed, 'owned-before'))
        fixed, selected = preserve_multiline_return_shapes(changed, reference, None)
        after = compare(reference, compile_source(fixed, 'owned-after'))
        self.assertEqual(selected, ['<module>/Owner#1/work#1'])
        self.assertTrue({entry['unit'] for entry in before['entries'] if entry['matched']}
                        <= {entry['unit'] for entry in after['entries'] if entry['matched']})
        self.assertFalse(after['all_units_match'])
        self.assertIn('return value + 2', fixed)
        original, repaired = {}, {}
        exec(reference, original)
        exec(compile_source(fixed, 'owned-after'), repaired)
        for rows in ([], [[]], [[1, 2]], [['bad']], [[1], [2]]):
            self.assertEqual(original['Owner'].work(rows, float), repaired['Owner'].work(rows, float))
        self.assertEqual(repaired['Owner'].work.__doc__, original['Owner'].work.__doc__)

    @unittest.skipUnless(os.environ.get('PYARMOR_MODERN_BACKEND'),
                         'Set PYARMOR_MODERN_BACKEND for the complete pipeline')
    def test_complete_pipeline_keeps_the_proved_line_boundary(self):
        source_fixture = self.SOURCE.replace('return None\n', 'return -1\n')
        reference = compile_source(source_fixture, 'owned-original')
        backend = Path(os.environ['PYARMOR_MODERN_BACKEND']).resolve()
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / 'owned.1shot'
            Path(str(prefix) + '.recovered.pyc').write_bytes(
                importlib.util.MAGIC_NUMBER + bytes(12) + marshal.dumps(reference))
            sequence = Path(str(prefix) + '.seq')
            sequence.write_bytes(b'\xa1' + bytes(16) + b'\xa2' + bytes(12) + b'\xf0\xff\x00\x00')
            report = recover(sequence, backend)
            self.assertTrue(report['syntax_valid'], report)
            source = Path(str(prefix) + '.cdc.py').read_text()
            self.assertTrue(compare(reference, compile_source(source, 'owned-pipeline'))['all_units_match'])
            self.assertEqual(report['verified_multiline_return_shapes'], ['<module>/work#1'])
            self.assertIn('# Keep this line boundary', source)
            self.check_behavior(source, source_fixture)


if __name__ == '__main__':
    unittest.main()
