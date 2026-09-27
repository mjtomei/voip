// Bundled into the hosted Functions; local tests load it as an ordinary module.
// Never log access configuration or submitted keypad digits.
function voiceAccess(context, number, requestedSession, outgoing = false) {
  try {
    let config;
    if (context.VOICE_ACCESS_ASSET) {
      config = JSON.parse(Runtime.getAssets()[context.VOICE_ACCESS_ASSET].open());
    } else if (context.VOICE_ACCESS_JSON !== undefined) {
      config = JSON.parse(context.VOICE_ACCESS_JSON); // Local integration tests.
    } else {
      const allowed = (outgoing ? context.OUTBOUND_ALLOWED_RECIPIENTS : context.ALLOWED_CALLERS) || '';
      if (!allowed.split(',').map(x => x.trim()).includes(number) ||
          !/^\d{6,12}$/.test(context.CALL_PIN || '') ||
          !/^[A-Za-z0-9_.+-]+$/.test(context.SIP_USERNAME || '') ||
          (requestedSession && requestedSession !== 'default')) return null;
      return { id: 'default', pin: context.CALL_PIN, session: 'default', username: context.SIP_USERNAME };
    }
    if (!config || !Array.isArray(config.recipients) || !config.sessions) return null;
    const matches = config.recipients.filter(r => r.number === number);
    if (matches.length !== 1) return null;
    const recipient = matches[0];
    const session = requestedSession || recipient.incoming_session;
    const username = config.sessions[session];
    if (!/^[a-z][a-z0-9_-]{0,31}$/.test(recipient.id || '') ||
        !/^[a-z][a-z0-9_-]{0,31}$/.test(session || '') ||
        !Array.isArray(recipient.sessions) || !recipient.sessions.includes(session) ||
        !(voiceAccess.validAuth(recipient.pin_auth) || /^\d{6,12}$/.test(recipient.pin || '')) ||
        !/^[A-Za-z0-9_.+-]+$/.test(username || '')) return null;
    return { id: recipient.id, pin: recipient.pin, pin_auth: recipient.pin_auth, session, username };
  } catch (_) { return null; }
}
exports.voiceAccess = voiceAccess;

voiceAccess.directoryPolicy = function (context, access) {
  try {
    const config = context.VOICE_ACCESS_ASSET ? JSON.parse(Runtime.getAssets()[context.VOICE_ACCESS_ASSET].open()) :
      JSON.parse(context.VOICE_ACCESS_JSON || '{}');
    const policy = config.directories;
    return access && policy && /^[0-9a-f]{64}$/.test(policy.id || '') &&
      /^dir_[0-9a-f]{24}$/.test(policy.home || '') && Array.isArray(policy.recipients) &&
      policy.recipients.includes(access.id) ? policy : null;
  } catch (_) { return null; }
};

voiceAccess.directoryMap = async function (context, access) {
  try {
    const policy = voiceAccess.directoryPolicy(context, access);
    if (!policy || !/^IS[0-9a-fA-F]{32}$/.test(context.PIN_SYNC_SERVICE_SID || '') ||
        !/^SK[0-9a-fA-F]{32}$/.test(context.PIN_SYNC_KEY_SID || '') || !context.PIN_SYNC_KEY_SECRET) return null;
    const url = `https://sync.twilio.com/v1/Services/${context.PIN_SYNC_SERVICE_SID}/Documents/directory-routes`;
    const response = await fetch(url, {redirect:'error', signal:AbortSignal.timeout(3000),
      headers:{Authorization:'Basic '+Buffer.from(context.PIN_SYNC_KEY_SID+':'+context.PIN_SYNC_KEY_SECRET).toString('base64')}});
    if (!response.ok) return null;
    const {data} = await response.json();
    const now = Math.floor(Date.now()/1000);
    if (!data || data.v !== 1 || data.policy_id !== policy.id || data.home !== policy.home ||
        !/^[0-9a-f]{64}$/.test(data.generation || '') || !Number.isInteger(data.expires_at) ||
        data.expires_at <= now || data.expires_at > now+240 || !data.routes || !data.targets) return null;
    return data;
  } catch (_) { return null; }
};

voiceAccess.directoryTarget = function (context, access, map, id) {
  if (!voiceAccess.directoryPolicy(context, access) || !map || !/^dir_[0-9a-f]{24}$/.test(id || '') ||
      !Object.hasOwn(map.targets, id)) return null;
  const target = map.targets[id];
  if (!target || !/^[a-z][a-z0-9_-]{0,31}$/.test(target.session || '') ||
      !/^[A-Za-z0-9_.+-]+$/.test(target.username || '')) return null;
  return {...access, session:target.session, username:target.username};
};

voiceAccess.validAuth = function (a) {
  return a && a.v === 1 && Number.isInteger(a.length) && a.length >= 6 && a.length <= 12 &&
    /^[0-9a-f]{32}$/.test(a.salt || '') && /^[0-9a-f]{64}$/.test(a.hash || '') && /^[0-9a-f]{64}$/.test(a.tag || '');
};
voiceAccess.pinLength = a => a.pin_auth ? a.pin_auth.length : a.pin.length;
voiceAccess.verify = function (context, access, digits) {
  try {
    if (!access || typeof digits !== 'string' || !/^[0-9]{6,12}$/.test(digits)) return false;
    const c = require('node:crypto');
    let expected, supplied;
    if (access.pin_auth) {
      if (!voiceAccess.validAuth(access.pin_auth) || !/^[0-9a-f]{64}$/.test(context.PIN_MASTER_KEY || '')) return false;
      const pepper = c.createHmac('sha256', Buffer.from(context.PIN_MASTER_KEY, 'hex')).update('pin-auth-v1').digest();
      const input = c.createHmac('sha256', pepper).update(digits).digest();
      supplied = c.scryptSync(input, Buffer.from(access.pin_auth.salt, 'hex'), 32, {N:16384, r:8, p:5});
      expected = Buffer.from(access.pin_auth.hash, 'hex');
    } else {
      // Legacy local deployments only; setup migrates and removes CALL_PIN.
      expected = Buffer.from(access.pin || ''); supplied = Buffer.from(digits);
    }
    return expected.length === supplied.length && c.timingSafeEqual(expected, supplied);
  } catch (_) { return false; }
};

voiceAccess.manualExtensions = function (context) {
  try {
    const config = context.VOICE_ACCESS_ASSET ?
      JSON.parse(Runtime.getAssets()[context.VOICE_ACCESS_ASSET].open()) :
      JSON.parse(context.VOICE_ACCESS_JSON || '{}');
    const routes = config.extensions || {};
    if (!routes || typeof routes !== 'object' || Array.isArray(routes)) return {};
    return Object.fromEntries(Object.entries(routes).filter(([extension]) => /^[1-9][0-9]{0,4}$/.test(extension)));
  } catch (_) { return {}; }
};

// Extensions select only routes already allowed for this authenticated number.
voiceAccess.extensions = (context, number) => Object.fromEntries(
  Object.entries(voiceAccess.manualExtensions(context)).filter(([, session]) => voiceAccess(context, number, session)));
