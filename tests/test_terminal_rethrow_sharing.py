"""Owned graph and source controls for the explicit terminal-sharing variance."""

from dataclasses import replace
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import CanonicalCode, canonical, compare, compile_source, share_adjacent_rethrows


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class TerminalRethrowSharingTests(unittest.TestCase):
    SPLIT = '''def work(flag, error):
    try:
        raise error
    finally:
        if flag:
            pass
'''
    SHARED = '''def work(flag, error):
    try:
        raise error
    finally:
        while True:
            if flag:
                pass
            break
'''

    def code(self, source):
        return next(value for value in compile_source(source, 'owned-terminal').co_consts
                    if isinstance(value, types.CodeType))

    def model(self):
        # The branch reaches the second copy; a handler target also reaches
        # that copy. Dispatch is split precisely at the duplicate boundary.
        return CanonicalCode((), (
            ('POP_JUMP_IF_FALSE', ('target', 3)), ('NOP', None),
            ('RERAISE', 0), ('RERAISE', 0), ('RERAISE', 1),
        ), ((0, 2, 3, 0, False), (2, 3, 4, 1, True), (3, 4, 4, 1, True)), ())

    def test_literal_source_layouts_match_with_provenance(self):
        split, shared = self.code(self.SPLIT), self.code(self.SHARED)
        self.assertTrue(compare(split, shared)['all_units_match'])
        self.assertEqual(canonical(split), canonical(shared))
        self.assertEqual(len(canonical(split).terminal_rethrow_provenance), 1)
        self.assertEqual(canonical(shared).terminal_rethrow_provenance, ())

    def test_destinations_and_split_half_open_dispatch_boundaries_relocate_correctly(self):
        result = share_adjacent_rethrows(self.model())
        self.assertEqual(result.instructions, (
            ('POP_JUMP_IF_FALSE', ('target', 2)), ('NOP', None),
            ('RERAISE', 0), ('RERAISE', 1)))
        self.assertEqual(result.exceptions, ((0, 2, 2, 0, False), (2, 3, 3, 1, True)))
        self.assertEqual(result.terminal_rethrow_provenance, ((3, 2, (3, 1, True)),))

    def test_changed_operand_work_or_either_handler_route_is_preserved(self):
        baseline = self.model()
        expected = share_adjacent_rethrows(baseline)
        for index in (2, 3):
            changed = list(baseline.instructions)
            changed[index] = ('RERAISE', 1)
            result = share_adjacent_rethrows(replace(baseline, instructions=tuple(changed)))
            self.assertEqual(result.terminal_rethrow_provenance, ())
            self.assertNotEqual(result, expected)
        for index in (1, 2):
            for field, value in ((2, 0), (3, 2), (4, False)):
                rows = list(baseline.exceptions)
                changed = list(rows[index]); changed[field] = value
                rows[index] = tuple(changed)
                result = share_adjacent_rethrows(replace(baseline, exceptions=tuple(rows)))
                self.assertEqual(result.terminal_rethrow_provenance, ())
                self.assertNotEqual(result, expected)
        for omitted in (1, 2):
            rows = baseline.exceptions[:omitted] + baseline.exceptions[omitted + 1:]
            result = share_adjacent_rethrows(replace(baseline, exceptions=rows))
            self.assertEqual(result.terminal_rethrow_provenance, ())
            self.assertNotEqual(result, expected)
        extra_work = self.SPLIT.replace('            pass', '            flag')
        self.assertFalse(compare(self.code(extra_work), self.code(self.SHARED))['all_units_match'])

    def test_overlapping_priority_and_unresolved_shapes_remain_ineligible(self):
        baseline = self.model()
        first = baseline.exceptions[0]
        other = (*first[:2], 4, 0, True)
        left = replace(baseline, exceptions=(first, other, *baseline.exceptions[1:]))
        right = replace(baseline, exceptions=(other, first, *baseline.exceptions[1:]))
        self.assertEqual(share_adjacent_rethrows(left), left)
        self.assertEqual(share_adjacent_rethrows(right), right)
        self.assertNotEqual(share_adjacent_rethrows(left), share_adjacent_rethrows(right))
        unsupported = replace(baseline, unresolved=(10,))
        self.assertEqual(share_adjacent_rethrows(unsupported), unsupported)

    def test_longer_runs_resolve_transitively_without_hiding_a_changed_member(self):
        run = CanonicalCode((), (
            ('JUMP', ('target', 3)), ('RERAISE', 0), ('RERAISE', 0),
            ('RERAISE', 0), ('RERAISE', 1)), ((1, 4, 4, 1, True),), ())
        result = share_adjacent_rethrows(run)
        self.assertEqual(result.instructions, (('JUMP', ('target', 1)),
                                                ('RERAISE', 0), ('RERAISE', 1)))
        self.assertEqual(result.exceptions, ((1, 2, 2, 1, True),))
        self.assertEqual(len(result.terminal_rethrow_provenance), 2)
        changed = list(run.instructions); changed[2] = ('RERAISE', 1)
        result = share_adjacent_rethrows(replace(run, instructions=tuple(changed)))
        self.assertEqual(result.terminal_rethrow_provenance, ())
        self.assertEqual(result.instructions, tuple(changed))

    def test_prior_return_alias_diagnostics_relocate_consistently(self):
        shape = CanonicalCode((), (
            ('RERAISE', 0), ('RERAISE', 0), ('RETURN_VALUE', None),
            ('RETURN_VALUE', None), ('CALL_INTRINSIC_1', 3),
        ), ((0, 2, 4, 1, True), (2, 4, 4, 0, True)), (),
            ((2, 3, (4, 0, False), (4, 0, True)),))
        result = share_adjacent_rethrows(shape)
        self.assertEqual(result.return_alias_provenance, ((1, 2, (3, 0, False), (3, 0, True)),))
        self.assertEqual(result.terminal_rethrow_provenance, ((1, 0, (3, 1, True)),))


if __name__ == '__main__':
    unittest.main()
