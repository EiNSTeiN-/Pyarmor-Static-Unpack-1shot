"""Owned nested-block fixtures for protected return evaluation."""
import ast
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import restore_try_return_scope


@unittest.skipUnless(sys.version_info[:2]==(3,14),'Requires CPython 3.14')
class NestedTryReturnScopeTests(unittest.TestCase):
    def recover(self,source,candidate):
        original=compile_source(source,'owned-reference')
        tree,selected=restore_try_return_scope(ast.parse(candidate),original,None)
        recovered=compile_source(ast.unparse(tree),'owned-recovered')
        return original,recovered,tree,selected

    def test_nested_if_scope_preserves_row_fallback_and_getter_errors(self):
        reference='''def cell(row, index, key):
    if hasattr(row, 'keys'):
        try:
            return row[key]
        except Exception:
            return row[index]
    return row[index]
'''
        candidate=reference.replace('            return row[key]\n','            pass\n').replace(
            '            return row[index]\n','            return row[index]\n        return row[key]\n')
        original,recovered,tree,selected=self.recover(reference,candidate)
        self.assertEqual(selected,['<module>/cell#1'])
        self.assertTrue(compare(original,recovered)['all_units_match'])
        namespaces=[]
        for code in (original,recovered):
            namespace={}; exec(code,namespace); namespaces.append(namespace)
        for mode in ('ok','key-error','fallback-error','missing-keys','keys-error'):
            outcomes=[]
            for namespace in namespaces:
                calls=[]
                class Row:
                    @property
                    def keys(self):
                        calls.append('keys')
                        if mode=='missing-keys': raise AttributeError('owned missing keys')
                        if mode=='keys-error': raise ValueError('owned keys getter')
                        return object()
                    def __getitem__(self,key):
                        calls.append(key)
                        if key=='value' and mode in ('key-error','fallback-error'):
                            raise KeyError('owned key')
                        if key==0 and mode=='fallback-error': raise IndexError('owned index')
                        return 'result'
                try: value=namespace['cell'](Row(),0,'value')
                except (ValueError,IndexError) as error: value=type(error).__name__,str(error)
                outcomes.append((value,calls))
            self.assertEqual(outcomes[1],outcomes[0])
        self.assertEqual(restore_try_return_scope(tree,original,None)[1],[])

    def test_nested_loop_and_definition_trials_preserve_prior_matches(self):
        reference='''def first(rows):
    for row in rows:
        try:
            return row['value']
        except KeyError:
            return row[0]
    return None
def factory():
    def inner(row):
        try:
            return row['value']
        except KeyError:
            return row[0]
    return inner
'''
        candidate=reference.replace("            return row['value']\n",'            pass\n').replace(
            '            return row[0]\n',"            return row[0]\n        return row['value']\n")
        original,recovered,_,selected=self.recover(reference,candidate)
        self.assertEqual(selected,['<module>/factory#1/inner#1','<module>/first#1'])
        self.assertTrue(compare(original,recovered)['all_units_match'])
        namespaces=[]
        for code in (original,recovered):
            namespace={}; exec(code,namespace); namespaces.append(namespace)
        for rows in ([],[{'value':7}],[{0:8}],[{'value':None}],[{'value':7},{'value':9}]):
            self.assertEqual(namespaces[1]['first'](rows),namespaces[0]['first'](rows))
        for row in ({'value':7},{0:8},{'value':None}):
            self.assertEqual(namespaces[1]['factory']()(row),namespaces[0]['factory']()(row))
        wrong=candidate.replace("row['value']","row['other']")
        _,changed,_,selected=self.recover(reference,wrong)
        self.assertEqual(selected,[])
        self.assertFalse(compare(original,changed)['all_units_match'])


if __name__=='__main__':
    unittest.main()
