import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { searchInput } from '../worker/index.js';
import { neonEndpoint, sql } from '../worker/neon.js';
import { fetchJson, finiteVector, readJsonBounded, sha256 } from '../worker/http.js';
import { queryVector, searchGroups, qdrantBase, resetQueryCacheForTests } from '../worker/retrieval.js';

const snapshot='a'.repeat(64), manifest='b'.repeat(64);
const token='iar_v2_'+Buffer.alloc(32,7).toString('base64url');
const vector=Array(512).fill(0); vector[0]=1;
async function env() {
  return { PRIVATE_API_ENABLED:'true', SNAPSHOT_ID:snapshot,SNAPSHOT_MANIFEST_SHA256:manifest,
    DATABASE_URL:'postgresql://user:synthetic-password@ep-unit-test.us-east-2.aws.neon.tech/db?sslmode=require',
    QDRANT_ENDPOINT:'https://example.eu-central-1-0.aws.cloud.qdrant.io',QDRANT_API_KEY:'synthetic-only',
    TEXT_COLLECTION:`image_archive_v2_${snapshot.slice(0,16)}_text512`,
    API_TOKEN_HASHES:JSON.stringify([{id:'owner',sha256:await sha256(token),scopes:['rag:search','archive:read'],
      expires_at:'2099-01-01T00:00:00Z',revoked:false}]), OWNER_RATE_LIMITER:{limit:async()=>({success:true})}};
}
function request(body,extra={}) {
  return new Request('https://owner.example/api/private/v2/search',{method:'POST',headers:{
    'Authorization':`Bearer ${token}`,'Content-Type':'application/json',...extra},body:JSON.stringify(body)});
}
function dbResult(rows) {
  if(!rows.length)return Response.json({fields:[],rows:[]});
  const keys=Object.keys(rows[0]);
  return Response.json({fields:keys.map(name=>({name,dataTypeID: typeof rows[0][name]==='object'?3802:typeof rows[0][name]==='boolean'?16:25})),
    rows:rows.map(row=>keys.map(k=>typeof row[k]==='object'?JSON.stringify(row[k]):typeof row[k]==='boolean'?(row[k]?'t':'f'):String(row[k])))});
}
async function withFetch(fake,fn){const original=globalThis.fetch;globalThis.fetch=fake;try{return await fn();}finally{globalThis.fetch=original;resetQueryCacheForTests();}}
function item(id,group='g1',rep='A') {return {item_id:id,group_id:group,representative_id:rep,original_prompt:`exact\n${id}`,rights_json:{status:'unknown'},metadata:{},metadata_review_status:'needs_review',human_note:null,text_ready:true};}
function qresult(groups){return Response.json({result:{groups:groups.map(([group,id,rep,score])=>({id:group,hits:[{score,payload:{item_id:id,group_id:group,representative_id:rep,snapshot_id:snapshot,image_approved:true}}]}))}});}

test('input accepts only top1/3/5 and exactly one query source',()=>{
  assert.deepEqual(searchInput({query:'교육 포스터',top:3}),{query:'교육 포스터',top:3});
  for(const body of [{query:'a'},{query:'two',top:2},{query:'two',query_id:'q1'},{query:'two',model:'override'},{query:' two '},{query:'a\nb'}])assert.throws(()=>searchInput(body));
});
test('Neon endpoint allowlist and API regional hostname',()=>{
  assert.equal(neonEndpoint('postgres://u:p@ep-test.us-east-2.aws.neon.tech/db'),'https://api.us-east-2.aws.neon.tech/sql');
  for(const dsn of ['postgres://u:p@evil.test/db','postgres://u:p@neon.tech.evil.test/db','https://ep-test.aws.neon.tech/db'])assert.throws(()=>neonEndpoint(dsn));
});

