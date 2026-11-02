import concurrent.futures
import contextlib
import base64
import http.client
import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from backend import DeliveryError
from server import HTTPServer, Relay
from launcher import service_lock


class MobileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.home = self.root / 'codex'
        self.home.mkdir()
        with contextlib.closing(sqlite3.connect(self.home / 'state_5.sqlite')) as connection, connection:
            connection.execute('CREATE TABLE threads (id TEXT,title TEXT,cwd TEXT,rollout_path TEXT,updated_at INT,archived INT)')
            connection.execute('INSERT INTO threads VALUES (?,?,?,?,?,?)', ('chat-a','中文任务',str(self.root),'',1000,0))
        with contextlib.closing(sqlite3.connect(self.home / 'queue_1.sqlite')) as connection, connection:
            connection.execute('CREATE TABLE queued_items (id TEXT,thread_id TEXT,payload_json TEXT)')
        self.calls = []
        def sender(cli, home, chat, text):
            self.calls.append((chat.id, text))
            with contextlib.closing(sqlite3.connect(self.home / 'queue_1.sqlite')) as connection, connection:
                connection.execute('INSERT INTO queued_items VALUES (?,?,?)', ('q-a',chat.id,json.dumps({'text':text})))
            return {'warning':False}
        self.config = {'origin':'https://example.tailnet.ts.net:8444','port':0,'label':'Test Mac',
                       'codex_home':str(self.home),'cli':'fixture-cli','peers':[]}
        def answer_sender(chat, text, request_id, home, queue_ids):
            self.assertEqual(home, self.home)
            self.calls.append((chat.id, text))
            return {'warning':False,'queue_ids':[],'delivery_mode':'immediate','turn_id':'turn-q'}
        self.model = 'gpt-fixture'
        self.model_calls = []
        def model_bridge(chat, home, model=None):
            self.model_calls.append((chat.id, model))
            if model is not None:
                self.model = model
            return self.model
        self.relay = Relay(self.root / 'mobile',self.config,sender,answer_sender=answer_sender,
                           model_bridge=model_bridge, model_catalog=lambda *args: ['gpt-fixture', 'gpt-next'])
        self.server = HTTPServer(self.relay)
        self.port = self.server.server_address[1]
        self.relay.local_origin = f'http://127.0.0.1:{self.port}'
        self.relay.hosts.add(f'127.0.0.1:{self.port}')
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, body=None, cookie='', origin=None, host=None, extra=None):
        connection = http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        headers = {'Host':host or f'127.0.0.1:{self.port}'}
        if cookie:
            headers['Cookie'] = cookie
        headers.update(extra or {})
        if body is not None:
            headers.update({'Content-Type':'application/json','Origin':origin or self.relay.local_origin})
        connection.request('POST' if body is not None else 'GET',path,
                           json.dumps(body).encode() if body is not None else None,headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status,dict(response.getheaders()),data)
        connection.close()
        return result

    def paired(self):
        status, headers, _ = self.request('/api/pair',{'token':self.relay.token})
        self.assertEqual(status,200)
        return headers['Set-Cookie'].split(';')[0]

    def upload_image(self, cookie=None):
        raw = (pathlib.Path(__file__).parent/'web/icon-192.png').read_bytes()
        body = {'name':'../../照片.png','data':base64.b64encode(raw).decode()}
        status, _, output = self.request('/api/upload',body,cookie=cookie or self.paired())
        self.assertEqual(status,200)
        return json.loads(output), raw

    def test_auth_origin_and_host_boundaries(self):
        self.assertEqual(self.request('/api/chats')[0],401)
        self.assertEqual(self.request('/api/health',host='evil.test')[0],403)
        self.assertEqual(self.request('/api/pair',{'token':self.relay.token},origin='https://evil.test')[0],403)
        self.assertEqual(self.request('/api/pair',{'token':'wrong'})[0],400)
        cookie = self.paired()
        self.assertEqual(self.request('/api/chats',cookie=cookie)[0],200)
        status, headers, _ = self.request('/api/pair',{'token':self.relay.token},
                                         host='example.tailnet.ts.net:8444',origin=self.relay.origin)
        self.assertEqual(status,200)
        self.assertIn('Secure',headers['Set-Cookie'])
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        self.assertEqual(self.request('/api/local/setup',host='example.tailnet.ts.net:8444',cookie=cookie)[0],403)
        self.assertEqual(self.request('/api/local/setup',extra={'X-Forwarded-For':'100.64.0.2'})[0],403)
        self.assertEqual(self.request('/api/local/stop',{},extra={'X-Forwarded-For':'100.64.0.2'})[0],403)
        self.assertEqual(self.request('/api/logout',{},cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/chats',cookie=cookie)[0],401)

    def test_mobile_can_switch_provider(self):
        cookie = self.paired()
        status, _, raw = self.request('/api/meta', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['provider'], 'codex')
        status, _, raw = self.request('/api/provider', {'provider':'all'}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['provider'], 'all')
        status, _, raw = self.request('/api/meta', cookie=cookie)
        self.assertEqual(json.loads(raw)['provider'], 'all')

    def test_codex_model_catalog_and_confirmed_selection(self):
        cookie = self.paired()
        with mock.patch('server.time.monotonic', return_value=5):
            status, _, raw = self.request('/api/codex/models', {'thread_id': 'chat-a'}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['choices'], ['gpt-fixture', 'gpt-next'])
        status, _, raw = self.request('/api/codex/model', {'thread_id': 'chat-a', 'model': 'gpt-next'}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['requested_model'], 'gpt-next')
        receipt = self.relay.send('chat-a', 'model test', 'model-request-123456', codex_model='gpt-next')
        self.assertEqual(receipt['requested_model'], 'gpt-next')
        self.model = 'desktop-changed'
        # Retrying an accepted request only returns its receipt.
        self.assertEqual(self.relay.send('chat-a', 'model test', 'model-request-123456', codex_model='gpt-next')['id'], receipt['id'])
        with self.assertRaisesRegex(DeliveryError, '模型已变化'):
            self.relay.send('chat-a', 'new message', 'model-request-654321', codex_model='gpt-next')
        self.assertEqual(len(self.calls), 1)

    def test_codex_model_failure_does_not_claim_success(self):
        cookie = self.paired()
        def reject(*args, **kwargs):
            raise DeliveryError('请打开桌面聊天')
        self.relay.model_bridge = reject
        status, _, raw = self.request('/api/codex/model', {'thread_id': 'chat-a', 'model': 'gpt-next'}, cookie=cookie)
        self.assertEqual(status, 409)
        self.assertIn('打开桌面', json.loads(raw)['error'])
        self.assertEqual(self.relay.codex_model_cache, {})
        self.assertEqual(self.calls, [])

    def claude_session(self, model='claude-opus-fixture'):
        from claude_backend import ClaudeData
        project = self.root / 'claude/projects/demo'
        project.mkdir(parents=True)
        self.claude_transcript = project / 'session-fixture.jsonl'
        entries = [{'type':'user','sessionId':'session-fixture','cwd':str(self.root),
                    'message':{'content':'演示问题'}}]
        if model:
            entries.append({'type':'assistant','sessionId':'session-fixture',
                            'message':{'model':model,'content':'演示回复'}})
        self.claude_transcript.write_text('\n'.join(json.dumps(item) for item in entries)+'\n',encoding='utf-8')
        self.relay.claude = ClaudeData(self.root / 'claude')
        self.relay.set_provider('all')
        return 'claude:session-fixture'

    def test_claude_model_selection_persists_and_is_pinned_to_delivery(self):
        identity = self.claude_session()
        cookie = self.paired()
        status, _, raw = self.request('/api/chat/'+identity,cookie=cookie)
        self.assertEqual(status,200)
        snapshot = json.loads(raw)
        self.assertEqual(snapshot['claude_model']['requested_model'],'claude-opus-fixture')
        self.assertEqual(snapshot['messages'][-1]['model'],'claude-opus-fixture')
        self.assertEqual(self.request('/api/claude/model',{'thread_id':identity,'model':'opus'})[0],401)
        status, _, raw = self.request('/api/claude/model',{'thread_id':identity,'model':'vendor/opus-custom'},cookie=cookie)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['requested_model'],'vendor/opus-custom')
        stored = json.loads(self.relay.config_path.read_text('utf-8'))
        self.assertEqual(stored['claude_chat_models'][identity],'vendor/opus-custom')
        called = []
        def sender(*args, **kwargs):
            called.append(kwargs['model'])
            return {'delivery_mode':'claude-cli'}
        self.relay.claude_sender = sender
        body = {'thread_id':identity,'text':'新任务','request_id':'claude-request-12345'}
        status, _, raw = self.request('/api/send',body,cookie=cookie)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['requested_model'],'vendor/opus-custom')
        self.relay.set_claude_model(identity,'haiku')
        status, _, raw = self.request('/api/send',dict(body,claude_model='haiku'),cookie=cookie)
        self.assertEqual(json.loads(raw)['requested_model'],'vendor/opus-custom')
        self.assertEqual(called,['vendor/opus-custom'])
        with self.claude_transcript.open('a',encoding='utf-8') as stream:
            for item in [{'type':'user','uuid':'new-user','message':{'content':'新任务'}},
                         {'type':'assistant','message':{'id':'new-answer','model':'provider-reported-model','content':'已处理'}}]:
                stream.write(json.dumps(item)+'\n')
        status, _, raw = self.request('/api/chat/'+identity,cookie=cookie)
        snapshot = json.loads(raw)
        self.assertEqual(snapshot['messages'][-2]['requested_model'],'vendor/opus-custom')
        self.assertEqual(snapshot['messages'][-1]['model'],'provider-reported-model')
        body.update(request_id='claude-request-67890',claude_model='opus')
        self.request('/api/send',body,cookie=cookie)
        self.assertEqual(called,['vendor/opus-custom','opus'])

    def test_claude_unknown_model_requires_selection_and_codex_cannot_be_changed(self):
        identity = self.claude_session(model='')
        cookie = self.paired()
        called = []
        self.relay.claude_sender = lambda *args, **kwargs: called.append(kwargs)
        body = {'thread_id':identity,'text':'任务','request_id':'unknown-model-1234'}
        self.assertEqual(self.request('/api/send',body,cookie=cookie)[0],400)
        self.assertEqual(called,[])
        self.assertEqual(self.request('/api/claude/model',{'thread_id':identity,'model':'--invalid'},cookie=cookie)[0],400)
        self.assertEqual(self.request('/api/claude/model',{'thread_id':'chat-a','model':'opus'},cookie=cookie)[0],400)

    def test_double_submit_and_restart_never_send_twice(self):
        cookie = self.paired()
        body = {'thread_id':'chat-a','text':'中文\n`literal` $(literal) & |','request_id':'request-unique-1234'}
        def submit(_):
            return self.request('/api/send',body,cookie=cookie)[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(list(executor.map(submit,range(4))),[200]*4)
        self.assertEqual(len(self.calls),1)
        reopened = Relay(self.root / 'mobile',self.config,self.relay.sender)
        self.assertEqual(reopened.send('chat-a',body['text'],body['request_id'])['result'],'sent')
        self.assertEqual(len(self.calls),1)
        status, headers, data = self.request('/api/chat/chat-a',cookie=cookie)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(data)['receipts'][0]['status'],'等待接收')
        self.assertEqual(headers['Cache-Control'],'no-store')
        body['text'] = 'different'
        self.assertEqual(self.request('/api/send',body,cookie=cookie)[0],400)

    def test_timeout_persisted_and_never_automatically_replayed(self):
        def timeout(*args):
            self.calls.append(args)
            raise DeliveryError('timeout',True)
        self.relay.sender = timeout
        first = self.relay.send('chat-a','任务','uncertain-request-123')
        self.assertEqual(first['result'],'uncertain')
        self.assertEqual(self.relay.send('chat-a','任务','uncertain-request-123')['result'],'uncertain')
        reopened = Relay(self.root/'mobile',self.config,timeout)
        reopened.send('chat-a','任务','uncertain-request-123')
        self.assertEqual(len(self.calls),1)

    def test_invalid_or_archived_thread_cannot_be_sent(self):
        for text in ['', '中'*12001]:
            with self.assertRaises(ValueError):
                self.relay.send('chat-a',text,'test-request-123456')
        with self.assertRaises(ValueError):
            self.relay.send('unknown','任务','test-request-123456')
        self.assertFalse(self.calls)

    def test_pairing_and_pwa_assets(self):
        self.assertEqual(self.request('/api/local/setup')[0],200)
        status, headers, data = self.request('/api/local/qr.svg')
        self.assertEqual(status,200)
        self.assertIn(b'<svg',data)
        for path in ['/','/style.css','/app.js','/marked.js','/purify.js','/manifest.webmanifest','/sw.js','/icon-192.png']:
            status, headers, data = self.request(path)
            self.assertEqual(status,200,path)
            self.assertTrue(data,path)

    def test_only_one_service_can_own_the_state_directory(self):
        code = ('import pathlib,sys;sys.path.insert(0,sys.argv[1]);from launcher import service_lock;'
                'context=service_lock(pathlib.Path(sys.argv[2]));print(context.__enter__());context.__exit__(None,None,None)')
        def probe():
            return subprocess.check_output([sys.executable,'-c',code,str(pathlib.Path(__file__).parent),
                                            str(self.root/'lock')],text=True).strip()
        with service_lock(self.root/'lock') as acquired:
            self.assertTrue(acquired)
            self.assertEqual(probe(),'False')
        self.assertEqual(probe(),'True')

    def test_images_require_auth_and_valid_image_data(self):
        cookie = self.paired()
        self.assertEqual(self.request('/api/upload',{'name':'a','data':'bad'})[0],401)
        for raw in [b'<svg onload="alert(1)"></svg>',b'not an image',b'']:
            self.assertEqual(self.request('/api/upload',{'name':'photo.png','data':base64.b64encode(raw).decode()},cookie=cookie)[0],400)
        image, raw = self.upload_image(cookie)
        self.assertEqual(image['name'],'照片.png')
        self.assertEqual(self.request(image['url'])[0],401)
        status, headers, output = self.request(image['url'],cookie=cookie)
        self.assertEqual(status,200)
        self.assertEqual(headers['Content-Type'],'image/png')
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(raw,output)
        self.assertEqual(self.request('/api/image/../../config.json',cookie=cookie)[0],400)

    def test_image_only_delivery_deduplication_and_history(self):
        image, raw = self.upload_image()
        calls = []
        def sender(cli,home,chat,text,paths,request_id):
            self.assertEqual(text,'')
            self.assertEqual(paths[0].read_bytes(),raw)
            calls.append(request_id)
            return {'warning':False,'queue_ids':['image-queue']}
        self.relay.image_sender = sender
        first = self.relay.send('chat-a','','image-only-request-123',[image['id']])
        self.assertEqual(first['result'],'sent')
        self.relay.send('chat-a','','image-only-request-123',[image['id']])
        self.assertEqual(len(calls),1)
        with self.assertRaises(ValueError):
            self.relay.send('chat-a','','image-only-request-123',[])
        for ids in [['f'*32],['../../secrets']]:
            with self.assertRaises(ValueError):
                self.relay.send('chat-a','说明','invalid-image-request',ids)
        content = [{'type':'image','url':'data:image/png;base64,'+base64.b64encode(raw).decode()}]
        with contextlib.closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as c, c:
            c.execute('CREATE TABLE thread_items (thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,item_type TEXT,item_json TEXT)')
            c.execute('CREATE TABLE thread_turns (thread_id TEXT,turn_id TEXT,rollout_ordinal INT,status TEXT,error_json TEXT)')
            c.execute("INSERT INTO thread_turns VALUES ('chat-a','turn-image',1,'completed',NULL)")
            c.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?)',('chat-a','turn-image','native-image-message',1,'userMessage',json.dumps({'type':'userMessage','content':content})))
        snapshot = self.relay.snapshot('chat-a')
        self.assertEqual(snapshot['receipts'][0]['status'],'已完成')
        self.assertTrue(snapshot['receipts'][0]['seen_in_history'])
        self.assertEqual(snapshot['messages'][0]['images'][0]['id'],image['id'])
        self.assertEqual(snapshot['messages'][0]['text'],'')

    def test_text_receipt_does_not_match_a_different_picture(self):
        from backend import Message, CodexData
        image, raw = self.upload_image()
        receipt={'id':'request','text':'相同说明','images':[image],'baseline_ids':[]}
        other=Message('different','user','相同说明',images=('data:image/png;base64,YWJj',))
        self.assertIsNone(CodexData.receipt_message(receipt,[other]))
        actual=Message('correct','user','相同说明',images=('data:image/png;base64,'+base64.b64encode(raw).decode(),))
        self.assertEqual(CodexData.receipt_message(receipt,[other,actual]).id,'correct')

    def test_receipt_trailing_newlines_and_persisted_turn(self):
        from backend import Message, CodexData
        receipt = {'id':'request', 'text':'聊天测试', 'baseline_ids':[]}
        for ending in ['\n', '\r\n', '']:
            actual = Message('native', 'user', '聊天测试'+ending)
            self.assertEqual(CodexData.receipt_message(receipt, [actual]).id, 'native')
        self.assertIsNone(CodexData.receipt_message(receipt, [Message('space','user','聊天测试 ')]))
        self.add_question()
        with contextlib.closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as c,c:
            c.execute("UPDATE thread_turns SET status='failed'")
        receipt.update(thread_id='chat-a',seen_in_history=True,message_id='original',turn_id='turn-q')
        self.assertIsNone(CodexData.receipt_message(receipt,[Message('later','user','聊天测试')]))
        self.assertEqual(self.relay.data.receipt_status(receipt,[],set()),'执行失败')

    def test_old_receipt_recovers_beyond_visible_history(self):
        self.add_question()
        self.relay.send('chat-a','聊天测试','old-receipt-request-123')
        with contextlib.closing(sqlite3.connect(self.home/'queue_1.sqlite')) as c,c:
            c.execute('DELETE FROM queued_items')
        with contextlib.closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as c,c:
            c.execute("UPDATE thread_turns SET status='completed'")
            c.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?)',('chat-a','turn-q','old-native',2,'userMessage',json.dumps({'type':'userMessage','content':[{'type':'text','text':'聊天测试\n'}]})))
            for index in range(70):
                c.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?)',('chat-a','turn-q',f'later-{index}',index+3,'agentMessage',json.dumps({'type':'agentMessage','text':f'进度 {index}'})))
        first=self.relay.snapshot('chat-a')['receipts'][0]
        self.assertEqual(first['status'],'已完成')
        self.assertEqual(first['message_id'],'old-native')
        self.assertEqual(first['turn_id'],'turn-q')
        self.assertEqual(self.relay.snapshot('chat-a')['receipts'][0]['status'],'已完成')
        with self.relay.connect() as c:
            first.pop('turn_id');first.pop('message_id')
            self.relay.save_receipt(c,first)
        migrated=self.relay.snapshot('chat-a')['receipts'][0]
        self.assertEqual(migrated['turn_id'],'turn-q')
        self.assertEqual(migrated['status'],'已完成')

    def test_multiple_images_and_total_byte_limit(self):
        image, raw = self.upload_image()
        self.relay.image_sender = lambda *args: {'warning':False,'queue_ids':['many-images']}
        self.assertEqual(self.relay.send('chat-a','12 张截图','many-image-request-123',[image['id']]*12)['result'],'sent')
        with self.relay.connect() as c:
            c.execute('UPDATE uploads SET size=? WHERE id=?',(8*1024*1024,image['id']))
        with self.assertRaisesRegex(ValueError,'32 MB'):
            self.relay.send('chat-a','超过总大小','oversize-images-12345',[image['id']]*5)

    def add_question(self):
        with contextlib.closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as c,c:
            c.execute('CREATE TABLE IF NOT EXISTS thread_items (thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,item_type TEXT,item_json TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS thread_turns (thread_id TEXT,turn_id TEXT,rollout_ordinal INT,status TEXT,error_json TEXT)')
            c.execute("INSERT INTO thread_turns VALUES ('chat-a','turn-q',1,'inProgress',NULL)")
            question={'type':'agentMessage','id':'call-question','delivery':'async','phase':'final_answer','text':'请选择设备',
                      'questions':[{'title':'选择设备','options':['Mac','Windows']},{'title':'补充说明'}]}
            c.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?)',('chat-a','turn-q','call-question',1,'agentMessage',json.dumps(question)))

    def test_question_options_and_native_answer_ids(self):
        from notifications import question_answers
        self.add_question()
        cookie=self.paired()
        status,_,raw=self.request('/api/chat/chat-a',cookie=cookie)
        self.assertEqual(json.loads(raw)['messages'][0]['questions'][0]['options'],['Mac','Windows'])
        body={'thread_id':'chat-a','message_id':'call-question','answers':['Mac','中文说明'],'request_id':'answer-question-1234'}
        self.assertEqual(self.request('/api/answer',body)[0],401)
        self.assertEqual(self.request('/api/answer',body,cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/answer',body,cookie=cookie)[0],200)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(question_answers(self.calls[0][1]),{('call-question',0):'Mac',('call-question',1):'中文说明'})
        snapshot=self.relay.snapshot('chat-a')
        self.assertEqual(snapshot['messages'][0]['questions'][1]['answer'],'中文说明')
        self.assertEqual(snapshot['messages'][0]['questions'][1]['reply_status'],'submitted')
        self.assertEqual(snapshot['receipts'][0]['status'],'桌面已接收 · 已立即提交')
        self.assertEqual(self.relay.data.queue_ids('chat-a'),set())
        body['request_id']='answer-question-other'
        self.assertEqual(self.request('/api/answer',body,cookie=cookie)[0],400)

    def test_answer_connection_failure_and_uncertain_delivery(self):
        self.add_question()
        body={'thread_id':'chat-a','message_id':'call-question','answers':['Mac','说明'],'request_id':'failed-answer-12345'}
        cookie=self.paired()
        attempts=[]
        def fail(*args):
            attempts.append(args)
            raise DeliveryError('桌面未连接')
        self.relay.answer_sender=fail
        receipt=json.loads(self.request('/api/answer',body,cookie=cookie)[2])
        self.assertEqual(receipt['result'],'failed')
        self.assertIsNone(self.relay.snapshot('chat-a')['messages'][0]['questions'][0]['answer'])
        def uncertain(*args):
            attempts.append(args)
            raise DeliveryError('回执丢失',True)
        self.relay.answer_sender=uncertain
        body['request_id']='uncertain-answer-12345'
        for _ in range(2):
            receipt=json.loads(self.request('/api/answer',body,cookie=cookie)[2])
            self.assertEqual(receipt['result'],'uncertain')
        self.assertEqual(len(attempts),2)
        self.assertEqual(self.relay.snapshot('chat-a')['messages'][0]['questions'][0]['reply_status'],'uncertain')
        self.assertEqual(self.relay.data.queue_ids('chat-a'),set())

    def legacy_queued_answer(self):
        self.add_question()
        values=[{'questionItemId':json.dumps(['request_user_input_async','call-question',index],separators=(',',':')),
                 'question':title,'answer':answer} for index,(title,answer) in enumerate(
                     [('选择设备','Mac'),('补充说明','中文说明')])]
        text='<send_user_message_question_reply>\n'+json.dumps(values,ensure_ascii=False)+'\n</send_user_message_question_reply>'
        self.relay.send('chat-a',text,'legacy-answer-12345')
        return {'thread_id':'chat-a','message_id':'call-question','answers':['Mac','中文说明'],'request_id':'legacy-answer-12345'}

    def test_legacy_queued_answer_can_be_submitted_immediately_once(self):
        body=self.legacy_queued_answer()
        snapshot=self.relay.snapshot('chat-a')
        self.assertEqual(snapshot['messages'][0]['questions'][0]['reply_status'],'queued')
        self.assertEqual(snapshot['messages'][0]['questions'][0]['reply_request_id'],body['request_id'])
        native=self.relay.answer_sender
        def recover(chat,text,request_id,home,queue_ids):
            self.assertEqual(queue_ids,['q-a'])
            with contextlib.closing(sqlite3.connect(self.home/'queue_1.sqlite')) as c,c:
                c.execute('DELETE FROM queued_items WHERE id=?',('q-a',))
            return native(chat,text,request_id,home,queue_ids)
        self.relay.answer_sender=recover
        cookie=self.paired()
        for _ in range(2):
            receipt=json.loads(self.request('/api/answer',body,cookie=cookie)[2])
            self.assertEqual(receipt['delivery_mode'],'immediate')
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.relay.data.queue_ids('chat-a'),set())

    def test_failed_legacy_recovery_retains_the_original_queue_identity(self):
        body=self.legacy_queued_answer()
        def fail(*args):
            raise DeliveryError('请打开桌面聊天')
        self.relay.answer_sender=fail
        receipt=self.relay.answer('chat-a','call-question',body['answers'],body['request_id'])
        self.assertEqual(receipt['delivery_mode'],'queue')
        self.assertEqual(receipt['queue_ids'],['q-a'])
        self.assertEqual(self.relay.snapshot('chat-a')['messages'][0]['questions'][0]['reply_status'],'queued')
        with self.assertRaises(ValueError):
            self.relay.answer('chat-a','call-question',body['answers'],'new-answer-not-allowed')

    def test_notification_transitions_persistence_and_logout(self):
        self.add_question()
        cookie=self.paired()
        self.assertEqual(self.request('/api/notifications')[0],401)
        self.assertEqual(self.request('/api/notifications',{'enabled':True},cookie=cookie)[0],200)
        self.assertEqual(self.relay.notifications.poll(cookie.split('=',1)[1],0)['events'],[])
        with contextlib.closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as c,c:
            c.execute("UPDATE thread_turns SET status='completed'")
        self.relay.notifications.collect()
        self.relay.notifications.collect()
        events=self.relay.notifications.poll(cookie.split('=',1)[1],0)['events']
        self.assertEqual([e['kind'] for e in events],['completed'])
        with contextlib.closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as c,c:
            c.execute("UPDATE thread_turns SET status='failed'")
        self.relay.notifications.collect()
        question={'type':'agentMessage','id':'call-new-question','delivery':'async','questions':[{'title':'继续吗？','options':['继续','暂停']}],'text':'继续吗？'}
        with contextlib.closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as c,c:
            c.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?)',('chat-a','turn-q','call-new-question',2,'agentMessage',json.dumps(question)))
        self.relay.notifications.collect()
        events=self.relay.notifications.poll(cookie.split('=',1)[1],0)['events']
        self.assertEqual([e['kind'] for e in events],['completed','failed','needs_input'])
        reopened=Relay(self.root/'mobile',self.config,self.relay.sender)
        reopened.notifications.collect()
        self.assertEqual(len(reopened.notifications.poll(cookie.split('=',1)[1],0)['events']),3)
        with contextlib.closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as c,c:
            c.execute("UPDATE thread_turns SET status='inProgress'")
        reopened.notifications.collect()
        with contextlib.closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as c,c:
            c.execute("UPDATE thread_turns SET status='completed'")
        reopened.notifications.collect()
        self.assertEqual(len(reopened.notifications.poll(cookie.split('=',1)[1],0)['events']),3)
        self.assertEqual(events[0]['chat_title'],'中文任务')
        self.assertIn('中文任务',events[0]['body'])
        self.request('/api/logout',{},cookie=cookie)
        self.assertEqual(reopened.notifications.poll(cookie.split('=',1)[1],0)['events'],[])

    def test_push_subscription_validation_and_expiry(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        from notifications import Notifications
        cookie=self.paired().split('=',1)[1]
        def encoded(raw):return base64.urlsafe_b64encode(raw).decode().rstrip('=')
        key=ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
        subscription={'endpoint':'https://web.push.apple.com/example','keys':{'p256dh':encoded(key),'auth':encoded(b'0'*16)}}
        self.assertEqual(Notifications.validate_subscription(subscription),subscription)
        for endpoint in ['http://127.0.0.1/','https://evil.push.apple.com.evil.test/','https://web.push.apple.com:8879/','https://127.0.0.1/']:
            with self.assertRaises(ValueError):Notifications.validate_subscription(dict(subscription,endpoint=endpoint))
        self.relay.notifications.configure(cookie,True,subscription)
        sent=[]
        self.relay.notifications.sender=lambda sub,payload:sent.append(payload)
        with self.relay.connect() as c:self.relay.notifications.event(c,'chat-a','completed')
        self.relay.notifications.send_pushes()
        self.relay.notifications.send_pushes()
        self.assertEqual(len(sent),1)
        with self.relay.connect() as c:
            original=dict(c.execute('SELECT * FROM notification_devices').fetchone())
        self.assertEqual(self.request('/api/notifications/sync',{'subscription':subscription})[0],401)
        status,_,body=self.request('/api/notifications/sync',{'subscription':subscription},cookie='relay_session='+cookie)
        self.assertEqual(status,200)
        with self.relay.connect() as c:
            synced=dict(c.execute('SELECT * FROM notification_devices').fetchone())
        self.assertEqual(synced['push_cursor'],original['push_cursor'])
        self.assertEqual(synced['enabled_after'],original['enabled_after'])
        self.relay.notifications.configure(cookie,False)
        self.relay.notifications.sync(cookie,subscription)
        self.assertFalse(self.relay.notifications.settings(cookie)['enabled'])
        self.assertFalse(self.relay.notifications.settings(cookie)['push'])
        self.relay.notifications.revoke(cookie)
        with self.relay.connect() as c:self.relay.notifications.event(c,'chat-a','failed')
        self.relay.notifications.send_pushes()
        self.assertEqual(len(sent),1)

    def test_webpush_encrypts_payload_and_disables_redirects(self):
        import http_ece
        import requests
        from unittest.mock import patch
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        recipient=ec.generate_private_key(ec.SECP256R1())
        raw=recipient.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
        encoded=lambda data:base64.urlsafe_b64encode(data).decode().rstrip('=')
        subscription={'endpoint':'https://web.push.apple.com/offline-test','keys':{'p256dh':encoded(raw),'auth':encoded(b'a'*16)}}
        payload={'title':'Codex 通知','body':'任务已完成','thread_id':'chat-a'}
        self.relay.notifications.public_key()
        response=requests.Response();response.status_code=201
        with patch.object(requests.Session,'post',return_value=response) as post:
            self.relay.notifications.push(subscription,payload)
        kwargs=post.call_args.kwargs
        self.assertFalse(kwargs['allow_redirects'])
        self.assertIn('authorization',{name.lower() for name in kwargs['headers']})
        decoded=http_ece.decrypt(kwargs['data'],private_key=recipient,auth_secret=b'a'*16,version='aes128gcm')
        self.assertEqual(json.loads(decoded),payload)


if __name__ == '__main__':
    unittest.main(verbosity=2)
