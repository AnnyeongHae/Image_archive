import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { privateImage } from '../worker/media.js';
import { neonEndpoint } from '../worker/neon.js';
const dsn='postgresql://fixture:synthetic-password@ep-unit.us-east-2.aws.neon.tech/db?sslmode=require';
const png = new Uint8Array(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aR8sAAAAASUVORK5CYII=', 'base64'));
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const mediaObject = (bytes = png, extra = {}) => ({ size: bytes.byteLength,
  httpMetadata: { contentType: 'image/png' }, body: new Response(bytes).body, ...extra });

async function withMedia(get, run, digest = sha(png)) {
  const original = globalThis.fetch;
  globalThis.fetch = async () => Response.json({fields:[{name:'sha256',dataTypeID:25}],rows:[[digest]]});
  try { return await run({DATABASE_URL:dsn,SNAPSHOT_ID:'a'.repeat(64),PRIVATE_MEDIA:{get}}); }
  finally { globalThis.fetch = original; }
}
test('Neon refuses libpq option overrides and duplicate options',()=>{
  for(const suffix of ['&host=outside.invalid','&hostaddr=127.0.0.1','&dbname=other','&user=other','&service=local','&port=123','&sslmode=disable'])
    assert.throws(()=>neonEndpoint(dsn+suffix));
  assert.equal(neonEndpoint(dsn+'&channel_binding=require'),'https://api.us-east-2.aws.neon.tech/sql');
});
test('private image uses pinned hash key and never a caller URL',async()=>{
  let key;
  await withMedia(async value => {key=value;return mediaObject();}, async env => {
    const r=await privateImage(env,'CASE-001');assert.equal(r.status,200);
    assert.deepEqual(new Uint8Array(await r.arrayBuffer()),png);
    assert.equal(r.headers.get('content-length'),String(png.byteLength));
    assert.equal(key,`private/v2/sha256/${sha(png)}.png`);assert.match(r.headers.get('cache-control'),/no-store/);
    assert.equal(r.headers.get('access-control-allow-origin'),null);
    await assert.rejects(()=>privateImage(env,'https://evil.invalid/'),{code:'invalid_item'});
    env.PRIVATE_MEDIA.get=async()=>null;await assert.rejects(()=>privateImage(env,'CASE-001'),{code:'private_media_not_ready'});
    env.PRIVATE_MEDIA.get=async()=>mediaObject(png,{httpMetadata:{contentType:'text/html'}});
    await assert.rejects(()=>privateImage(env,'CASE-001'),{code:'media_contract_mismatch'});
  });
});

test('private media rejects changed bytes even when size and MIME match',async()=>{
  const corrupted=png.slice();corrupted[corrupted.length-1]^=1;
  await withMedia(async()=>mediaObject(corrupted), env => assert.rejects(()=>privateImage(env,'CASE-001'),{code:'media_contract_mismatch'}));
});

test('private media rejects non-PNG bytes even if their hash is pinned',async()=>{
  const data=new TextEncoder().encode('not a PNG despite declared content type');
  await withMedia(async()=>mediaObject(data), env => assert.rejects(()=>privateImage(env,'CASE-001'),{code:'media_contract_mismatch'}),sha(data));
});

test('private media rejects short and overlong bodies against declared content length',async()=>{
  for(const size of [png.length-1,png.length+1]) {
    await withMedia(async()=>mediaObject(png,{size}), env => assert.rejects(()=>privateImage(env,'CASE-001'),{code:'media_contract_mismatch'}));
  }
});

test('private media enforces the 15 MiB cap before reading and cancels malformed streams',async()=>{
  for(const size of [0,7,15*1048576+1,NaN,undefined]) {
    let cancelled=false;
    const body={cancel:async()=>{cancelled=true;},getReader:()=>assert.fail('metadata must be checked before stream reads')};
    await withMedia(async()=>mediaObject(png,{size,body}), env => assert.rejects(()=>privateImage(env,'CASE-001'),{code:'media_contract_mismatch'}));
    assert.equal(cancelled,true);
  }
  let cancelled=false;
  const body=new ReadableStream({pull(controller){controller.enqueue(new Uint8Array(9));},cancel(){cancelled=true;}});
  await withMedia(async()=>mediaObject(png,{size:8,body}), env => assert.rejects(()=>privateImage(env,'CASE-001'),{code:'media_contract_mismatch'}));
  assert.equal(cancelled,true);
});

test('private media validates chunked bytes and hides stream failure details',async()=>{
  let offset=0;
  const body=new ReadableStream({pull(controller){
    if(offset===png.length){controller.close();return;}
    controller.enqueue(png.slice(offset,offset+3));offset=Math.min(offset+3,png.length);
  }});
  await withMedia(async()=>mediaObject(png,{body}),async env=>{
    const response=await privateImage(env,'CASE-001');assert.deepEqual(new Uint8Array(await response.arrayBuffer()),png);
  });
  const broken=new ReadableStream({pull(controller){controller.error(new Error('synthetic upstream private detail'));}});
  await withMedia(async()=>mediaObject(png,{body:broken}),env=>assert.rejects(()=>privateImage(env,'CASE-001'),error=>{
    assert.equal(error.code,'media_contract_mismatch');assert.equal(error.message,'media_contract_mismatch');return true;
  }));
});

test('malformed database hash never becomes an object key',async()=>{
  await withMedia(async()=>assert.fail('untrusted hash must not reach R2'),env=>assert.rejects(()=>privateImage(env,'CASE-001'),{code:'media_contract_mismatch'}),'../other-object');
});
