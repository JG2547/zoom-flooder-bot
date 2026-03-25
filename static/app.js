document.addEventListener("DOMContentLoaded", () => {
    const socket = io();

    // ── DOM refs ─────────────────────────────────────────────────────
    const btnStart       = document.getElementById("btn-start");
    const btnStop        = document.getElementById("btn-stop");
    const botGrid        = document.getElementById("bot-grid");
    const botPlaceholder = document.getElementById("bot-placeholder");
    const logContainer   = document.getElementById("log-container");
    const connIndicator  = document.getElementById("connection-indicator");
    const progressWrap   = document.getElementById("progress-wrap");
    const progressBar    = document.getElementById("progress-bar");
    const progressText   = document.getElementById("progress-text");
    const sessionTimer   = document.getElementById("session-timer");
    const btnClearLogs   = document.getElementById("btn-clear-logs");
    const btnDownloadLogs = document.getElementById("btn-download-logs");

    const inputs = {
        meetingId:    document.getElementById("meeting-id"),
        passcode:     document.getElementById("passcode"),
        threadCount:  document.getElementById("thread-count"),
        numBots:      document.getElementById("num-bots"),
        customName:   document.getElementById("custom-name"),
        chatRecipient: document.getElementById("chat-recipient"),
        chatMessage:  document.getElementById("chat-message"),
        autoRestart:  document.getElementById("auto-restart"),
        restartDelay: document.getElementById("restart-delay"),
        useProxies:   document.getElementById("use-proxies"),
        waitingRoomTimeout: document.getElementById("waiting-room-timeout"),
        reactionCount: document.getElementById("reaction-count"),
        reactionDelay: document.getElementById("reaction-delay"),
        persistMode:  document.getElementById("persist-mode"),
    };

    const screenshotGrid = document.getElementById("screenshot-grid");
    const screenshotPlaceholder = document.getElementById("screenshot-placeholder");

    const statEls = {
        joined:  document.getElementById("stat-joined"),
        failed:  document.getElementById("stat-failed"),
        total:   document.getElementById("stat-total"),
        avg:     document.getElementById("stat-avg"),
        fastest: document.getElementById("stat-fastest"),
        slowest: document.getElementById("stat-slowest"),
    };

    // ── State ────────────────────────────────────────────────────────
    let timerInterval = null;
    let timerStart    = null;
    let cycleCount    = 0;

    // ── Auto-restart toggle ───────────────────────────────────────────
    inputs.autoRestart.addEventListener("change", () => {
        socket.emit("set_auto_restart", {
            enabled: inputs.autoRestart.checked,
            delay: parseInt(inputs.restartDelay.value) || 5,
        });
    });
    inputs.restartDelay.addEventListener("change", () => {
        if (inputs.autoRestart.checked) {
            socket.emit("set_auto_restart", {
                enabled: true,
                delay: parseInt(inputs.restartDelay.value) || 5,
            });
        }
    });

    // ── Pre-populate form ────────────────────────────────────────────
    fetch("/api/defaults")
        .then(r => r.json())
        .then(d => {
            if (d.meeting_id)   inputs.meetingId.value   = d.meeting_id;
            if (d.passcode)     inputs.passcode.value    = d.passcode;
            if (d.thread_count) inputs.threadCount.value  = d.thread_count;
        })
        .catch(() => {});

    // ── Connection status ────────────────────────────────────────────
    socket.on("connect", () => {
        connIndicator.textContent = "Connected";
        connIndicator.className   = "connected";
    });
    socket.on("disconnect", () => {
        connIndicator.textContent = "Disconnected";
        connIndicator.className   = "disconnected";
    });

    // ── Form lock ────────────────────────────────────────────────────
    function setFormLocked(locked) {
        Object.entries(inputs).forEach(([key, el]) => {
            // Keep auto-restart controls always interactive
            if (key === "autoRestart" || key === "restartDelay" || key === "useProxies") return;
            el.disabled = locked;
        });
    }

    // ── Timer ────────────────────────────────────────────────────────
    function startTimer() {
        timerStart = Date.now();
        sessionTimer.classList.remove("hidden");
        sessionTimer.textContent = "00:00";
        timerInterval = setInterval(() => {
            const elapsed = Math.floor((Date.now() - timerStart) / 1000);
            const m = String(Math.floor(elapsed / 60)).padStart(2, "0");
            const s = String(elapsed % 60).padStart(2, "0");
            sessionTimer.textContent = m + ":" + s;
        }, 1000);
    }

    function stopTimer() {
        if (timerInterval) {
            clearInterval(timerInterval);
            timerInterval = null;
        }
    }

    // ── Toast notifications ──────────────────────────────────────────
    function showToast(message, type) {
        const container = document.getElementById("toast-container");
        const toast = document.createElement("div");
        toast.className = "toast " + (type || "info");
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    }

    // ── Progress bar ─────────────────────────────────────────────────
    function updateProgress(succeeded, failed, total) {
        if (total === 0) return;
        const done = succeeded + failed;
        const pct = Math.round((done / total) * 100);
        progressBar.style.width = pct + "%";
        progressText.textContent = done + " / " + total + "  (" + pct + "%)";
    }

    // ── Start button ─────────────────────────────────────────────────
    btnStart.addEventListener("click", () => {
        const numBots = parseInt(inputs.numBots.value) || 1;

        // Collect selected reactions
        const selectedReactions = [];
        document.querySelectorAll("#reaction-checkboxes input:checked").forEach(cb => {
            selectedReactions.push(cb.value);
        });

        const payload = {
            meeting_id:   inputs.meetingId.value,
            passcode:     inputs.passcode.value,
            thread_count: parseInt(inputs.threadCount.value) || 1,
            num_bots:     numBots,
            custom_name:  inputs.customName.value,
            use_proxies:  inputs.useProxies.checked,
            chat_recipient: inputs.chatRecipient.value,
            chat_message: inputs.chatMessage.value,
            waiting_room_timeout: parseInt(inputs.waitingRoomTimeout.value) || 60,
            reactions:    selectedReactions,
            reaction_count: parseInt(inputs.reactionCount.value) || 0,
            reaction_delay: parseFloat(inputs.reactionDelay.value) || 1.0,
            persist_mode: inputs.persistMode.checked,
        };

        if (!payload.meeting_id || !payload.passcode) {
            showToast("Meeting ID and Passcode are required.", "error");
            return;
        }

        // Sync auto-restart setting before launching
        socket.emit("set_auto_restart", {
            enabled: inputs.autoRestart.checked,
            delay: parseInt(inputs.restartDelay.value) || 5,
        });

        socket.emit("start", payload);
        btnStart.disabled = true;
        btnStop.disabled  = false;
        setFormLocked(true);

        // Show progress bar
        progressWrap.classList.remove("hidden");
        progressBar.style.width = "0%";
        progressText.textContent = "0%";

        // Start timer
        startTimer();

        // Build bot cards
        botGrid.innerHTML = "";
        botPlaceholder.style.display = "none";

        for (let i = 0; i < numBots; i++) {
            const card = document.createElement("div");
            card.className = "bot-card pending";
            card.id = "bot-" + i;
            card.textContent = "Bot " + (i + 1);
            botGrid.appendChild(card);
        }
    });

    // ── Stop button ──────────────────────────────────────────────────
    btnStop.addEventListener("click", () => {
        socket.emit("stop");
        btnStop.disabled = true;
        showToast("Stop signal sent.", "info");
    });

    // ── Server status responses ──────────────────────────────────────
    socket.on("status", (data) => {
        if (!data.ok) {
            showToast(data.message, "error");
            appendLog("[ERROR] " + data.message, "ERROR");
            btnStart.disabled = false;
            btnStop.disabled  = true;
            setFormLocked(false);
            stopTimer();
        }
    });

    // ── Live stats ───────────────────────────────────────────────────
    socket.on("stats_update", (s) => {
        statEls.joined.textContent  = s.succeeded;
        statEls.failed.textContent  = s.failed;
        statEls.total.textContent   = s.total;
        statEls.avg.textContent     = s.avg_time ? s.avg_time.toFixed(1) + "s" : "—";
        statEls.fastest.textContent = s.fastest  ? s.fastest.toFixed(1) + "s"  : "—";
        statEls.slowest.textContent = s.slowest  ? s.slowest.toFixed(1) + "s"  : "—";

        // Update progress bar
        updateProgress(s.succeeded, s.failed, s.total);

        // Detect new restart cycle: stats reset while still running
        if (s.running && s.cycle > cycleCount) {
            cycleCount = s.cycle;
            showToast("Auto-restart: cycle " + cycleCount + " started.", "info");
            // Rebuild bot cards for new cycle
            const numBots = s.total;
            botGrid.innerHTML = "";
            for (let i = 0; i < numBots; i++) {
                const card = document.createElement("div");
                card.className = "bot-card pending";
                card.id = "bot-" + i;
                card.textContent = "Bot " + (i + 1);
                botGrid.appendChild(card);
            }
        }

        // Session ended
        if (!s.running && btnStart.disabled) {
            btnStart.disabled = false;
            btnStop.disabled  = true;
            setFormLocked(false);
            stopTimer();
            cycleCount = 0;

            if (s.total > 0 && (s.succeeded + s.failed) >= s.total) {
                if (s.failed === 0) {
                    showToast("All " + s.succeeded + " bots joined successfully!", "success");
                } else {
                    showToast(s.succeeded + " joined, " + s.failed + " failed.", s.failed > s.succeeded ? "error" : "info");
                }
            }
        }

        // Sync bot card statuses
        if (s.bot_statuses) {
            for (const [id, status] of Object.entries(s.bot_statuses)) {
                const card = document.getElementById("bot-" + id);
                if (card) card.className = "bot-card " + status;
            }
        }
    });

    // ── Individual bot update (fast path) ────────────────────────────
    socket.on("bot_update", (data) => {
        const card = document.getElementById("bot-" + data.bot_id);
        if (!card) return;
        card.className = "bot-card " + data.status;
        if (data.status === "joined" && data.elapsed > 0) {
            card.textContent = "Bot " + (data.bot_id + 1) + " (" + data.elapsed + "s)";
        }
    });

    // ── Log stream ───────────────────────────────────────────────────
    socket.on("log", (data) => {
        appendLog(data.message, data.level);
    });

    function appendLog(message, level) {
        const line = document.createElement("div");
        line.className = "log-line " + (level || "INFO");
        line.textContent = message;
        logContainer.appendChild(line);
        logContainer.scrollTop = logContainer.scrollHeight;

        while (logContainer.children.length > 500) {
            logContainer.removeChild(logContainer.firstChild);
        }
    }

    // ── Clear logs ───────────────────────────────────────────────────
    btnClearLogs.addEventListener("click", () => {
        logContainer.innerHTML = "";
    });

    // ── Download logs ────────────────────────────────────────────────
    btnDownloadLogs.addEventListener("click", () => {
        const lines = [];
        logContainer.querySelectorAll(".log-line").forEach(el => {
            lines.push(el.textContent);
        });
        if (lines.length === 0) {
            showToast("No logs to download.", "info");
            return;
        }
        const blob = new Blob([lines.join("\n")], { type: "text/plain" });
        const url  = URL.createObjectURL(blob);
        const a    = document.createElement("a");
        a.href     = url;
        a.download = "bot-log-" + new Date().toISOString().slice(0, 19).replace(/:/g, "-") + ".txt";
        a.click();
        URL.revokeObjectURL(url);
        showToast("Log file downloaded.", "success");
    });

    // ── Screenshot viewer ──────────────────────────────────────────
    function loadScreenshots() {
        fetch("/api/screenshots")
            .then(r => r.json())
            .then(data => {
                screenshotGrid.innerHTML = "";
                if (!data.length) {
                    screenshotPlaceholder.style.display = "";
                    return;
                }
                screenshotPlaceholder.style.display = "none";
                data.slice(-30).forEach(s => {
                    const thumb = document.createElement("div");
                    thumb.className = "screenshot-thumb";
                    thumb.innerHTML = '<img src="/screenshots/' + s.filename + '" alt="' + s.label + '" loading="lazy"><span>' + s.label + '</span>';
                    thumb.addEventListener("click", () => {
                        window.open("/screenshots/" + s.filename, "_blank");
                    });
                    screenshotGrid.appendChild(thumb);
                });
            })
            .catch(() => {});
    }

    document.getElementById("btn-refresh-screenshots").addEventListener("click", loadScreenshots);

    // Auto-refresh screenshots when a new one arrives
    socket.on("screenshot_update", () => {
        loadScreenshots();
    });
});
