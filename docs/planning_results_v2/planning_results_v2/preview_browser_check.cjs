#!/usr/bin/env node
/* Optional preview verification. Install playwright and its Chromium browser,
 * then run: node preview_browser_check.cjs
 * This is a development check; planning_preview.html itself has no dependencies.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const crypto = require('crypto');
const {pathToFileURL} = require('url');
const {chromium} = require('playwright');

async function main() {
  const root = __dirname;
  const evidence = path.join(root, 'preview_evidence');
  fs.mkdirSync(evidence, {recursive: true});
  const plan = JSON.parse(fs.readFileSync(path.join(root, 'planning_result.json'), 'utf8'));
  const report = {
    scope: 'Independent offline file:// browser rendering and interaction only; geometric validity is checked separately by validate_plan.py.',
    input_file: 'planning_preview.html',
    input_json_sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(root, 'planning_result.json'))).digest('hex'),
    preview_html_sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(root, 'planning_preview.html'))).digest('hex'),
    browser: null,
    external_network_requests: [],
    javascript_errors: [],
    checks: [],
    scenes: [],
    screenshots: [],
  };
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage']});
  report.browser = await browser.version();
  try {
    const context = await browser.newContext({viewport: {width: 1700, height: 1260}, deviceScaleFactor: 1, offline: true});
    const page = await context.newPage();
    page.on('request', request => {
      if (!/^(file:|data:|blob:)/.test(request.url())) report.external_network_requests.push(request.url());
    });
    page.on('pageerror', error => report.javascript_errors.push(String(error)));
    await page.goto(pathToFileURL(path.join(root, 'planning_preview.html')).href);
    await page.waitForFunction(() => window.previewReady === true);
    let diag = await page.evaluate(() => window.previewDiagnostics());
    assert.equal(diag.sceneCount, 3);
    assert.equal(diag.zScale, 1);
    assert.equal(diag.coordinateFrame, 'LOCAL_NORTH_WEST');
    assert.equal(diag.returnCatalogEmbedded, true);
    assert.equal(diag.returnCatalogPlanId, plan.plan_id);
    report.checks.push({name: 'default three scenes, six UAVs and true XY:Z = 1:1', passed: true});

    for (let i = 0; i < plan.scenes.length; i++) {
      const scene = plan.scenes[i];
      await page.selectOption('#sceneSelect', String(i));
      await page.click('#view3d');
      diag = await page.evaluate(() => window.previewDiagnostics());
      const expectedPoints = scene.uavs.reduce((sum, u) => sum + u.layers.reduce((s, l) => s + l.waypoint_ids.length, 0), 0);
      assert.equal(diag.pointCount, expectedPoints);
      assert.equal(diag.scene, scene.scene_id);
      for (const layers of diag.radii) for (const layer of layers) assert.equal(layer.radius, layer.layer === 'upper' ? 50 : 30);
      assert.deepEqual(diag.returnClimbEvents, new Array(6).fill(scene.scene_id === 'final_scene_1' ? 0 : 1));
      const screenshot3d = `preview_evidence/${scene.scene_id}_3d.png`;
      await page.screenshot({path: path.join(root, screenshot3d)});
      report.screenshots.push(screenshot3d);
      await page.click('#viewTop');
      await page.selectOption('#diskPlane', 'ground');
      const screenshotTop = `preview_evidence/${scene.scene_id}_top.png`;
      await page.screenshot({path: path.join(root, screenshotTop)});
      report.screenshots.push(screenshotTop);
      await page.selectOption('#diskPlane', 'altitude');
      await page.click('#view3d');

      // Layer filters preserve all UAVs while displaying only the chosen layer.
      await page.uncheck('#upper');
      const lowerCount = scene.uavs.reduce((s, u) => s + u.layers.filter(l => l.layer_id === 'lower').reduce((a, l) => a + l.waypoint_ids.length, 0), 0);
      assert.equal((await page.evaluate(() => window.previewDiagnostics())).pointCount, lowerCount);
      await page.check('#upper');
      await page.uncheck('#lower');
      assert.equal((await page.evaluate(() => window.previewDiagnostics())).pointCount, expectedPoints - lowerCount);
      await page.check('#lower');

      // Verify individual aircraft filter and complete polygon components/holes.
      await page.click('#noUavs');
      await page.check('input[data-uav="1"]');
      assert.equal((await page.evaluate(() => window.previewDiagnostics())).pointCount, scene.uavs.find(u => u.uav_id === 1).layers.reduce((s, l) => s + l.waypoint_ids.length, 0));
      await page.click('#allUavs');
      diag = await page.evaluate(() => window.previewDiagnostics());
      const componentCounts = scene.uavs.map(u => u.region_geometry_wgs84.type === 'Polygon' ? 1 : u.region_geometry_wgs84.coordinates.length);
      const holeCounts = scene.uavs.map(u => (u.region_geometry_wgs84.type === 'Polygon' ? [u.region_geometry_wgs84.coordinates] : u.region_geometry_wgs84.coordinates).reduce((s, p) => s + p.length - 1, 0));
      assert.deepEqual(diag.partitionComponents, componentCounts);
      assert.deepEqual(diag.partitionHoles, holeCounts);
      const polygons = g => !g ? [] : g.type === 'Polygon' ? [g.coordinates] : g.coordinates;
      const countHoles = g => polygons(g).reduce((s, p) => s + p.length - 1, 0);
      assert.equal(diag.effectiveComponents, polygons(scene.effective_area_geometry_wgs84).length);
      assert.equal(diag.effectiveHoles, countHoles(scene.effective_area_geometry_wgs84));
      assert.deepEqual(diag.exclusionComponents, scene.exclusions.map(e => polygons(e.geometry_wgs84).length));
      assert.deepEqual(diag.exclusionHoles, scene.exclusions.map(e => countHoles(e.geometry_wgs84)));

      await page.evaluate(t => {const e = document.getElementById('time');e.value = t;e.dispatchEvent(new Event('input', {bubbles: true}));}, scene.scene_metrics.maximum_completion_time_s / 2);
      assert((await page.evaluate(() => window.previewDiagnostics())).time > 0);
      const timeBefore = (await page.evaluate(() => window.previewDiagnostics())).time;
      await page.click('#play');
      await page.waitForTimeout(300);
      await page.click('#play');
      assert((await page.evaluate(() => window.previewDiagnostics())).time > timeBefore);
      await page.click('#resetTime');
      await page.check('#uncovered');
      await page.uncheck('#uncovered');
      await page.selectOption('#zScale', '2');
      assert.equal((await page.evaluate(() => window.previewDiagnostics())).zScale, 2);
      await page.selectOption('#zScale', '1');
      if (scene.scene_id !== 'final_scene_1') {
        await page.click('#noUavs');
        await page.check('input[data-uav="1"]');
        await page.click('#jumpReturn');
        assert((await page.locator('#returnEvent').textContent()).includes('返航回升'));
        const screenshotReturn = `preview_evidence/${scene.scene_id}_return_climb.png`;
        await page.screenshot({path: path.join(root, screenshotReturn)});
        report.screenshots.push(screenshotReturn);
        await page.click('#allUavs');
        await page.click('#resetTime');
      }
      report.scenes.push({scene_id: scene.scene_id, independent_points_drawn: expectedPoints, partition_components: componentCounts, partition_holes: holeCounts, effective_components: diag.effectiveComponents, effective_holes: diag.effectiveHoles, exclusion_components: diag.exclusionComponents, exclusion_holes: diag.exclusionHoles, scene_switch: true, layer_filters: true, uav_filters: true, playback: true, uncovered_toggle: true, top_and_3d_views: true, z_scale: true, revised_upper50_lower30: true, correct_return_climb_events: true});
    }

    // Exercise pointer rotation, wheel zoom and a local JSON import.
    const rect = await page.locator('#view').boundingBox();
    await page.mouse.move(rect.x + rect.width * 0.5, rect.y + rect.height * 0.5);
    await page.mouse.down();
    await page.mouse.move(rect.x + rect.width * 0.6, rect.y + rect.height * 0.55, {steps: 5});
    await page.mouse.up();
    await page.mouse.wheel(0, -150);
    await page.click('#fitView');
    await page.setInputFiles('#importFile', path.join(root, 'planning_result.json'));
    await page.waitForFunction(() => window.previewDiagnostics().scene === 'final_scene_1');
    report.checks.push({name: 'pointer rotation, wheel zoom, fit and local JSON import', passed: true});
    assert.equal(report.javascript_errors.length, 0);
    assert.equal(report.external_network_requests.length, 0);
    report.checks.push({name: 'no external requests in an explicitly offline browser context', passed: true});
    report.checks.push({name: 'no JavaScript runtime errors', passed: true});
    report.passed = true;
  } catch (error) {
    report.passed = false;
    report.failure = String(error.stack || error);
    process.exitCode = 1;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(root, 'preview_validation_report.json'), JSON.stringify(report, null, 2) + '\n');
  }
  console.log(JSON.stringify(report, null, 2));
}
main().catch(error => {console.error(error);process.exitCode = 1;});
