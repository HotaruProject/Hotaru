import asyncio
import sqlite3
import sys
from types import SimpleNamespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hotaru.provenance import MessageProvenance
from relay.firewall import module_scope
from hotaru.kernel import Kernel

async def main():
 db=sqlite3.connect(':memory:')
 guard=MessageProvenance(db)
 started=asyncio.Event()
 release=asyncio.Event()
 async def rpc(method, **kw):
  if kw.get('message')=='race':
   started.set();await release.wait()
  return {'_':'updates','updates':[{'_':'updateNewMessage','message':{'_':'message','id':10,'peer_id':{'_':'peerUser','user_id':42}}}]}
 app=SimpleNamespace(_mt_rpc=rpc)
 guard.install(app)
 with module_scope('sender'):
  await app._mt_rpc('messages.sendMessage',peer=42,message='.danger')
 assert await guard.generated(SimpleNamespace(chat_id=42,id=10))
 assert not await guard.generated(SimpleNamespace(chat_id=43,id=10))
 assert not await guard.generated(SimpleNamespace(chat_id=42,id=11))
 with module_scope('sender'):
  await app._mt_rpc('messages.editMessage',peer=42,id=11,message='.danger')
 assert await guard.generated(SimpleNamespace(chat_id=42,id=11))
 reopened=MessageProvenance(db)
 assert await reopened.generated(SimpleNamespace(chat_id=42,id=11))
 async def send():
  with module_scope('sender'):
   await app._mt_rpc('messages.sendMessage',peer=42,message='race')
 task=asyncio.create_task(send());await started.wait()
 check=asyncio.create_task(guard.generated(SimpleNamespace(chat_id=42,id=10)))
 await asyncio.sleep(0)
 assert not check.done()
 release.set();await task
 assert await check
 kernel=Kernel();kernel.provenance=guard
 assert await kernel.dispatch(SimpleNamespace(chat_id=42,id=11,text='.danger'),source='edit') is None
 await kernel.dispatch_watchers(SimpleNamespace(chat_id=42,id=10,text='.danger'))
 print('provenance: send/edit, durable IDs, peer separation, manual messages, update race, kernel command/watchers PASS')
asyncio.run(main())
