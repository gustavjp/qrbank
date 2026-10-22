// Dependency-free browser-event harness for the Scan Mode camera controller.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  const template = fs.readFileSync(
    path.join(__dirname, '..', 'app', 'templates', 'scan_mode.html'), 'utf8',
  );
  const script = template.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(script, 'Scan Mode controller script exists');
  new Function(script[1]);

  const handlers = new Map();
  const elements = {};
  const classNames = new Set();
  let decodeCallback;
  let startCount = 0;
  let stopCount = 0;
  let submitted = 0;
  let clock = 10000;
  const form = {
    action: 'https://example.test/classrooms/test/scan/execute',
    requestSubmit() { submitted += 1; },
  };
  elements['scan-form'] = form;
  elements['scanner-input'] = {value: '', disabled: false, focus() {}, blur() {}};
  elements['scanner-panel'] = {dataset: {soundEnabled: '0'}};
  elements['scanner-status'] = {textContent: ''};
  elements['scanner-toggle'] = {textContent: '', setAttribute() {}};
  elements['camera-start'] = {textContent: '', disabled: false};
  elements['camera-status'] = {textContent: ''};
  elements['camera-panel'] = {hidden: true};
  elements['camera-video'] = {srcObject: null};
  const document = {
    body: {classList: {
      add(name) { classNames.add(name); },
      remove(name) { classNames.delete(name); },
    }},
    getElementById(id) { return elements[id] || null; },
    addEventListener(name, handler) {
      if (!handlers.has(name)) handlers.set(name, []);
      handlers.get(name).push(handler);
    },
    querySelector() { return null; },
  };
  const window = {
    isSecureContext: true,
    ZXingBrowser: {BrowserQRCodeReader: class {
      async decodeFromConstraints(constraints, video, callback) {
        assert.equal(constraints.video.facingMode.ideal, 'environment');
        assert.equal(video, elements['camera-video']);
        startCount += 1;
        decodeCallback = callback;
        return {stop() { stopCount += 1; }};
      }
    }},
    addEventListener(name, handler) {
      if (!handlers.has(name)) handlers.set(name, []);
      handlers.get(name).push(handler);
    },
  };
  const FakeDate = class extends Date { static now() { return clock; } };
  vm.runInNewContext(script[1], {
    document, window, Date: FakeDate, navigator: {mediaDevices: {getUserMedia() {}}},
    setInterval() {}, setTimeout, FormData: class {}, DOMParser: class {},
  });
  function click(id) {
    for (const fn of handlers.get('click') || []) fn({target: {id}});
  }
  function frame(value) {
    decodeCallback(value === null ? null : {getText() { return value; }});
  }

  assert.equal(startCount, 0, 'camera does not start without a click');
  click('camera-start');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(startCount, 1);
  assert.equal(elements['camera-panel'].hidden, false);
  assert.ok(classNames.has('camera-active'));

  frame('student:01');
  assert.equal(elements['scanner-input'].value, 'STUDENT:01');
  assert.equal(submitted, 1);
  frame('student:01');
  assert.equal(submitted, 1, 'holding a QR must not resubmit');
  frame('https://unrelated.test');
  assert.equal(submitted, 1, 'unrelated QR must not reach the server');
  frame('QUICK:QA1');
  assert.equal(submitted, 2, 'different QR can scan immediately');

  frame(null);
  clock += 1300;
  frame(null);
  frame('QUICK:QA1');
  assert.equal(submitted, 3, 'same QR rearms after leaving the frame');

  click('scanner-toggle');
  frame('ACTION:A01');
  assert.equal(submitted, 3, 'paused scanner cannot submit');
  click('scanner-toggle');
  form.action = 'https://example.test/classrooms/test/scan/confirm';
  frame('ACTION:A01');
  assert.equal(submitted, 3, 'confirmation cannot be bypassed by camera');
  form.action = 'https://example.test/classrooms/test/scan/execute';
  frame('ACTION:A01');
  assert.equal(submitted, 4);

  click('camera-stop');
  assert.equal(stopCount, 1);
  assert.equal(elements['camera-panel'].hidden, true);
  assert.equal(classNames.has('camera-active'), false);
  frame('STUDENT:02');
  assert.equal(submitted, 4, 'stopped camera cannot submit');
  window.isSecureContext = false;
  click('camera-start');
  assert.equal(startCount, 1, 'insecure pages must not start camera');
  assert.match(elements['scanner-status'].textContent, /HTTPS/);
  window.isSecureContext = true;
  click('camera-start');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(startCount, 2);
  document.hidden = true;
  for (const fn of handlers.get('visibilitychange') || []) fn();
  assert.equal(stopCount, 2, 'backgrounding must stop video tracks');
  process.stdout.write('Camera controller handoff/duplicate/stop checks passed\n');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
