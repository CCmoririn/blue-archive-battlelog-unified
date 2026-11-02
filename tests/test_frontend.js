// Run with node tests/test_frontend.js. Browser state and fetch are local fakes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const cookies = new Map();
let alerts = [];
let captured;
let result = {success: false, message: 'simulated failure'};
function button(type, season = 's9') {
    const classes = new Set();
    return {
        dataset: {type, season, rowId: '12'}, disabled: false, handlers: [],
        classList: {toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); }},
        addEventListener(event, handler) { this.handlers.push(handler); }
    };
}
const good = button('good'), bad = button('bad');
function row(order, goodCount, badCount, rowId = '12') {
    const counts = {good: {textContent: goodCount}, bad: {textContent: badCount}};
    return {dataset: {order: String(order), good: String(goodCount), bad: String(badCount), rowId},
        querySelector(selector) { return counts[selector.includes('good') ? 'good' : 'bad']; }, counts};
}
const first = row(0, 2, 3), second = row(1, 8, 1, '13');
let rows = [first, second];
const container = {
    querySelectorAll() { return rows.slice(); },
    appendChild(value) { rows = rows.filter(other => other !== value); rows.push(value); }
};
const document = {
    querySelectorAll(selector) { return selector === '.vote-btn' ? [good, bad] : rows; },
    getElementById(id) { return id === 'searchResults' ? container : null; }
};
Object.defineProperty(document, 'cookie', {
    get() { return [...cookies].map(([key, value]) => `${key}=${value}`).join('; '); },
    set(value) {
        const [pair] = value.split(';');
        const [key, content] = pair.split('=');
        if (content) cookies.set(key, content); else cookies.delete(key);
    }
});
const sandbox = {document, window: {addEventListener() {}}, alert: message => alerts.push(message),
    fetch: async (url, options) => { captured = JSON.parse(options.body); return {json: async () => result}; }};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../Blue_Archive_Battlelog_collect_public/static/db.js'), 'utf8'), sandbox);
const html = fs.readFileSync(path.join(__dirname, '../Blue_Archive_Battlelog_collect_public/templates/db.html'), 'utf8');
vm.runInContext(html.match(/function escapeHtml\(value\) \{[\s\S]*?\n\}/)[0], sandbox);

(async () => {
    sandbox.setupVoteHandlers();
    sandbox.setupVoteHandlers();
    assert.equal(good.handlers.length, 1, 'handlers must not be registered twice');
    await good.handlers[0]();
    assert.equal(captured.season, 's9');
    assert.equal(first.counts.good.textContent, 2, 'failed vote must not change visible counts');
    assert.equal(cookies.size, 0, 'failed vote must not change cookie state');
    assert.equal(good.disabled, false);
    assert.equal(alerts.length, 1);

    result = {success: true, good_count: 3, bad_count: 3};
    await good.handlers[0]();
    assert.equal(cookies.get('voted_v2_s9_good_12'), '1');
    assert.equal(first.counts.good.textContent, 3);
    assert.equal(first.dataset.good, '3');
    assert.notEqual(sandbox.voteCookie(good), sandbox.voteCookie(button('good', 's11')));
    result = {success: true, good_count: 2, bad_count: 3};
    await good.handlers[0]();
    assert.equal(captured.current_status, true);
    assert.equal(cookies.size, 0);

    sandbox.sortResults('good');
    assert.equal(rows[0], second);
    sandbox.sortResults('new');
    assert.equal(rows[0], first, 'newest order must be restored after a different sort');
    assert.equal(sandbox.escapeHtml('<img src=x onerror="x">'), '&lt;img src=x onerror=&quot;x&quot;&gt;');
    console.log('Frontend regressions passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
