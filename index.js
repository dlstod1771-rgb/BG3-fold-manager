const fs = require('fs');
const path = require('path');
const { actions, selectors } = require('vortex-api');

const base = path.join(process.env.LOCALAPPDATA, 'BG3ModBridge');
const commandPath = path.join(base, 'vortex-command.json');
const responsePath = path.join(base, 'vortex-response.json');
let lastId = '';
let busy = false;

function writeResponse(data) {
  fs.mkdirSync(base, { recursive: true });
  fs.writeFileSync(responsePath, JSON.stringify(data, null, 2));
}

async function poll(api) {
  if (busy || !fs.existsSync(commandPath)) return;
  let command;
  try {
    command = JSON.parse(fs.readFileSync(commandPath, 'utf8'));
  } catch (_) {
    return;
  }
  if (!command.id || command.id === lastId) return;
  lastId = command.id;
  busy = true;
  try {
    const state = api.getState();
    const profileId = selectors.lastActiveProfileForGame(state, command.gameId || 'baldursgate3');
    if (!profileId) throw new Error('BG3 active profile not found');
    await actions.setModsEnabled(api, profileId, [command.modId], !!command.enabled, { allowAutoDeploy: true });
    writeResponse({ id: command.id, ok: true, profileId, modId: command.modId, enabled: !!command.enabled });
  } catch (err) {
    writeResponse({ id: command.id, ok: false, error: String(err && err.message || err) });
    api.sendNotification({ type: 'error', title: 'BG3 Mod Bridge', message: String(err && err.message || err), allowReport: false });
  } finally {
    busy = false;
  }
}

function init(context) {
  context.once(() => {
    setInterval(() => poll(context.api), 1000);
    poll(context.api);
  });
  return true;
}

exports.default = init;
