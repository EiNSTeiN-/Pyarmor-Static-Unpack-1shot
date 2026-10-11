"""Round-trip small, original fixtures; never execute recovered third-party code."""

import asyncio
from contextlib import nullcontext
import importlib.util
import marshal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'build/pyarmor-1shot'


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class Python314Tests(unittest.TestCase):
    def recover(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.pyc'
            code = compile(source, 'fixture.py', 'exec')
            path.write_bytes(importlib.util.MAGIC_NUMBER + bytes(12) + marshal.dumps(code))
            process = subprocess.run([str(BINARY), '--pyc', str(path)], capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            generated = Path(str(path) + '.cdc.py').read_text()
            self.last_generated = generated
            self.assertNotIn('Unsupported opcode', process.stderr)
            self.assertNotIn('Decompyle incomplete', generated)
            original, recovered = {}, {}
            # Both programs originate in the literal fixtures in this file.
            exec(code, original)
            try:
                exec(compile(generated, 'recovered_fixture.py', 'exec'), recovered)
            except Exception as error:
                self.fail(f'{error}\n{generated}')
            return original, recovered

    def test_small_integer_literals(self):
        original, recovered = self.recover('def values():\n    return 0, 1, 42, 255\n')
        self.assertEqual(recovered['values'](), original['values']())

    def test_borrowed_local_loads(self):
        original, recovered = self.recover('def calculate(a, b):\n    return a * 2 + b * 3\n')
        for args in [(0, 0), (3, -7), (12, 25)]:
            self.assertEqual(recovered['calculate'](*args), original['calculate'](*args))

    def test_none_guard_branches(self):
        original, recovered = self.recover('def fallback(value):\n    if value is None:\n        return 7\n    return value + 3\n')
        for value in [None, 0, -2, 9]:
            self.assertEqual(recovered['fallback'](value), original['fallback'](value), self.last_generated)

    def test_formatted_strings(self):
        original, recovered = self.recover('def label(value):\n    return f"value={value:.2f}"\n')
        for value in [0, 1.234, -20.8]:
            self.assertEqual(recovered['label'](value), original['label'](value))

    def test_generator_body(self):
        original, recovered = self.recover('def values():\n    yield 1\n    yield 2\n')
        self.assertEqual(list(recovered['values']()), list(original['values']()))

    def test_coroutine_body(self):
        original, recovered = self.recover('async def calculate(value):\n    return value + 1\n')
        self.assertEqual(asyncio.run(recovered['calculate'](5)), asyncio.run(original['calculate'](5)))

    def test_await_expression(self):
        original, recovered = self.recover('async def inner(value):\n    return value + 2\n\nasync def calculate(value):\n    return await inner(value)\n')
        self.assertEqual(asyncio.run(recovered['calculate'](5)), asyncio.run(original['calculate'](5)))

    def test_fused_stores(self):
        original, recovered = self.recover('def calculate(a, b):\n    a, b = b, a\n    total = a + b\n    return total * 2\n')
        self.assertEqual(recovered['calculate'](3, 7), original['calculate'](3, 7), self.last_generated)

    def test_super_method(self):
        source = 'class Base:\n    def value(self):\n        return 10\n\nclass Child(Base):\n    def value(self):\n        return super().value() + 5\n'
        original, recovered = self.recover(source)
        self.assertEqual(recovered['Child']().value(), original['Child']().value())

    def test_loop_and_branches(self):
        source = 'def total(values):\n    result = 0\n    for value in values:\n        if value > 0:\n            result += value\n    return result\n'
        original, recovered = self.recover(source)
        for values in [[], [-2, 0, 3, 4], [1, 2, 3]]:
            self.assertEqual(recovered['total'](values), original['total'](values), self.last_generated)

    def test_expanded_call_arguments(self):
        original, recovered = self.recover('def calculate(function):\n    return function(*(2, 5))\n')
        function = lambda a, b: a * 10 + b
        self.assertEqual(recovered['calculate'](function), original['calculate'](function), self.last_generated)

    def test_subscript_operator(self):
        original, recovered = self.recover('def select(values):\n    return values[1]\n')
        self.assertEqual(recovered['select']([3, 7]), original['select']([3, 7]))

    def test_function_defaults(self):
        original, recovered = self.recover('def calculate(value=3, *, multiplier=2):\n    return value * multiplier\n')
        self.assertEqual(recovered['calculate'](), original['calculate'](), self.last_generated)

    def test_exception_handler(self):
        source = 'def parse(value):\n    try:\n        return int(value)\n    except ValueError:\n        return -1\n'
        original, recovered = self.recover(source)
        for value in ['5', 'invalid']:
            self.assertEqual(recovered['parse'](value), original['parse'](value), self.last_generated)

    @unittest.expectedFailure
    def test_context_manager(self):
        original, recovered = self.recover('def select(manager):\n    with manager as value:\n        return value + 3\n')
        self.assertEqual(recovered['select'](nullcontext(7)), original['select'](nullcontext(7)), self.last_generated)

    def test_list_comprehension(self):
        original, recovered = self.recover('def select(values):\n    return [value * 2 for value in values if value > 0]\n')
        self.assertEqual(recovered['select']([-1, 0, 2, 4]), original['select']([-1, 0, 2, 4]), self.last_generated)

    def test_dictionary_comprehension(self):
        original, recovered = self.recover('def select(values):\n    return {value: value * 2 for value in values}\n')
        self.assertEqual(recovered['select']([2, 4]), original['select']([2, 4]), self.last_generated)

    def test_set_comprehension(self):
        original, recovered = self.recover('def select(values):\n    return {value * 2 for value in values}\n')
        self.assertEqual(recovered['select']([2, 4, 2]), original['select']([2, 4, 2]), self.last_generated)

    def test_formatted_conversion(self):
        original, recovered = self.recover('def label(value):\n    return f"{value!r:>12}"\n')
        self.assertEqual(recovered['label']('hello'), original['label']('hello'), self.last_generated)


if __name__ == '__main__':
    unittest.main()
