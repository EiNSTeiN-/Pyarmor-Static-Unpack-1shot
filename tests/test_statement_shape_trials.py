"""Owned strict proofs for context scope, return edges and short circuits."""
import ast
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import (restore_context_boundaries, restore_coupled_statement_shapes,
                            preserve_individual_return_edges)


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class StatementShapeTrials(unittest.TestCase):
    def repaired(self, source, candidate, repair):
        original = compile_source(source, '<owned-original>')
        tree, selected = repair(ast.parse(candidate), original, None)
        rebuilt = compile_source(ast.unparse(tree), '<owned-rebuilt>')
        self.assertTrue(compare(original, rebuilt)['all_units_match'])
        namespaces = [{}, {}]
        for code, namespace in zip((original, rebuilt), namespaces):
            exec(code, namespace)
        return namespaces, tree, selected

    def test_context_closes_before_writes_and_keeps_suppressed_setup_failure(self):
        source = '''async def select(database, required):
    async with database.query() as cursor:
        rows = await cursor.fetch()
        names = {row for row in rows}
    if required not in names:
        await database.alter(required)
    return True
'''
        tree = ast.parse(source)
        tree.body[0].body[0].body.extend(tree.body[0].body[1:])
        tree.body[0].body = tree.body[0].body[:1]
        namespaces, _, selected = self.repaired(source, ast.unparse(tree), restore_context_boundaries)
        self.assertTrue(selected)
        class Database:
            def __init__(self, failure, suppress):
                self.failure, self.suppress, self.events = failure, suppress, []
            def query(self): return self
            async def __aenter__(self): self.events.append('enter'); return self
            async def __aexit__(self, kind, error, traceback):
                self.events.append(('exit', kind.__name__ if kind else None))
                return self.suppress
            async def fetch(self):
                self.events.append('fetch')
                if self.failure: raise ValueError('owned fetch')
                return ['present']
            async def alter(self, value): self.events.append(('alter', value))
        async def outcome(function, required, failure, suppress):
            database = Database(failure, suppress)
            try: result = ('value', await function(database, required))
            except Exception as error: result = (type(error).__name__, str(error))
            return result, database.events
        for required in ('present', 'missing'):
            for failure in (False, True):
                for suppress in (False, True):
                    values = [asyncio.run(outcome(ns['select'], required, failure, suppress)) for ns in namespaces]
                    self.assertEqual(values[1], values[0])
        result, events = asyncio.run(outcome(namespaces[1]['select'], 'missing', False, False))
        self.assertEqual(result, ('value', True))
        self.assertLess(events.index(('exit', None)), events.index(('alter', 'missing')))

    def test_real_transaction_protection_and_wrong_call_are_never_dedented(self):
        source = '''async def select(database):
    async with database.query() as cursor:
        rows = await cursor.fetch()
        if rows:
            await database.alter(rows)
        return True
'''
        _, tree, selected = self.repaired(source, source, restore_context_boundaries)
        self.assertEqual(selected, [])
        candidate = source.replace('alter(rows)', 'wrong(rows)')
        original = compile_source(source, '<owned-original>')
        tree, selected = restore_context_boundaries(ast.parse(candidate), original, None)
        self.assertEqual(selected, [])
        self.assertEqual(ast.dump(tree), ast.dump(ast.parse(candidate)))

    def test_comprehension_or_keeps_either_operand_and_call_order(self):
        source = '''def select(rows, first, second):
    return [row for row in rows if first(row) or second(row)]
'''
        candidate = source.replace('if first(row) or second(row)', 'if first(row) if second(row)')
        namespaces, tree, selected = self.repaired(source, candidate, restore_coupled_statement_shapes)
        self.assertTrue(selected)
        for failure in (None, 2):
            outcomes = []
            for namespace in namespaces:
                events = []
                def first(value): events.append(('first', value)); return value == 1
                def second(value):
                    events.append(('second', value))
                    if value == failure: raise ValueError('owned predicate')
                    return value == 2
                try: result = namespace['select']([0, 1, 2, 3], first, second)
                except ValueError as error: result = (type(error).__name__, str(error))
                outcomes.append((result, events))
            self.assertEqual(outcomes[1], outcomes[0])
        self.assertEqual(restore_coupled_statement_shapes(tree, compile_source(source, 'original'), None)[1], [])
        _, _, selected = self.repaired(candidate, candidate, restore_coupled_statement_shapes)
        self.assertEqual(selected, [])

    def test_empty_and_arm_keeps_short_circuit(self):
        source = '''def select(rows, values):
    return sum(values) if rows and all(v is not None for v in values) else None
'''
        candidate = '''def select(rows, values):
    if rows:
        pass
    return sum(values) if all(v is not None for v in values) else None
'''
        namespaces, _, selected = self.repaired(source, candidate, restore_coupled_statement_shapes)
        self.assertTrue(selected)
        for rows, values in (([], [2]), ([1], [2, 3]), ([1], [None]), ([], [None])):
            self.assertEqual(namespaces[1]['select'](rows, values), namespaces[0]['select'](rows, values))

    def test_rejection_or_preserves_value_calls_and_existing_neighbors(self):
        source = '''def select(value, accepting, rejecting):
    if not accepting(value) or rejecting(value):
        return 0
    return 7

def existing(value):
    return value + 1
'''
        candidate = source.replace('if not accepting(value) or rejecting(value):\n        return 0',
                                   'if accepting(value):\n        if rejecting(value):\n            return 0')
        namespaces, _, selected = self.repaired(source, candidate, restore_coupled_statement_shapes)
        self.assertEqual([entry['unit'] for entry in selected], ['<module>/select#1'])
        for accepting_value in (False, True):
            for rejecting_value in (False, True):
                outcomes = []
                for namespace in namespaces:
                    events = []
                    def accepting(value): events.append('accepting'); return accepting_value
                    def rejecting(value): events.append('rejecting'); return rejecting_value
                    outcomes.append((namespace['select'](3, accepting, rejecting), events))
                self.assertEqual(outcomes[1], outcomes[0])

    def test_async_generator_bare_return_is_legal_and_keeps_context_cleanup(self):
        source = '''async def select(manager):
    async with manager:
        yield 7
    if True:
        return
'''
        candidate = source.replace('    if True:\n        return\n', '')
        namespaces, tree, selected = self.repaired(source, candidate, preserve_individual_return_edges)
        self.assertTrue(selected)
        class Manager:
            def __init__(self, suppress): self.suppress, self.events = suppress, []
            async def __aenter__(self): self.events.append('enter'); return self
            async def __aexit__(self, kind, error, traceback):
                self.events.append(('exit', kind.__name__ if kind else None)); return self.suppress
        async def outcome(function, operation, suppress):
            manager = Manager(suppress)
            generator = function(manager)
            result = []
            try:
                result.append(await anext(generator))
                if operation == 'normal': result.append(await anext(generator))
                elif operation == 'close': result.append(await generator.aclose())
                else: result.append(await generator.athrow(ValueError('owned body')))
            except BaseException as error: result.append((type(error).__name__, str(error)))
            finally: await generator.aclose()
            return result, manager.events
        for operation in ('normal', 'close', 'throw'):
            for suppress in (False, True):
                values = [asyncio.run(outcome(ns['select'], operation, suppress)) for ns in namespaces]
                self.assertEqual(values[1], values[0])
        self.assertEqual(preserve_individual_return_edges(tree, compile_source(source, 'original'), None)[1], [])

    def test_coupled_try_scope_and_statement_return_keeps_exception_coverage(self):
        source = '''def select(call):
    try:
        value = call()
        if True:
            return value.result
    except ValueError:
        return False
'''
        candidate = '''def select(call):
    try:
        value = call()
    except ValueError:
        return False
    return value.result
'''
        namespaces, _, selected = self.repaired(source, candidate, restore_coupled_statement_shapes)
        self.assertTrue(selected)
        for failure in (None, ValueError, RuntimeError):
            outcomes = []
            for namespace in namespaces:
                def call():
                    if failure: raise failure('owned failure')
                    from types import SimpleNamespace
                    return SimpleNamespace(result=7)
                try: result = namespace['select'](call)
                except Exception as error: result = (type(error).__name__, str(error))
                outcomes.append(result)
            self.assertEqual(outcomes[1], outcomes[0])

    def test_existing_cleanup_copies_restore_genuine_finally_on_uncaught_errors(self):
        source = '''def select(engine, call):
    previous = engine.value
    engine.value = 7
    try:
        result = call()
    except ValueError:
        return 0
    finally:
        engine.value = previous
    return result
'''
        candidate = '''def select(engine, call):
    previous = engine.value
    engine.value = 7
    try:
        result = call()
    except ValueError:
        engine.value = previous
        return 0
    engine.value = previous
    return result
'''
        namespaces, _, selected = self.repaired(source, candidate, restore_coupled_statement_shapes)
        self.assertTrue(selected)
        from types import SimpleNamespace
        for failure in (None, ValueError, RuntimeError, asyncio.CancelledError):
            outcomes = []
            for namespace in namespaces:
                engine = SimpleNamespace(value=3)
                events = []
                def call():
                    events.append(engine.value)
                    if failure: raise failure('owned calculation')
                    return 11
                try: result = ('value', namespace['select'](engine, call))
                except BaseException as error: result = (type(error).__name__, str(error))
                outcomes.append((result, events, engine.value))
            self.assertEqual(outcomes[1], outcomes[0])
            self.assertEqual(outcomes[1][2], 3)

    def test_wrong_cleanup_operation_and_unproven_neighbor_roll_back(self):
        source = '''def select(engine, call):
    try:
        return call()
    finally:
        engine.value = 3
'''
        candidate = '''def select(engine, call):
    try:
        return call()
    except ValueError:
        pass
    engine.value = 4
'''
        tree, selected = restore_coupled_statement_shapes(ast.parse(candidate), compile_source(source, 'original'), None)
        self.assertEqual(selected, [])
        self.assertEqual(ast.dump(tree), ast.dump(ast.parse(candidate)))


if __name__ == '__main__':
    unittest.main()
