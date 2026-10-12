"""Owned guard scope and rich-comparison cases, with complete subtree proof."""
import ast
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import restore_shared_guard_shapes

@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class SharedGuardShapes(unittest.TestCase):
    def prove(self, original, candidate):
        reference = compile_source(original, '<owned-reference>')
        tree, selected = restore_shared_guard_shapes(ast.parse(candidate), reference, None)
        code = compile_source(ast.unparse(tree), '<owned-rebuilt>')
        self.assertTrue(compare(reference, code)['all_units_match'])
        spaces = [{}, {}]
        for compiled, space in zip((reference, code), spaces):
            exec(compiled, space)
        return spaces, selected

    def test_two_rejections_must_be_corrected_together(self):
        source = '''def select(value, lower, upper):
    if not isinstance(value, int) or value < lower:
        raise ValueError('value')
    if not isinstance(upper, int) or upper < lower:
        raise ValueError('upper')
    return value < upper
'''
        candidate = source.replace("if not isinstance(value, int) or value < lower:",
            "if isinstance(value, int):\n        if value < lower:").replace(
            "raise ValueError('value')", "    raise ValueError('value')").replace(
            "if not isinstance(upper, int) or upper < lower:",
            "if isinstance(upper, int):\n        if upper < lower:").replace(
            "raise ValueError('upper')", "    raise ValueError('upper')")
        spaces, selected = self.prove(source, candidate)
        self.assertTrue(selected)
        for value in (None, -1, 0, 2):
            for upper in (None, -1, 0, 3):
                outcomes = []
                for space in spaces:
                    try: outcome = ('return', space['select'](value, 0, upper))
                    except ValueError as error: outcome = ('error', str(error))
                    outcomes.append(outcome)
                self.assertEqual(outcomes[0], outcomes[1])
        _, selected = self.prove(candidate, candidate)
        self.assertFalse(selected)

    def test_chronological_scope_keeps_generator_child_and_fallback(self):
        source = '''def select(minute, events):
    dawn, noon, dusk = events
    valid = all(isinstance(value, int) for value in events)
    if valid:
        if 0 <= dawn < noon < dusk < 1440:
            cutoff = noon + 20
            if minute < dawn:
                return 'night'
            if minute < cutoff:
                return 'day'
            return 'night'
    return 'fallback'
'''
        tree = ast.parse(source)
        guard = tree.body[0].body[2]
        suffix = guard.body[0].body[3:]
        guard.body[0].body = guard.body[0].body[:3]
        guard.body.extend(suffix)
        candidate = ast.unparse(tree)
        spaces, selected = self.prove(source, candidate)
        self.assertTrue(selected)
        for events in ((10, 100, 200), (200, 100, 10), (0, 0, 0), (None, 100, 200)):
            for minute in (0, 50, 100, 220):
                self.assertEqual(spaces[0]['select'](minute, events),
                                 spaces[1]['select'](minute, events))
        _, selected = self.prove(candidate, candidate)
        self.assertFalse(selected)

    def test_real_nested_guard_keeps_rich_comparison_and_sibling_scope(self):
        source = '''def select(first, second, last, work):
    if first == second:
        if isinstance(last, dict):
            if last.get('reject'):
                return 'reject'
        work()
    return 'accept'
'''
        spaces, selected = self.prove(source, source)
        self.assertFalse(selected)
        for accepted in (False, True):
            outputs = []
            for space in spaces:
                events = []
                class Value:
                    def __eq__(self, other):
                        events.append('eq')
                        return accepted
                    def __ne__(self, other):
                        raise AssertionError('not-equal is independent')
                result = space['select'](Value(), None, {}, lambda: events.append('work'))
                outputs.append((result, events))
            self.assertEqual(outputs[0], outputs[1])

    def test_shared_rejection_keeps_actual_not_equal_operator(self):
        source = '''def select(first, second, value):
    if first != second or not isinstance(value, dict) or value.get('reject'):
        return None
    return value
'''
        candidate = '''def select(first, second, value):
    if first == second:
        if isinstance(value, dict):
            if value.get('reject'):
                return None
    return value
'''
        spaces, selected = self.prove(source, candidate)
        self.assertTrue(selected)
        for unequal in (False, True):
            for value in ({}, {'reject': True}, [], None):
                outcomes = []
                for space in spaces:
                    events = []
                    class Value:
                        def __ne__(self, other):
                            events.append('ne')
                            return unequal
                        def __eq__(self, other):
                            raise AssertionError('equality is independent')
                    outcomes.append((space['select'](Value(), None, value), events))
                self.assertEqual(outcomes[0], outcomes[1])

    def test_negated_equality_retains_eq_and_ne_independence(self):
        source = '''def select(first, second, reject):
    if not first == second or reject():
        return 'rejected'
    return 'accepted'
'''
        candidate = '''def select(first, second, reject):
    if first == second:
        if reject():
            return 'rejected'
    return 'accepted'
'''
        spaces, selected = self.prove(source, candidate)
        self.assertTrue(selected)
        for accepted in (False, True):
            for rejected in (False, True):
                outcomes = []
                for space in spaces:
                    events = []
                    class Value:
                        def __eq__(self, other):
                            events.append('eq')
                            return accepted
                        def __ne__(self, other):
                            raise AssertionError('not-equal is independent')
                    def reject():
                        events.append('predicate')
                        return rejected
                    outcomes.append((space['select'](Value(), None, reject), events))
                self.assertEqual(outcomes[0], outcomes[1])

    def test_timer_handler_and_clamping_remain_in_their_original_scopes(self):
        source = '''def select(stamp, parse, clamp):
    try:
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
        tree = ast.parse(source)
        outer = tree.body[0].body[0]
        guard = outer.body[1]
        inner = guard.body[0]
        guard.body = [ast.Pass()]
        returned = outer.body.pop()
        outer.body.append(inner)
        tree.body[0].body.append(returned)
        candidate = ast.unparse(tree)
        spaces, selected = self.prove(source, candidate)
        self.assertTrue(selected)
        for stamp in (None, '', 'valid', 'invalid'):
            for clamp_error in (False, True):
                outcomes = []
                for space in spaces:
                    events = []
                    def parse(value):
                        events.append(('parse', value))
                        if value == 'invalid':
                            raise ValueError('timer')
                        return 3
                    def clamp(value):
                        events.append(('clamp', value))
                        if clamp_error:
                            raise RuntimeError('clamp')
                        return value
                    outcomes.append((space['select'](stamp, parse, clamp), events))
                self.assertEqual(outcomes[0], outcomes[1])

    def test_optional_query_remains_inside_database_context(self):
        source = '''def select(context):
    try:
        with context as db:
            rows = db.primary()
            extra = {}
            try:
                extra = db.optional()
            except ValueError:
                extra = {}
        return rows, extra
    except RuntimeError:
        return 'fallback'
