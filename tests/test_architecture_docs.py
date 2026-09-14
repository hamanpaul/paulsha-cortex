"""Regression contract for the source-pinned, checked-in HTML review artifact."""
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ARCH = ROOT / 'docs/architecture'


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__();self.scripts={};self.ids={};self.current=None;self.external=[];self.feed(text)
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        for key in ('data-node-id','data-relation-id','data-role-id','data-step-id','data-recovery-id','data-state-id'):
            if key in a:self.ids.setdefault(key,[]).append(a[key])
        if tag=='script':
            if a.get('src'):self.external.append(a['src'])
            self.current=a.get('id');self.scripts.setdefault(self.current,'')
    def handle_endtag(self,tag):
        if tag=='script':self.current=None
    def handle_data(self,text):
        if self.current:self.scripts[self.current]+=text


def load():
    f=json.loads((ARCH/'facts.json').read_text());ir=json.loads((ARCH/'architecture.json').read_text());p=Page((ARCH/'architecture.html').read_text());return f,ir,p


def test_html_embeds_exact_current_facts_and_ir():
    f,ir,p=load()
    assert json.loads(p.scripts['architecture-facts']) == f
    assert json.loads(p.scripts['architecture-ir']) == ir
    assert ir['meta']['workflow_review']==f['workflow_review']
    assert ir['meta']['renderer']=='workflow-review/v1'
    assert not p.external


def test_all_fact_records_have_one_canonical_html_element():
    f,ir,p=load();r=f['workflow_review']
    for attr,records in [('node-id',f['components']),('relation-id',f['relations']),('role-id',r['roles']),('step-id',r['steps']),('recovery-id',r['recoveries']),('state-id',r['state_models'])]:
        assert sorted(p.ids['data-'+attr])==sorted(x['id'] for x in records)
    assert {x['id'] for x in r['roles']}=={'manager','planner','builder','reviewer'}
    assert [x['id'] for x in r['steps']]==['claim','define','plan','build','verify','review','ship']
    assert r['controller']=='manager' and r['observer']=='monitor' and r['state_store']=='workflow-registry'
    assert not any(x['from']=='workflow-registry' and x['to']=='headless-executors' for x in f['relations'])


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
