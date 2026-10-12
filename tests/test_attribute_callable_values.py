"""Owned callable-value shapes, descriptor behavior, laziness and rejection."""
import ast
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import restore_attribute_callable_values


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class AttributeCallableValueTests(unittest.TestCase):
    def recover(self, source, candidate):
        original = compile_source(source, 'owned-original')
        tree, selected = restore_attribute_callable_values(ast.parse(candidate), original, None)
        rebuilt = compile_source(ast.unparse(tree), 'owned-recovered')
        return original, rebuilt, tree, selected

    def test_descriptor_getters_and_argument_order_remain_exact(self):
        source = '''def pick(target, read):
    return (target.work if True else None)(read(), extra=read())
'''
        candidate = source.replace('(target.work if True else None)', 'target.work')
        original, rebuilt, tree, selected = self.recover(source, candidate)
        self.assertEqual(selected, ['<module>/pick#1'])
        self.assertTrue(compare(original, rebuilt)['all_units_match'])
        namespaces = [{}, {}]; exec(original, namespaces[0]); exec(rebuilt, namespaces[1])
        for failure in (None, 'lookup', 'argument', 'call'):
            outcomes = []
            for namespace in namespaces:
                events = []
                class Target:
                    @property
                    def work(self):
                        events.append('lookup')
                        if failure == 'lookup': raise AttributeError('owned lookup')
                        def call(value, *, extra):
                            events.append(('call', value, extra))
                            if failure == 'call': raise RuntimeError('owned call')
                            return value + extra
                        return call
                def read():
                    events.append('argument')
                    if failure == 'argument': raise ValueError('owned argument')
                    return len(events)
                try: value = namespace['pick'](Target(), read)
                except (AttributeError, ValueError, RuntimeError) as error:
                    value = type(error).__name__, str(error)
                outcomes.append((value, events))
            self.assertEqual(outcomes[1], outcomes[0])
        self.assertEqual(restore_attribute_callable_values(tree, original, None)[1], [])

    def test_nested_generator_retains_closure_laziness_and_bound_methods(self):
        source = '''def pick(target, values):
    def produce():
        return ((target.work if True else None)(value) for value in values)
    return produce()
'''
        original, rebuilt, _, selected = self.recover(
            source, source.replace('(target.work if True else None)', 'target.work'))
        self.assertEqual(selected, ['<module>/pick#1/produce#1/<genexpr>#1'])
        self.assertTrue(compare(original, rebuilt)['all_units_match'])
        namespaces = [{}, {}]; exec(original, namespaces[0]); exec(rebuilt, namespaces[1])
        for values in ([], [1], [1, 2, 3]):
            outcomes = []
            for namespace in namespaces:
                calls = []
                class Target:
                    def work(self, value): calls.append(value); return value * 2
                generator = namespace['pick'](Target(), values)
                self.assertEqual(calls, [])
                first = next(generator, None)
                calls_after_first = list(calls)
                outcomes.append((first, calls_after_first, tuple(generator), calls))
            self.assertEqual(outcomes[1], outcomes[0])

    def test_coroutine_keeps_handled_call_errors_and_cancellation(self):
        source = '''async def pick(target):
    try:
        return await (target.work if True else None)(3)
    except ValueError:
        return 'handled'
'''
        original, rebuilt, _, selected = self.recover(
            source, source.replace('(target.work if True else None)', 'target.work'))
        self.assertEqual(selected, ['<module>/pick#1'])
        self.assertTrue(compare(original, rebuilt)['all_units_match'])
        namespaces = [{}, {}]; exec(original, namespaces[0]); exec(rebuilt, namespaces[1])
        for error in (None, ValueError('owned'), RuntimeError('owned'), asyncio.CancelledError()):
            outcomes = []
            for namespace in namespaces:
                calls = []
                class Target:
                    async def work(self, value):
                        calls.append(value)
                        if error is not None: raise error
                        return value * 2
                try: value = asyncio.run(namespace['pick'](Target()))
                except (RuntimeError, asyncio.CancelledError) as raised:
                    value = type(raised).__name__
                outcomes.append((value, calls))
            self.assertEqual(outcomes[1], outcomes[0])

    def test_unproven_changes_and_mixed_call_shapes_are_retained(self):
        source = '''def pick(target):
    return (target.work if True else None)(3)
'''
        for candidate in (
            source.replace('(target.work if True else None)', 'target.work').replace('(3)', '(4)'),
            source.replace('(target.work if True else None)', 'target.other'),
            source.replace('(target.work if True else None)', 'target.work').replace('(3)', '(amount=3)'),
        ):
            original, rebuilt, _, selected = self.recover(source, candidate)
            self.assertEqual(selected, [])
            self.assertFalse(compare(original, rebuilt)['all_units_match'])
        mixed = '''def pick(target):
    return (target.work if True else None)(3), target.work(4)
'''
        _, rebuilt, _, selected = self.recover(
            mixed, mixed.replace('(target.work if True else None)', 'target.work'))
        self.assertEqual(selected, [])
        self.assertEqual(rebuilt.co_consts[0].co_code,
                         compile_source(mixed.replace('(target.work if True else None)', 'target.work'),
                                        'owned-direct').co_consts[0].co_code)
        original, rebuilt, _, selected = self.recover(source, source)
        self.assertEqual(selected, [])
        self.assertTrue(compare(original, rebuilt)['all_units_match'])

    def test_callable_and_shared_coroutine_return_require_a_joint_trial(self):
        source = '''async def select(target, read):
    value = read()
    if value is None:
        value = await (target.create if True else None)(3)
    elif not value.ready:
        await value.connect()
    return value if True else None
'''
        direct = source.replace('(target.create if True else None)', 'target.create').replace(
            'return value if True else None', 'return value')
        original = compile_source(source, 'owned-original')
        for partial in (direct, direct.replace('target.create', '(target.create if True else None)'),
                        direct.replace('return value', 'return value if True else None')):
            self.assertFalse(compare(original, compile_source(partial, 'owned-partial'))['all_units_match'])
        original, rebuilt, tree, selected = self.recover(source, direct)
        self.assertEqual(selected, ['<module>/select#1'])
        self.assertTrue(compare(original, rebuilt)['all_units_match'])
        self.assertEqual(restore_attribute_callable_values(tree, original, None)[1], [])
        namespaces = [{}, {}]
        for code, namespace in zip((original, rebuilt), namespaces): exec(code, namespace)
        for initial in ('missing', 'connected', 'disconnected'):
            for failure in ('', 'read', 'lookup', 'create', 'connect', 'cancel-create', 'cancel-connect'):
                outputs = []
                for namespace in namespaces:
                    events = []
                    class Connection:
                        ready = initial == 'connected'
                        async def connect(self):
                            events.append('connect'); await asyncio.sleep(0)
                            if failure == 'connect': raise RuntimeError('owned reconnect')
                            if failure == 'cancel-connect': raise asyncio.CancelledError('owned reconnect cancelled')
                            self.ready = True
                    connection = Connection()
                    class Target:
                        @property
                        def create(self):
                            events.append('lookup')
                            if failure == 'lookup': raise AttributeError('owned descriptor')
                            async def create(amount):
                                events.append(('create', amount)); await asyncio.sleep(0)
                                if failure == 'create': raise ValueError('owned create')
                                if failure == 'cancel-create': raise asyncio.CancelledError('owned create cancelled')
                                connection.ready = True
                                return connection
                            return create
                    def read():
                        events.append('read')
                        if failure == 'read': raise ValueError('owned read')
                        return None if initial == 'missing' else connection
                    try:
                        result = asyncio.run(namespace['select'](Target(), read))
                        self.assertIs(result, connection)
                        self.assertTrue(result.ready)
                        outcome = ('connection', result.ready)
                    except (AttributeError, ValueError, RuntimeError, asyncio.CancelledError) as error:
                        outcome = (type(error).__name__, str(error))
                    outputs.append((outcome, events))
                self.assertEqual(outputs[1], outputs[0])

    def test_joint_trials_preserve_nested_scopes_and_reject_wrong_work(self):
        source = '''def untouched(value):
    return value + 1
class Owner:
    async def select(self, target, read):
        value = read()
        if value is None:
            value = await (target.create if True else None)(3)
        elif not value.ready:
            await value.connect()
        return value if True else None
'''
        direct = source.replace('(target.create if True else None)', 'target.create').replace(
            'return value if True else None', 'return value')
        original, rebuilt, _, selected = self.recover(source, direct)
        self.assertEqual(selected, ['<module>/Owner#1/select#1'])
        self.assertTrue(compare(original, rebuilt)['all_units_match'])
        for wrong in (direct.replace('create(3)', 'create(4)'),
                      direct.replace('await value.connect()', 'await target.connect()'),
                      direct.replace('not value.ready', 'value.ready')):
            original, rebuilt, tree, selected = self.recover(source, wrong)
            self.assertEqual(selected, [])
            report = compare(original, rebuilt)
            self.assertFalse(report['all_units_match'])
            self.assertTrue(next(e for e in report['entries'] if e['unit'] == '<module>/untouched#1')['matched'])
            self.assertEqual(ast.dump(tree, include_attributes=False),
                             ast.dump(ast.parse(wrong), include_attributes=False))
