#!/usr/bin/env python3
"""Exercise the delivered native SVG, not a table containing architecture words."""
import argparse
import hashlib
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def run(html, output, executable=None, transport='file'):
    html, output = Path(html).resolve(), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    ir = json.loads(html.with_suffix('.json').read_text(encoding='utf-8'))
    nodes = {n['id'] for n in ir['components']}
    edges = {e['id']: e for e in ir['connections']}
    errors, network, measurements = [], [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable)
        page = browser.new_page(viewport={'width':1440,'height':900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda q: network.append(q.url) if q.url.startswith(('http:', 'https:')) else None)
        if transport == 'file':
            page.goto(html.as_uri(), wait_until='load')
        else:
            page.set_content(html.read_text(encoding='utf-8'), wait_until='load')
        svg = page.locator('.diagram-container svg').first
        assert svg.is_visible(), 'Native SVG architecture canvas is absent'
        actual_nodes = svg.locator('g[data-node-id]')
        assert set(actual_nodes.evaluate_all('(xs)=>xs.map(x=>x.dataset.nodeId)')) == nodes
        actual_edges = svg.locator('path[data-edge-id]')
        rows = actual_edges.evaluate_all('(xs)=>xs.map(x=>({id:x.dataset.edgeId,d:x.getAttribute("d"),marker:x.getAttribute("marker-end")}))')
        assert {x['id'] for x in rows} == set(edges), 'Diagram has missing or substituted relationships'
        assert all(x['d'] and x['marker'] for x in rows), 'A relationship is not an actual directional SVG path'
        assert all(x.is_visible() for x in actual_nodes.all()), 'Core nodes hidden by default'
        for width, height in [(1440,900),(1600,1000),(1920,1080),(2048,1320),(390,844)]:
            page.set_viewport_size({'width':width,'height':height}); page.wait_for_timeout(250)
            m = page.evaluate('({width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight})')
            assert m['scrollWidth'] <= width, m
            if width >= 1440: assert m['scrollHeight'] <= height, m
            measurements.append(m)
            page.screenshot(path=str(output/f'diagram-{width}.png'),full_page=True)
        page.set_viewport_size({'width':1440,'height':900})
        target = 'manager' if 'manager' in nodes else sorted(nodes)[0]
        svg.locator(f'g[data-node-id="{target}"]').click()
        assert page.locator('#focus-chip').is_visible()
        assert target in page.locator('#focus-id').inner_text()
        links = page.locator('#focus-evidence-links a')
        assert links.count() > 0 and ir['meta']['repository']['revision'] in links.first.get_attribute('href')
        page.locator('#btn-reach-downstream').click()
        assert page.locator('#btn-reach-downstream').get_attribute('aria-pressed') == 'true'
        page.locator('#btn-focus-clear').click()
        page.locator('#btn-node-finder').click()
        page.locator('#node-finder-input').fill(target)
        assert page.locator('#node-finder-results').is_visible()
        page.locator('#node-finder-close').click()
        before = page.locator('html').get_attribute('data-theme')
        page.locator('#btn-theme').click()
        assert page.locator('html').get_attribute('data-theme') != before
        page.screenshot(path=str(output/'diagram-dark.png'),full_page=True)
        assert not errors and not network, (errors, network)
        receipt = {'ok':True,'html_sha256':hashlib.sha256(html.read_bytes()).hexdigest(),
                   'browser':browser.version,'transport':transport,'file_navigation':transport=='file',
                   'nodes':len(nodes),'directional_svg_paths':len(edges),'viewports':measurements,
                   'browser_errors':errors,'http_requests':network}
        (output/'browser-review.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
        browser.close()
        return receipt


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--html',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--executable');p.add_argument('--transport',choices=['file','content'],default='file')
    a=p.parse_args(); print(json.dumps(run(a.html,a.output,a.executable,a.transport),indent=2))