'''
        tree = ast.parse(source)
        outer = tree.body[0].body[0]
        context = outer.body[0]
        optional = context.body.pop()
        outer.body.insert(1, optional)
        candidate = ast.unparse(tree)
        spaces, selected = self.prove(source, candidate)
        self.assertTrue(selected)
        for failure in (None, 'primary', 'optional', 'exit'):
            outcomes = []
            for space in spaces:
                events = []
                class Database:
                    opened = False
                    def __enter__(self):
                        events.append('enter')
                        self.opened = True
                        return self
                    def __exit__(self, *error):
                        events.append(('exit', error[0].__name__ if error[0] else None))
                        self.opened = False
                        if failure == 'exit':
                            raise RuntimeError('exit')
                    def primary(self):
                        self.assert_open('primary')
                        if failure == 'primary':
                            raise RuntimeError('primary')
                        return [1]
                    def optional(self):
                        self.assert_open('optional')
                        if failure == 'optional':
                            raise ValueError('optional')
                        return {'extra': 2}
                    def assert_open(self, action):
                        events.append(action)
                        if not self.opened:
                            raise RuntimeError('closed')
                outcomes.append((space['select'](Database()), events))
            self.assertEqual(outcomes[0], outcomes[1])
        # A deliberately separate query retains its closed-context handling.
        _, selected = self.prove(candidate, candidate)
        self.assertFalse(selected)

    def test_guarded_date_success_suffix_and_shared_async_return(self):
        source = '''async def select(stamp, parse, save):
    try:
        value = None
        if stamp:
            try:
                value = parse(stamp)
            except ValueError:
                value = None
        await save(value)
    except RuntimeError:
        return
    return None if True else None
'''
        tree = ast.parse(source)
        function = tree.body[0]
        outer = function.body[0]
        guard = outer.body[1]
        inner = guard.body[0]
        guard.body = [ast.Pass()]
        inner.orelse = outer.body[2:]
        outer.body = outer.body[:2] + [inner]
        function.body.pop()
        spaces, selected = self.prove(source, ast.unparse(tree))
        self.assertTrue(selected)
        import asyncio
        for stamp in (None, '', 'valid', 'invalid'):
            for save_error in (False, True):
                outcomes = []
                for space in spaces:
                    events = []
                    def parse(value):
                        events.append(('parse', value))
                        if value == 'invalid':
                            raise ValueError('date')
                        return 7
                    async def save(value):
                        events.append(('save', value))
                        if save_error:
                            raise RuntimeError('save')
                    outcomes.append((asyncio.run(space['select'](stamp, parse, save)), events))
                self.assertEqual(outcomes[0], outcomes[1])

    def test_optional_query_keeps_both_context_and_outer_handler(self):
        source = '''def select(context):
    try:
        with context as read:
            rows = read('primary')
            extra = {}
            try:
                extra = read('optional')
            except ValueError:
                extra = {}
    except RuntimeError:
        return 'fallback'
    return rows, extra
'''
        tree = ast.parse(source)
        function = tree.body[0]
        optional = function.body[0].body[0].body.pop()
        function.body.insert(1, optional)
        candidate = ast.unparse(tree)
        spaces, selected = self.prove(source, candidate)
        self.assertTrue(selected)
        from contextlib import contextmanager
        for failure in (None, 'primary', 'optional', 'exit'):
            outcomes = []
            for space in spaces:
                events = []
                opened = [False]
                def read(action):
                    events.append(action)
                    if not opened[0]:
                        raise RuntimeError('closed')
                    if failure == action:
                        raise ValueError('optional') if action == 'optional' else RuntimeError('primary')
                    return [1] if action == 'primary' else {'extra': 2}
                @contextmanager
                def context():
                    events.append('enter')
                    opened[0] = True
                    try:
                        yield read
                    finally:
                        events.append('exit')
                        opened[0] = False
                        if failure == 'exit':
                            raise RuntimeError('exit')
                outcomes.append((space['select'](context()), events))
            self.assertEqual(outcomes[0], outcomes[1])
        _, selected = self.prove(candidate, candidate)
        self.assertFalse(selected)

if __name__ == '__main__':
    unittest.main()
