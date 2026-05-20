import {
  createSdkClient,
  initSdk,
  joinMeeting,
  sendChatToAll,
  leaveMeeting,
  ConnectionState,
} from './sdk-client.js';
import { ReconnectManager } from './reconnect-manager.js';
import { stateStore } from './state-store.js';
import { ChatSpamDetector } from './chat-spam-detector.js';

// ── Configuration ──────────────────────────────────────────────────────────
// Fill in before running. See server/signature.mjs for the matching backend.
const config = {
  meetingNumber: '',                    // ← e.g. '1234567890'
  password: '',
  userName: 'Persistent Bot',           // identity is preserved across reconnects
  userEmail: '',
  sdkKey: '',                           // ← Client ID from your Zoom App Marketplace SDK app
  signatureEndpoint: '/api/signature',  // server endpoint that returns { signature }
  language: 'en-US',
};

const FALLBACK_RESPONSES = [
  "You're filling silence with nothing.",
  "There's no audience here.",
  "This is going nowhere fast.",
  "You're putting energy into a dead end.",
];

const TERMINAL_REASON_FRAGMENTS = [
  'removed', 'expel', 'kicked', 'denied',
  'ended by host', 'meeting ended', 'host ended',
  'unauthorized', 'forbidden',
];

// ── DOM ────────────────────────────────────────────────────────────────────
const statusEl = document.getElementById('status');
const logEl = document.getElementById('log');
const rootEl = document.getElementById('meetingSDKElement');

function logLine(msg) {
  const line = `[${new Date().toLocaleTimeString()}] ${msg}`;
  console.log(line);
  logEl.textContent += line + '\n';
  logEl.scrollTop = logEl.scrollHeight;
}

function setStatus(state) {
  statusEl.dataset.state = state;
  statusEl.textContent = state;
}

// ── Wire up modules ────────────────────────────────────────────────────────
const persisted = stateStore.load();
let client = null;
let userInitiatedLeave = false;
let spamResponses = FALLBACK_RESPONSES;

const spamDetector = new ChatSpamDetector({
  threshold: 10,
  cooldownSec: 90,
  responses: spamResponses,
  persisted: persisted.spam || {},
  onSpam: async ({ sender, text, count, strikes, reply, ts }) => {
    logLine(`SPAM ${sender || '?'} ×${count} "${text.slice(0, 40)}" (strike #${strikes})`);
    persistSpamState();
    if (reply && client) {
      try {
        await sendChatToAll(client, reply);
        logLine(`replied: ${reply.slice(0, 60)}`);
      } catch (err) {
        logLine(`reply failed: ${err?.message || err}`);
      }
    }
    // Append to in-memory audit (mirrors the Python spam_log.jsonl)
    const audit = stateStore.load().audit || [];
    audit.push({ ts, sender, text, count, strikes, reply });
    stateStore.patch({ audit: audit.slice(-200) });
  },
});

function persistSpamState() {
  stateStore.patch({
    spam: spamDetector.serialize(),
    updatedAt: Date.now(),
  });
}

const reconnectMgr = new ReconnectManager({
  onAttempt: async (n) => {
    logLine(`reconnecting… (#${n})`);
    await join();
  },
  onLog: logLine,
});

async function fetchSignature() {
  const res = await fetch(config.signatureEndpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ meetingNumber: config.meetingNumber, role: 0 }),
  });
  if (!res.ok) throw new Error(`signature endpoint ${res.status}`);
  const { signature } = await res.json();
  if (!signature) throw new Error('signature endpoint returned no signature');
  return signature;
}

async function loadSpamResponses() {
  // Optional: serve spam_responses.txt at the site root and we'll pick it up.
  try {
    const res = await fetch('/spam_responses.txt', { cache: 'no-cache' });
    if (!res.ok) return;
    const text = await res.text();
    const lines = text.split('\n').map((s) => s.trim())
      .filter((s) => s && !s.startsWith('#'));
    if (lines.length) {
      spamResponses = lines;
      spamDetector.responses = lines;
      logLine(`loaded ${lines.length} spam responses`);
    }
  } catch {
    /* keep fallback */
  }
}

function isTerminalReason(reason) {
  const r = String(reason || '').toLowerCase();
  return TERMINAL_REASON_FRAGMENTS.some((f) => r.includes(f));
}

function setupListeners() {
  client.on('connection-change', (payload) => {
    const state = payload?.state || 'Unknown';
    const reason = payload?.reason ?? '';
    logLine(`connection-change → ${state}${reason ? ` (${reason})` : ''}`);
    setStatus(state);

    if (userInitiatedLeave) return;

    if (state === ConnectionState.Connected) {
      reconnectMgr.reset();
      stateStore.patch({ lastJoinedAt: Date.now() });
      logLine(`resumed; ${spamDetector.processedIds.size} prior msg ids tracked`);
      return;
    }

    if (state === ConnectionState.Closed || state === ConnectionState.Failed) {
      if (isTerminalReason(reason)) {
        reconnectMgr.abort(`terminal: ${reason}`);
        return;
      }
      reconnectMgr.schedule(`${state}/${reason || 'unknown'}`);
    }
  });

  client.on('chat-on-message', (payload) => {
    const text = (payload?.message ?? payload?.content ?? '').toString().trim();
    const sender = payload?.sender?.name || payload?.userName || '';
    const id = payload?.id || payload?.messageId
      || `${sender}:${payload?.timestamp || Date.now()}:${text}`;
    spamDetector.observe({ id, sender, text });
    persistSpamState();
  });
}

async function join() {
  if (!config.meetingNumber || !config.sdkKey) {
    throw new Error('Set config.meetingNumber and config.sdkKey in app.js first');
  }
  if (!client) {
    client = createSdkClient();
    await initSdk(client, { rootEl, language: config.language });
    setupListeners();
  }
  setStatus('Connecting');
  const signature = await fetchSignature();
  const userName = persisted.userName || config.userName;
  await joinMeeting(client, {
    signature,
    sdkKey: config.sdkKey,
    meetingNumber: config.meetingNumber,
    password: config.password,
    userName,
    userEmail: config.userEmail,
  });
  // Persist the identity used for this session so any rejoin reuses it.
  stateStore.patch({ userName });
}

// ── UI wiring ──────────────────────────────────────────────────────────────
document.getElementById('join-btn').addEventListener('click', async () => {
  userInitiatedLeave = false;
  reconnectMgr.reset();
  await loadSpamResponses();
  try {
    await join();
  } catch (err) {
    logLine(`initial join failed: ${err?.message || err}`);
    setStatus('Failed');
    reconnectMgr.schedule('initial-join-error');
  }
});

document.getElementById('leave-btn').addEventListener('click', async () => {
  userInitiatedLeave = true;
  reconnectMgr.abort('user-leave');
  await leaveMeeting(client);
  setStatus('Idle');
});

document.getElementById('reset-btn').addEventListener('click', () => {
  stateStore.clear();
  logLine('persisted state cleared');
});

window.addEventListener('beforeunload', persistSpamState);

logLine('ready — set config in app.js, run a signature server, click Join');