test('provider fetch refuses redirects and never forwards credentials to Location',async()=>{
  for(const status of [301,302,303,307,308]) {
    let calls=0;
    await withFetch(async(url,init)=>{
      calls++;
      assert.equal(url,'https://api.us-east-2.aws.neon.tech/sql');
      assert.equal(init.redirect,'manual');
      return new Response(null,{status,headers:{Location:'https://untrusted.example.test/steal'}});
    },async()=>{
      await assert.rejects(fetchJson('https://api.us-east-2.aws.neon.tech/sql',{
        method:'POST',headers:{'Neon-Connection-String':'synthetic-only'},body:'{}',redirect:'follow',
      }),{code:'upstream_unavailable',status:503});
      assert.equal(calls,1);
    });
  }
});
test('Qdrant endpoint/config rejects unsafe sources',async()=>{
  const e=await env();assert.match(qdrantBase(e),/^https:/);
  for(const url of ['http://localhost:6333','https://cloud.qdrant.io.evil.test/','https://example.cloud.qdrant.io/api','https://u:p@example.cloud.qdrant.io'])assert.throws(()=>qdrantBase({...e,QDRANT_ENDPOINT:url}));
});
test('vector contract rejects dimension/NaN/zero',()=>{
  for(const v of [Array(511).fill(1),Array(512).fill(0),[NaN,...Array(511).fill(1)]])assert.throws(()=>finiteVector(v,512));
});
test('bounded JSON limits streams without content-length',async()=>{
  await assert.rejects(()=>readJsonBounded(new Response(JSON.stringify({x:'x'.repeat(100)})),32));
});
test('health has no private counts or calls',async()=>withFetch(()=>{throw Error('no call allowed');},async()=>{
  const r=await worker.fetch(new Request('https://owner.example/healthz'),{});assert.equal(r.status,200);assert.equal((await r.json()).version,'2.0.0');
}));
test('unauthenticated calls never reach rate limiter/provider/cache',async()=>withFetch(()=>{throw Error('no call allowed');},async()=>{
  const e=await env();e.OWNER_RATE_LIMITER.limit=()=>{throw Error('auth order');};
  const r=await worker.fetch(new Request('https://owner.example/api/private/v2/search',{method:'POST'}),e);assert.equal(r.status,401);
}));
test('token cannot enter admin route',async()=>withFetch(()=>{throw Error('no call allowed');},async()=>{
  const r=await worker.fetch(new Request('https://owner.example/api/admin/v2/status',{headers:{Authorization:`Bearer ${token}`}}),await env());assert.ok([403,503].includes(r.status));
}));
test('invalid input and rate limits call no providers',async()=>withFetch(()=>{throw Error('no call allowed');},async()=>{
  const e=await env();assert.equal((await worker.fetch(request({query:'abc',top:99}),e)).status,400);
  e.OWNER_RATE_LIMITER.limit=async()=>({success:false});assert.equal((await worker.fetch(request({query:'abc'}),e)).status,429);
}));
test('inactive snapshot cannot use stored query or provider',async()=>{
  let calls=0;await withFetch(async()=>{calls++;return dbResult([{snapshot_id:snapshot,manifest_sha256:manifest,state:'staged'}]);},async()=>{
    const r=await worker.fetch(request({query_id:'q1'}),await env());assert.equal(r.status,503);assert.equal(calls,1);
  });
});
test('stored query search uses no Voyage calls and preserves representative/original prompt',async()=>{
  const seen=[];
  await withFetch(async(url,init)=>{
    seen.push(url);const body=JSON.parse(init.body);
    if(url.includes('qdrant')){assert.equal(body.group_by,'group_id');assert.equal(body.limit,3);assert.equal(body.with_vector,false);return qresult([['g1','B','A',0.9],['g2','C','C',0.8]]);}
    if(body.query.includes('FROM image_archive_v2.snapshots'))return dbResult([{snapshot_id:snapshot,manifest_sha256:manifest,state:'ready'}]);
    if(body.query.includes('query_vectors'))return dbResult([{query_id:'q1',query_text:'교육 포스터',model:'voyage-4-lite',dimension:512,vector_json:vector}]);
    if(body.query.includes('FROM image_archive_v2.items'))return dbResult([item('A'),item('B'),item('C','g2','C')]);
    throw Error('unexpected SQL');
  },async()=>{
    const r=await worker.fetch(request({query_id:'q1',top:3}),await env());assert.equal(r.status,200);
    const body=await r.json();assert.equal(body.returned_groups,2);assert.equal(body.usage.provider_calls,0);
    assert.equal(body.results[0].representative.item_id,'A');assert.equal(body.results[0].matched_item_id,'B');
    assert.equal(body.results[0].representative.original_prompt,'exact\nA');assert.equal(body.results[0].representative.metadata_review_status,'needs_review');
    assert.equal(seen.filter(url=>url.includes('voyage')).length,0);assert.match(r.headers.get('cache-control'),/no-store/);
  });
});
test('Qdrant duplicate groups or snapshot mismatch are rejected',async()=>{
  await withFetch(()=>qresult([['g1','A','A',1],['g1','B','A',0.9]]),async()=>assert.rejects(async()=>searchGroups(await env(),vector,3)));
});
test('daily reservation exhausted prevents Voyage',async()=>{
  let calls=0;
  await withFetch((url)=>{calls++;assert.ok(url.includes('neon.tech'));return dbResult([]);},async()=>{
    await assert.rejects(async()=>queryVector({...await env(),LIVE_QUERY_EMBEDDING_ENABLED:'true',VOYAGE_API_KEY:'synthetic'}, {id:'owner'}, {query:'new query'}),{code:'daily_query_budget_exhausted'});assert.equal(calls,1);
  });
});
test('new query observed usage and warm cache with a single provider call',async()=>{
  let providers=0,db=0;
  await withFetch((url,init)=>{
    if(url.includes('voyage')){providers++;return Response.json({model:'voyage-4-lite',data:[{index:0,embedding:vector}],usage:{total_tokens:8}});}
    db++;const body=JSON.parse(init.body);assert.ok(body.params.every(p=>p===null||typeof p==='string'));
    return dbResult(body.query.includes('RETURNING request_id')?[{request_id:'r'}]:[]);
  },async()=>{
    const e={...await env(),LIVE_QUERY_EMBEDDING_ENABLED:'true',VOYAGE_API_KEY:'synthetic'};
    const first=await queryVector(e,{id:'owner'},{query:'new query'}),second=await queryVector(e,{id:'owner'},{query:'new query'});
    assert.equal(first.usage.actual_tokens,8);assert.equal(second.usage.provider_calls,0);assert.equal(providers,1);assert.equal(db,2);
  });
});
test('provider failure logs uncertainty but never retries the model',async()=>{
  let providers=0,uncertain=0;
  await withFetch((url,init)=>{
    if(url.includes('voyage')){providers++;throw Error('synthetic secret must not escape');}
    const body=JSON.parse(init.body);if(body.query.includes("state='uncertain'"))uncertain++;
    return dbResult(body.query.includes('RETURNING request_id')?[{request_id:'r'}]:[]);
  },async()=>{
    await assert.rejects(async()=>queryVector({...await env(),LIVE_QUERY_EMBEDDING_ENABLED:'true',VOYAGE_API_KEY:'synthetic'},{id:'owner'},{query:'new query'}),{code:'upstream_unavailable'});
    assert.equal(providers,1);assert.equal(uncertain,1);
  });
});

