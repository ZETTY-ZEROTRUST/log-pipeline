import json
from pathlib import Path
import tempfile
import unittest
from zetty_log.http import normalize,validate
from zetty_log.__main__ import produce


def raw():return {'request_id':'1'*32,'time':'2026-09-27T00:01:00Z','remote_addr':'192.0.2.1',
 'user_agent':'fixture','method':'GET','uri':'/items/1','status':200,'body_bytes':42}

class HTTPFile(unittest.TestCase):
 def test_privacy_and_stable_identity(self):
  a=normalize(raw(),bytes(32),'fixture-v1');self.assertEqual(a,normalize(raw(),bytes(32),'fixture-v1'))
  text=json.dumps(a)
  for secret in ('192.0.2.1','/items/1','user_agent'):self.assertNotIn(secret,text)
  validate(a)
 def test_no_extra_fields_or_missing_bytes_coercion(self):
  r=raw();r['authorization']='fixture-not-a-token'
  with self.assertRaises(ValueError):normalize(r,bytes(32),'v1')
  r=raw();r['body_bytes']=None;self.assertIsNone(normalize(r,bytes(32),'v1')['response_body_bytes'])
 def test_duplicate_and_invalid_capture(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);src=root/'input';src.write_text(json.dumps(raw())+'\n'+json.dumps(raw())+'\ninvalid\n')
   m=produce(src,root/'output',bytes(32),'v1','2026-09-27T00:00:00Z','2026-09-27T00:05:00Z',True)
   self.assertEqual(m['events'],1);self.assertEqual(m['duplicate_rows'],1);self.assertFalse(m['complete'])
 def test_conflicting_id_rejects_output(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);src=root/'input';a=raw();b=raw();b['body_bytes']=9
   src.write_text(json.dumps(a)+'\n'+json.dumps(b)+'\n')
   with self.assertRaisesRegex(ValueError,'event_id_content_conflict'):
    produce(src,root/'out',bytes(32),'v1','2026-09-27T00:00:00Z','2026-09-27T00:05:00Z',True)
   self.assertFalse((root/'out').exists())
