"""Owned loop payload and fragmented-handler trials with strict controls."""
import ast
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import restore_payload_and_handler_shapes


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class PayloadHandlerShapes(unittest.TestCase):
    def prove(self, original, candidate, accepted=True):
        reference = compile_source(original, '<owned-original>')
        tree, selected = restore_payload_and_handler_shapes(ast.parse(candidate), reference, None)
        rebuilt = compile_source(ast.unparse(tree), '<owned-rebuilt>')
        self.assertTrue(compare(reference, rebuilt)['all_units_match'])
        self.assertEqual(bool(selected), accepted)
        spaces = [{}, {}]
        for code, space in zip((reference, rebuilt), spaces):
            exec(code, space)
        return spaces

    def test_accepting_loop_preserves_protected_gap_and_errors(self):
        original = '''def select(values, convert):
    result = []
    try:
        for value in values:
            if value is not None:
                result.append(convert(value))
    except ValueError:
        return 'fallback'
    return result
'''
        candidate = original.replace('if value is not None:\n                result.append(convert(value))',
            'if value is None:\n                continue\n            result.append(convert(value))')
        before = compare(compile_source(original, 'original'), compile_source(candidate, 'candidate'))
        self.assertFalse(before['all_units_match'])
        self.assertTrue(any(entry.get('reason') == 'exception_table' for entry in before['entries']))
        spaces = self.prove(original, candidate)
        for values in ([], [None], [0, None, 3], [1, 2, 3]):
            for failure in (None, 2):
                outcomes = []
                for space in spaces:
                    events = []
                    def convert(value):
                        events.append(value)
                        if value == failure:
                            raise ValueError('owned conversion')
                        return value * 2
                    outcomes.append((space['select'](values, convert), events))
                self.assertEqual(outcomes[0], outcomes[1])

    def test_two_guards_in_same_suite_need_coupled_payload_ownership(self):
        original = '''def select(values):
    result = []
    try:
        for value in values:
            if value is not None:
                if value != 0:
                    result.append(100 / value)
    except (ValueError, ZeroDivisionError):
        return 'fallback'
    return result
'''
        candidate = '''def select(values):
    result = []
    try:
        for value in values:
            if value is None:
                continue
            if value == 0:
                continue
            result.append(100 / value)
    except (ValueError, ZeroDivisionError):
        return 'fallback'
    return result
'''
        spaces = self.prove(original, candidate)
        for values in ([], [None], [0], [None, 0, -5, 10]):
            self.assertEqual(spaces[0]['select'](values), spaces[1]['select'](values))

    def test_rich_negative_comparison_keeps_nan_and_operator_choice(self):
        original = '''def select(values, cutoff):
    result = []
    try:
        for value in values:
            if not value >= cutoff:
                result.append(value)
    except ValueError:
        return 'fallback'
    return result
'''
        candidate = original.replace('if not value >= cutoff:\n                result.append(value)',
            'if value >= cutoff:\n                continue\n            result.append(value)')
        spaces = self.prove(original, candidate)
        for values in ([], [-1, 0, 1], [float('nan')]):
            outputs = [space['select'](values, 0) for space in spaces]
            self.assertEqual(len(outputs[0]), len(outputs[1]))
            for left, right in zip(*outputs):
                self.assertTrue(left == right or math.isnan(left) and math.isnan(right))
        for space in spaces:
            events = []
            class Value:
                def __ge__(self, other):
                    events.append('ge')
                    return False
                def __lt__(self, other):
                    raise AssertionError('less-than is independent')
            value = Value()
            self.assertEqual(space['select']([value], 0), [value])
            self.assertEqual(events, ['ge'])

    def test_real_rejection_and_nested_loop_continue_are_retained(self):
        original = '''def select(groups):
    result = []
    try:
        for group in groups:
            for value in group:
                if value is None:
                    continue
                result.append(value)
            result.append('group')
    except ValueError:
        return 'fallback'
    return result
'''
        spaces = self.prove(original, original, accepted=False)
        for groups in ([], [[None]], [[1, None], [2]]):
            self.assertEqual(spaces[0]['select'](groups), spaces[1]['select'](groups))

    def test_pass_alias_keeps_original_docstring_and_handler(self):
        original = '''def select(value, convert):
    """Stable owned docstring."""
    try:
        if value is None:
            return False
        convert(value)
        return False
    except ValueError:
        return False
'''
        candidate = '''def select(value, convert):
    """Stable owned docstring."""
    try:
        if value is None:
            return False
    except ValueError:
        return False
    try:
        convert(value)
        return False
    except ValueError:
        pass
'''
        spaces = self.prove(original, candidate)
        for value in (None, 0, 3):
            for failure in (False, True):
                outcomes = []
                for space in spaces:
                    events = []
                    def convert(item):
                        events.append(item)
                        if failure:
                            raise ValueError('owned failure')
                    outcomes.append((space['select'](value, convert), events))
                    self.assertEqual(space['select'].__doc__, 'Stable owned docstring.')
                self.assertEqual(outcomes[0], outcomes[1])

    def test_handler_reunion_and_timer_guard_require_one_coupled_proof(self):
        original = '''def select(stamp, early, parse, clamp):
    """Keep the owner header."""
    try:
        if early:
            return 'early'
        timer = None
        if stamp:
            try:
                timer = parse(stamp)
            except ValueError:
                timer = None
        return clamp(timer)
    except RuntimeError:
        return 'fallback'
'''
        candidate = '''def select(stamp, early, parse, clamp):
    """Keep the owner header."""
    try:
        if early:
            return 'early'
    except RuntimeError:
        return 'fallback'
    try:
        timer = None
        if stamp:
            pass
        try:
            timer = parse(stamp)
        except ValueError:
            timer = None
    except RuntimeError:
        return 'fallback'
    return clamp(timer)
'''
        spaces = self.prove(original, candidate)
        for stamp in (None, '', 'valid', 'invalid'):
            for early in (False, True):
                for failure in (False, True):
                    outcomes = []
                    for space in spaces:
                        events = []
                        def parse(value):
                            events.append(('parse', value))
                            if value == 'invalid':
                                raise ValueError('owned timer')
                            return 3
                        def clamp(value):
                            events.append(('clamp', value))
                            if failure:
                                raise RuntimeError('owned clamp')
                            return value
                        outcomes.append((space['select'](stamp, early, parse, clamp), events))
                    self.assertEqual(outcomes[0], outcomes[1])

    def test_genuine_independent_handlers_and_bare_handlers_are_unchanged(self):
        for exception in ('ValueError', ''):
            source = '''def select(value, action):
    try:
        if value is None:
            return False
    except EXCEPTION:
        return False
    try:
        action(value)
    except EXCEPTION:
        pass
    return False
'''.replace('except EXCEPTION:', 'except ' + exception + ':' if exception else 'except:')
            spaces = self.prove(source, source, accepted=False)
            for value in (None, 1):
                events = [[], []]
                for index, space in enumerate(spaces):
                    space['select'](value, lambda item: events[index].append(item))
                self.assertEqual(events[0], events[1])

    def test_changed_predicate_or_discarded_docstring_is_rejected(self):
        source = '''def select(values):
    """Required owner metadata."""
    result = []
    try:
        for value in values:
            if value is not None:
                result.append(value)
    except ValueError:
        return 'fallback'
    return result
'''
        for candidate in (source.replace('value is not None', 'value != 0'),
                          source.replace('    """Required owner metadata."""\n', '')):
            reference = compile_source(source, 'original')
            tree, selected = restore_payload_and_handler_shapes(ast.parse(candidate), reference, None)
            self.assertFalse(selected)
            self.assertFalse(compare(reference, compile_source(ast.unparse(tree), 'candidate'))['all_units_match'])


if __name__ == '__main__':
    unittest.main()
