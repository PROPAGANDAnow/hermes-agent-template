/* Async UI regressions without a browser or live OAuth account. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
const source = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
function fixture(fetch) {
  const context = {window: {}, clearTimeout() {}, setTimeout() {}, confirm: () => true, fetch};
  vm.runInNewContext(source, context);
  const app = context.app();
  app.notifications = [];
  app.notify = (...args) => app.notifications.push(args);
  app.scheduleCodexPoll = () => { app.pollScheduled = true; };
  return app;
}

test('failed cancellation keeps the pending flow and never announces success', async () => {
  const app = fixture();
  app.codex.status = 'waiting';
  app.codexRequest = async () => { throw Error('connection lost'); };
  await app.cancelCodex();
  assert.equal(app.codex.status, 'waiting');
  assert.equal(app.codex.busy, false);
  assert.equal(app.pollScheduled, true);
  assert.equal(app.notifications.length, 1);
  assert.equal(app.notifications[0][1], false);
});

test('a stale poll response cannot revive a cancelled flow', async () => {
  const app = fixture();
  let resolve;
  app.codexRequest = () => new Promise(done => { resolve = done; });
  const pending = app.loadCodexStatus();
  ++app.codex.sequence;
  resolve({status: 'waiting', user_code: 'STALE'});
  await pending;
  assert.equal(app.codex.status, 'not_connected');
  assert.equal(app.codex.user_code, undefined);
});

test('reloading a pending login restores the provider without reusing another model', async () => {
  const app = fixture();
  app.vars.LLM_MODEL = 'another-providers-model';
  app.codexRequest = async () => ({status: 'waiting', user_code: 'ABC-123'});
  await app.loadCodexStatus();
  assert.equal(app.selectedProvider, 'ChatGPT (OAuth)');
  assert.equal(app.vars.LLM_MODEL, '');
  app.selectedProvider = 'MiniMax';
  await app.loadCodexStatus();
  assert.equal(app.selectedProvider, 'MiniMax');
});

test('transient polling failures retain the code and keep checking', async () => {
  const app = fixture();
  app.codex.status = 'waiting';
  app.codex.user_code = 'ABC-123';
  app.codexRequest = async () => { throw Error('temporary failure'); };
  await app.loadCodexStatus();
  assert.equal(app.codex.status, 'waiting');
  assert.equal(app.codex.user_code, 'ABC-123');
  assert.equal(app.pollScheduled, true);
});

test('failed disconnect does not hide the existing connection', async () => {
  const app = fixture();
  app.codex.connected = true;
  app.codexRequest = async () => { throw Error('stop not confirmed'); };
  await app.disconnectCodex();
  assert.equal(app.codex.connected, true);
  assert.equal(app.codex.busy, false);
  assert.equal(app.notifications[0][1], false);
});

test('save refuses an unauthorized or unlisted ChatGPT model', async () => {
  const app = fixture();
  app.selectedProvider = 'ChatGPT (OAuth)';
  app.vars.LLM_MODEL = 'unlisted';
  await app.saveConfig();
  assert.equal(app.saving, false);
  assert.equal(app.notifications[0][1], false);
});

test('confirm model persists only the model and keeps other drafts without starting', async () => {
  let body;
  const app = fixture(async (_, options) => {
    body = JSON.parse(options.body);
    return {ok: true, json: async () => ({ok: true, config_revision: 'new', config_complete: true})};
  });
  app.configRevision = 'old';
  app.vars = {LLM_MODEL: 'chosen', EMAIL_ADDRESS: 'unsaved-draft@example.invalid'};
  app.codex.connected = true;
  app.codex.models = ['chosen'];
  await app.confirmCodexModel();
  assert.deepEqual(body.vars, {LLM_MODEL: 'chosen'});
  assert.equal(body._restart, false);
  assert.equal(body._active_oauth_provider, 'openai-codex');
  assert.equal(app.codex.confirmedModel, 'chosen');
  assert.equal(app.configRevision, 'new');
  assert.equal(app.vars.EMAIL_ADDRESS, 'unsaved-draft@example.invalid');
});

test('failed confirmation cannot announce a model as saved', async () => {
  const app = fixture(async () => ({ok: false, json: async () => ({error: 'Settings changed; reload.'})}));
  app.configRevision = 'old';
  app.vars.LLM_MODEL = 'chosen';
  app.codex.connected = true;
  app.codex.models = ['chosen'];
  await app.confirmCodexModel();
  assert.equal(app.codex.confirmedModel, '');
  assert.equal(app.configRevision, 'old');
  assert.equal(app.notifications[0][1], false);
});

test('the configured-provider summary shows the saved model rather than a draft', () => {
  const app = fixture();
  app.codex.connected = true;
  app.codex.confirmedModel = 'saved';
  app.vars.LLM_MODEL = 'unconfirmed-draft';
  assert.equal(app.configuredProviders.find(p => p.providerKey === 'ChatGPT (OAuth)').model, 'saved');
});
