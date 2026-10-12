"""Owned lexical capture fixtures; closure slots remain verifier-significant."""
import ast
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import restore_capture_name_order


def renamed_metadata(code, names):
    """Model private name replacement without changing any operand positions."""
    return code.replace(
        co_varnames=tuple(names.get(name, name) for name in code.co_varnames),
        co_cellvars=tuple(names.get(name, name) for name in code.co_cellvars),
        co_freevars=tuple(names.get(name, name) for name in code.co_freevars),
        co_consts=tuple(renamed_metadata(value, names) if isinstance(value, types.CodeType)
                        else value for value in code.co_consts))


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class CaptureNameOrderTests(unittest.TestCase):
    def recover(self, source, names, wrong=None):
        reference = renamed_metadata(compile_source(source, 'owned-original'), names)
        candidate = source
        for name, spelling in names.items():
            candidate = candidate.replace(name, spelling)
        if wrong:
            candidate = wrong(candidate)
        tree, selected = restore_capture_name_order(ast.parse(candidate), reference, None)
        return reference, compile_source(ast.unparse(tree), 'owned-recovered'), tree, selected

    def test_multiple_private_cells_preserve_values_generator_and_nonlocal_mutation(self):
        source = '''def factory(first, second):
    alpha = first
    omega = second
    def update(delta):
        nonlocal alpha
        alpha += delta
        return alpha - omega
    def values():
        return (alpha - omega for _ in range(3))
    return update, values
'''
        reference, recovered, tree, selected = self.recover(
            source, {'alpha': '_var_var_9', 'omega': '_var_var_2'})
        self.assertTrue(selected)
        self.assertTrue(compare(reference, recovered)['all_units_match'])
        outputs = []
        for code in (reference, recovered):
            namespace = {}; exec(code, namespace)
            update, values = namespace['factory'](10, 3)
            outputs.append((list(values()), update(2), list(values()), update(-5), list(values())))
        self.assertEqual(outputs[1], outputs[0])
        self.assertEqual(outputs[1], ([7, 7, 7], 9, [9, 9, 9], 4, [4, 4, 4]))
        self.assertEqual(restore_capture_name_order(tree, reference, None)[1], [])

    def test_public_self_parameter_and_global_are_preserved(self):
        source = '''_var_var_8 = 3
class Sensor:
    def reading(self, value):
        telemetry = value
        return lambda: self.offset + telemetry + _var_var_8
'''
        reference, recovered, tree, selected = self.recover(source, {'telemetry': '_var_var_2'})
        self.assertTrue(selected)
        self.assertTrue(compare(reference, recovered)['all_units_match'])
        self.assertIn('_var_var_8 = 3', ast.unparse(tree))
        self.assertIn('def reading(self, value)', ast.unparse(tree))
        outputs = []
        for code in (reference, recovered):
            namespace = {}; exec(code, namespace)
            sensor = namespace['Sensor'](); sensor.offset = 5
            read = sensor.reading(value=10)
            outputs.append(read()); sensor.offset = 7; outputs.append(read())
        self.assertEqual(outputs, [18, 20, 18, 20])

    def test_changed_capture_use_is_still_rejected(self):
        source = '''def factory(first, second):
    alpha = first
    omega = second
    return lambda: alpha - omega
'''
        reference, recovered, _, selected = self.recover(source,
            {'alpha': '_var_var_9', 'omega': '_var_var_2'},
            lambda text: text.replace('lambda: _var_var_9 - _var_var_2',
                                      'lambda: _var_var_2 - _var_var_9'))
        report = compare(reference, recovered)
        self.assertFalse(report['all_units_match'])
        # The swapped expression happens to use the old child's numeric slots;
        # its parent binds those slots in the wrong order. Correcting the parent
        # would lose the prior child match, so the complete trial is rejected.
        self.assertEqual(selected, [])
        parent = next(entry for entry in report['entries'] if entry['unit'] == '<module>/factory#1')
        self.assertFalse(parent['matched'])
        self.assertEqual(parent['reason'], 'instructions')

    def test_already_matching_and_public_keyword_parameters_are_unchanged(self):
        source = '''def factory(_var_var_2, _var_var_9):
    return lambda: _var_var_2 - _var_var_9
'''
        original = compile_source(source, 'owned-original')
        tree, selected = restore_capture_name_order(ast.parse(source), original, None)
        self.assertEqual(selected, [])
        self.assertIn('def factory(_var_var_2, _var_var_9)', ast.unparse(tree))
        self.assertTrue(compare(original, compile_source(ast.unparse(tree), 'owned-recovered'))['all_units_match'])
