#!/usr/bin/env python3
"""Verify the actual workflow-review HTML in Chromium; never dispatch real work.

Default navigation is file://. --transport content is an explicitly recorded
sandbox option; it is not reported as a successful file navigation. No fallback
is automatic. Screenshots are inspection evidence, not the user deliverable.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys


def run(html_path: Path, output: Path, executable: str | None = None,
        transport: str = 'file') -> dict:
    from playwright.sync_api import sync_playwright
    raw = html_path.read_bytes()
    output.mkdir(parents=True, exist_ok=True)
    receipt = {'schema': 'workflow-html-browser-review/v1', 'html_sha256': hashlib.sha256(raw).hexdigest(), 'transport': transport, 'file_navigation': transport == 'file', 'checks': [], 'runtime_e2e': 'not-run', 'human_acceptance': 'pending'}
    with sync_playwright() as p:
        opts = {'headless': True}
        if executable:
            opts['executable_path'] = executable
        browser = p.chromium.launch(**opts)
        receipt['browser_version'] = browser.version
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
        errors, requests = [], []
        page.on('pageerror', lambda err: errors.append(str(err)))
        page.on('request', lambda req: requests.append(req.url))
        if transport == 'file':
            page.goto(html_path.resolve().as_uri(), wait_until='load')
        elif transport == 'content':
            page.set_content(raw.decode('utf-8'), wait_until='load')
        else:
            raise ValueError('unsupported transport')
        facts = json.loads(page.locator('#architecture-facts').text_content())
        ir = json.loads(page.locator('#architecture-ir').text_content())
        canon = lambda x: json.dumps(x, ensure_ascii=False,sort_keys=True,separators=(',', ':')).encode()
        for name, value in [('facts',facts),('ir',ir)]:
            assert page.locator('#'+name+'-hash').inner_text() == hashlib.sha256(canon(value)).hexdigest(), name+' semantic hash mismatch'
        r = facts['workflow_review']
        checks = receipt['checks']
        for key, attr, records in [('components','node-id',facts['components']),('relations','relation-id',facts['relations']),('roles','role-id',r['roles']),('steps','step-id',r['steps']),('recoveries','recovery-id',r['recoveries']),('states','state-id',r['state_models'])]:
            ids = page.locator('[data-'+attr+']').evaluate_all('(nodes,attr)=>nodes.map(n=>n.getAttribute(attr))','data-'+attr)
            assert sorted(ids) == sorted(x['id'] for x in records), key+' coverage or duplication'
            for identity in ids:
                node = page.locator('[data-'+attr+'="'+identity+'"]')
                assert node.is_visible(), key+' is hidden: '+identity
            checks.append(key+'-visible-exactly-once')
        for s in r['steps']:
            row = page.locator('#step-'+s['id'])
            assert row.locator('.cell').count() == 4
            assert row.locator('.failure').inner_text().strip()
            assert s['persona'] in row.locator('.owner').inner_text()
            for field in ('cards','inputs','outputs','gates','failure'):
                for text in s[field]:
                    assert text in row.inner_text(), s['id']+' missing '+field
        checks.append('phase-actor-input-output-gate-failure-visible')
        # Test the success path, without hiding any phase or sending network requests.
        page.locator('#reset').click()
        for index, s in enumerate(r['steps']):
            page.locator('#next').click()
            assert 'selected' in page.locator('#step-'+s['id']).get_attribute('class')
            assert s['label'] in page.locator('#trace-status').inner_text()
            assert page.locator('[data-step-id]:visible').count() == len(r['steps'])
        assert page.locator('#next').is_disabled()
        page.locator('#previous').click()
        assert r['steps'][-2]['label'] in page.locator('#trace-status').inner_text()
        checks.append('next-previous-success-path')
        for recovery in r['recoveries']:
            for step in recovery['resume_steps']:
                page.locator('[data-recovery-id="'+recovery['id']+'"] [data-step="'+step+'"]').click()
                assert 'selected' in page.locator('#step-'+step).get_attribute('class')
        checks.append('recovery-links-select-correct-stage')
        for identity in [r['controller'],r['observer'],r['state_store'],r['roles'][0]['component']]:
            page.locator('[data-component="'+identity+'"]').first.click()
            assert 'selected' in page.locator('#component-'+identity).get_attribute('class')
        checks.append('component-focus-without-topology-change')
        source = page.locator('.sources').first
        source.locator('summary').click()
        assert source.locator('a').first.is_visible()
        assert '/blob/'+facts['repository']['revision']+'/' in source.locator('a').first.get_attribute('href')
        source.locator('summary').click()
        checks.append('expand-pinned-evidence')
        before = page.locator('html').get_attribute('data-theme')
        page.locator('#theme').click()
        assert page.locator('html').get_attribute('data-theme') != before
        page.locator('#theme').click()
        checks.append('theme-toggle')
        page.locator('#reset').click()
        viewports = []
        for width, height in [(1920,1080),(1440,1000),(1024,900),(390,844)]:
            page.set_viewport_size({'width':width,'height':height})
            page.evaluate('window.scrollTo(0,0)')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), 'horizontal overflow '+str(width)
            assert page.locator('[data-step-id]:visible').count() == len(r['steps'])
            page.screenshot(path=str(output/f'html-top-{width}.png'))
            page.locator('#workflow').scroll_into_view_if_needed()
            page.screenshot(path=str(output/f'html-workflow-{width}.png'))
            viewports.append({'width':width,'height':height,'horizontal_overflow':False})
        checks.append('four-viewports-no-horizontal-overflow')
        # Opening source links is deliberately not tested: no external side effect.
        assert not errors, errors
        assert not [x for x in requests if x.startswith(('http:', 'https:'))], 'unexpected network dependency'
        checks.extend(['no-browser-errors','no-network-runtime-dependencies'])
        receipt.update(ok=True,viewports=viewports,console_errors=errors,request_count=len(requests),step_count=len(r['steps']),component_count=len(facts['components']))
        browser.close()
    (output/'browser-review.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    return receipt


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--html',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--executable')
    parser.add_argument('--transport',choices=['file','content'],default='file')
    args=parser.parse_args()
    try:
        result=run(args.html,args.output,args.executable,args.transport)
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc),'transport':args.transport},ensure_ascii=False))
        return 1
    print(json.dumps(result,ensure_ascii=False))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
