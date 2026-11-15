"""Capture the shipped browser UI using synthetic API responses only.
Install playwright separately; set DEMO_CHROME to an existing Chrome executable,
or run `playwright install chromium`. No real server or account is contacted.
"""
import os, json, mimetypes
from pathlib import Path
from urllib.parse import urlparse, unquote
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/images'; OUT.mkdir(parents=True,exist_ok=True)
WEB=ROOT
FIXTURES={
 '/auth/me':{'authenticated':True,'user':{'id':1,'username':'demo-reader'}},
 '/models':{'default':'demo-model','models':[{'id':'demo-model','label':'Demo model','average':'fixture data','installed':True,'description':'Synthetic preview','speed':'not benchmarked'}]},
 '/chats':[{'id':1,'name':'Planning a home media library'}],
 '/chats/1':[{'id':1,'role':'user','content':'How can I organize an unsorted music archive?'},{'id':2,'role':'assistant','content':'Start with a preview. Read existing tags, group tracks by artist and album, and review unresolved matches before writing changes.\n\nThis is a synthetic documentation conversation, not a live model response.'}],
 '/health':{'ok':True}}
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True,executable_path=os.environ.get('DEMO_CHROME') or None)
 page=browser.new_page(viewport={'width':1440,'height':960},device_scale_factor=1)
 def serve(route):
  u=urlparse(route.request.url)
  if u.hostname!='demo.local':route.abort();return
  path=unquote(u.path)
  if path in FIXTURES:route.fulfill(json=FIXTURES[path]);return
  if path.startswith(('/api/','/auth/')):route.fulfill(json={});return
  file=(WEB/('index.html' if path=='/' else path.lstrip('/'))).resolve()
  if not file.is_relative_to(WEB.resolve()) or not file.is_file():route.fulfill(status=404,body='Not found');return
  route.fulfill(body=file.read_bytes(),content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
 page.route('**/*',serve)
 page.goto('http://demo.local/',wait_until='networkidle')
 page.locator(".message.assistant").wait_for()
 page.screenshot(path=str(OUT/"overview.png"),full_page=True)
 browser.close()
