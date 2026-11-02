import json
import socket
import threading
import unittest

from app_bridge import DesktopClient, DesktopError, submit_answer, model_settings


class BridgeTests(unittest.TestCase):
    def client(self, handler):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        for sock in (left, right):
            sock.settimeout(2)
        def endpoint(sock):
            client = DesktopClient.__new__(DesktopClient)
            client.connection = sock
            client.read, client.write = sock.recv, sock.send
            client.client_id = 'initializing-client'
            return client
        client, router = endpoint(left), endpoint(right)
        self.calls, self.router_errors = [], []
        def serve():
            try:
                while True:
                    request = router.receive()
                    if request.get('type') == 'client-discovery-response':
                        self.assertFalse(request['response']['canHandle'])
                        continue
                    method = request['method']
                    self.calls.append(request)
                    router.send({'type':'broadcast','method':'client-status-changed','params':{}})
                    router.send({'type':'client-discovery-request','requestId':'discovery','request':{}})
                    result, error = handler(request)
                    reply = {'type':'response','requestId':request['requestId'],'method':method,
                             'handledByClientId':'mobile' if method == 'initialize' else 'desktop',
                             'resultType':'error' if error else 'success','result':result}
                    if error:
                        reply['error'] = error
                    # Split frames as pipes and sockets may return partial reads.
                    raw = json.dumps(reply).encode()
                    import struct
                    frame = struct.pack('<I',len(raw)) + raw
                    for index in range(0,len(frame),7):
                        right.sendall(frame[index:index+7])
            except OSError:
                pass
            except Exception as error:
                self.router_errors.append(error)
        thread = threading.Thread(target=serve,daemon=True)
        thread.start()
        def cleanup():
            left.close()
            thread.join(timeout=3)
            if self.router_errors:
                raise self.router_errors[0]
        self.addCleanup(cleanup)
        client.initialize()
        return client

    @staticmethod
    def response(request):
        if request['method'] == 'initialize':
            return {'clientId':'mobile'}, None
        if request['method'] == 'thread-owner-discovery':
            return {'supportsUntrustedAppInput':True}, None
        return {'result':{'turnId':'turn-active'}}, None

    def payload(self):
        return {'thread_id':'fixture-chat','cwd':'/fixture','request_id':'fixture-answer-id',
                'text':'<send_user_message_question_reply>\n[{"answer":"Mac"}]\n</send_user_message_question_reply>'}

    def test_active_turn_is_steered_as_user_input(self):
        client = self.client(self.response)
        self.assertEqual(submit_answer(client,self.payload()),{'ok':True,'turn_id':'turn-active'})
        self.assertEqual([call['method'] for call in self.calls],
                         ['initialize','thread-owner-discovery','thread-follower-steer-turn'])
        call = self.calls[-1]
        self.assertEqual(call['targetClientId'],'desktop')
        self.assertEqual(call['sourceClientId'],'mobile')
        self.assertEqual(call['params']['clientUserMessageId'],'fixture-answer-id')
        self.assertEqual(call['params']['input'][0]['text'],self.payload()['text'])
        self.assertEqual(call['params']['restoreMessage']['context']['workspaceRoots'],['/fixture'])

    def test_finished_turn_starts_user_reply(self):
        def response(request):
            if request['method'] == 'thread-follower-steer-turn':
                return None, 'Cannot steer conversation fixture-chat because its active turn already ended'
            if request['method'] == 'thread-follower-start-turn':
                return {'result':{'turn':{'id':'turn-next'}}},None
            return self.response(request)
        client = self.client(response)
        self.assertEqual(submit_answer(client,self.payload())['turn_id'],'turn-next')
        self.assertEqual(self.calls[-1]['version'],2)
        self.assertEqual(self.calls[-1]['params']['turnStart']['request']['clientUserMessageId'],'fixture-answer-id')

    def test_missing_owner_is_a_definite_failure(self):
        def response(request):
            return (None,'no-client-found') if request['method'] == 'thread-owner-discovery' else self.response(request)
        client = self.client(response)
        with self.assertRaises(DesktopError) as caught:
            submit_answer(client,self.payload())
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(self.calls),2)

    def test_uncertain_reply_is_not_retried_as_new_turn(self):
        def response(request):
            return (None,'request-timeout') if request['method'] == 'thread-follower-steer-turn' else self.response(request)
        client = self.client(response)
        with self.assertRaises(DesktopError) as caught:
            submit_answer(client,self.payload())
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.calls),3)

    def test_success_requires_a_turn_receipt(self):
        def response(request):
            return ({'result':{}},None) if request['method'] == 'thread-follower-steer-turn' else self.response(request)
        client = self.client(response)
        with self.assertRaises(DesktopError) as caught:
            submit_answer(client,self.payload())
        self.assertTrue(caught.exception.uncertain)

    def test_queued_reply_is_removed_before_immediate_submission(self):
        def response(request):
            if request['method'] == 'thread-follower-remove-queued-message':
                return {'removed':{'id':request['params']['messageId']}},None
            return self.response(request)
        client = self.client(response)
        payload = dict(self.payload(),queue_ids=['queued-answer'])
        self.assertTrue(submit_answer(client,payload)['ok'])
        self.assertEqual([call['method'] for call in self.calls[-2:]],
                         ['thread-follower-remove-queued-message','thread-follower-steer-turn'])
        self.assertEqual(self.calls[-2]['params']['messageId'],'queued-answer')

    def test_taken_queue_item_does_not_trigger_a_second_submission(self):
        def response(request):
            if request['method'] == 'thread-follower-remove-queued-message':
                return {'removed':None},None
            return self.response(request)
        client = self.client(response)
        with self.assertRaises(DesktopError) as caught:
            submit_answer(client,dict(self.payload(),queue_ids=['taken-answer']))
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.calls[-1]['method'],'thread-follower-remove-queued-message')

    def model_client(self, applied=True, mismatch=False, effort=True):
        current = ['gpt-before']
        def response(request):
            if request['method'] == 'thread-follower-read-model-settings':
                settings = {'model': current[0]}
                if effort:
                    settings['reasoningEffort'] = 'high'
                return {'settings': settings}, None
            if request['method'] == 'thread-follower-update-thread-settings':
                if applied:
                    current[0] = 'gpt-other' if mismatch else request['params']['threadSettings']['model']
                return {'applied': applied}, None
            return self.response(request)
        return self.client(response)

    def test_model_update_is_incremental_and_read_back(self):
        client = self.model_client()
        result = model_settings(client, {'thread_id': 'fixture-chat', 'action': 'set_model', 'model': 'gpt-next'})
        self.assertEqual(result, {'ok': True, 'model': 'gpt-next'})
        update = self.calls[-2]
        self.assertEqual(update['version'], 2)
        self.assertEqual(update['params'], {'conversationId': 'fixture-chat', 'threadSettings': {'model': 'gpt-next'},
                          'condition': {'ifModelEquals': 'gpt-before', 'ifEffortEquals': 'high'}})
        self.assertEqual(self.calls[-1]['method'], 'thread-follower-read-model-settings')

    def test_model_update_rejection_or_mismatch_is_not_success(self):
        for arguments in [{'applied': False}, {'mismatch': True}]:
            with self.subTest(arguments=arguments), self.assertRaises(DesktopError):
                model_settings(self.model_client(**arguments),
                               {'thread_id': 'fixture-chat', 'action': 'set_model', 'model': 'gpt-next'})

    def test_model_read_does_not_start_or_steer_a_turn(self):
        self.assertEqual(model_settings(self.model_client(), {'thread_id': 'fixture-chat', 'action': 'read_model'})['model'], 'gpt-before')
        self.assertEqual([call['method'] for call in self.calls],
                         ['initialize', 'thread-owner-discovery', 'thread-follower-read-model-settings'])

    def test_model_update_with_no_reasoning_setting(self):
        result = model_settings(self.model_client(effort=False),
                                {'thread_id': 'fixture-chat', 'action': 'set_model', 'model': 'gpt-next'})
        self.assertEqual(result['model'], 'gpt-next')
        self.assertNotIn('condition', self.calls[-2]['params'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
