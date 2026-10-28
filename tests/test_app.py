import io
import json
import os
import struct
import wave
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('RECORDER_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('RECORDER_PASSWORD', raising=False)
    from recorder import app as module
    module.ROOT = tmp_path
    module.init_db()
    with TestClient(module.app) as client:
        yield client


def create_project(client):
    r = client.post('/api/projects', json={'title': 'KV Cache', 'script': 'First paragraph.\n\nSecond paragraph.'})
    assert r.status_code == 201, r.text
    return r.json()


def begin(client, sid, rate=16000):
    r = client.post(f'/api/sections/{sid}/takes', json={'sample_rate': rate})
    assert r.status_code == 201, r.text
    return r.json()['id']


def pcm(*samples):
    return struct.pack('<' + 'h' * len(samples), *samples)


def finish(client, sid, parts, rate=16000):
    tid=begin(client,sid,rate)
    for i, chunk in enumerate(parts):
        assert client.put(f'/api/takes/{tid}/chunks/{i}', content=chunk).status_code == 200
    result=client.post(f'/api/takes/{tid}/finish',json={'expected_chunks':len(parts)})
    assert result.status_code == 200, result.text
    return tid, result.json()


def test_project_and_sections(setup):
    c=setup
    assert c.get('/api/projects').json() == []
    p=create_project(c)
    assert [s['text'] for s in p['sections']] == ['First paragraph.', 'Second paragraph.']
    assert len(c.get('/api/projects').json()) == 1
    sid=p['sections'][0]['id']
    assert c.patch(f'/api/sections/{sid}',json={'text':'Changed safely'}).status_code==200
    assert c.post(f'/api/projects/{p["id"]}/sections',json={'text':'Third.'}).status_code==201
    assert len(c.get(f'/api/projects/{p["id"]}').json()['sections'])==3
    assert c.get('/api/projects/missing').status_code==404


def test_chunks_idempotent_and_wav(setup):
    c=setup;p=create_project(c);tid=begin(c,p['sections'][0]['id'])
    chunk0=pcm(0,-32768,32767,100,0)
    chunk1=pcm(50,100)
    assert c.put(f'/api/takes/{tid}/chunks/0',content=chunk0).json()['duplicate'] is False
    assert c.put(f'/api/takes/{tid}/chunks/0',content=chunk0).json()['duplicate'] is True
    assert c.put(f'/api/takes/{tid}/chunks/0',content=chunk1).status_code==409
    assert c.put(f'/api/takes/{tid}/chunks/1',content=chunk1).status_code==200
    assert c.get(f'/api/takes/{tid}/upload').json()['received']==[0,1]
    r=c.post(f'/api/takes/{tid}/finish',json={'expected_chunks':2})
    assert r.status_code==200,r.text
    t=r.json();assert t['samples']==7 and t['selected'] and t['duration']==7/16000
    assert t['clipping_pct']>0
    wav_bytes=c.get(f'/api/takes/{tid}/audio').content
    with wave.open(io.BytesIO(wav_bytes),'rb') as wav:
        assert (wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes())==(1,2,16000,7)
        assert wav.readframes(7)==chunk0+chunk1
    assert c.post(f'/api/takes/{tid}/finish',json={'expected_chunks':2}).status_code==200
    assert c.put(f'/api/takes/{tid}/chunks/2',content=chunk0).status_code==409


def test_missing_chunks_and_bad_inputs(setup):
    c=setup;p=create_project(c);tid=begin(c,p['sections'][0]['id'])
    assert c.put(f'/api/takes/{tid}/chunks/1',content=pcm(23)).status_code==200
    assert c.post(f'/api/takes/{tid}/finish',json={'expected_chunks':2}).status_code==409
    assert c.put(f'/api/takes/{tid}/chunks/0',content=b'odd').status_code==413
    assert c.put(f'/api/takes/{tid}/chunks/0',content=b'').status_code==413
    assert c.put(f'/api/takes/{tid}/chunks/-1',content=pcm(2)).status_code==422
    assert c.post(f'/api/sections/{p["sections"][0]["id"]}/takes',json={'sample_rate':2}).status_code==422
    assert c.post(f'/api/takes/{tid}/finish',json={'expected_chunks':1}).status_code==409
    assert c.put(f'/api/takes/{tid}/chunks/0',content=pcm(0)).status_code==200
    assert c.post(f'/api/takes/{tid}/finish',json={'expected_chunks':2}).status_code==200


def test_select_trim_mix_export(setup):
    c=setup;p=create_project(c);s0,s1=[s['id'] for s in p['sections']]
    a,_=finish(c,s0,[pcm(*([1000]*16000))]);b,_=finish(c,s0,[pcm(*([2000]*16000))]);d,_=finish(c,s1,[pcm(*([3000]*16000))]);
    assert c.get(f'/api/projects/{p["id"]}').json()['sections'][0]['takes'][0]['selected'] is False
    assert c.patch(f'/api/takes/{a}',json={'selected':True}).status_code==200
    assert c.patch(f'/api/takes/{a}',json={'trim_start':0.1,'trim_end':0.6}).status_code==200
    assert c.patch(f'/api/takes/{a}',json={'trim_start':1.1}).status_code==422
    assert c.patch(f'/api/takes/{a}',json={'trim_end':0.02}).status_code==422
    assert c.patch(f'/api/takes/{a}',json={'transcript':'My actual words','transcript_reviewed':True}).status_code==200
    r=c.get(f'/api/projects/{p["id"]}/mix.wav');assert r.status_code==200,r.text
    with wave.open(io.BytesIO(r.content)) as wav:
        assert wav.getnframes()==8000+4000+16000 # 0.5s, 0.25s gap, 1s
        assert struct.unpack('<h',wav.readframes(1))[0]==1000
    z=c.get(f'/api/projects/{p["id"]}/export.zip');assert z.status_code==200
    with zipfile.ZipFile(io.BytesIO(z.content)) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        assert len(manifest['takes'])==2
        assert manifest['takes'][0]['spoken_transcript']=='My actual words'
        assert manifest['takes'][0]['transcript_reviewed'] is True
        assert archive.read('script.txt').decode()=='First paragraph.\n\nSecond paragraph.'
        assert len([x for x in archive.namelist() if x.endswith('.wav')])==2


def test_auth_and_origin(setup, monkeypatch):
    c=setup
    monkeypatch.setenv('RECORDER_PASSWORD','correct horse')
    assert c.get('/api/projects').status_code==401
    assert c.get('/api/auth/status').json()['authenticated'] is False
    assert c.post('/api/auth/login',json={'password':'wrong'}).status_code==401
    assert c.post('/api/auth/login',json={'password':'correct horse'}).status_code==200
    assert c.get('/api/projects').status_code==200
    assert c.post('/api/projects',json={'title':'x','script':'hi'},headers={'Origin':'http://wrong.example'}).status_code==403
    assert c.post('/api/auth/logout').status_code==200
    assert c.get('/api/projects').status_code==401


def test_empty_and_export_requires_selection(setup):
    c=setup;p=create_project(c)
    assert c.get(f'/api/projects/{p["id"]}/mix.wav').status_code==409
    tid,_=finish(c,p['sections'][0]['id'],[pcm(1,2,3)])
    assert c.patch(f'/api/takes/{tid}',json={'selected':False}).status_code==200
    assert c.get(f'/api/projects/{p["id"]}/mix.wav').status_code==409