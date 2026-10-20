"""Exercise the real card publishing method with mocked transport; no CAN output."""
import ast
from pathlib import Path
from types import SimpleNamespace as N
import time
import unittest

class CardPublishing(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[2] / "openpilot"
        tree=ast.parse((root/'selfdrive/car/card.py').read_text())
        method=next(n for cls in tree.body if isinstance(cls,ast.ClassDef) and cls.name=='Car'
                    for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='controls_update')
        for arg in method.args.args:arg.annotation=None
        method.returns=None
        ns={'time':time,'REPLAY':False,'can_list_to_can_capnp':lambda frames,**kwargs:(frames,kwargs['valid'])}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<card publishing>','exec'),ns)
        self.update=ns['controls_update']
        self.sent=[]
        self.normal=[(1,b'normal',0)]
        self.cleanup=[(0x64b,bytes.fromhex('04 31 02 03 34 00 00 00'),0)]
        self.car=N(initialized_prev=True,sm=N(all_alive=lambda services:True),
                   CI=N(apply=lambda cc,now:(None,self.normal)),airride_sends=list(self.cleanup),
                   pm=N(send=lambda topic,msg:self.sent.append((topic,msg))))
    def test_combines_with_existing_publication(self):
        self.update(self.car,N(canValid=True),N())
        self.assertEqual(self.sent,[('sendcan',(self.normal+self.cleanup,True))])
        self.assertEqual(self.car.airride_sends,[])
    def test_invalid_car_sends_cleanup_only(self):
        self.update(self.car,N(canValid=False),N())
        self.assertEqual(self.sent,[('sendcan',(self.cleanup,True))])
    def test_no_carcontrol_leaves_cleanup_for_step(self):
        self.car.sm.all_alive=lambda services:False
        self.update(self.car,N(canValid=True),N())
        self.assertEqual(self.sent,[])
        self.assertEqual(self.car.airride_sends,self.cleanup)

if __name__=='__main__':unittest.main()