test('malformed truthy rate-limiter success never admits provider calls',async()=>{
  await withFetch(()=>{throw Error('no provider call allowed');},async()=>{
    for(const response of [{success:'false'},{success:1},{success:{}},{},null]){
      const e=await env();e.OWNER_RATE_LIMITER.limit=async()=>response;
      const result=await worker.fetch(request({query_id:'q1'}),e);
      assert.equal(result.status,429);
      assert.equal((await result.json()).error,'rate_limited');
    }
  });
});

test('malformed embedding responses record uncertainty with no observed receipt or warm cache',async()=>{
  const invalid=[
    {model:'wrong-model',data:[{index:0,embedding:vector}],usage:{total_tokens:8}},
    {model:'voyage-4-lite',data:[{index:1,embedding:vector}],usage:{total_tokens:8}},
    {model:'voyage-4-lite',data:[{index:0,embedding:Array(511).fill(1)}],usage:{total_tokens:8}},
    {model:'voyage-4-lite',data:[{index:0,embedding:Array(512).fill(0)}],usage:{total_tokens:8}},
    {model:'voyage-4-lite',data:[{index:0,embedding:vector}],usage:{total_tokens:'8'}},
  ];
  for(const payload of invalid){
    let providers=0,uncertain=0,observed=0,reservationId;
    await withFetch((url,init)=>{
      if(url.includes('voyage')){providers++;return Response.json(payload);}
      const body=JSON.parse(init.body);
      if(body.query.includes('RETURNING request_id')){
        reservationId=body.params[4];return dbResult([{request_id:reservationId}]);
      }
      if(body.query.includes("state='uncertain'")){
        uncertain++;assert.equal(body.params[0],reservationId);assert.match(body.query,/AND state='reserved'/);
      }
      if(body.query.includes("state='observed'"))observed++;
      return dbResult([]);
    },async()=>{
      const e={...await env(),LIVE_QUERY_EMBEDDING_ENABLED:'true',VOYAGE_API_KEY:'synthetic'};
      await assert.rejects(()=>queryVector(e,{id:'owner'},{query:'malformed-response query'}));
      assert.equal(providers,1);assert.equal(uncertain,1);assert.equal(observed,0);
      await assert.rejects(()=>queryVector({...e,LIVE_QUERY_EMBEDDING_ENABLED:'false'},{id:'owner'},
        {query:'malformed-response query'}),{code:'new_query_embedding_disabled'});
      assert.equal(providers,1);
    });
  }
});

