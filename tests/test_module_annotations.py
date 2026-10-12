"""Owned namespace fixtures; implicit compiler bindings remain significant."""
import ast
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import restore_module_annotation_binding


@unittest.skipUnless(sys.version_info[:2] == (3, 14), 'Requires CPython 3.14')
class ModuleAnnotationTests(unittest.TestCase):
    def test_compiler_set_and_annotation_namespace_are_recreated(self):
        reference = ('"Owned context documentation"\n'
                     'from __future__ import annotations\n'
                     'VALUE: int | None = None\nPATH: str | None = None\n')
        candidate = reference.replace('VALUE:', '__conditional_annotations__ = {}\nVALUE:')
        code = compile_source(reference, 'reference')
        self.assertFalse(compare(code, compile_source(candidate, 'candidate'))['all_units_match'])
        tree, changed = restore_module_annotation_binding(ast.parse(candidate), code, None)
        self.assertTrue(changed)
        recovered = compile_source(ast.unparse(tree), 'recovered')
        self.assertTrue(compare(code, recovered)['all_units_match'])
        namespaces = []
        for program in (code, recovered):
            namespace = {}; exec(program, namespace)
            namespaces.append({key: value for key, value in namespace.items() if key != '__builtins__'})
        self.assertEqual(namespaces[1], namespaces[0])
        self.assertIsInstance(namespaces[1]['__conditional_annotations__'], set)
        self.assertEqual(namespaces[1]['__annotations__'], {'VALUE': 'int | None', 'PATH': 'str | None'})

    def test_genuine_explicit_namespace_binding_is_kept(self):
        reference = ('from __future__ import annotations\nVALUE: int = 1\n'
                     '__conditional_annotations__ = {}\n')
        code = compile_source(reference, 'reference')
        tree, changed = restore_module_annotation_binding(ast.parse(reference), code, None)
        self.assertFalse(changed)
        self.assertTrue(compare(code, compile_source(ast.unparse(tree), 'recovered'))['all_units_match'])
        namespace = {}; exec(compile_source(ast.unparse(tree), 'recovered'), namespace)
        self.assertIsInstance(namespace['__conditional_annotations__'], dict)

    def test_unproven_constants_and_observed_stores_are_not_removed(self):
        reference = 'from __future__ import annotations\nVALUE: int = 1\n'
        code = compile_source(reference, 'reference')
        candidates = [reference.replace('VALUE:', '__conditional_annotations__ = {}\nVALUE:').replace('= 1', '= 2'),
                      reference + '__conditional_annotations__ = {}\nOBSERVED = __conditional_annotations__\n']
        for candidate in candidates:
            tree, changed = restore_module_annotation_binding(ast.parse(candidate), code, None)
            self.assertFalse(changed)
            self.assertIn('__conditional_annotations__ = {}', ast.unparse(tree))
            self.assertFalse(compare(code, compile_source(ast.unparse(tree), 'candidate'))['all_units_match'])

    def test_idempotency_and_independent_function_match_are_preserved(self):
        reference = ('from __future__ import annotations\nVALUE: int = 1\n'
                     'def double(value):\n    return value * 2\n')
        code = compile_source(reference, 'reference')
        candidate = reference.replace('VALUE:', '__conditional_annotations__ = {}\nVALUE:')
        tree, changed = restore_module_annotation_binding(ast.parse(candidate), code, None)
        self.assertTrue(changed)
        tree, changed = restore_module_annotation_binding(tree, code, None)
        self.assertFalse(changed)
        self.assertTrue(compare(code, compile_source(ast.unparse(tree), 'candidate'))['all_units_match'])
