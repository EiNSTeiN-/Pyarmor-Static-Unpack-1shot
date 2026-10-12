"""Owned shared-return fixtures; meaningful handler coverage stays strict."""
import asyncio
import dis
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'oneshot'))
from bytecode_fidelity import compare, compile_source
from modern_backend import _varint


def table(rows):
    return b''.join(_varint(row.start//2,marker=True)+_varint((row.end-row.start)//2)
                    +_varint(row.target//2)+_varint((row.depth<<1)|row.lasti) for row in rows)


def function(source):
    return next(value for value in compile_source(source,'owned-return-fixture').co_consts
                if isinstance(value,types.CodeType) and value.co_name=='convert')


def forward_alias(code):
    """Model one protected jump to an existing bare return, at fixed addresses."""
    instructions=list(dis.get_instructions(code))
    returns=[item for item in instructions if item.opname=='RETURN_VALUE']
    entries=list(dis.Bytecode(code).exception_entries)
    index,entry=next((index,entry) for index,entry in enumerate(entries)
                     if not entry.lasti and any(item.offset==entry.end for item in returns))
    offset=entry.end
    target=next(item.offset for item in returns if item.offset>offset)
    argument=(target-offset-2)//2
    assert 0<=argument<256
    data=bytearray(code.co_code)
    data[offset:offset+2]=bytes((dis.opmap['JUMP_FORWARD'],argument))
    entries[index]=entry._replace(end=offset+2)
    return code.replace(co_code=bytes(data),co_exceptiontable=table(entries)),offset,index


@unittest.skipUnless(sys.version_info[:2]==(3,14),'Requires CPython 3.14')
class ReturnAliasCoverageTests(unittest.TestCase):
    source='''def convert(value, callback, predicate):
    try:
        number = callback(value)
        return number if predicate(number) else None
    except (ValueError, TypeError):
        return 'handled'
'''

    def test_forward_alias_preserves_identity_calls_truth_and_handled_errors(self):
        candidate=function(self.source)
        reference,_,_=forward_alias(candidate)
        self.assertTrue(compare(reference,candidate)['all_units_match'])
        for truth in (False,True,ValueError('owned truth'),KeyError('owned truth')):
            for conversion in (None,TypeError('owned conversion'),KeyError('owned conversion')):
                outcomes=[]
                for code in (reference,candidate):
                    calls=[]
                    sentinel=object()
                    def callback(value):
                        calls.append('callback')
                        if conversion is not None: raise conversion
                        return sentinel
                    class Truth:
                        def __bool__(self):
                            calls.append('truth')
                            if isinstance(truth,Exception): raise truth
                            return truth
                    def predicate(value):
                        self.assertIs(value,sentinel)
                        calls.append('predicate')
                        return Truth()
                    try:
                        value=types.FunctionType(code,{})(object(),callback,predicate)
                        value='same object' if value is sentinel else value
                    except KeyError as error: value=type(error).__name__,str(error)
                    outcomes.append((value,calls))
                self.assertEqual(outcomes[1],outcomes[0])

    def test_real_return_coverage_and_cleanup_lasti_remain_significant(self):
        candidate=function(self.source)
        reference,offset,index=forward_alias(candidate)
        entries=list(dis.Bytecode(reference).exception_entries)
        real_return=candidate.replace(co_exceptiontable=table(entries))
        self.assertFalse(compare(real_return,candidate)['all_units_match'])
        entries[index]=entries[index]._replace(lasti=True)
        self.assertFalse(compare(reference.replace(co_exceptiontable=table(entries)),candidate)['all_units_match'])
        entries=list(dis.Bytecode(reference).exception_entries)
        entries[index]=entries[index]._replace(depth=1)
        # The remaining protected evaluation keeps its changed stack depth.
        self.assertFalse(compare(reference.replace(co_exceptiontable=table(entries)),candidate)['all_units_match'])

    def test_changed_call_coverage_handler_or_literals_are_rejected(self):
        candidate=function(self.source)
        reference,_,index=forward_alias(candidate)
        entries=list(dis.Bytecode(reference).exception_entries)
        call=next(item for item in dis.get_instructions(candidate) if item.opname=='CALL')
        entries[index]=entries[index]._replace(start=call.offset+2)
        self.assertFalse(compare(reference.replace(co_exceptiontable=table(entries)),candidate)['all_units_match'])
        changed=function(self.source.replace("return 'handled'","return 'wrong'"))
        self.assertFalse(compare(reference,changed)['all_units_match'])
        changed=function(self.source.replace('ValueError, TypeError','KeyError, TypeError'))
        self.assertFalse(compare(reference,changed)['all_units_match'])

    def test_backward_alias_and_intervening_work_are_not_new_allowances(self):
        candidate=function(self.source)
        instructions=list(dis.get_instructions(candidate))
        returns=[item for item in instructions if item.opname=='RETURN_VALUE']
        entries=list(dis.Bytecode(candidate).exception_entries)
        index,entry=next((i,e) for i,e in reversed(list(enumerate(entries)))
                         if not e.lasti and any(item.offset==e.end for item in returns)
                         and e.end>returns[0].offset)
        data=bytearray(candidate.co_code)
        data[entry.end:entry.end+2]=bytes((dis.opmap['JUMP_BACKWARD'],(entry.end+2-returns[0].offset)//2))
        entries[index]=entry._replace(end=entry.end+2)
        backward=candidate.replace(co_code=bytes(data),co_exceptiontable=table(entries))
        self.assertFalse(compare(backward,candidate)['all_units_match'])
        reference,offset,_=forward_alias(candidate)
        target=next(item for item in instructions if item.offset>offset and item.opname=='LOAD_CONST')
        data=bytearray(reference.co_code)
        data[offset+1]=(target.offset-offset-2)//2
        self.assertFalse(compare(reference.replace(co_code=bytes(data)),candidate)['all_units_match'])

    def test_generator_stopiteration_route_and_value_are_preserved(self):
        source=self.source.replace('    try:\n','    yield "ready"\n    try:\n')
        candidate=function(source)
        reference,_,_=forward_alias(candidate)
        self.assertTrue(compare(reference,candidate)['all_units_match'])
        for accepted in (False,True):
            outcomes=[]
            for code in (reference,candidate):
                iterator=types.FunctionType(code,{})(7,lambda value:value,lambda value:accepted)
                first=next(iterator)
                try: next(iterator)
                except StopIteration as stop: outcomes.append((first,stop.value))
            self.assertEqual(outcomes[1],outcomes[0])
        entries=list(dis.Bytecode(reference).exception_entries)
        stop=next(index for index,entry in enumerate(entries) if entry.lasti and entry.depth==0)
        entries[stop]=entries[stop]._replace(lasti=False)
        self.assertFalse(compare(reference.replace(co_exceptiontable=table(entries)),candidate)['all_units_match'])

    def test_coroutine_errors_and_cancellation_keep_original_routes(self):
        source=self.source.replace('def convert(','async def convert(').replace('number = callback(value)',
                                                                              'number = await callback(value)')
        candidate=function(source)
        reference,_,_=forward_alias(candidate)
        self.assertTrue(compare(reference,candidate)['all_units_match'])
        for failure in (None,ValueError('owned'),asyncio.CancelledError('owned')):
            outcomes=[]
            for code in (reference,candidate):
                calls=[]
                async def callback(value):
                    calls.append('callback')
                    if failure is not None: raise failure
                    return value
                try: value=asyncio.run(types.FunctionType(code,{})(7,callback,lambda value:True))
                except asyncio.CancelledError as error: value=type(error).__name__,str(error)
                outcomes.append((value,calls))
            self.assertEqual(outcomes[1],outcomes[0])


if __name__=='__main__':
    unittest.main()
