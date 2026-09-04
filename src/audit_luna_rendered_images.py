"""Read-only supplementary evidence audit; never a semantic or release approval gate."""
import argparse
import base64
import binascii
import hashlib
import json
import os
import re
from pathlib import Path


def audit(root: Path, session_root: Path) -> dict:
    b=root / 'data/private-research/image-rag-admin/luna-analysis/2026-09-04-luna-full-library-v3'
    m=json.loads((b/'tasks.json').read_text('utf-8'))
    tasks={t['style_id']:t for t in m['tasks'] if t['analysis_mode']=='new_compact'}
    sessions={};receipt_count=0
    for p in sorted((b/'execution').glob('*.tokens.json')):
     d=json.loads(p.read_text('utf-8'));sid=d['session_id_reported'];receipt_count+=1
     s=sessions.setdefault(sid,{'latest':d,'turns':{}})
     if d['source_prefix_line_count']>s['latest']['source_prefix_line_count']:s['latest']=d
     for tid in d['turn_ids']:s['turns'].setdefault(tid,set()).update(d['assigned_styles'])
    covered=set();exact=set();image_calls=0;opaque=[];sources=[]
    for sid,s in sorted(sessions.items()):
     d=s['latest'];name=d['source_log_name']
     if type(d['source_prefix_bytes']) is not int or not 0 < d['source_prefix_bytes'] <= 1024**3 or type(d['source_prefix_line_count']) is not int or not 0 < d['source_prefix_line_count'] <= 1000000:raise ValueError('Bounded log prefix required')
     match=re.fullmatch(r'rollout-(\d{4})-(\d{2})-(\d{2})T\d{2}-\d{2}-\d{2}-'+re.escape(sid)+r'\.jsonl',name)
     if not match:raise ValueError('Unexpected exact log name')
     path=session_root.joinpath(*match.groups(),name)
     pending={};seen=set();ctx=None;h=hashlib.sha256();size=0
     with path.open('rb') as f:
      for n in range(1,d['source_prefix_line_count']+1):
       line=f.readline(16*1024*1024+1)
       if not line or len(line)>16*1024*1024:raise ValueError('Missing or oversized evidence line')
       h.update(line);size+=len(line)
       if size>d['source_prefix_bytes']:raise ValueError('Source prefix byte bound exceeded')
       r=json.loads(line);pl=r.get('payload',{})
       if r.get('type')=='session_meta' and pl.get('id')!=sid:raise ValueError('Session mismatch')
       if r.get('type')=='turn_context':ctx=pl.get('turn_id')
       if r.get('type')!='response_item':continue
       if pl.get('type')=='custom_tool_call' and 'tools.view_image' in pl.get('input',''):
        tid=pl.get('internal_chat_message_metadata_passthrough',{}).get('turn_id') or ctx
        if tid not in s['turns']:continue
        source=pl['input'];spaced={x.replace(' ','') for x in re.findall(r'"([0-9a-f ]{65,100})"\.replace\(/ /g,\x27\x27\)',source)}
        matches={style for style in s['turns'][tid] if tasks[style]['prepared_image_sha256'] in source or tasks[style]['prepared_image_sha256'] in spaced}
        pending[pl['call_id']]={'styles':matches,'hashes':{tasks[style]['prepared_image_sha256'] for style in matches},'line':n,'turn':tid}
       if pl.get('type')=='custom_tool_call_output' and pl.get('call_id') in pending and pl['call_id'] not in seen:
        call=pending[pl['call_id']]
        images=sum(1 for x in pl.get('output',[]) if isinstance(x,dict) and x.get('type')=='input_image')
        if not images:continue
        returned_hashes=set()
        for block in pl.get('output',[]):
         if not isinstance(block,dict) or block.get('type')!='input_image':continue
         url=block.get('image_url')
         if not isinstance(url,str) or not re.match(r'^data:image/[a-zA-Z0-9.+-]+;base64,',url):continue
         try:returned_hashes.add(hashlib.sha256(base64.b64decode(url.split(',',1)[1],validate=True)).hexdigest())
         except (ValueError,binascii.Error):continue
        exact.update(style for style in s['turns'][call['turn']] if tasks[style]['prepared_image_sha256'] in returned_hashes)
        seen.add(pl['call_id']);image_calls+=1
        if call['hashes'] and images>=len(call['hashes']):covered.update(call['styles'])
        else:opaque.append({'session_id':sid,'turn_id':call['turn'],'call_line':call['line'],'returned_images':images,'literal_asset_count':len(call['hashes'])})
     if size!=d['source_prefix_bytes'] or h.hexdigest()!=d['source_prefix_sha256']:raise ValueError('Source prefix changed')
     sources.append({'session_id':sid,'log_name':name,'prefix_bytes':size,'prefix_lines':d['source_prefix_line_count'],'prefix_sha256':h.hexdigest()})
    assigned=set().union(*(set().union(*s['turns'].values()) for s in sessions.values()))
    out={'schema_version':'luna-rendered-image-evidence-1','analysis_run_id':m['analysis_run_id'],'task_manifest_sha256':hashlib.sha256((b/'tasks.json').read_bytes()).hexdigest(),'evidence_level':'literal_or_constant_space_removal_asset_hash_in_view_image_call_and_returned_image_blocks','receipt_count':receipt_count,'assigned_style_count':len(assigned),'literal_rendered_style_count':len(covered),'literal_rendered_style_ids':sorted(covered),'not_literal_matched_style_ids':sorted(assigned-covered),'image_producing_call_count':image_calls,'opaque_calls':opaque,'sources':sources,'metadata_human_approved':False,'release_eligible':False,'limitations':['Exact constant hex literals with .replace(/ /g, empty_string) are resolved by removing ASCII spaces only; no code is evaluated.','This verifies returned image evidence in assigned completed turns, not model comprehension or semantic accuracy.','It does not prove the complete visual-draft-before-prompt event ordering.','Dynamic-path calls without literal asset hashes remain unverified here, not failed image reads.']}
    out.update(schema_version='luna-rendered-image-evidence-2', evidence_level='exact_returned_image_bytes_with_separate_literal_call_fallback',
               exact_returned_image_style_count=len(exact), exact_returned_image_style_ids=sorted(exact),
               not_exact_returned_image_style_ids=sorted(assigned-exact), literal_only_style_ids=sorted(covered-exact))
    out['limitations'].append('Exact byte matches can establish an image returned before a later call failed. Literal-only matches are weaker evidence and are reported separately.')
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-root", type=Path, default=Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".codex" / "sessions")
    args = parser.parse_args()
    print(json.dumps(audit(Path(__file__).resolve().parents[1], args.session_root), ensure_ascii=False))
