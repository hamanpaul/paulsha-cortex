"""Regression tests require a real native diagram, not a documentation lookalike."""
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import pytest
ROOT=Path(__file__).resolve().parents[1]
ARCH=ROOT/'docs/architecture'

class Page(HTMLParser):
    def __init__(self,text):
        super().__init__();self.nodes=[];self.edges={};self.generators=[];self.external=[];self.feed(text)
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='g' and 'data-node-id' in a:self.nodes.append(a['data-node-id'])
        if tag=='path' and 'data-edge-id' in a:self.edges[a['data-edge-id']]=a
        if tag=='meta' and a.get('name')=='generator':self.generators.append(a.get('content',''))
        if tag=='script' and a.get('src'):self.external.append(a['src'])

def load():
    return (json.loads((ARCH/'facts.json').read_text()),json.loads((ARCH/'architecture.json').read_text()),Page((ARCH/'architecture.html').read_text()))

def assert_graph(ir,p):
    assert any(s.startswith('archify ') for s in p.generators)
    assert set(p.nodes)=={x['id'] for x in ir['components']}
    assert len(p.nodes)==len(ir['components'])
    assert set(p.edges)=={x['id'] for x in ir['connections']}
    assert all(e.get('d') and e.get('marker-end') for e in p.edges.values())
    assert not p.external

def test_native_diagram_has_actual_svg_nodes_and_arrows():
    _,ir,p=load();assert_graph(ir,p)

def test_native_ir_preserves_contract_nodes_and_workflow():
    f,ir,p=load();ids={x['id'] for x in ir['components']}
    assert ids=={x['id'] for x in f['components']}|{'phase-'+s['id'] for s in f['workflow_review']['steps']}
    assert {'manager','monitor','persona-contracts','deck-compiler','workflow-registry'}<=ids
    assert ir['meta']['quality_profile']=='showcase' and 'renderer' not in ir['meta']
    for e in f['relations']:
        expected={k:e[k] for k in ('id','from','to','label','variant') if k in e}
        actual=next(x for x in ir['connections'] if x['id']==e['id'])
        assert expected=={k:actual[k] for k in expected}
    assert not any(e['from']=='workflow-registry' and e['to']=='headless-executors' for e in ir['connections'])
    assert any(e['id']=='recovery-candidate-repair' and e['from']=='phase-review' and e['to']=='phase-build' for e in ir['connections'])

def test_prose_page_cannot_pass_by_copying_labels():
    _,ir,_=load();fake=Page('<meta name="generator" content="archify fake">'+''.join('<p>'+x['label']+'</p>' for x in ir['components']))
    with pytest.raises(AssertionError):assert_graph(ir,fake)

def test_missing_persona_or_recovery_arrow_is_rejected():
    _,ir,p=load();p.nodes.remove('persona-contracts')
    with pytest.raises(AssertionError):assert_graph(ir,p)
    _,ir,p=load();del p.edges['recovery-candidate-repair']
    with pytest.raises(AssertionError):assert_graph(ir,p)

def test_source_hashes_refer_to_real_pinned_git_blobs():
    f,_,_=load();revision=f['repository']['revision'];r=f['workflow_review'];cache={}
    records=f['components']+f['relations']+f['boundaries']+r['roles']+r['steps']+r['recoveries']+r['state_models']
    for record in records:
        assert record['evidence']
        if record['basis']=='inferred':assert len({x['path'] for x in record['evidence']})>=2
        for anchor in record['evidence']:
            path=anchor['path'];assert not path.startswith('docs/architecture/') and '..' not in Path(path).parts
            if path not in cache:cache[path]=subprocess.check_output(['git','-C',str(ROOT),'show',revision+':'+path]).splitlines(keepends=True)
            a,b=anchor['line'],anchor['end_line'];assert 1<=a<=b<=len(cache[path])
            assert hashlib.sha256(b''.join(cache[path][a-1:b])).hexdigest()==anchor['excerpt_sha256']


def test_readme_link_and_unknowns_remain_explicit():
    f,_,_=load()
    assert '(docs/architecture/architecture.html)' in (ROOT/'README.md').read_text()
    assert isinstance(f['conflicts'],list) and isinstance(f['unknowns'],list)
    assert any(x['id']=='deployment-enforcement' for x in f['unknowns'])