test('token-bound breach reconciles usage and blocks the next reservation before a provider call',async()=>{
  let providers=0,observations=0,uncertain=0,blocked=false,reserved=0,accounted=0,receiptState='missing';
  await withFetch((url,init)=>{
    if(url.includes('voyage')){
      providers++;
      return Response.json({model:'voyage-4-lite',data:[{index:0,embedding:vector}],usage:{total_tokens:reserved+17}});
    }
    const body=JSON.parse(init.body);
    if(body.query.includes('RETURNING request_id')){
      assert.match(body.query,/api_model_guard WHERE model=\$1 AND blocked=false/);
      if(blocked)return dbResult([]);
      reserved=Number(body.params[1]);accounted+=reserved;receiptState='reserved';
      return dbResult([{request_id:body.params[4]}]);
    }
    if(body.query.includes('WITH observed AS')){
      observations++;
      // One submitted SQL statement must contain receipt observation, positive
      // actual-token reconciliation and the model latch, not three requests.
      assert.match(body.query,/UPDATE image_archive_v2\.api_query_receipts/);
      assert.match(body.query,/UPDATE image_archive_v2\.api_daily_budget/);
      assert.match(body.query,/GREATEST\(0,\$2::bigint-o\.reserved_tokens\)/);
      assert.match(body.query,/UPDATE image_archive_v2\.api_model_guard SET blocked=true/);
      accounted+=Math.max(0,Number(body.params[1])-reserved);blocked=true;receiptState='observed';
    }
    if(body.query.includes("state='uncertain'")){
      uncertain++;assert.match(body.query,/AND state='reserved'/);
      if(receiptState==='reserved')receiptState='uncertain';
    }
    return dbResult([]);
  },async()=>{
    const e={...await env(),LIVE_QUERY_EMBEDDING_ENABLED:'true',VOYAGE_API_KEY:'synthetic'};
    await assert.rejects(()=>queryVector(e,{id:'owner'},{query:'first bound breach'}),{code:'query_token_bound_exceeded'});
    assert.equal(providers,1);assert.equal(observations,1);assert.equal(accounted,reserved+17);
    assert.equal(receiptState,'observed');assert.equal(uncertain,1);
    await assert.rejects(()=>queryVector(e,{id:'owner'},{query:'second query after breach'}),{code:'daily_query_budget_exhausted'});
    assert.equal(providers,1);
  });
});

test('failed usage reconciliation leaves an uncertain receipt and no cached vector',async()=>{
  let providers=0,uncertain=0;
  await withFetch((url,init)=>{
    if(url.includes('voyage')){providers++;return Response.json({model:'voyage-4-lite',data:[{index:0,embedding:vector}],usage:{total_tokens:8}});}
    const body=JSON.parse(init.body);
    if(body.query.includes('WITH observed AS'))throw Error('synthetic database failure');
    if(body.query.includes("state='uncertain'"))uncertain++;
    return dbResult(body.query.includes('RETURNING request_id')?[{request_id:body.params[4]}]:[]);
  },async()=>{
    const e={...await env(),LIVE_QUERY_EMBEDDING_ENABLED:'true',VOYAGE_API_KEY:'synthetic'};
    await assert.rejects(()=>queryVector(e,{id:'owner'},{query:'usage transaction failed'}),{code:'upstream_unavailable'});
    assert.equal(providers,1);assert.equal(uncertain,1);
    await assert.rejects(()=>queryVector({...e,LIVE_QUERY_EMBEDDING_ENABLED:'false'},{id:'owner'},
      {query:'usage transaction failed'}),{code:'new_query_embedding_disabled'});
    assert.equal(providers,1);
  });
});
