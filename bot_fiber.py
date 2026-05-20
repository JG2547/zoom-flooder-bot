# -*- coding: utf-8 -*-

"""Fiber-only detector module for the standalone Zoom bot.

Phase 2 of the fiber-only migration plan (see
``docs/DETECTION_ARCHITECTURE.md``). Mirrors the ZMT agent contract at
``Botify-Network/services/zmt-electron-client/`` — specifically:

- ``enforcer/fiber_result.py`` (discriminated outcome type)
- ``enforcer/zoom_auto_kick.py:JS_FIBER_PARTICIPANTS`` (3-path walker)
- ``agent/src/enforcement/adapters/PlaywrightAdapter.js:_doCaptureParticipants``
  (fiber-only, hard deadline, no scroll mutation)
- ``agent/src/enforcement/ActionRegistry.js:capture_participants``
  (``extractionMethod: 'fiber'``, ``fiberTimeoutMs: 100``)

This module does NOT replace any existing reader in ``bot.py`` — that is
Phase 3, gated on ``config.DETECTION_MODE == "fiber_only"``. Phase 2
ships the adapter only.

Public surface:

- :class:`FiberOutcome` — outcome enum.
- :class:`FiberResult` — discriminated result dataclass.
- :func:`capture_participants` — fiber participant reader.
- :func:`capture_chat_messages` — fiber chat reader.
- :func:`capture_meeting_state` — fiber meeting-state probe.

Each wrapper:

- Executes a single JS payload via the Selenium driver.
- Enforces a hard time budget inside the JS (passed via ``arguments[0]``).
- Catches driver/JS exceptions and maps them to ``DRIVER_ERROR``.
- Validates the returned shape and maps malformed results to
  ``PARSE_ERROR``.
- Never mutates Zoom UI (no clicks, no scroll-into-view, no chat sends).
- Never logs secrets.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


# ── Outcome enum ──────────────────────────────────────────────────────────

class FiberOutcome(str, Enum):
    """Canonical fiber-extraction outcomes.

    Values are uppercase strings so they appear verbatim in telemetry
    counter names (e.g. ``fiber.deadline_exceeded`` would be derived
    from ``FiberOutcome.DEADLINE_EXCEEDED.value``).
    """

    OK = "OK"
    EMPTY = "EMPTY"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    PARSE_ERROR = "PARSE_ERROR"
    DRIVER_ERROR = "DRIVER_ERROR"
    UNSUPPORTED = "UNSUPPORTED"


# ── Result dataclass ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class FiberResult:
    """Discriminated result of a fiber extraction call.

    Invariants:

    - ``outcome == OK`` ⇒ ``ok is True`` and ``data`` is the canonical
      payload (list for participants/chat, dict for meeting-state).
    - ``outcome != OK`` ⇒ ``ok is False`` and ``data`` is the empty
      shape appropriate for the caller (list or dict).
    - ``error`` carries a short detail for PARSE_ERROR / DRIVER_ERROR
      / UNSUPPORTED; empty for the OK / EMPTY / DEADLINE_EXCEEDED.
    """

    outcome: FiberOutcome
    ok: bool = False
    data: Any = None
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    source: str = "fiber"

    # --- Factories ---------------------------------------------------

    @classmethod
    def ok_result(cls, data: Any, elapsed_ms: Optional[int] = None) -> "FiberResult":
        return cls(FiberOutcome.OK, True, data, None, elapsed_ms)

    @classmethod
    def empty(cls, default_shape: Any, elapsed_ms: Optional[int] = None) -> "FiberResult":
        return cls(FiberOutcome.EMPTY, False, default_shape, None, elapsed_ms)

    @classmethod
    def deadline(cls, default_shape: Any, elapsed_ms: Optional[int] = None) -> "FiberResult":
        return cls(FiberOutcome.DEADLINE_EXCEEDED, False, default_shape, None, elapsed_ms)

    @classmethod
    def parse_error(cls, err: Any, default_shape: Any) -> "FiberResult":
        return cls(FiberOutcome.PARSE_ERROR, False, default_shape, str(err)[:200])

    @classmethod
    def driver_error(cls, err: Any, default_shape: Any) -> "FiberResult":
        return cls(FiberOutcome.DRIVER_ERROR, False, default_shape, str(err)[:200])

    @classmethod
    def unsupported(cls, reason: str, default_shape: Any) -> "FiberResult":
        return cls(FiberOutcome.UNSUPPORTED, False, default_shape, reason[:200])

    # --- Convenience -------------------------------------------------

    @property
    def is_ok(self) -> bool:
        """True iff fiber ran successfully (note: ``OK([])`` is still ok)."""
        return self.outcome is FiberOutcome.OK

    @property
    def is_terminal_error(self) -> bool:
        """True for outcomes that indicate the driver itself is broken.

        Callers may short-circuit the polling cycle on this signal.
        """
        return self.outcome is FiberOutcome.DRIVER_ERROR


# ── JS payloads ──────────────────────────────────────────────────────────

# Each payload is wrapped as an IIFE that accepts ``arguments[0]`` as the
# deadline in milliseconds. The return shape is always:
#
#     { outcome: <string>, data: <list|object>, elapsedMs: <int> }
#
# where outcome ∈ ``FiberOutcome`` value names.

_FIBER_PARTICIPANTS_JS = r"""
(function() {
    var timeoutMs = Math.max(20, Math.min(2000, arguments[0] | 0 || 100));
    var t0 = Date.now();
    var deadline = t0 + timeoutMs;

    function elapsed() { return Date.now() - t0; }

    function isParticipantObj(obj) {
        if (!obj || typeof obj !== 'object' || obj.$$typeof) return false;
        var keys = Object.keys(obj);
        if (keys.length < 2) return false;
        var hasName = false;
        for (var i = 0; i < keys.length; i++) {
            var v = obj[keys[i]];
            if (typeof v !== 'string') continue;
            if (v.length < 2 || v.length > 80) continue;
            if (/^(http|\/|#|rgb|wss?:|data:|blob:|\{)/.test(v)) continue;
            hasName = true; break;
        }
        if (!hasName) return false;
        if (obj.type && obj.props && obj.key !== undefined) return false;
        if (obj.nodeName || obj.nodeType) return false;
        for (var fi = 0; fi < keys.length; fi++) {
            if (typeof obj[keys[fi]] === 'function') return false;
        }
        return true;
    }

    function extractParticipant(obj) {
        var keys = Object.keys(obj);
        var displayName = '';
        for (var i = 0; i < keys.length; i++) {
            var k = keys[i];
            if (typeof obj[k] === 'string' &&
                /^(displayName|userName|name|display_name|user_name|screenName|dn|uname)$/i.test(k)) {
                displayName = obj[k]; break;
            }
        }
        if (!displayName) {
            for (var j = 0; j < keys.length; j++) {
                var kk = keys[j];
                if (typeof obj[kk] === 'string' && obj[kk].length >= 1 && obj[kk].length < 80 &&
                    !/^(http|\/|#|rgb|wss|ws)/.test(obj[kk])) {
                    if (obj[kk].length > displayName.length) displayName = obj[kk];
                }
            }
        }
        var role = 'participant', isMe = false, isHost = false, isCoHost = false;
        var isMuted = false, isVideoOn = null, isSharing = false;
        var isHandRaised = false, inWaitingRoom = false;
        var userId = '', persistentId = '';
        for (var i = 0; i < keys.length; i++) {
            var k = keys[i], v = obj[k], kl = k.toLowerCase();
            if ((kl.indexOf('host') !== -1 && kl.indexOf('co') === -1) && v === true) { role = 'host'; isHost = true; }
            if ((kl.indexOf('cohost') !== -1 || kl.indexOf('co_host') !== -1 || kl.indexOf('co-host') !== -1) && v === true) { role = 'cohost'; isCoHost = true; }
            if (kl === 'role' && v === 1) { role = 'host'; isHost = true; }
            if (kl === 'role' && v === 2) { role = 'cohost'; isCoHost = true; }
            if (/^(isMe|bMe|isSelf|isMyself|bSelf)$/i.test(k) && v === true) isMe = true;
            if (/^(bVideoOn|isVideoOn|videoOn|video_on|bCameraOn|isCameraOn|cameraOn)$/i.test(k)) isVideoOn = !!v;
            if (/^(bVideoOff|isVideoOff|videoOff|video_off|bCameraOff|isCameraOff|cameraOff)$/i.test(k) && v === true) isVideoOn = false;
            if (/^(videoStatus|cameraStatus|camStatus)$/i.test(k) && typeof v === 'number') isVideoOn = v !== 0;
            if (/^(bMuted|isMuted|muted|audio_muted|audioMuted|isAudioMuted)$/i.test(k)) isMuted = !!v;
            if (/^(bHandRaised|isHandRaised|handRaised|bRaiseHand|isRaiseHand|raiseHand|bHand|hasHand|handUp|isHandUp)$/i.test(k) && v === true) isHandRaised = true;
            if (kl === 'handstatus' && typeof v === 'number' && v > 0) isHandRaised = true;
            if (/^(bShareScreen|isSharing|sharing|isSharingScreen|bSharing|sharingScreen)$/i.test(k) && v === true) isSharing = true;
            if (kl === 'sharingstatus' && typeof v === 'number' && v > 0) isSharing = true;
            if (/^(isInWaitingRoom|bInWaitingRoom|isWaitingRoom|inWaitingRoom|bWaitingRoom|waitingRoom)$/i.test(k) && v === true) inWaitingRoom = true;
            if (kl === 'attendeestatus' && typeof v === 'number' && v === 0) inWaitingRoom = true;
            if (!userId && /^(userId|user_id|nUserID|nID|uid)$/i.test(k) && v != null) userId = String(v);
            if (!persistentId && /^(userGUID|persistentId|participantId|participant_id|zoomID|zoomId|guid)$/i.test(k) && v != null) persistentId = String(v);
        }
        if (!userId && obj.id != null) userId = String(obj.id);
        return {
            displayName: displayName,
            userId: userId,
            persistentId: persistentId,
            role: role,
            isHost: isHost,
            isCoHost: isCoHost,
            isMe: isMe,
            inWaitingRoom: inWaitingRoom,
            isMuted: isMuted,
            isVideoOn: isVideoOn,
            isSharing: isSharing,
            isHandRaised: isHandRaised,
            rawKeys: keys
        };
    }

    function tryExtract(coll) {
        var items;
        if (coll instanceof Map) items = Array.from(coll.values());
        else if (Array.isArray(coll)) items = coll;
        else return null;
        if (items.length < 2) return null;
        if (items[0] && items[0].$$typeof) return null;
        if (!isParticipantObj(items[0])) return null;
        var out = items.map(extractParticipant).filter(function(p) { return p.displayName.length > 1; });
        return out.length >= 2 ? out : null;
    }

    var best = null;
    function update(arr) { if (arr && (!best || arr.length > best.length)) best = arr; }

    function searchFiber(fiber) {
        if (Date.now() >= deadline) return;
        var sources = [];
        if (fiber.memoizedProps) sources.push(fiber.memoizedProps);
        var hs = fiber.memoizedState, hi = 0;
        while (hs && hi < 25) {
            if (hs.memoizedState != null) sources.push(hs.memoizedState);
            if (hs.queue && hs.queue.lastRenderedState != null) sources.push(hs.queue.lastRenderedState);
            hs = hs.next; hi++;
        }
        for (var s = 0; s < sources.length; s++) {
            if (Date.now() >= deadline) return;
            var src = sources[s];
            if (!src || typeof src !== 'object') continue;
            update(tryExtract(src));
            if (Array.isArray(src) || src instanceof Map) continue;
            var sk = Object.keys(src);
            for (var ki = 0; ki < sk.length; ki++) {
                if (Date.now() >= deadline) return;
                try {
                    var v = src[sk[ki]];
                    update(tryExtract(v));
                    if (v && typeof v === 'object' && !Array.isArray(v) && !(v instanceof Map) && !v.$$typeof) {
                        var ik = Object.keys(v);
                        if (ik.length < 30) {
                            for (var ii = 0; ii < ik.length; ii++) update(tryExtract(v[ik[ii]]));
                        }
                    }
                } catch(e) {}
            }
        }
    }

    try {
        // Path A: walk up from participant-panel containers.
        var containers = document.querySelectorAll(
            '[class*="participants-ul"], [class*="virtuoso"], .participants-section-container, #participants-ul'
        );
        for (var ci = 0; ci < containers.length; ci++) {
            if (Date.now() >= deadline) break;
            var c = containers[ci];
            if (c.children.length === 0) continue;
            var fk = Object.keys(c).find(function(k) {
                return k.indexOf('__reactFiber$') === 0 || k.indexOf('__reactInternalInstance$') === 0;
            });
            if (!fk) continue;
            var fiber = c[fk], depth = 0;
            while (fiber && depth < 60 && Date.now() < deadline) {
                searchFiber(fiber);
                fiber = fiber.return;
                depth++;
            }
        }
        if (best && best.length >= 10) {
            return { outcome: "OK", data: best, elapsedMs: elapsed() };
        }

        // Path B: BFS from root fiber.
        var rootEl = document.getElementById('root') || document.getElementById('app') || document.body;
        var rfk = Object.keys(rootEl).find(function(k) {
            return k.indexOf('__reactFiber$') === 0 || k.indexOf('__reactContainer$') === 0;
        });
        if (rfk && Date.now() < deadline) {
            var visited = new Set();
            var queue = [rootEl[rfk]];
            var bfs = 0;
            while (queue.length > 0 && bfs < 300 && Date.now() < deadline) {
                var f = queue.shift();
                if (!f || visited.has(f)) continue;
                visited.add(f);
                bfs++;
                searchFiber(f);
                if (best && best.length >= 10) {
                    return { outcome: "OK", data: best, elapsedMs: elapsed() };
                }
                if (f.child) queue.push(f.child);
                if (f.sibling) queue.push(f.sibling);
            }
        }
        if (best && best.length >= 5) {
            return { outcome: "OK", data: best, elapsedMs: elapsed() };
        }

        // Path C: global store scan.
        if (Date.now() < deadline) {
            var pn = Object.getOwnPropertyNames(window);
            for (var wi = 0; wi < pn.length; wi++) {
                if (Date.now() >= deadline) break;
                try {
                    var wv = window[pn[wi]];
                    if (!wv || typeof wv !== 'object') continue;
                    if (typeof wv.getState === 'function') {
                        var state = wv.getState();
                        if (state && typeof state === 'object') {
                            var sk = Object.keys(state);
                            for (var si = 0; si < sk.length; si++) {
                                update(tryExtract(state[sk[si]]));
                                if (best && best.length >= 10) {
                                    return { outcome: "OK", data: best, elapsedMs: elapsed() };
                                }
                                var sv = state[sk[si]];
                                if (sv && typeof sv === 'object' && !Array.isArray(sv)) {
                                    var svk = Object.keys(sv);
                                    for (var svi = 0; svi < svk.length; svi++) {
                                        update(tryExtract(sv[svk[svi]]));
                                        if (best && best.length >= 10) {
                                            return { outcome: "OK", data: best, elapsedMs: elapsed() };
                                        }
                                    }
                                }
                            }
                        }
                    }
                } catch(e) {}
            }
        }

        if (best && best.length >= 1) {
            return { outcome: "OK", data: best, elapsedMs: elapsed() };
        }
        if (Date.now() >= deadline) {
            return { outcome: "DEADLINE_EXCEEDED", data: [], elapsedMs: elapsed() };
        }
        return { outcome: "EMPTY", data: [], elapsedMs: elapsed() };
    } catch (err) {
        return { outcome: "PARSE_ERROR", data: [], elapsedMs: elapsed(), error: String(err).slice(0, 200) };
    }
})();
"""


_FIBER_CHAT_JS = r"""
(function() {
    var timeoutMs = Math.max(20, Math.min(2000, arguments[0] | 0 || 100));
    var t0 = Date.now();
    var deadline = t0 + timeoutMs;
    function elapsed() { return Date.now() - t0; }

    function isMessageObj(obj) {
        if (!obj || typeof obj !== 'object' || obj.$$typeof) return false;
        var keys = Object.keys(obj);
        if (keys.length < 2 || keys.length > 40) return false;
        var hasText = false;
        for (var i = 0; i < keys.length; i++) {
            var v = obj[keys[i]];
            if (typeof v !== 'string') continue;
            if (!/^(text|message|content|msg|body|messageText|content_msg|messageBody|sMessage|sText)$/i.test(keys[i])) continue;
            if (v.length === 0 || v.length > 8000) continue;
            hasText = true; break;
        }
        if (!hasText) return false;
        for (var i = 0; i < keys.length; i++) {
            var k = keys[i], v = obj[k];
            if (!/^(senderName|sender|displayName|userName|from|fromName|sender_name|sName|sFromUserName|sFromName|fromUserName|userDisplayName)$/i.test(k)) continue;
            if (typeof v === 'string' && v.length > 0) return true;
            if (v && typeof v === 'object' && typeof v.name === 'string') return true;
        }
        return false;
    }

    function extractMessage(obj) {
        var keys = Object.keys(obj);
        var text = '', sender = '', timestamp = 0, messageId = '';
        for (var i = 0; i < keys.length; i++) {
            var k = keys[i], v = obj[k];
            if (!text && typeof v === 'string' &&
                /^(text|message|content|msg|body|messagetext|content_msg|messagebody|smessage|stext)$/i.test(k)) {
                text = v;
            }
            if (!sender && /^(sendername|sender|displayname|username|from|fromname|sender_name|sname|sfromusername|sfromname|fromusername|userdisplayname)$/i.test(k)) {
                if (typeof v === 'string') sender = v;
                else if (v && typeof v === 'object' && typeof v.name === 'string') sender = v.name;
            }
            if (!timestamp && typeof v === 'number' &&
                /^(timestamp|time|ts|sentat|createdat|sendtime|stamp|nsendtime)$/i.test(k)) {
                timestamp = v;
            }
            if (!messageId && /^(messageid|msgid|id|mid|uid|guid|nmsgid)$/i.test(k) &&
                (typeof v === 'string' || typeof v === 'number')) {
                messageId = String(v);
            }
        }
        return {
            sender: sender,
            text: text,
            timestamp: timestamp,
            messageId: messageId,
            rawKeys: keys
        };
    }

    function tryExtract(coll) {
        var items;
        if (coll instanceof Map) items = Array.from(coll.values());
        else if (Array.isArray(coll)) items = coll;
        else return null;
        if (items.length < 1) return null;
        if (items[0] && items[0].$$typeof) return null;
        if (!isMessageObj(items[0])) return null;
        var out = items.map(extractMessage).filter(function(m) { return m.text.length > 0; });
        return out.length >= 1 ? out : null;
    }

    var best = null;
    function update(arr) { if (arr && (!best || arr.length > best.length)) best = arr; }

    function searchFiber(fiber) {
        if (Date.now() >= deadline) return;
        var sources = [];
        if (fiber.memoizedProps) sources.push(fiber.memoizedProps);
        var hs = fiber.memoizedState, hi = 0;
        while (hs && hi < 25) {
            if (hs.memoizedState != null) sources.push(hs.memoizedState);
            if (hs.queue && hs.queue.lastRenderedState != null) sources.push(hs.queue.lastRenderedState);
            hs = hs.next; hi++;
        }
        for (var s = 0; s < sources.length; s++) {
            if (Date.now() >= deadline) return;
            var src = sources[s];
            if (!src || typeof src !== 'object') continue;
            update(tryExtract(src));
            if (Array.isArray(src) || src instanceof Map) continue;
            var sk = Object.keys(src);
            for (var ki = 0; ki < sk.length; ki++) {
                if (Date.now() >= deadline) return;
                try {
                    var v = src[sk[ki]];
                    update(tryExtract(v));
                    if (v && typeof v === 'object' && !Array.isArray(v) && !(v instanceof Map) && !v.$$typeof) {
                        var ik = Object.keys(v);
                        if (ik.length < 30) {
                            for (var ii = 0; ii < ik.length; ii++) update(tryExtract(v[ik[ii]]));
                        }
                    }
                } catch(e) {}
            }
        }
    }

    try {
        // Path A: chat panel containers.
        var containers = document.querySelectorAll(
            '[class*="chat-virtualized-list"], [class*="chat-virtuoso"], [class*="chat-container"], [class*="chat-list"], [class*="ChatList"], [class*="chat-content"]'
        );
        for (var ci = 0; ci < containers.length; ci++) {
            if (Date.now() >= deadline) break;
            var c = containers[ci];
            if (c.children.length === 0) continue;
            var fk = Object.keys(c).find(function(k) {
                return k.indexOf('__reactFiber$') === 0 || k.indexOf('__reactInternalInstance$') === 0;
            });
            if (!fk) continue;
            var fiber = c[fk], depth = 0;
            while (fiber && depth < 60 && Date.now() < deadline) {
                searchFiber(fiber);
                fiber = fiber.return;
                depth++;
            }
        }
        if (best && best.length >= 1) {
            return { outcome: "OK", data: best, elapsedMs: elapsed() };
        }

        // Path B: BFS from root fiber.
        var rootEl = document.getElementById('root') || document.getElementById('app') || document.body;
        var rfk = Object.keys(rootEl).find(function(k) {
            return k.indexOf('__reactFiber$') === 0 || k.indexOf('__reactContainer$') === 0;
        });
        if (rfk && Date.now() < deadline) {
            var visited = new Set();
            var queue = [rootEl[rfk]];
            var bfs = 0;
            while (queue.length > 0 && bfs < 400 && Date.now() < deadline) {
                var f = queue.shift();
                if (!f || visited.has(f)) continue;
                visited.add(f);
                bfs++;
                searchFiber(f);
                if (f.child) queue.push(f.child);
                if (f.sibling) queue.push(f.sibling);
            }
        }
        if (best && best.length >= 1) {
            return { outcome: "OK", data: best, elapsedMs: elapsed() };
        }
        if (Date.now() >= deadline) {
            return { outcome: "DEADLINE_EXCEEDED", data: [], elapsedMs: elapsed() };
        }
        return { outcome: "EMPTY", data: [], elapsedMs: elapsed() };
    } catch (err) {
        return { outcome: "PARSE_ERROR", data: [], elapsedMs: elapsed(), error: String(err).slice(0, 200) };
    }
})();
"""


_FIBER_MEETING_STATE_JS = r"""
(function() {
    var timeoutMs = Math.max(20, Math.min(2000, arguments[0] | 0 || 100));
    var t0 = Date.now();
    var deadline = t0 + timeoutMs;
    function elapsed() { return Date.now() - t0; }

    var state = {
        inMeeting: null,
        inWaitingRoom: null,
        meetingEnded: null,
        leaveButtonVisible: null,
        errorCode: null,
        rawKeys: []
    };
    var foundAny = false;

    function hasMeetingId(obj, keys) {
        for (var i = 0; i < keys.length; i++) {
            var k = keys[i], v = obj[k];
            if (/^(meetingNumber|meetingId|meetingNum|mn|mNum)$/i.test(k) &&
                (typeof v === 'string' || typeof v === 'number') &&
                String(v).length > 4) {
                return true;
            }
        }
        return false;
    }

    function ingest(obj) {
        if (!obj || typeof obj !== 'object' || obj.$$typeof) return false;
        var keys = Object.keys(obj);
        if (keys.length < 2 || keys.length > 60) return false;
        if (!hasMeetingId(obj, keys)) return false;
        var touched = false;
        for (var i = 0; i < keys.length; i++) {
            var k = keys[i], v = obj[k], kl = k.toLowerCase();
            if (/^(meetingStatus|connectionStatus|status|sessionStatus)$/i.test(k)) {
                if (typeof v === 'number' && v >= 1 && v <= 5) {
                    state.inMeeting = true; touched = true;
                }
                if (typeof v === 'string') {
                    if (/(joined|connected|active|inSession|inMeeting)/i.test(v)) { state.inMeeting = true; touched = true; }
                    if (/(ended|left|disconnected|failed|removed|kicked)/i.test(v)) { state.meetingEnded = true; touched = true; }
                }
            }
            if (/^(inMeeting|isJoined|isInMeeting|isConnected|joined|connected|isActive)$/i.test(k) && v === true) {
                state.inMeeting = true; touched = true;
            }
            if (/^(meetingEnded|isEnded|hasLeft|removedByHost|kickedOut|wasKicked)$/i.test(k) && v === true) {
                state.meetingEnded = true; touched = true;
            }
            if (/^(isInWaitingRoom|bInWaitingRoom|inWaitingRoom|bWaitingRoom|waitingRoom)$/i.test(k) && v === true) {
                state.inWaitingRoom = true; touched = true;
            }
            if (/^(errorCode|reasonCode|disconnectReason|kickReason)$/i.test(k) &&
                (typeof v === 'string' || typeof v === 'number')) {
                state.errorCode = String(v); touched = true;
            }
            if (/^(leaveButtonVisible|hasLeaveButton|showLeaveButton)$/i.test(k) && typeof v === 'boolean') {
                state.leaveButtonVisible = v; touched = true;
            }
        }
        if (touched) {
            for (var rk = 0; rk < keys.length; rk++) {
                if (state.rawKeys.indexOf(keys[rk]) < 0 && state.rawKeys.length < 40) {
                    state.rawKeys.push(keys[rk]);
                }
            }
        }
        return touched;
    }

    function searchFiber(fiber) {
        if (!fiber || Date.now() >= deadline) return;
        var sources = [];
        if (fiber.memoizedProps) sources.push(fiber.memoizedProps);
        var hs = fiber.memoizedState, hi = 0;
        while (hs && hi < 25) {
            if (hs.memoizedState != null) sources.push(hs.memoizedState);
            if (hs.queue && hs.queue.lastRenderedState != null) sources.push(hs.queue.lastRenderedState);
            hs = hs.next; hi++;
        }
        for (var s = 0; s < sources.length; s++) {
            if (Date.now() >= deadline) return;
            var src = sources[s];
            if (!src || typeof src !== 'object') continue;
            if (ingest(src)) foundAny = true;
            var sk = Object.keys(src);
            for (var i = 0; i < sk.length; i++) {
                if (Date.now() >= deadline) return;
                try {
                    if (ingest(src[sk[i]])) foundAny = true;
                } catch(e) {}
            }
        }
    }

    try {
        var rootEl = document.getElementById('root') || document.getElementById('app') || document.body;
        var fk = Object.keys(rootEl).find(function(k) {
            return k.indexOf('__reactFiber$') === 0
                || k.indexOf('__reactContainer$') === 0
                || k.indexOf('__reactInternalInstance$') === 0;
        });
        if (fk) {
            var visited = new Set();
            var queue = [rootEl[fk]];
            var bfs = 0;
            while (queue.length > 0 && bfs < 250 && Date.now() < deadline) {
                var f = queue.shift();
                if (!f || visited.has(f)) continue;
                visited.add(f);
                bfs++;
                searchFiber(f);
                if (f.child) queue.push(f.child);
                if (f.sibling) queue.push(f.sibling);
            }
        }

        if (foundAny) {
            return { outcome: "OK", data: state, elapsedMs: elapsed() };
        }
        if (Date.now() >= deadline) {
            return { outcome: "DEADLINE_EXCEEDED", data: state, elapsedMs: elapsed() };
        }
        // No fiber meeting-state object surfaced; do NOT fall back to DOM
        // text scraping. Report UNSUPPORTED so callers can decide.
        return { outcome: "UNSUPPORTED", data: state, elapsedMs: elapsed(),
                 error: "No fiber meeting-state object found" };
    } catch (err) {
        return { outcome: "PARSE_ERROR", data: state, elapsedMs: elapsed(), error: String(err).slice(0, 200) };
    }
})();
"""


# ── Internal execution helper ─────────────────────────────────────────────

_KNOWN_OUTCOMES = {o.value for o in FiberOutcome}


def _execute_fiber_js(
    driver: Any,
    js: str,
    timeout_ms: int,
    default_shape: Any,
) -> FiberResult:
    """Run a fiber JS payload and map the result to a :class:`FiberResult`.

    Centralises:

    - timeout clamping (20..2000 ms),
    - driver-level exception catch (mapped to DRIVER_ERROR),
    - shape validation (mapped to PARSE_ERROR on malformed return),
    - outcome whitelisting against :class:`FiberOutcome`.

    ``default_shape`` is the empty value the caller expects when the
    outcome is non-OK (``[]`` for list readers, ``{}`` for the
    meeting-state probe). Keeps callers from special-casing None.
    """
    clamped = max(20, min(2000, int(timeout_ms)))
    start = time.monotonic()
    try:
        raw = driver.execute_script(js, clamped)
    except Exception as exc:  # noqa: BLE001 — boundary mapping
        log.debug("fiber: driver exception (%s): %s", type(exc).__name__, exc)
        return FiberResult.driver_error(exc, default_shape)

    if raw is None:
        # Older Selenium drivers can return None instead of the IIFE result.
        return FiberResult.parse_error("driver returned None", default_shape)

    if not isinstance(raw, dict):
        return FiberResult.parse_error(
            "expected dict, got " + type(raw).__name__, default_shape
        )

    outcome_str = raw.get("outcome")
    if outcome_str not in _KNOWN_OUTCOMES:
        return FiberResult.parse_error(
            "unknown outcome: " + repr(outcome_str)[:60], default_shape
        )
    outcome = FiberOutcome(outcome_str)

    elapsed_ms = raw.get("elapsedMs")
    if not isinstance(elapsed_ms, (int, float)):
        # Fall back to wall-clock if the JS forgot to include it.
        elapsed_ms = int((time.monotonic() - start) * 1000)
    elapsed_ms = int(elapsed_ms)

    data = raw.get("data")
    err = raw.get("error")
    if isinstance(err, str):
        err = err[:200]
    else:
        err = None

    if outcome is FiberOutcome.OK:
        if isinstance(default_shape, list) and not isinstance(data, list):
            return FiberResult.parse_error(
                "OK outcome but data is not a list", default_shape
            )
        if isinstance(default_shape, dict) and not isinstance(data, dict):
            return FiberResult.parse_error(
                "OK outcome but data is not a dict", default_shape
            )
        return FiberResult.ok_result(data, elapsed_ms)

    # Non-OK: keep the JS-returned data only if it matches the expected
    # default shape; otherwise substitute the default.
    if isinstance(default_shape, list) and not isinstance(data, list):
        data = list(default_shape)
    elif isinstance(default_shape, dict) and not isinstance(data, dict):
        data = dict(default_shape)

    if outcome is FiberOutcome.EMPTY:
        return FiberResult(FiberOutcome.EMPTY, False, data, err, elapsed_ms)
    if outcome is FiberOutcome.DEADLINE_EXCEEDED:
        return FiberResult(FiberOutcome.DEADLINE_EXCEEDED, False, data, err, elapsed_ms)
    if outcome is FiberOutcome.PARSE_ERROR:
        return FiberResult(FiberOutcome.PARSE_ERROR, False, data, err or "parse error", elapsed_ms)
    if outcome is FiberOutcome.DRIVER_ERROR:
        return FiberResult(FiberOutcome.DRIVER_ERROR, False, data, err or "driver error", elapsed_ms)
    if outcome is FiberOutcome.UNSUPPORTED:
        return FiberResult(FiberOutcome.UNSUPPORTED, False, data,
                           err or "outcome unsupported", elapsed_ms)
    # Defensive: should be unreachable given the whitelist above.
    return FiberResult.parse_error("unhandled outcome: " + outcome.value, default_shape)


# ── Public wrappers ──────────────────────────────────────────────────────

def capture_participants(driver: Any, timeout_ms: int = 100) -> FiberResult:
    """Fiber-only participant list reader.

    Returns a :class:`FiberResult` whose ``data`` is a list of participant
    dicts on OK, or ``[]`` on any non-OK outcome.

    Side-effect free: walks the React fiber tree only — no clicks, no
    scrolls, no chat sends, no panel toggles.
    """
    return _execute_fiber_js(driver, _FIBER_PARTICIPANTS_JS, timeout_ms, default_shape=[])


def capture_chat_messages(driver: Any, timeout_ms: int = 100) -> FiberResult:
    """Fiber-only chat message reader.

    Returns a :class:`FiberResult` whose ``data`` is a list of message
    dicts on OK, or ``[]`` on any non-OK outcome.

    Side-effect free. No DOM scraping fallback — if the fiber tree does
    not surface a chat collection, returns ``EMPTY`` (collection mounted
    but empty) or the JS will time out to ``DEADLINE_EXCEEDED``.
    """
    return _execute_fiber_js(driver, _FIBER_CHAT_JS, timeout_ms, default_shape=[])


def capture_meeting_state(driver: Any, timeout_ms: int = 100) -> FiberResult:
    """Fiber-only meeting-state probe.

    Returns a :class:`FiberResult` whose ``data`` is a dict shaped like
    ``{inMeeting, inWaitingRoom, meetingEnded, leaveButtonVisible,
    errorCode, rawKeys}``. Any field may be ``None`` if the fiber tree
    did not surface a corresponding signal.

    If no fiber meeting-state object is found, the result is
    :attr:`FiberOutcome.UNSUPPORTED` — callers should NOT fall back to
    DOM text scraping; they should treat unsupported as "unknown" and
    keep their prior state.
    """
    return _execute_fiber_js(driver, _FIBER_MEETING_STATE_JS, timeout_ms, default_shape={})


__all__ = [
    "FiberOutcome",
    "FiberResult",
    "capture_participants",
    "capture_chat_messages",
    "capture_meeting_state",
]
