import unittest,tempfile,os,secrets
TMP=tempfile.TemporaryDirectory();os.environ['AI_DATA_DIR']=TMP.name;os.environ['AI_WEBAPP_NO_WORKER']='1';os.environ['SESSION_COOKIE_SECURE']='false'
import server
with server.app.app_context():server.db.create_all()
class Tests(unittest.TestCase):
 def test_auth_and_internal(self):
  c=server.app.test_client();self.assertEqual(c.post('/internal/process-pending').status_code,401)
  self.assertEqual(c.get('/chats').status_code,401)
  pw=secrets.token_hex(16);name='user_'+secrets.token_hex(4)
  self.assertEqual(c.post('/auth/register',json={'username':name,'password':pw}).status_code,201)
  self.assertEqual(c.post('/auth/login',json={'username':name,'password':pw}).status_code,200)
  self.assertEqual(c.get('/chats').status_code,200)
 def test_short_password(self):
  self.assertEqual(server.app.test_client().post('/auth/register',json={'username':'demo','password':'short'}).status_code,400)
