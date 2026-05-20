// Thin wrapper over Zoom Meeting SDK for Web (Component View / `ZoomMtgEmbedded`).
// The script tag in index.html is what loads the SDK; this module assumes the
// global is present.

export const ConnectionState = {
  Connected: 'Connected',
  Reconnecting: 'Reconnecting',
  Closed: 'Closed',
  Failed: 'Failed',
};

export function createSdkClient() {
  if (!window.ZoomMtgEmbedded) {
    throw new Error('ZoomMtgEmbedded not loaded — check the <script> tag in index.html');
  }
  return window.ZoomMtgEmbedded.createClient();
}

export async function initSdk(client, { rootEl, language = 'en-US' }) {
  await client.init({
    zoomAppRoot: rootEl,
    language,
    patchJsMedia: true,
    leaveOnPageUnload: true,
  });
}

export function joinMeeting(client, {
  signature,
  sdkKey,
  meetingNumber,
  password = '',
  userName,
  userEmail = '',
}) {
  return client.join({
    signature,
    sdkKey,
    meetingNumber: String(meetingNumber),
    password,
    userName,
    userEmail,
  });
}

export async function sendChatToAll(client, text) {
  const chat = client.getChatClient?.();
  if (!chat?.sendToAll) {
    throw new Error('chat client unavailable (in-meeting chat may be disabled by host)');
  }
  return chat.sendToAll(text);
}

export async function leaveMeeting(client) {
  try {
    await client.leaveMeeting();
  } catch (err) {
    // Best effort — the SDK throws if you weren't in a meeting
    console.debug('[sdk] leaveMeeting:', err?.message || err);
  }
}
