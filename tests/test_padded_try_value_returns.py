"""Owned end-to-end fixtures for a live value at a padded try return."""
import dis
import os
from pathlib import Path
import sys
import types
import unittest

import test_modern_backend as modern

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import _varint


def padded_returns(test, code, count):
    """Retain precise jump and handler maps while adding owned return padding."""
    instructions = list(dis.get_instructions(code))
    test.assertFalse(any(item.opname == 'EXTENDED_ARG' for item in instructions))
    starts, sizes, cursor = {}, {}, 0
    for index, item in enumerate(instructions):
        starts[item.offset] = cursor
        end = (instructions[index + 1].offset if index + 1 < len(instructions)
               else len(code.co_code))
        sizes[item.offset] = end - item.offset
        cursor += sizes[item.offset] + (2 * count if item.opname == 'RETURN_VALUE' else 0)
    starts[len(code.co_code)] = cursor
    output = bytearray()
    for item in instructions:
        if item.opname == 'RETURN_VALUE':
            output.extend(bytes((dis.opmap['NOP'], 0)) * count)
        actual = len(output)
        raw = bytearray(code.co_code[item.offset:item.offset + sizes[item.offset]])
        if item.opcode in dis.hasjump:
            delta = (starts[item.argval] - (actual + sizes[item.offset])) // 2
            operand = -delta if 'BACKWARD' in item.opname else delta
            test.assertTrue(0 <= operand <= 255)
            raw[1] = operand
        output.extend(raw)
    table = b''.join(_varint(starts[row.start] // 2, True)
                     + _varint((starts[row.end] - starts[row.start]) // 2)
                     + _varint(starts[row.target] // 2)
                     + _varint((row.depth << 1) | row.lasti)
                     for row in dis.Bytecode(code).exception_entries)
    return code.replace(co_code=bytes(output), co_exceptiontable=table, co_linetable=b'')


@unittest.skipUnless(os.environ.get('PYARMOR_MODERN_BACKEND')
                     and sys.version_info[:2] == (3, 14),
                     'Set PYARMOR_MODERN_BACKEND and use CPython 3.14')
class PaddedTryValueReturnTests(unittest.TestCase):
    def test_computed_return_keeps_complete_unit_proof_and_fallback_errors(self):
        for expression in ('make(value)', 'make(value).result'):
            source = ('def select(value):\n'
                      '    if value:\n        try:\n'
                      f'            return {expression}\n'
                      '        except Exception:\n            pass\n'
                      '    return (fallback.read if True else None)().result or default.result\n')
            original_code = compile_source(source, 'owned-original')
            function_code = next(v for v in original_code.co_consts if isinstance(v, types.CodeType))
            for count in (1, 3, 9):
                padded = padded_returns(self, function_code, count)
                exported = original_code.replace(co_consts=tuple(
                    padded if v is function_code else v for v in original_code.co_consts))
                original, recovered = modern.ModernBackendTests.recover_code(self, exported)
                self.assertTrue(compare(exported, compile_source(self.last_generated, 'owned-candidate'))
                                ['all_units_match'])
                def exercise(function, truth, failure):
                    calls = []
                    class Value:
                        def __bool__(self):
                            calls.append('truth')
                            if failure == 'truth': raise ValueError('truth failed')
                            return truth
                    class Result:
                        @property
                        def result(self):
                            calls.append('result')
                            if failure == 'attribute': raise AttributeError('result failed')
                            return 'computed'
                    class Fallback:
                        def read(self):
                            calls.append('fallback')
                            if failure == 'fallback': raise RuntimeError('fallback failed')
                            return types.SimpleNamespace(result='fallback-result')
                    def make(value):
                        calls.append('make')
                        if failure == 'make': raise KeyError('make failed')
                        if failure == 'base': raise KeyboardInterrupt('owned interrupt')
                        return Result() if expression.endswith('.result') else 'computed'
                    function.__globals__.update(make=make, fallback=Fallback(),
                                                default=types.SimpleNamespace(result='default'))
                    try: outcome = ('value', function(Value()))
                    except (Exception, KeyboardInterrupt) as error:
                        outcome = (type(error).__name__, str(error))
                    return outcome, calls
                for truth in (False, True):
                    for failure in ('', 'truth', 'attribute', 'make', 'base', 'fallback'):
                        self.assertEqual(exercise(recovered['select'], truth, failure),
                                         exercise(original['select'], truth, failure))
                self.assertEqual(exercise(recovered['select'], True, '')[0], ('value', 'computed'))
                self.assertEqual(exercise(recovered['select'], False, '')[0], ('value', 'fallback-result'))


if __name__ == '__main__':
    unittest.main()
