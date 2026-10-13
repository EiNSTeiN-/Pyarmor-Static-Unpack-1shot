"""Only owned literal bytecode is mutated for return-dispatch regressions."""

import dis
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import canonical, compare, compile_source
from modern_backend import _varint


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class ReturnAliasCleanupTests(unittest.TestCase):
    SOURCE = '''async def work(flag):
    try:
        if flag:
            return 7
        return 9
    except Exception:
        return 11
'''

    @staticmethod
    def table(rows, *, preserve_order=False):
        if not preserve_order:
            rows = sorted(rows, key=lambda row: (row.start, row.end, row.target))
        return b''.join(
            _varint(row.start // 2, True)
            + _varint((row.end - row.start) // 2)
            + _varint(row.target // 2)
            + _varint((row.depth << 1) | row.lasti)
            for row in rows)

    @staticmethod
    def without(rows, offset):
        result = []
        for row in rows:
            if not row.start <= offset < row.end:
                result.append(row)
                continue
            if row.start < offset:
                result.append(row._replace(end=offset))
            if offset + 2 < row.end:
                result.append(row._replace(start=offset + 2))
        return result

    def fixture(self):
        module = compile_source(self.SOURCE, 'owned-alias-cleanup')
        stock = next(value for value in module.co_consts if isinstance(value, types.CodeType))
        operations = list(dis.get_instructions(stock))
        first, target = [op.offset for op in operations if op.opname == 'RETURN_VALUE'][:2]
        intrinsic = next(op.offset for op in operations
                         if op.opname == 'CALL_INTRINSIC_1' and op.arg == 3)
        rows = list(dis.Bytecode(stock).exception_entries)
        user = next(row for row in rows if not row.lasti and row.depth == 0)
        alias_rows = self.without(rows, first) + [user._replace(start=first, end=first + 2)]
        operations = bytearray(stock.co_code)
        operations[first:first + 2] = bytes((dis.opmap['JUMP_FORWARD'], (target - first - 2) // 2))
        alias = stock.replace(co_code=bytes(operations), co_exceptiontable=self.table(alias_rows))
        return stock, alias, first, target, intrinsic, rows, alias_rows

    def test_effective_return_cleanup_is_projected_and_transfer_provenance_recorded(self):
        stock, alias, _, _, _, _, _ = self.fixture()
        original, projected = canonical(stock), canonical(alias)
        self.assertEqual(original, projected)
        self.assertEqual(original.return_alias_provenance, ())
        self.assertEqual(len(projected.return_alias_provenance), 1)
        index, target, transfer, terminal = projected.return_alias_provenance[0]
        self.assertLess(index, target)
        self.assertEqual(transfer[1:], (0, False))
        self.assertEqual(terminal[1:], (0, True))
        self.assertEqual(projected.instructions[terminal[0]], ('CALL_INTRINSIC_1', 3))
        proof = compare(alias, stock)
        self.assertTrue(proof['all_units_match'])
        self.assertEqual(len(proof['entries'][0]['return_alias_provenance']['reference']), 1)

    def test_previous_false_acceptance_of_a_real_return_hole_is_rejected(self):
        stock, alias, first, _, _, rows, _ = self.fixture()
        hole = stock.replace(co_exceptiontable=self.table(self.without(rows, first)))
        self.assertEqual(stock.co_code, hole.co_code)
        self.assertFalse(compare(alias, hole)['all_units_match'])
        self.assertFalse(compare(hole, alias)['all_units_match'])
        self.assertFalse(compare(stock, hole)['all_units_match'])

    def test_actual_shared_terminal_cleanup_hole_is_rejected(self):
        _, alias, _, target, _, _, rows = self.fixture()
        hole = alias.replace(co_exceptiontable=self.table(self.without(rows, target)))
        self.assertEqual(alias.co_code, hole.co_code)
        self.assertEqual(canonical(hole).return_alias_provenance, ())
        self.assertFalse(compare(alias, hole)['all_units_match'])

    def test_lasti_alias_dispatch_is_retained_without_projection(self):
        _, alias, first, _, _, _, rows = self.fixture()
        changed = alias.replace(co_exceptiontable=self.table([
            row._replace(lasti=True) if row.start <= first < row.end else row for row in rows]))
        shape = canonical(changed)
        self.assertEqual(shape.return_alias_provenance, ())
        self.assertFalse(compare(alias, changed)['all_units_match'])

    def test_target_handler_depth_and_lasti_mutations_are_ineligible_and_rejected(self):
        _, alias, _, target, _, _, rows = self.fixture()
        for mutation in ({'depth': 1}, {'lasti': False}, {'target': 0}):
            with self.subTest(mutation=mutation):
                changed = alias.replace(co_exceptiontable=self.table([
                    row._replace(**mutation) if row.start <= target < row.end else row for row in rows]))
                self.assertEqual(canonical(changed).return_alias_provenance, ())
                self.assertFalse(compare(alias, changed)['all_units_match'])

    def test_uncovered_transfer_and_noncoroutine_are_not_projected(self):
        _, alias, first, _, _, _, rows = self.fixture()
        uncovered = alias.replace(co_exceptiontable=self.table(self.without(rows, first)))
        self.assertEqual(canonical(uncovered).return_alias_provenance, ())
        self.assertFalse(compare(alias, uncovered)['all_units_match'])
        synchronous = alias.replace(co_flags=alias.co_flags & ~128)
        self.assertEqual(canonical(synchronous).return_alias_provenance, ())

    def test_overlapping_routes_elsewhere_keep_their_priority_and_fail_comparison(self):
        _, alias, first, _, intrinsic, _, rows = self.fixture()
        rows = sorted(rows, key=lambda row: (row.start, row.end, row.target))
        original = next(row for row in rows
                        if row.depth == 0 and not row.lasti and row.end <= first)
        other = original._replace(target=intrinsic, lasti=True)
        index = rows.index(original)
        first_order = rows[:index] + [original, other] + rows[index + 1:]
        second_order = rows[:index] + [other, original] + rows[index + 1:]
        left = alias.replace(co_exceptiontable=self.table(first_order, preserve_order=True))
        right = alias.replace(co_exceptiontable=self.table(second_order, preserve_order=True))
        self.assertEqual(canonical(left).return_alias_provenance, ())
        self.assertEqual(canonical(right).return_alias_provenance, ())
        self.assertNotEqual(canonical(left).exceptions, canonical(right).exceptions)
        self.assertFalse(compare(left, right)['all_units_match'])
        self.assertFalse(compare(right, left)['all_units_match'])

    def test_ambiguous_deep_backward_and_chained_transfers_do_not_qualify(self):
        _, alias, first, target, _, _, rows = self.fixture()
        for offset in (first, target):
            with self.subTest(ambiguous_offset=offset):
                duplicated = next(row for row in rows if row.start <= offset < row.end)
                changed = alias.replace(co_exceptiontable=self.table(rows + [duplicated]))
                self.assertEqual(canonical(changed).return_alias_provenance, ())
        deep = alias.replace(co_exceptiontable=self.table([
            row._replace(depth=1) if row.start <= first < row.end else row for row in rows]))
        self.assertEqual(canonical(deep).return_alias_provenance, ())
        operations = bytearray(alias.co_code)
        operations[first:first + 2] = bytes((dis.opmap['RETURN_VALUE'], 0))
        operations[target:target + 2] = bytes((dis.opmap['JUMP_BACKWARD'], (target + 2 - first) // 2))
        backward_rows = self.without(rows, target)
        user = next(row for row in rows if row.start <= first < row.end)
        backward_rows.append(user._replace(start=target, end=target + 2))
        backward = alias.replace(co_code=bytes(operations), co_exceptiontable=self.table(backward_rows))
        self.assertEqual(canonical(backward).return_alias_provenance, ())
        final_return = next(op.offset for op in dis.get_instructions(alias)
                            if op.opname == 'RETURN_VALUE' and op.offset > target)
        operations = bytearray(alias.co_code)
        operations[target:target + 2] = bytes((dis.opmap['JUMP_FORWARD'],
                                               (final_return - target - 2) // 2))
        chained = alias.replace(co_code=bytes(operations))
        self.assertEqual(canonical(chained).return_alias_provenance, ())


if __name__ == '__main__':
    unittest.main()
