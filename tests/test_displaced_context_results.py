"""Owned protocol-result trials with scope, fault and strict negative controls."""
import ast
import asyncio
import copy
from pathlib import Path
import sys
import unittest
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import restore_displaced_context_results


SOURCE = '''async def select(context, rows, parse, build, log):
    """Read and return a protected result."""
    try:
        async with context:
            result = []
            for row in rows:
                try:
                    result.append(parse(row))
                except ValueError:
                    log('row-error')
                    continue
            total = sum(value for value in result)
            if result:
                total = max(total, 0)
            return build(result, total)
    except RuntimeError:
        log('fallback')
        return []
    if True:
        return
'''


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class DisplacedContextResults(unittest.TestCase):
    def displaced(self, move_continue=False):
        tree = ast.parse(SOURCE)
        owner = tree.body[0]
        region = owner.body[1]
        context = region.body[0]
        returned = context.body.pop()
        suffix = context.body[-2:]
        del context.body[-2:]
        if move_continue:
            loop = context.body[-1]
            clause = loop.body[-1].handlers[0]
            loop.body.append(clause.body.pop())
        alias = copy.deepcopy(region)
        alias.body = [ast.Expr(ast.Await(ast.Call(ast.Constant(None),
            [returned.value, ast.Constant(None), ast.Constant(None), ast.Constant(None)], [])))]
        owner.body = owner.body[:2] + suffix + [alias]
        return ast.fix_missing_locations(tree)

    def checked(self, candidate, accepted=True):
        reference = compile_source(SOURCE, 'owned-original')
        tree, selected = restore_displaced_context_results(candidate, reference, None)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            rebuilt = compile_source(ast.unparse(tree), 'owned-rebuilt')
        self.assertEqual(bool(selected), accepted)
        self.assertEqual(compare(reference, rebuilt)['all_units_match'], accepted)
        return reference, rebuilt, tree

    def exercise(self, reference, rebuilt):
        spaces = [{}, {}]
        for code, space in zip((reference, rebuilt), spaces):
            exec(code, space)
        count = 0
        for rows in ([], [1, 2], [1, 'bad', -4]):
            for failure in (None, 'enter', 'parse', 'build', 'exit', 'cancel'):
                for suppress in (False, True):
                    outcomes = []
                    for space in spaces:
                        events = []
                        active = False
                        class Context:
                            async def __aenter__(self):
                                nonlocal active
                                events.append('enter')
                                if failure == 'enter':
                                    raise RuntimeError('owned enter')
                                active = True
                            async def __aexit__(self, kind, error, traceback):
                                nonlocal active
                                events.append(('exit', kind.__name__ if kind else None))
                                active = False
                                if failure == 'exit':
                                    raise RuntimeError('owned exit')
                                return suppress
                        def parse(value):
                            events.append(('parse', value, active))
                            if failure == 'parse':
                                raise RuntimeError('owned parse')
                            if failure == 'cancel':
                                raise asyncio.CancelledError('owned cancel')
                            if value == 'bad':
                                raise ValueError('owned row')
                            return value
                        def build(values, total):
                            events.append(('build', tuple(values), total, active))
                            if failure == 'build':
                                raise RuntimeError('owned build')
                            return list(values), total
                        try:
                            result = ('return', asyncio.run(space['select'](
                                Context(), rows, parse, build, events.append)))
                        except BaseException as error:
                            result = ('error', type(error).__name__, str(error))
                        outcomes.append((result, events))
                    self.assertEqual(outcomes[0], outcomes[1])
                    count += 1
        self.assertEqual(count, 36)

    def test_existing_result_and_suffix_return_before_context_exit(self):
        reference, rebuilt, tree = self.checked(self.displaced())
        self.assertEqual(ast.get_docstring(tree.body[0]), 'Read and return a protected result.')
        self.exercise(reference, rebuilt)

    def test_handler_owned_continue_keeps_full_exception_coverage(self):
        reference, rebuilt, _ = self.checked(self.displaced(move_continue=True))
        self.exercise(reference, rebuilt)

    def test_wrong_protocol_handler_payload_or_extra_work_is_rejected(self):
        candidates = []
        tree = self.displaced()
        tree.body[0].body[-1].body[0].value.value.args[1] = ast.Constant('not a protocol placeholder')
        candidates.append(tree)
        tree = self.displaced()
        tree.body[0].body[1].handlers[0].type = ast.Name('ValueError', ast.Load())
        tree.body[0].body[-1].handlers[0].type = ast.Name('ValueError', ast.Load())
        candidates.append(tree)
        tree = self.displaced()
        tree.body[0].body[-1].body[0].value.value.args[0] = ast.Constant('wrong result')
        candidates.append(tree)
        tree = self.displaced()
        tree.body[0].body.insert(-1, ast.Expr(ast.Call(ast.Name('log', ast.Load()), [ast.Constant('extra work')], [])))
        candidates.append(tree)
        for candidate in candidates:
            _, _, rebuilt = self.checked(candidate, accepted=False)
            self.assertEqual(ast.dump(rebuilt, include_attributes=False),
                             ast.dump(ast.parse(ast.unparse(candidate)), include_attributes=False))

    def test_matching_and_genuine_independent_none_calls_remain_unchanged(self):
        for source in (SOURCE, '''async def select(context, value):
    try:
        async with context:
            pass
    except RuntimeError:
        return 'first fallback'
    try:
        await None(value, None, None, None)
    except RuntimeError:
        return 'second fallback'
'''):
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', SyntaxWarning)
                reference = compile_source(source, 'owned-genuine')
                tree, selected = restore_displaced_context_results(ast.parse(source), reference, None)
                rebuilt = compile_source(ast.unparse(tree), 'owned-genuine-rebuilt')
            self.assertFalse(selected)
            self.assertTrue(compare(reference, rebuilt)['all_units_match'])


if __name__ == '__main__':
    unittest.main()
