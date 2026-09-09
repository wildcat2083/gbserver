(() => {
  "use strict";

  const WIDTH = 160, HEIGHT = 144;
  const MSG_VIDEO = 1;
  const MSG_AUDIO = 2;

  // Room-aware requests: when this page is a private session (/r/<code>),
  // every API/WS call needs the same "/r/<code>" prefix so it talks to that
  // room's Emulator instead of the default shared game.
  const ROOM = document.body.dataset.room || null;
  const ROOM_MISSING = document.body.dataset.roomMissing === "true";
  const SHARED_DISABLED_AT_LOAD = document.body.dataset.sharedDisabled === "true";

  function apiPath(path) {
    return ROOM ? `/r/${ROOM}${path}` : path;
  }

  // Identifies this browser page-load to the server, so Settings actions
  // (plain HTTP requests) can be checked against controller status the
  // same way WebSocket button input already is - sent on the WS handshake
  // and as a header on every mutating Settings request.
  function generateClientId() {
    try {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    } catch (_) { /* fall through to the fallback below */ }
    return "cid-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
  }
  const CLIENT_ID = generateClientId();
  // Must match KICK_CLOSE_CODE in config.py - the WebSocket close code
  // used specifically for a deliberate admin kick, so it can be told
  // apart from any other disconnect reason (see ws.onclose below).
  const KICK_CLOSE_CODE = 4001;
  // Must match SHARED_DISABLED_CLOSE_CODE in config.py.
  const SHARED_DISABLED_CLOSE_CODE = 4002;

  // Whether THIS connection currently holds control. Server-enforced (a
  // rejected press/release is simply ignored server-side) - this flag only
  // gates the client's own UI/input so a viewer doesn't get misleading
  // visual feedback or send input that will just be dropped.
  let isController = false;

  const canvas = document.getElementById("screen");
  const ctx = canvas.getContext("2d", { alpha: false });
  const canvasGL = document.getElementById("screenGL");
  const imageData = ctx.createImageData(WIDTH, HEIGHT);

  // --- WebGL video filters (Off / Smooth / Smart smooth) -----------------
  //
  // "Smooth" is a single call to the GPU's own built-in bilinear texture
  // sampling - not really custom logic, just asking for LINEAR instead of
  // NEAREST filtering. It blurs the whole image uniformly, flat areas
  // included, which is the tradeoff of plain bilinear.
  //
  // "Smart smooth" is an original technique written for this project - it
  // is NOT a port of HQ2x (a specific, well-known pattern-matching
  // algorithm with its own large reference lookup table) or any other
  // named filter. The goal is similar - soften diagonal lines and curves
  // while keeping flat regions and strong edges crisp - but the approach
  // here is simpler: for each output pixel, blend its 4 nearest source
  // texels using standard bilinear distance weights, then reduce the
  // weight of any texel that differs a lot from the four-texel average
  // (i.e. likely sits on the far side of an edge). That biases the blend
  // toward whichever texel(s) actually match the local neighborhood
  // instead of always mixing uniformly - producing a softer look than
  // Off, but with noticeably less blur in flat color areas than Smooth.

  const FILTER_KEY = "gbserver.videoFilter";
  let currentFilter = "off";
  const SMOOTHNESS_KEY = "gbserver.smartSmoothness";
  let smartSmoothness = 2.0; // matches the value this used to be hardcoded at
  const THEME_KEY = "gbserver.theme";
  const VALID_THEMES = ["dmg", "pocket", "grape", "light-yellow", "dark"];
  let glState = null; // set up lazily on first non-"off" selection

  const GL_VERTEX_SRC = `
    attribute vec2 aPosition;
    varying vec2 vTexCoord;
    void main() {
      vTexCoord = aPosition * 0.5 + 0.5;
      vTexCoord.y = 1.0 - vTexCoord.y;
      gl_Position = vec4(aPosition, 0.0, 1.0);
    }
  `;

  const GL_FRAGMENT_SMOOTH_SRC = `
    precision mediump float;
    varying vec2 vTexCoord;
    uniform sampler2D uTexture;
    void main() {
      gl_FragColor = texture2D(uTexture, vTexCoord);
    }
  `;

  // Original "smart smooth" technique - see comment above. Blends each
  // pixel's 4 nearest source texels, biased two ways: toward whichever
  // corners lie along the LOWER-CONTRAST diagonal (the direction an edge
  // is actually running, if there is one - blending harder along it and
  // softer across it is what turns a jagged staircase diagonal into a
  // smooth one, rather than just blurring uniformly near any edge), and
  // away from any single corner that's a strong outlier vs the 4-texel
  // average (keeps flat color regions crisp instead of softened
  // everywhere).
  //
  // The diagonal-contrast check looks one step beyond each end of both
  // diagonals, not just the 2x2 cell alone - a genuine edge running
  // along a diagonal should stay comparatively flat across a short run
  // of pixels, not just the two immediately adjacent ones, so a single
  // anomalous pixel at one corner is far less likely to flip the
  // decision than a narrower 2-pixel comparison would be. This is the
  // same general principle real HQx-style filters use - favor wider
  // neighborhood context over a single adjacent pixel-pair when judging
  // edge direction - written as an original implementation here, not a
  // port of any specific existing shader.
  const GL_FRAGMENT_SMART_SRC = `
    precision mediump float;
    varying vec2 vTexCoord;
    uniform sampler2D uTexture;
    uniform vec2 uTextureSize;
    uniform float uSmoothness;
    void main() {
      vec2 texel = 1.0 / uTextureSize;
      vec2 texelPos = vTexCoord * uTextureSize;
      vec2 base = (floor(texelPos - 0.5) + 0.5) * texel;
      vec2 frac = fract(texelPos - 0.5);

      vec3 c00 = texture2D(uTexture, base).rgb;
      vec3 c10 = texture2D(uTexture, base + vec2(texel.x, 0.0)).rgb;
      vec3 c01 = texture2D(uTexture, base + vec2(0.0, texel.y)).rgb;
      vec3 c11 = texture2D(uTexture, base + texel).rgb;

      // One extra sample beyond each end of both diagonals, purely to
      // judge edge direction with wider context - not otherwise used in
      // the blend itself.
      vec3 preTL  = texture2D(uTexture, base - texel).rgb;
      vec3 postBR = texture2D(uTexture, base + texel * 2.0).rgb;
      vec3 preTR  = texture2D(uTexture, base + vec2(texel.x * 2.0, -texel.y)).rgb;
      vec3 postBL = texture2D(uTexture, base + vec2(-texel.x, texel.y * 2.0)).rgb;

      float w00 = (1.0 - frac.x) * (1.0 - frac.y);
      float w10 = frac.x * (1.0 - frac.y);
      float w01 = (1.0 - frac.x) * frac.y;
      float w11 = frac.x * frac.y;

      // Which diagonal has LESS contrast across its whole run - c00/c11
      // ("\\") or c10/c01 ("/")? The lower-contrast one is the more
      // likely edge direction, so bias the blend toward it and away
      // from the other.
      float diagTLBR = length(c00 - c11) + 0.5 * (length(preTL - c00) + length(c11 - postBR));
      float diagTRBL = length(c10 - c01) + 0.5 * (length(preTR - c10) + length(c01 - postBL));
      float diagDiff = diagTRBL - diagTLBR;  // positive => "\\" is smoother
      float bias = clamp(diagDiff * 4.0, -1.0, 1.0);
      w00 *= 1.0 + max(bias, 0.0) * uSmoothness;
      w11 *= 1.0 + max(bias, 0.0) * uSmoothness;
      w10 *= 1.0 + max(-bias, 0.0) * uSmoothness;
      w01 *= 1.0 + max(-bias, 0.0) * uSmoothness;

      vec3 avg = (c00 + c10 + c01 + c11) * 0.25;
      float d00 = 1.0 - clamp(length(c00 - avg) * 3.0, 0.0, 1.0);
      float d10 = 1.0 - clamp(length(c10 - avg) * 3.0, 0.0, 1.0);
      float d01 = 1.0 - clamp(length(c01 - avg) * 3.0, 0.0, 1.0);
      float d11 = 1.0 - clamp(length(c11 - avg) * 3.0, 0.0, 1.0);

      w00 *= mix(1.0, d00, 0.6);
      w10 *= mix(1.0, d10, 0.6);
      w01 *= mix(1.0, d01, 0.6);
      w11 *= mix(1.0, d11, 0.6);

      float wsum = w00 + w10 + w01 + w11;
      gl_FragColor = vec4((c00 * w00 + c10 * w10 + c01 * w01 + c11 * w11) / max(wsum, 0.0001), 1.0);
    }
  `;

  function compileShader(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      console.error("Shader compile error:", gl.getShaderInfoLog(shader));
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  }

  function buildProgram(gl, fragmentSrc) {
    const vs = compileShader(gl, gl.VERTEX_SHADER, GL_VERTEX_SRC);
    const fs = compileShader(gl, gl.FRAGMENT_SHADER, fragmentSrc);
    if (!vs || !fs) return null;
    const program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.error("Program link error:", gl.getProgramInfoLog(program));
      return null;
    }
    return program;
  }

  function ensureGL() {
    if (glState) return glState;
    const gl = canvasGL.getContext("webgl") || canvasGL.getContext("experimental-webgl");
    if (!gl) {
      console.error("WebGL not available - falling back to Off filter");
      return null;
    }
    const smoothProgram = buildProgram(gl, GL_FRAGMENT_SMOOTH_SRC);
    const smartProgram = buildProgram(gl, GL_FRAGMENT_SMART_SRC);
    if (!smoothProgram || !smartProgram) return null;

    // Attribute/uniform locations never change once a program is linked -
    // looking them up fresh on every single frame (up to 60x/sec) was
    // real, avoidable per-frame overhead that only existed on this
    // filtered path, not the plain 2D canvas one. That extra main-thread
    // work per frame was the likely cause of audio drifting slightly
    // behind video specifically when a filter was active - cached here,
    // once, instead.
    const smoothLocs = {
      pos: gl.getAttribLocation(smoothProgram, "aPosition"),
    };
    const smartLocs = {
      pos: gl.getAttribLocation(smartProgram, "aPosition"),
      size: gl.getUniformLocation(smartProgram, "uTextureSize"),
      smoothness: gl.getUniformLocation(smartProgram, "uSmoothness"),
    };

    const quadBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);

    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    glState = {
      gl, smoothProgram, smartProgram, smoothLocs, smartLocs, quadBuffer, texture,
      lastTexFilterMode: null, // also cached, so texParameteri isn't reset every frame either
    };
    return glState;
  }

  function renderFilteredFrame(pixelBytes) {
    const state = ensureGL();
    if (!state) {
      currentFilter = "off"; // WebGL unavailable - silently fall back
      return false;
    }
    const { gl, smoothProgram, smartProgram, smoothLocs, smartLocs, quadBuffer, texture } = state;
    const isSmart = currentFilter === "smart";
    const program = isSmart ? smartProgram : smoothProgram;
    const locs = isSmart ? smartLocs : smoothLocs;

    gl.viewport(0, 0, canvasGL.width, canvasGL.height);
    gl.useProgram(program);

    gl.bindTexture(gl.TEXTURE_2D, texture);
    const filterMode = isSmart ? gl.NEAREST : gl.LINEAR;
    if (state.lastTexFilterMode !== filterMode) {
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filterMode);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filterMode);
      state.lastTexFilterMode = filterMode;
    }
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, WIDTH, HEIGHT, 0, gl.RGBA, gl.UNSIGNED_BYTE, pixelBytes);

    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
    gl.enableVertexAttribArray(locs.pos);
    gl.vertexAttribPointer(locs.pos, 2, gl.FLOAT, false, 0, 0);

    if (isSmart) {
      gl.uniform2f(locs.size, WIDTH, HEIGHT);
      gl.uniform1f(locs.smoothness, smartSmoothness);
    }

    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    return true;
  }

  function applyFilterVisibility() {
    if (currentFilter === "off") {
      canvas.hidden = false;
      canvasGL.hidden = true;
    } else {
      canvas.hidden = true;
      canvasGL.hidden = false;
    }
  }

  function applyTheme(themeId) {
    // "dmg" needs no class at all - it's just the plain :root defaults
    // with nothing overriding them, so removing every theme-* class
    // covers that case for free rather than needing its own branch.
    document.body.classList.remove(...VALID_THEMES.map((t) => `theme-${t}`));
    if (themeId !== "dmg") {
      document.body.classList.add(`theme-${themeId}`);
    }
  }

  function loadTheme() {
    let theme = "dmg";
    try {
      const stored = localStorage.getItem(THEME_KEY);
      if (VALID_THEMES.includes(stored)) theme = stored;
    } catch (_) { /* localStorage unavailable - default to "dmg" */ }
    const select = document.getElementById("themeSelect");
    if (select) select.value = theme;
    applyTheme(theme);
  }

  function bindThemeSelect() {
    const select = document.getElementById("themeSelect");
    if (!select) return;
    select.addEventListener("change", () => {
      const theme = select.value;
      applyTheme(theme);
      try {
        localStorage.setItem(THEME_KEY, theme);
      } catch (_) { /* ignore - see loadTheme */ }
    });
  }

  function loadVideoFilter() {
    try {
      const stored = localStorage.getItem(FILTER_KEY);
      if (stored === "off" || stored === "smooth" || stored === "smart") {
        currentFilter = stored;
      }
    } catch (_) { /* localStorage unavailable - default to "off" */ }
    const select = document.getElementById("filterSelect");
    if (select) select.value = currentFilter;
    applyFilterVisibility();
  }

  function bindVideoFilterSelect() {
    const select = document.getElementById("filterSelect");
    if (!select) return;
    select.addEventListener("change", () => {
      currentFilter = select.value;
      try {
        localStorage.setItem(FILTER_KEY, currentFilter);
      } catch (_) { /* ignore - see loadVideoFilter */ }
      applyFilterVisibility();
    });
  }

  function loadSmoothness() {
    try {
      const stored = parseFloat(localStorage.getItem(SMOOTHNESS_KEY));
      if (!isNaN(stored)) smartSmoothness = stored;
    } catch (_) { /* localStorage unavailable - default stays in place */ }
    const slider = document.getElementById("smoothnessRange");
    const value = document.getElementById("smoothnessValue");
    if (slider) slider.value = smartSmoothness;
    if (value) value.textContent = smartSmoothness.toFixed(1);
  }

  function bindSmoothnessSlider() {
    const slider = document.getElementById("smoothnessRange");
    const value = document.getElementById("smoothnessValue");
    if (!slider) return;
    slider.addEventListener("input", () => {
      smartSmoothness = parseFloat(slider.value);
      if (value) value.textContent = smartSmoothness.toFixed(1);
      try {
        localStorage.setItem(SMOOTHNESS_KEY, String(smartSmoothness));
      } catch (_) { /* ignore - see loadSmoothness */ }
    });
  }

  // Double-click the screen to toggle fullscreen. Vendor-prefixed fallback
  // covers older Safari, which hasn't adopted the unprefixed API.
  function activeCanvas() {
    // Fullscreen needs to target whichever canvas is actually visible -
    // #screen when no filter is active, #screenGL when Smooth/Smart smooth
    // is selected (see applyFilterVisibility).
    return currentFilter === "off" ? canvas : canvasGL;
  }

  function toggleFullscreen() {
    const fsElement =
      document.fullscreenElement || document.webkitFullscreenElement;
    if (fsElement) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      return;
    }
    const target = activeCanvas();
    const request = target.requestFullscreen || target.webkitRequestFullscreen;
    if (request) {
      request.call(target).catch(() => {
        // Some browsers reject if not triggered by a direct user gesture -
        // dblclick always counts, so this is mostly a defensive no-op.
      });
    }
  }

  function bindFullscreen() {
    canvas.addEventListener("dblclick", toggleFullscreen);
    canvasGL.addEventListener("dblclick", toggleFullscreen);
  }

  // Fills the screen with the same off-color as the canvas's own CSS
  // background (--gb-screen-bg), so stopping emulation looks like the LCD
  // powering off rather than just freezing on the last frame.
  function clearScreen() {
    ctx.fillStyle = "#000000";
    ctx.fillRect(0, 0, WIDTH, HEIGHT);

    // With a filter active, #screen (above) is hidden and #screenGL is
    // what's actually showing - clearing only the 2D canvas had no visible
    // effect in that case, so the WebGL canvas just kept showing whatever
    // frame was last rendered before Stop was clicked. Clear that one too,
    // using the same off color as --gb-screen-bg in style.css, but only
    // if a GL context actually exists yet (lazily created on first filter
    // selection - nothing to clear if a filter was never turned on this
    // session).
    if (glState) {
      const { gl } = glState;
      gl.viewport(0, 0, canvasGL.width, canvasGL.height);
      gl.clearColor(0, 0, 0, 1.0);
      gl.clear(gl.COLOR_BUFFER_BIT);
    }
  }

  // Video frames now arrive zlib/deflate-compressed (see app.py) - GB
  // screens are mostly flat color blocks, so this shrinks them a lot for
  // very little CPU cost. Uses the browser's native DecompressionStream,
  // so no extra library is needed - available in current Chrome/Firefox/
  // Safari.
  async function decompressDeflate(bytes) {
    const stream = new DecompressionStream("deflate");
    const writer = stream.writable.getWriter();
    writer.write(bytes);
    writer.close();
    const reader = stream.readable.getReader();
    const chunks = [];
    let totalLength = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      chunks.push(value);
      totalLength += value.length;
    }
    const result = new Uint8Array(totalLength);
    let offset = 0;
    for (const chunk of chunks) {
      result.set(chunk, offset);
      offset += chunk.length;
    }
    return result;
  }

  const statusEl = document.getElementById("status");
  const liveDot = document.querySelector(".dot");
  const romNameEl = document.getElementById("romName");
  const romFileEl = document.getElementById("romFile");
  const romSelectEl = document.getElementById("romSelect");
  const romPlayBtn = document.getElementById("romPlayBtn");
  const romResumeBtn = document.getElementById("romResumeBtn");
  const engineRow = document.getElementById("engineRow");
  const engineSelect = document.getElementById("engineSelect");
  const romDeleteBtn = document.getElementById("romDeleteBtn");
  const romSearchEl = document.getElementById("romSearch");
  const audioBadge = document.getElementById("audioBadge");
  const storageText = document.getElementById("storageText");
  const storageDetail = document.getElementById("storageDetail");
  const uploadBar = document.getElementById("uploadBar");
  const uploadMsg = document.getElementById("uploadMsg");
  const settingsBtn = document.getElementById("settingsBtn");
  const settingsClose = document.getElementById("settingsClose");
  const settingsPanel = document.getElementById("settingsPanel");
  const settingsBackdrop = document.getElementById("settingsBackdrop");
  const chatBtn = document.getElementById("chatBtn");
  const chatClose = document.getElementById("chatClose");
  const chatPanel = document.getElementById("chatPanel");
  const chatBackdrop = document.getElementById("chatBackdrop");
  const helpBtn = document.getElementById("helpBtn");
  const helpClose = document.getElementById("helpClose");
  const helpPanel = document.getElementById("helpPanel");
  const helpBackdrop = document.getElementById("helpBackdrop");
  const cheatPanel = document.getElementById("cheat-panel");
  const cheatCodesInput = document.getElementById("cheat-codes-input");
  const cheatApplyBtn = document.getElementById("cheat-apply-btn");
  const cheatClearBtn = document.getElementById("cheat-clear-btn");
  const cheatCloseBtn = document.getElementById("cheat-close-btn");
  const cheatError = document.getElementById("cheat-error");
  const cheatActiveList = document.getElementById("cheat-active-list");
  const chatMessages = document.getElementById("chatMessages");
  const chatForm = document.getElementById("chatForm");
  const chatInput = document.getElementById("chatInput");
  const chatNameInput = document.getElementById("chatNameInput");
  const bufferRange = document.getElementById("bufferRange");
  const bufferValue = document.getElementById("bufferValue");
  const hapticToggle = document.getElementById("hapticToggle");
  const saveInfo = document.getElementById("saveInfo");
  const saveMsg = document.getElementById("saveMsg");
  const saveNow = document.getElementById("saveNow");
  const saveDownload = document.getElementById("saveDownload");
  const saveDelete = document.getElementById("saveDelete");
  const saveFileEl = document.getElementById("saveFile");
  const stopBtn = document.getElementById("stopBtn");
  const fastForwardBtn = document.getElementById("fastForwardBtn");
  const requestControlBtn = document.getElementById("requestControlBtn");
  const grantControlBtn = document.getElementById("grantControlBtn");
  const stopMsg = document.getElementById("stopMsg");

  let SAMPLE_RATE = 24000; // overwritten by /api/config
  let ws = null;
  let wsReady = false;
  let settingsOpen = false;
  let chatOpen = false;
  let helpOpen = false;
  let cheatPanelOpen = false;
  let uploading = false;
  let hapticsEnabled = true;

  const HAPTIC_KEY = "gbserver.haptics";
  const MUTE_KEY = "gbserver.muted";
  const HAPTIC_MS = 12;

  // --- Status dot / text ----------------------------------------------------

  function setStatus(text, live) {
    statusEl.textContent = text;
    liveDot.classList.toggle("live", !!live);
  }

  // --- Audio playback (Web Audio API, streaming int8 PCM) -------------------

  let audioCtx = null;
  let nextStartTime = 0;
  let audioEnabled = false;
  let audioMuted = false; // separate from audioEnabled above - that's "has the browser's autoplay
                          // lock been satisfied yet" (one-time, never goes back to false); this is
                          // the actual, reversible mute toggle

  // Small safety cushion added whenever the schedule is (re)established -
  // without this, nextStartTime always sits exactly at audioCtx.currentTime
  // with zero slack, so any brief main-thread delay (a GC pause, a slow
  // video frame decompressing, a burst of WebSocket messages) immediately
  // triggers the "fell behind, skip ahead to now" fallback below, which
  // silently drops whatever time span was missed - heard as small gaps/
  // stutters in the audio, distinct from the waveform-continuity clicks
  // the smoothing filter and (previously) the declick fade dealt with.
  // Verified with a scheduling simulation against realistic jitter: zero
  // margin dropped ~18/200 chunks (~310ms lost); 60ms margin drops that
  // to ~4/200 (~30ms lost) - diminishing returns much past this, and more
  // margin means more added latency for what's an interactive stream.
  const SCHEDULE_LOOKAHEAD = 0.06;

  // Bounds drift in the OTHER direction from the lookahead margin above -
  // if audio ever arrives even slightly faster than real-time playback
  // consumes it, nextStartTime creeps further ahead of audioCtx.currentTime
  // on every chunk (nextStartTime += buffer.duration, with nothing ever
  // pulling it back down) and, left unchecked, that drift accumulates
  // without limit over a long enough session - heard as audio falling
  // further and further behind video, which has no such buffering to
  // drift in the first place. This caps how far ahead playback is allowed
  // to schedule; past this, the excess is treated the same as an
  // underrun - snapped back to "now", not gradually drained.
  const MAX_SCHEDULE_AHEAD = 0.25;

  // PyBoy's sound buffer is fixed at 8-bit (256 amplitude levels) - this is
  // a limitation of the emulator's audio core, not the sample rate. Raw
  // 8-bit playback has an audible "grainy" quantization texture. A simple
  // one-pole low-pass filter, applied here across the whole stream (state
  // persists between chunks so there's no seam at chunk boundaries), softens
  // those harsh steps without needing extra source resolution. 0 = no
  // smoothing (raw/grainier/brighter), 1 = max smoothing (duller highs).
  const SMOOTH_ALPHA = 0.35;
  let smoothPrevL = 0;
  let smoothPrevR = 0;
  // Denormal-float guard: as the filter decays toward silence (constant in
  // game audio - between notes, quiet passages) it passes through extremely
  // small non-zero values. Many CPUs handle denormal float math via a much
  // slower path than normal floats, and since this runs per-sample on the
  // main thread, that slowdown can stall message processing long enough to
  // blow past the scheduled audio window - heard as periodic dropouts.
  // Snapping negligibly-small values to exact 0 avoids the slow path.
  const DENORMAL_FLOOR = 1e-6;

  function ensureAudioContext() {
    if (audioCtx) return;
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: SAMPLE_RATE });
    nextStartTime = audioCtx.currentTime + SCHEDULE_LOOKAHEAD;
    audioEnabled = true;
  }

  function resetAudioSchedule() {
    if (audioCtx) nextStartTime = audioCtx.currentTime + SCHEDULE_LOOKAHEAD;
    smoothPrevL = 0;
    smoothPrevR = 0;
  }

  function playAudioChunk(int8Bytes) {
    if (!audioEnabled || !audioCtx || audioMuted) return;
    const nSamples = int8Bytes.length / 2;
    if (nSamples < 1) return;

    const buffer = audioCtx.createBuffer(2, nSamples, SAMPLE_RATE);
    const left = buffer.getChannelData(0);
    const right = buffer.getChannelData(1);
    for (let i = 0; i < nSamples; i++) {
      const rawL = int8Bytes[i * 2] / 128;
      const rawR = int8Bytes[i * 2 + 1] / 128;
      smoothPrevL += SMOOTH_ALPHA * (rawL - smoothPrevL);
      smoothPrevR += SMOOTH_ALPHA * (rawR - smoothPrevR);
      if (Math.abs(smoothPrevL) < DENORMAL_FLOOR) smoothPrevL = 0;
      if (Math.abs(smoothPrevR) < DENORMAL_FLOOR) smoothPrevR = 0;
      left[i] = smoothPrevL;
      right[i] = smoothPrevR;
    }

    // NOTE: there used to be a "declick" fade here (forcing the first/last
    // few samples of every chunk toward silence) - removed. The smoothing
    // filter above already carries its state across chunk boundaries, so
    // consecutive chunks are naturally continuous on their own; the fade
    // was overwriting that continuity with an artificial ramp-to-zero on
    // both sides of every boundary, which is itself a sharp discontinuity
    // (a real click) whenever the true signal wasn't already near zero
    // there - i.e. most of the time. Verified numerically: a signal
    // hovering around 0.45-0.5 got forced through 0.491 -> 0.238 -> 0.0
    // in 3 samples by the old fade, versus 0.491 -> 0.477 -> 0.45
    // naturally - the fade was the click, not the fix for one.

    const source = audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(audioCtx.destination);

    const now = audioCtx.currentTime;
    if (nextStartTime < now) nextStartTime = now + SCHEDULE_LOOKAHEAD;
    if (nextStartTime > now + MAX_SCHEDULE_AHEAD) nextStartTime = now + SCHEDULE_LOOKAHEAD;
    source.start(nextStartTime);
    nextStartTime += buffer.duration;
  }

  // --- WebSocket (video + audio in, button presses out) ---------------------

  function bindConnectGate() {
    const gate = document.getElementById("connectGate");
    const btn = document.getElementById("connectBtn");
    if (!gate || !btn) return;
    // Bound to the whole overlay, not just the button itself - easier to
    // hit on mobile, and it's still a genuine, deliberate click/tap
    // either way, which is all that actually matters here.
    gate.addEventListener("click", () => {
      gate.hidden = true;
      connectWS();
    }, { once: true });
  }

  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    setStatus("Connecting\u2026", false);
    ws = new WebSocket(`${proto}://${location.host}${apiPath("/ws")}?client_id=${encodeURIComponent(CLIENT_ID)}`);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      wsReady = true;
      setStatus("Linked", true);
    };
    ws.onclose = (event) => {
      wsReady = false;
      if (event.code === KICK_CLOSE_CODE) {
        // A deliberate admin kick, not a network drop or server restart -
        // reconnecting immediately would just undo the kick, so this is
        // the one disconnect reason that's treated as final rather than
        // transient.
        setStatus("Disconnected by an admin", false);
        return;
      }
      if (event.code === SHARED_DISABLED_CLOSE_CODE) {
        showSharedDisabledBanner();
        return;
      }
      setStatus("Reconnecting\u2026", false);
      setTimeout(connectWS, 1000);
    };
    ws.onerror = () => ws.close();

    ws.onmessage = async (event) => {
      if (typeof event.data === "string") {
        if (event.data.startsWith("controller:")) {
          // Always update, even if the value is the same as the current
          // default - a fresh viewer's first-ever message is "controller:0",
          // which matches isController's starting default of false, so a
          // change-only check here would skip ever calling
          // updateControllerUI() and leave the badge permanently hidden.
          isController = event.data === "controller:1";
          updateControllerUI();
        } else if (event.data.startsWith("viewers:")) {
          updateViewerCountUI(parseInt(event.data.slice("viewers:".length), 10) || 0);
        } else if (event.data.startsWith("chatmsg:")) {
          try {
            const entry = JSON.parse(event.data.slice("chatmsg:".length));
            appendChatMessage(entry);
          } catch (err) {
            console.error("Failed to parse chat message:", err);
          }
        } else if (event.data.startsWith("fastforward:")) {
          setFastForwardUI(event.data === "fastforward:1");
        } else if (event.data === "stopped") {
          // The controller (or whoever) stopped the game - clear the
          // screen here too, so a viewer who didn't click Stop themselves
          // doesn't stay frozen on the last frame.
          clearScreen();
        } else if (event.data === "controlrequested:1") {
          if (isController && grantControlBtn) grantControlBtn.hidden = false;
        } else if (event.data.startsWith("redirect:")) {
          // An admin moved this specific client to a fresh private room -
          // a full navigation, not just closing the socket, so there's no
          // lingering connection left behind trying to reconnect to the
          // old session (unlike a kick, this needs no close-code trickery
          // at all for that reason).
          const code = event.data.slice("redirect:".length);
          window.location.href = `/r/${code}`;
        }
        return;
      }
      const bytes = new Uint8Array(event.data);
      if (bytes.length < 1) return;

      const msgType = bytes[0];
      const payload = bytes.subarray(1);

      if (msgType === MSG_VIDEO) {
        try {
          const decompressed = await decompressDeflate(payload);
          if (decompressed.length === imageData.data.length) {
            if (currentFilter === "off") {
              imageData.data.set(decompressed);
              ctx.putImageData(imageData, 0, 0);
            } else if (!renderFilteredFrame(decompressed)) {
              // WebGL unavailable - renderFilteredFrame already reset
              // currentFilter to "off" and applyFilterVisibility() wasn't
              // called yet from here, so draw this frame the normal way
              // too, instead of a blank canvas until the next message.
              applyFilterVisibility();
              imageData.data.set(decompressed);
              ctx.putImageData(imageData, 0, 0);
            }
          }
        } catch (err) {
          console.error("Video frame decompression failed:", err);
        }
      } else if (msgType === MSG_AUDIO) {
        const signed = new Int8Array(payload.buffer, payload.byteOffset, payload.length);
        playAudioChunk(signed);
      }
    };
  }

  function sendInput(action, button) {
    if (!isController) return; // viewers' input is dropped server-side anyway; skip client-side too
    if (wsReady) ws.send(`${action}:${button}`);
  }

  function updateControllerUI() {
    const badge = document.getElementById("controlBadge");
    if (badge) {
      badge.hidden = false;
      badge.textContent = isController ? "In control" : "Viewing";
      badge.classList.toggle("mine", isController);
    }
    const notice = document.getElementById("viewerNotice");
    if (notice) notice.hidden = isController;
    const waitingNotice = document.getElementById("waitingForControlNotice");
    if (waitingNotice) waitingNotice.hidden = isController;
    // Grays out and disables the on-screen buttons for viewers (CSS
    // pointer-events:none), so a tap doesn't even trigger the press
    // animation/haptic when it wouldn't do anything. The equivalent
    // Settings actions are also grayed out via the same class - those are
    // additionally enforced server-side (see controller_check in app.py),
    // this is just matching UI, not the actual security boundary.
    document.body.classList.toggle("viewer-mode", !isController);

    if (requestControlBtn) requestControlBtn.hidden = isController;
    // Losing control (e.g. someone else was granted it) means any pending
    // request notice on THIS client is stale - hide it rather than leave
    // a "Grant" button that would now silently no-op server-side.
    if (!isController && grantControlBtn) grantControlBtn.hidden = true;
  }

  function updateViewerCountUI(count) {
    const badge = document.getElementById("viewerCountBadge");
    if (!badge) return;
    if (count <= 0) {
      // Nothing to say when nobody else is here - showing "0 watching"
      // would just be noise, not information.
      badge.hidden = true;
      return;
    }
    badge.hidden = false;
    badge.textContent = count === 1 ? "1 watching" : `${count} watching`;
  }

  let fastForwardActive = false;

  function setFastForwardUI(active) {
    fastForwardActive = active;
    if (!fastForwardBtn) return;
    fastForwardBtn.setAttribute("aria-pressed", active ? "true" : "false");
    fastForwardBtn.textContent = active
      ? "\u23e9 Fast forward (4x) \u2013 ON"
      : "\u23e9 Fast forward (4x)";
  }

  async function toggleFastForward() {
    const enabled = !fastForwardActive;
    try {
      const res = await fetch(apiPath("/api/fast-forward"), {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Client-Id": CLIENT_ID },
        body: JSON.stringify({ enabled }),
      });
      const data = await res.json();
      if (typeof data.enabled === "boolean") {
        setFastForwardUI(data.enabled);
      }
      // Other connected clients get the change via the "fastforward:"
      // WebSocket broadcast - this client applies it immediately from
      // the HTTP response instead of waiting on its own echo.
    } catch (_) {
      // Leave the button's state as-is; the next "fastforward:" message
      // (or a page refresh) will resync it if this request silently failed.
    }
  }

  function bindFastForward() {
    if (!fastForwardBtn) return;
    fastForwardBtn.addEventListener("click", toggleFastForward);
  }

  async function triggerReset() {
    if (!isController) return;  // matches the button's own dimmed/disabled state for viewers
    try {
      const res = await fetch(apiPath("/api/reset"), {
        method: "POST",
        headers: { "X-Client-Id": CLIENT_ID },
      });
      if (!res.ok) {
        const data = await res.json();
        console.error("Reset failed:", data.error || res.status);
      }
      // No local UI update needed here - the reset shows up as an
      // ordinary new video frame once it lands, the same as any other
      // in-game change; nothing about controller/fast-forward/etc.
      // status actually changes as a result of resetting.
    } catch (err) {
      console.error("Reset request failed:", err);
    }
  }

  function bindResetButton() {
    const btn = document.getElementById("resetBtn");
    if (!btn) return;
    btn.addEventListener("click", triggerReset);
  }

  function bindRequestControl() {
    if (requestControlBtn) {
      requestControlBtn.addEventListener("click", () => {
        if (wsReady) ws.send("requestcontrol:");
      });
    }
    if (grantControlBtn) {
      grantControlBtn.addEventListener("click", () => {
        if (wsReady) ws.send("grantcontrol:");
        grantControlBtn.hidden = true; // don't wait on the controller:X broadcast to hide it
      });
    }
  }

  // --- On-screen button handling (pointer + touch, matches their approach) --

  function hapticTap() {
    if (!hapticsEnabled || !navigator.vibrate) return;
    try { navigator.vibrate(HAPTIC_MS); } catch (_) { /* unsupported/denied */ }
  }

  function bindButtons() {
    document.addEventListener("contextmenu", (e) => e.preventDefault());

    // Tracks which button (if any) each currently-active pointer is
    // pressing, keyed by pointerId - centralized here on the document
    // rather than having each button independently track its own press
    // state via setPointerCapture. Per-element pointer capture is the
    // standard approach and usually fine, but has known edge-case
    // inconsistencies across mobile browsers/OS versions specifically
    // for closely-spaced touch targets - exactly what these circular
    // face buttons are, after being moved closer together earlier.
    // Determining which button a touch is over by its actual on-screen
    // position, re-checked on every move rather than delegated to
    // capture, sidesteps those quirks entirely - the same technique
    // real virtual-controller UIs commonly use for this exact reason.
    const activePointers = new Map(); // pointerId -> the button element currently pressed (or null)

    function buttonAt(x, y) {
      const el = document.elementFromPoint(x, y);
      return (el && el.closest(".btn[data-btn]")) || null;
    }

    function pressButton(btn) {
      btn.classList.add("pressed");
      hapticTap();
      sendInput("press", btn.dataset.btn);
    }

    function releaseButton(btn) {
      btn.classList.remove("pressed");
      sendInput("release", btn.dataset.btn);
    }

    function handlePointerDown(e) {
      const btn = buttonAt(e.clientX, e.clientY);
      if (!btn) return; // not a button press at all - leave the event alone for
                         // whatever else on the page it was actually meant for
                         // (the canvas, settings controls, text inputs, etc.)
      e.preventDefault();
      pressButton(btn);
      activePointers.set(e.pointerId, btn);
      startAudioAndHideHint();
    }

    function handlePointerMove(e) {
      if (!activePointers.has(e.pointerId)) return; // this pointer didn't start on a button
      const current = activePointers.get(e.pointerId);
      const btn = buttonAt(e.clientX, e.clientY);
      if (btn === current) return; // still over the same button (or still off all of them)
      // A finger sliding from one button to another - release the old,
      // press the new, a natural gesture on touchscreens specifically.
      if (current) releaseButton(current);
      if (btn) pressButton(btn);
      activePointers.set(e.pointerId, btn);
    }

    function handlePointerEnd(e) {
      const current = activePointers.get(e.pointerId);
      if (current) releaseButton(current);
      activePointers.delete(e.pointerId);
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("pointermove", handlePointerMove);
    document.addEventListener("pointerup", handlePointerEnd);
    document.addEventListener("pointercancel", handlePointerEnd);
  }

  // --- Keyboard controls ------------------------------------------------

  const KEY_MAP = {
    ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
    KeyZ: "a", KeyA: "a",
    KeyX: "b", KeyB: "b",
    ShiftLeft: "select", ShiftRight: "select", KeyQ: "select",
    Enter: "start", KeyW: "start",
  };
  const heldKeys = new Set();

  function bindKeyboard() {
    window.addEventListener("keydown", (e) => {
      const tag = document.activeElement && document.activeElement.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.code === "F1") {
        // Toggle, not hold-to-press like the game buttons below - only
        // fire once per physical press, not on every keyboard auto-repeat
        // event while held.
        e.preventDefault(); // stop the browser's own F1 help action
        if (!heldKeys.has(e.code)) {
          heldKeys.add(e.code);
          toggleFastForward();
        }
        return;
      }
      if (e.key === "*") {
        // Matches BGB's own convention (press * to reset) - a one-shot
        // action like fast-forward's toggle above, not hold-to-press, and
        // deliberately no confirmation dialog either, same reasoning as a
        // real hardware reset button: it's meant to be instant, not
        // interrupt gameplay with a modal.
        e.preventDefault();
        if (!heldKeys.has(e.code)) {
          heldKeys.add(e.code);
          triggerReset();
        }
        return;
      }
      const btn = KEY_MAP[e.code];
      if (!btn || heldKeys.has(e.code)) return;
      e.preventDefault();
      heldKeys.add(e.code);
      sendInput("press", btn);
      startAudioAndHideHint();
    });
    window.addEventListener("keyup", (e) => {
      const tag = document.activeElement && document.activeElement.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.code === "F1") {
        e.preventDefault();
        heldKeys.delete(e.code);
        return;
      }
      if (e.key === "*") {
        e.preventDefault();
        heldKeys.delete(e.code);
        return;
      }
      const btn = KEY_MAP[e.code];
      if (!btn) return;
      e.preventDefault();
      heldKeys.delete(e.code);
      sendInput("release", btn);
    });
  }

  // --- Bluetooth/USB gamepad controls (Xbox controller, etc.) -----------

  // Standard Gamepad API button indices (works for Xbox, PS, most modern pads)
  const GAMEPAD_BUTTON_MAP = {
    0: "a",       // A / Cross
    1: "b",       // B / Circle
    8: "select",  // View / Share / Back
    9: "start",   // Menu / Options / Start
    12: "up",
    13: "down",
    14: "left",
    15: "right",
  };
  const STICK_DEADZONE = 0.5; // left stick doubles as d-pad past this threshold
  const FAST_FORWARD_GAMEPAD_BUTTON = 5; // RB / R1 - unused by GAMEPAD_BUTTON_MAP above
  const RESET_GAMEPAD_BUTTON = 4; // LB / L1 - mirrors fast-forward's shoulder button placement
  const SETTINGS_GAMEPAD_BUTTON = 3; // Y / Triangle - unused by GAMEPAD_BUTTON_MAP above, and not a shoulder button so it can't be confused with fast-forward/reset
  const TURBO_A_GAMEPAD_BUTTON = 2; // X / Square - unused by GAMEPAD_BUTTON_MAP above
  const TURBO_INTERVAL_MS = 100; // ~10 toggles/sec (5 full press-release cycles/sec) - fast enough to feel like rapid-fire, slow enough that most games reliably register each individual press

  let gamepadIndex = null;
  const gamepadHeld = new Set(); // currently-pressed logical button names
  let ffGamepadWasPressed = false; // edge-detection so a held RB toggles once, not every frame
  let resetGamepadWasPressed = false; // same edge-detection, so a held LB resets once, not every frame
  let settingsGamepadWasPressed = false; // same edge-detection, for Y toggling the settings menu
  let turboAPhaseOn = false; // whether the current turbo cycle is in its "pressed" half
  let turboALastToggleTime = 0;
  // Edge-detection for menu-navigation buttons specifically - only
  // relevant while the settings menu is open, since that's the only time
  // these buttons mean "navigate the menu" rather than "game input".
  const menuNavWasPressed = { up: false, down: false, left: false, right: false, a: false, b: false };
  // Separate edge-detection for the Konami-code easter egg - deliberately
  // independent of menuNavWasPressed above and of whether the settings
  // menu is open at all, so the code can be entered on the controller at
  // any time, the same way it can be typed on the keyboard at any time.
  const konamiGamepadWasPressed = { up: false, down: false, left: false, right: false, a: false, b: false };
  // Separate edge-detection for the on-screen keyboard's own grid
  // navigation - kept independent of menuNavWasPressed (which now only
  // ever runs while the vkeyboard is CLOSED, see the restructured
  // pollGamepad below) rather than shared, so switching between panels
  // mid-press can't leave a stale "already held" flag behind and
  // silently eat the first press on whichever panel becomes active.
  const vkeyGamepadWasPressed = { up: false, down: false, left: false, right: false, a: false, b: false };
  // Same reasoning again, for the cheat panel's own button/textarea list
  // navigation.
  const cheatNavWasPressed = { up: false, down: false, left: false, right: false, a: false, b: false };

  function handleGamepadConnected(e) {
    gamepadIndex = e.gamepad.index;
    setStatus(`Linked \u00b7 ${e.gamepad.id.split("(")[0].trim()}`, true);
    startAudioAndHideHint();
  }

  function handleGamepadDisconnected(e) {
    if (e.gamepad.index !== gamepadIndex) return;
    gamepadIndex = null;
    for (const name of gamepadHeld) sendInput("release", name);
    gamepadHeld.clear();
  }

  function pressLogical(name) {
    if (gamepadHeld.has(name)) return;
    gamepadHeld.add(name);
    hapticTap();
    sendInput("press", name);
  }

  function releaseLogical(name) {
    if (!gamepadHeld.has(name)) return;
    gamepadHeld.delete(name);
    sendInput("release", name);
  }

  function pollGamepad() {
    if (gamepadIndex !== null) {
      const pads = navigator.getGamepads ? navigator.getGamepads() : [];
      const pad = pads[gamepadIndex];

      if (pad) {
        // Y toggles the settings menu regardless of whether it's
        // currently open or closed - checked before anything else below,
        // since it needs to work the same way either way.
        const settingsBtnState = pad.buttons[SETTINGS_GAMEPAD_BUTTON];
        const settingsIsDown = !!settingsBtnState && settingsBtnState.pressed;
        if (settingsIsDown && !settingsGamepadWasPressed) setSettingsOpen(!settingsOpen);
        settingsGamepadWasPressed = settingsIsDown;

        // Konami-code tracking for the cheat panel easter egg - also
        // runs unconditionally here (same reasoning as Y/settings just
        // above), so entering the code on a controller works regardless
        // of whether the settings menu or virtual keyboard happens to be
        // open at the time. Uses the same D-pad/A/B button indices as
        // menu navigation below (12-15, 0, 1) - standard-layout mapping.
        const konamiButtons = { up: 12, down: 13, left: 14, right: 15, a: 0, b: 1 };
        for (const [action, idx] of Object.entries(konamiButtons)) {
          const btn = pad.buttons[idx];
          const isDown = !!btn && btn.pressed;
          if (isDown && !konamiGamepadWasPressed[action]) feedKonamiBuffer(action);
          konamiGamepadWasPressed[action] = isDown;
        }

        if (isVkeyboardOpen()) {
          // Hoisted out to its own top-level branch, independent of
          // settingsOpen/cheatPanelOpen - the on-screen keyboard can now
          // be opened FROM either panel (a settings text field, or the
          // cheat panel's code textarea), so its own D-pad/A/B grid
          // navigation needs to keep working the same way regardless of
          // which panel is sitting underneath it.
          const navButtons = { up: 12, down: 13, left: 14, right: 15, a: 0, b: 1 };
          for (const [action, idx] of Object.entries(navButtons)) {
            const btn = pad.buttons[idx];
            const isDown = !!btn && btn.pressed;
            if (isDown && !vkeyGamepadWasPressed[action]) {
              if (action === "up") moveVkeyFocus(-1, 0);
              else if (action === "down") moveVkeyFocus(1, 0);
              else if (action === "left") moveVkeyFocus(0, -1);
              else if (action === "right") moveVkeyFocus(0, 1);
              else if (action === "a") pressVkeyFocused();
              else if (action === "b") closeVirtualKeyboard();
            }
            vkeyGamepadWasPressed[action] = isDown;
          }
        } else if (settingsOpen) {
          // While the menu is open, D-pad/A/B drive menu navigation
          // instead of game input entirely - otherwise navigating the
          // menu with the D-pad would simultaneously send button presses
          // to the game underneath it, which would be confusing at best.
          const navButtons = { up: 12, down: 13, left: 14, right: 15, a: 0, b: 1 };
          for (const [action, idx] of Object.entries(navButtons)) {
            const btn = pad.buttons[idx];
            const isDown = !!btn && btn.pressed;
            if (isDown && !menuNavWasPressed[action]) {
              if (action === "up") moveSettingsFocus(-1);
              else if (action === "down") moveSettingsFocus(1);
              else if (action === "left") adjustFocusedSettingsElement(-1);
              else if (action === "right") adjustFocusedSettingsElement(1);
              else if (action === "a") activateFocusedSettingsElement();
              else if (action === "b") setSettingsOpen(false);
            }
            menuNavWasPressed[action] = isDown;
          }
        } else if (cheatPanelOpen) {
          // Row/column navigation, not the flat list the settings menu
          // above uses - the panel's controls aren't a single vertical
          // list: the textarea is its own row, but Apply/Clear all/Close
          // sit side by side in one horizontal row (see .cheat-panel-actions
          // in style.css), so right/left should move along THAT row,
          // exactly like the on-screen keyboard's own grid below handles
          // rows of differing shapes - down from the textarea shouldn't
          // be the only way to reach Clear all when it's visually to the
          // right of Apply.
          const navButtons = { up: 12, down: 13, left: 14, right: 15, a: 0, b: 1 };
          for (const [action, idx] of Object.entries(navButtons)) {
            const btn = pad.buttons[idx];
            const isDown = !!btn && btn.pressed;
            if (isDown && !cheatNavWasPressed[action]) {
              if (action === "up") moveCheatFocus(-1, 0);
              else if (action === "down") moveCheatFocus(1, 0);
              else if (action === "left") moveCheatFocus(0, -1);
              else if (action === "right") moveCheatFocus(0, 1);
              else if (action === "a") activateFocusedElementIn(cheatPanel);
              else if (action === "b") setCheatPanelOpen(false);
            }
            cheatNavWasPressed[action] = isDown;
          }
        } else {
          // Face/menu buttons + d-pad
          for (const [idx, name] of Object.entries(GAMEPAD_BUTTON_MAP)) {
            const btn = pad.buttons[idx];
            const isDown = !!btn && btn.pressed;
            if (isDown) pressLogical(name);
            else releaseLogical(name);
          }

          // Left stick as an additional d-pad source
          const x = pad.axes[0] || 0;
          const y = pad.axes[1] || 0;
          if (y < -STICK_DEADZONE) pressLogical("up"); else if (!pad.buttons[12] || !pad.buttons[12].pressed) releaseLogical("up");
          if (y > STICK_DEADZONE) pressLogical("down"); else if (!pad.buttons[13] || !pad.buttons[13].pressed) releaseLogical("down");
          if (x < -STICK_DEADZONE) pressLogical("left"); else if (!pad.buttons[14] || !pad.buttons[14].pressed) releaseLogical("left");
          if (x > STICK_DEADZONE) pressLogical("right"); else if (!pad.buttons[15] || !pad.buttons[15].pressed) releaseLogical("right");

          // X is rapid-fire A - held down, it repeatedly presses and
          // releases A on a fixed interval rather than a single sustained
          // press, for games that need many quick taps in a row.
          const turboBtn = pad.buttons[TURBO_A_GAMEPAD_BUTTON];
          const turboIsDown = !!turboBtn && turboBtn.pressed;
          if (turboIsDown) {
            const now = performance.now();
            if (now - turboALastToggleTime >= TURBO_INTERVAL_MS) {
              turboALastToggleTime = now;
              turboAPhaseOn = !turboAPhaseOn;
              if (turboAPhaseOn) pressLogical("a"); else releaseLogical("a");
            }
          } else if (turboAPhaseOn) {
            // X released mid-cycle, with A currently in its pressed
            // phase - release it explicitly rather than leaving it stuck,
            // in case the regular A button (button 0) isn't also being
            // held to naturally clear it on the next frame.
            turboAPhaseOn = false;
            releaseLogical("a");
          }
        }

        // Fast-forward and reset stay available regardless of whether the
        // settings menu is open - they're shoulder buttons, not part of
        // the D-pad/A/B set used for menu navigation, so there's no
        // conflict either way.
        const ffBtn = pad.buttons[FAST_FORWARD_GAMEPAD_BUTTON];
        const ffIsDown = !!ffBtn && ffBtn.pressed;
        if (ffIsDown && !ffGamepadWasPressed) toggleFastForward();
        ffGamepadWasPressed = ffIsDown;

        const resetBtn = pad.buttons[RESET_GAMEPAD_BUTTON];
        const resetIsDown = !!resetBtn && resetBtn.pressed;
        if (resetIsDown && !resetGamepadWasPressed) triggerReset();
        resetGamepadWasPressed = resetIsDown;
      }
    }
    requestAnimationFrame(pollGamepad);
  }

  function bindGamepad() {
    window.addEventListener("gamepadconnected", handleGamepadConnected);
    window.addEventListener("gamepaddisconnected", handleGamepadDisconnected);
    requestAnimationFrame(pollGamepad);
  }

  // --- Unlock audio on first interaction --------------------------------

  function startAudioAndHideHint() {
    ensureAudioContext();
    if (audioCtx.state === "suspended") audioCtx.resume();
  }
  document.body.addEventListener("pointerdown", startAudioAndHideHint, { once: true });

  // --- Haptics setting (persisted) --------------------------------------

  function loadHapticSetting() {
    try {
      const stored = localStorage.getItem(HAPTIC_KEY);
      if (stored !== null) hapticsEnabled = stored === "1";
    } catch (_) { /* ignore */ }
    if (hapticToggle) hapticToggle.checked = hapticsEnabled;
  }

  function bindHapticSetting() {
    hapticToggle.addEventListener("change", () => {
      hapticsEnabled = hapticToggle.checked;
      try { localStorage.setItem(HAPTIC_KEY, hapticsEnabled ? "1" : "0"); } catch (_) { /* ignore */ }
    });
  }

  // --- Mute (persisted) ---------------------------------------------------
  // Purely local to this viewer - not sent to the server, not shared with
  // anyone else watching. Muting just skips scheduling audio buffers in
  // playAudioChunk above; it doesn't affect what the controller sends or
  // what any other viewer hears.

  function updateMuteButtonUI() {
    const btn = document.getElementById("muteBtn");
    if (!btn) return;
    btn.setAttribute("aria-pressed", audioMuted ? "true" : "false");
    btn.innerHTML = audioMuted ? "&#128263; Unmute" : "&#128266; Mute";
  }

  function loadMuteSetting() {
    try {
      const stored = localStorage.getItem(MUTE_KEY);
      if (stored !== null) audioMuted = stored === "1";
    } catch (_) { /* ignore */ }
    updateMuteButtonUI();
  }

  function bindMuteButton() {
    const btn = document.getElementById("muteBtn");
    if (!btn) return;
    btn.addEventListener("click", () => {
      audioMuted = !audioMuted;
      try { localStorage.setItem(MUTE_KEY, audioMuted ? "1" : "0"); } catch (_) { /* ignore */ }
      updateMuteButtonUI();
    });
  }

  // --- Audio batch ("buffer") setting, backed by the server ------------

  async function loadBufferSetting() {
    try {
      const res = await fetch(apiPath("/api/audio-batch"));
      const data = await res.json();
      applyBufferTicks(data.ticks, false);
    } catch (_) { /* use the slider's default */ }
  }

  function applyBufferTicks(ticks, push) {
    if (bufferRange) bufferRange.value = String(ticks);
    if (bufferValue) bufferValue.textContent = `${Math.round((ticks * 1000) / 60)} ms`;
    if (push) {
      fetch(apiPath("/api/audio-batch"), {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Client-Id": CLIENT_ID },
        body: JSON.stringify({ ticks }),
      }).catch(() => {});
    }
  }

  function bindBufferSetting() {
    bufferRange.addEventListener("input", () => {
      applyBufferTicks(Number(bufferRange.value), true);
    });
  }

  // --- Settings panel open/close ----------------------------------------

  function setSettingsOpen(open) {
    settingsOpen = open;
    settingsPanel.hidden = !open;
    settingsBackdrop.hidden = !open;
    settingsBtn.setAttribute("aria-expanded", open ? "true" : "false");
    document.body.style.overflow = open ? "hidden" : "";
    if (open && chatOpen) setChatOpen(false); // one panel at a time
    if (open && helpOpen) setHelpOpen(false);
    if (open && cheatPanelOpen) setCheatPanelOpen(false);
    if (open) {
      // Focus the first control immediately, mainly for controller users -
      // otherwise there'd be no visible focus indicator at all until the
      // first D-pad press, leaving no clue where navigation will start from.
      const elements = getFocusableSettingsElements();
      if (elements.length > 0) elements[0].focus();

      // The turbo-A gamepad loop only runs while settings is closed (A
      // means "activate this menu control" instead once it's open) - if
      // the menu opens mid-cycle, with A currently in its pressed phase,
      // nothing would ever release it afterward otherwise, leaving A
      // stuck held from the game's perspective indefinitely. Covered
      // here rather than only in the Y-button handler specifically, so
      // opening the menu any other way (the gear icon) is just as safe.
      if (turboAPhaseOn) {
        turboAPhaseOn = false;
        releaseLogical("a");
      }
    } else if (isVkeyboardOpen()) {
      // The keyboard is a separate overlay, not nested inside
      // settingsPanel - closing the panel around it (e.g. pressing Y
      // again while the keyboard happens to be open) wouldn't otherwise
      // hide it too, leaving it stuck visible with nothing behind it.
      closeVirtualKeyboard();
    }
  }

  // --- Controller navigation within the settings menu --------------------
  // Lets a gamepad fully drive the settings panel once it's open: D-pad
  // up/down moves focus between controls, left/right adjusts whichever
  // one is focused (a select's chosen option, a range slider's value),
  // and A activates it (clicking a button, toggling a checkbox). Reuses
  // real browser focus rather than a custom highlight system, so the
  // normal focus-ring styling and each control's own native semantics
  // come along for free.

  function getFocusableElementsIn(container) {
    if (!container) return [];
    return Array.from(container.querySelectorAll('button, select, input:not([type="file"]), textarea, [tabindex]'))
      .filter((el) => !el.disabled && el.offsetParent !== null); // offsetParent excludes hidden/collapsed elements
  }

  function moveFocusIn(container, direction) {
    const elements = getFocusableElementsIn(container);
    if (elements.length === 0) return;
    const currentIndex = elements.indexOf(document.activeElement);
    // If focus is currently outside the container (or nothing's focused
    // yet), start from the beginning rather than computing a meaningless
    // offset from index -1.
    const nextIndex = currentIndex === -1
      ? 0
      : (currentIndex + direction + elements.length) % elements.length;
    elements[nextIndex].focus();
  }

  function adjustFocusedElementIn(container, direction) {
    const el = document.activeElement;
    if (!container.contains(el)) return;
    if (el.tagName === "SELECT") {
      const newIndex = Math.max(0, Math.min(el.options.length - 1, el.selectedIndex + direction));
      if (newIndex !== el.selectedIndex) {
        el.selectedIndex = newIndex;
        el.dispatchEvent(new Event("change", { bubbles: true }));
      }
    } else if (el.tagName === "INPUT" && el.type === "range") {
      const step = parseFloat(el.step) || 1;
      const min = parseFloat(el.min);
      const max = parseFloat(el.max);
      const newValue = Math.max(min, Math.min(max, parseFloat(el.value) + direction * step));
      if (newValue !== parseFloat(el.value)) {
        el.value = newValue;
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
    // Buttons, checkboxes, and text areas don't have a meaningful
    // "adjust" direction - left/right does nothing for them, only A
    // (see below) does.
  }

  // Settings-specific wrappers - kept so the settings-menu call sites
  // below don't need to pass settingsPanel explicitly every time.
  function getFocusableSettingsElements() {
    return getFocusableElementsIn(settingsPanel);
  }
  function moveSettingsFocus(direction) {
    moveFocusIn(settingsPanel, direction);
  }
  function adjustFocusedSettingsElement(direction) {
    adjustFocusedElementIn(settingsPanel, direction);
  }

  // --- On-screen keyboard -------------------------------------------------
  // For text inputs specifically (just "Search library" today) - A on a
  // focused text field can't type anything on its own, since a gamepad
  // has no character keys, so it opens this instead. Grid navigation
  // (2D, not the linear list settings-panel navigation uses) because the
  // keys are laid out in actual rows/columns of different lengths, not a
  // simple top-to-bottom list.

  let vkeyTargetInput = null;
  let vkeyRow = 0;
  let vkeyCol = 0;

  function getVkeyRows() {
    // Read fresh from the DOM each time rather than duplicating the
    // layout in JS - the HTML stays the single source of truth for
    // which keys exist and where.
    const buttons = Array.from(document.querySelectorAll("#vkeyboardGrid .vkey"));
    const rows = [];
    for (const btn of buttons) {
      const r = parseInt(btn.dataset.row, 10);
      const c = parseInt(btn.dataset.col, 10);
      rows[r] = rows[r] || [];
      rows[r][c] = btn;
    }
    return rows.map((row) => row.filter(Boolean));
  }

  function updateVkeyPreview() {
    const preview = document.getElementById("vkeyboardPreview");
    if (!preview || !vkeyTargetInput) return;
    // A non-breaking space so the preview box doesn't visually collapse
    // to nothing while the field is still empty.
    preview.textContent = vkeyTargetInput.value || "\u00a0";
  }

  function openVirtualKeyboard(inputEl) {
    vkeyTargetInput = inputEl;
    vkeyRow = 0;
    vkeyCol = 0;
    document.getElementById("vkeyboardBackdrop").hidden = false;
    document.getElementById("vkeyboard").hidden = false;
    updateVkeyPreview();
    const rows = getVkeyRows();
    if (rows[0] && rows[0][0]) rows[0][0].focus();
  }

  function closeVirtualKeyboard() {
    document.getElementById("vkeyboardBackdrop").hidden = true;
    document.getElementById("vkeyboard").hidden = true;
    if (vkeyTargetInput) vkeyTargetInput.focus();
    vkeyTargetInput = null;
  }

  function isVkeyboardOpen() {
    return !document.getElementById("vkeyboard").hidden;
  }

  function moveVkeyFocus(dRow, dCol) {
    const rows = getVkeyRows();
    if (rows.length === 0) return;
    const newRow = Math.max(0, Math.min(rows.length - 1, vkeyRow + dRow));
    // Clamp to the TARGET row's own length, not the row being left -
    // rows have different lengths (10 keys on the top row, 3 wide keys
    // on the bottom), so moving straight down from column 9 needs to
    // land somewhere that row actually has, not fall off the end.
    const newCol = Math.max(0, Math.min(rows[newRow].length - 1, vkeyCol + dCol));
    vkeyRow = newRow;
    vkeyCol = newCol;
    const btn = rows[vkeyRow][vkeyCol];
    if (btn) btn.focus();
  }

  function pressVkeyFocused() {
    const btn = document.activeElement;
    if (!btn || !btn.classList.contains("vkey") || !vkeyTargetInput) return;
    const action = btn.dataset.action;
    if (action === "done") {
      closeVirtualKeyboard();
      return;
    }
    if (action === "backspace") {
      vkeyTargetInput.value = vkeyTargetInput.value.slice(0, -1);
    } else if (action === "space") {
      vkeyTargetInput.value += " ";
    } else {
      vkeyTargetInput.value += btn.textContent;
    }
    vkeyTargetInput.dispatchEvent(new Event("input", { bubbles: true }));
    updateVkeyPreview();
  }

  function bindVirtualKeyboard() {
    const backdrop = document.getElementById("vkeyboardBackdrop");
    if (backdrop) backdrop.addEventListener("click", closeVirtualKeyboard);
    // Mouse/touch: each key works as a plain click too, not just via
    // gamepad grid navigation - useful on any device without a physical
    // keyboard attached, not only controller users.
    const grid = document.getElementById("vkeyboardGrid");
    if (grid) {
      grid.addEventListener("click", (event) => {
        const btn = event.target.closest(".vkey");
        if (!btn) return;
        btn.focus();
        pressVkeyFocused();
      });
    }
  }

  function activateFocusedElementIn(container) {
    const el = document.activeElement;
    if (!container.contains(el)) return;
    if (el.tagName === "BUTTON" || el.tagName === "LABEL") {
      // A <label for="..."> click is standard browser behavior for
      // triggering its associated control - this is what makes the
      // "Add ROM to library" / "Upload .state" label-styled buttons
      // open their file picker, the same as an actual click would.
      el.click();
    } else if (el.tagName === "INPUT" && el.type === "checkbox") {
      el.checked = !el.checked;
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else if ((el.tagName === "INPUT" && el.type === "text") || el.tagName === "TEXTAREA") {
      // No physical keyboard attached, and a gamepad has no character
      // keys of its own - open the on-screen one instead. Works the same
      // for a <textarea> (the cheat panel's code box) as a single-line
      // text input - openVirtualKeyboard only ever reads/writes .value,
      // which both element types have.
      openVirtualKeyboard(el);
    } else if (el.tagName === "SELECT") {
      // No clean way to programmatically open a native <select>'s
      // dropdown - cycling it forward is a reasonable fallback so A still
      // does something useful when a dropdown has focus.
      adjustFocusedElementIn(container, 1);
    }
  }

  // Settings-specific wrapper - see the note above the other
  // getFocusableSettingsElements/moveSettingsFocus/etc. wrappers.
  function activateFocusedSettingsElement() {
    activateFocusedElementIn(settingsPanel);
  }

  function bindSettings() {
    settingsBtn.addEventListener("click", () => {
      const open = !settingsOpen;
      setSettingsOpen(open);
      if (open) refreshLibrary();
    });
    settingsClose.addEventListener("click", () => setSettingsOpen(false));
    settingsBackdrop.addEventListener("click", () => {
      if (!uploading) setSettingsOpen(false);
    });
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && settingsOpen && !uploading) setSettingsOpen(false);
    });
  }

  // --- Chat panel open/close ----------------------------------------------

  function setChatOpen(open) {
    chatOpen = open;
    chatPanel.hidden = !open;
    chatBackdrop.hidden = !open;
    chatBtn.setAttribute("aria-expanded", open ? "true" : "false");
    document.body.style.overflow = open ? "hidden" : "";
    if (open && settingsOpen) setSettingsOpen(false); // one panel at a time
    if (open && helpOpen) setHelpOpen(false);
    if (open) {
      chatMessages.scrollTop = chatMessages.scrollHeight;
      chatInput.focus();
    }
  }

  function bindChatPanel() {
    chatBtn.addEventListener("click", () => setChatOpen(!chatOpen));
    chatClose.addEventListener("click", () => setChatOpen(false));
    chatBackdrop.addEventListener("click", () => setChatOpen(false));
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && chatOpen) setChatOpen(false);
    });
    window.addEventListener("resize", syncSidePanelHeights);
    syncSidePanelHeights();
  }

  function setHelpOpen(open) {
    helpOpen = open;
    helpPanel.hidden = !open;
    helpBackdrop.hidden = !open;
    helpBtn.setAttribute("aria-expanded", open ? "true" : "false");
    document.body.style.overflow = open ? "hidden" : "";
    if (open && settingsOpen) setSettingsOpen(false); // one panel at a time
    if (open && chatOpen) setChatOpen(false);
  }

  function bindHelpPanel() {
    helpBtn.addEventListener("click", () => setHelpOpen(!helpOpen));
    helpClose.addEventListener("click", () => setHelpOpen(false));
    helpBackdrop.addEventListener("click", () => setHelpOpen(false));
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && helpOpen) setHelpOpen(false);
    });
    window.addEventListener("resize", syncSidePanelHeights);
    syncSidePanelHeights();
  }

  // Hidden easter egg: entering the classic Konami code
  // (up up down down left right left right b a) anywhere on the page
  // reveals the cheat engine panel. Tracked in a rolling buffer of the
  // last 10 keys pressed - deliberately does NOT preventDefault or
  // otherwise interfere with the arrow keys' normal job of also moving
  // the game's D-pad; this only listens alongside that, never instead
  // of it, so trying the code doesn't require pausing gameplay.
  //
  // Resolved through KEY_MAP (the same table bindKeyboard() above uses),
  // not raw key letters - so "b" and "a" here mean the game's B/A
  // buttons, matching whichever physical keys the player already has
  // their fingers on (Z/X or literal B/A both work, same as they do for
  // actually playing), rather than requiring the literal letter keys B
  // and A specifically.
  const KONAMI_SEQUENCE = [
    "up", "up", "down", "down",
    "left", "right", "left", "right",
    "b", "a",
  ];
  let konamiBuffer = [];
  // Shared by both input sources below (keyboard keydown and gamepad
  // polling) - each just resolves its own input to a logical button name
  // ("up"/"down"/"left"/"right"/"a"/"b") and feeds it in here, so the
  // actual sequence-matching only lives in one place.
  function feedKonamiBuffer(btn) {
    if (!btn) return;
    konamiBuffer.push(btn);
    if (konamiBuffer.length > KONAMI_SEQUENCE.length) konamiBuffer.shift();
    if (
      konamiBuffer.length === KONAMI_SEQUENCE.length &&
      konamiBuffer.every((k, i) => k === KONAMI_SEQUENCE[i])
    ) {
      konamiBuffer = [];
      setCheatPanelOpen(true);
    }
  }
  function bindKonamiEasterEgg() {
    if (!cheatPanel) return;
    window.addEventListener("keydown", (e) => {
      feedKonamiBuffer(KEY_MAP[e.code]); // undefined for non-game keys - feedKonamiBuffer ignores those
    });
  }

  // Row/column model for the cheat panel's controller navigation - the
  // textarea is its own row (one column), and the Apply/Clear all/Close
  // buttons form a second row (three columns), matching the actual
  // visual layout (see .cheat-panel-actions in style.css) rather than
  // treating all four controls as one flat vertical list. Read fresh
  // from the DOM each time, same reasoning as getVkeyRows() below - the
  // HTML stays the single source of truth for what's actually there.
  let cheatFocusRow = 0;
  let cheatFocusCol = 0;

  function getCheatRows() {
    if (!cheatPanel) return [];
    const textarea = document.getElementById("cheat-codes-input");
    const actionRow = Array.from(
      document.querySelectorAll("#cheat-panel .cheat-panel-actions .cheat-btn")
    );
    const rows = [];
    if (textarea) rows.push([textarea]);
    if (actionRow.length > 0) rows.push(actionRow);
    return rows;
  }

  function moveCheatFocus(dRow, dCol) {
    const rows = getCheatRows();
    if (rows.length === 0) return;
    // Clamped, not wrapped, at both edges - same convention as the
    // on-screen keyboard's own moveVkeyFocus below, so a controller
    // behaves consistently the same way across every panel in the app.
    const newRow = Math.max(0, Math.min(rows.length - 1, cheatFocusRow + dRow));
    const newCol = Math.max(0, Math.min(rows[newRow].length - 1, cheatFocusCol + dCol));
    cheatFocusRow = newRow;
    cheatFocusCol = newCol;
    const el = rows[cheatFocusRow][cheatFocusCol];
    if (el) el.focus();
  }

  function setCheatPanelOpen(open) {
    cheatPanelOpen = open;
    cheatPanel.hidden = !open;
    if (open) {
      cheatError.hidden = true;
      if (settingsOpen) setSettingsOpen(false); // one panel at a time, same rule as settings/chat/help
      if (chatOpen) setChatOpen(false);
      if (helpOpen) setHelpOpen(false);
      cheatFocusRow = 0;
      cheatFocusCol = 0;
      cheatCodesInput.focus();

      // Same reasoning as the equivalent guard in setSettingsOpen above -
      // if the Konami code's final press lands while X/turbo-A's rapid
      // fire is mid-cycle, nothing would otherwise release it once the
      // panel takes over A's meaning (activate a focused control instead
      // of pressing the game's A button).
      if (turboAPhaseOn) {
        turboAPhaseOn = false;
        releaseLogical("a");
      }
    }
  }

  function renderActiveCheats(parsed) {
    if (!parsed || parsed.length === 0) {
      cheatActiveList.textContent = "No cheats active.";
      return;
    }
    cheatActiveList.innerHTML = "";
    parsed.forEach((c) => {
      const row = document.createElement("div");
      row.className = "cheat-active-row";
      const addrHex = c.address.toString(16).toUpperCase().padStart(4, "0");
      const valHex = c.value.toString(16).toUpperCase().padStart(2, "0");
      row.textContent = `0x${addrHex} = 0x${valHex}`;
      cheatActiveList.appendChild(row);
    });
  }

  async function submitCheats(codes) {
    cheatError.hidden = true;
    try {
      const res = await fetch(apiPath("/api/cheats"), {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Client-Id": CLIENT_ID },
        body: JSON.stringify({ codes }),
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        cheatError.textContent = data.error || "Only the current controller can apply cheats.";
        cheatError.hidden = false;
        return;
      }
      renderActiveCheats(data.parsed);
    } catch (err) {
      cheatError.textContent = "Failed to reach the server.";
      cheatError.hidden = false;
    }
  }

  function bindCheatPanel() {
    if (!cheatPanel) return;
    bindKonamiEasterEgg();
    cheatCloseBtn.addEventListener("click", () => setCheatPanelOpen(false));
    cheatApplyBtn.addEventListener("click", () => {
      const lines = cheatCodesInput.value
        .split("\n")
        .map((l) => l.trim())
        .filter((l) => l.length > 0);
      submitCheats(lines);
    });
    cheatClearBtn.addEventListener("click", () => {
      cheatCodesInput.value = "";
      submitCheats([]);
    });
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && cheatPanelOpen) setCheatPanelOpen(false);
    });
  }

  // On the desktop-docked layout (see the min-width:900px media query),
  // matches both side panels' height to .shell's actual rendered height -
  // done here rather than in pure CSS since flexbox align-items:stretch
  // didn't reliably produce equal heights for this layout.
  const DESKTOP_CHAT_QUERY = "(min-width: 900px)";
  function syncSidePanelHeights() {
    const isDesktop = window.matchMedia(DESKTOP_CHAT_QUERY).matches;
    if (!isDesktop) {
      chatPanel.style.height = "";
      if (helpPanel) helpPanel.style.height = "";
      return;
    }
    const shellEl = document.querySelector(".shell");
    if (!shellEl) return;
    chatPanel.style.height = `${shellEl.offsetHeight}px`;
    if (helpPanel) helpPanel.style.height = `${shellEl.offsetHeight}px`;
  }

  // --- Chat messages --------------------------------------------------

  function appendChatMessage(entry) {
    const line = document.createElement("div");
    line.className = `chat-message role-${entry.role}`;

    const roleSpan = document.createElement("span");
    roleSpan.className = "chat-role";
    const displayName =
      entry.name && entry.name.trim()
        ? entry.name.trim()
        : entry.role === "controller"
        ? "Controller"
        : "Viewer";
    roleSpan.textContent = displayName;

    const textSpan = document.createElement("span");
    textSpan.textContent = entry.text; // textContent only - never innerHTML with chat text

    line.appendChild(roleSpan);
    line.appendChild(textSpan);
    chatMessages.appendChild(line);

    // Only auto-scroll if already near the bottom, so scrolling up to read
    // history isn't yanked away by a new message arriving.
    const nearBottom = chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight < 60;
    if (nearBottom || chatOpen) chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  const CHAT_NAME_KEY = "gbserver.chatName";

  function loadChatName() {
    if (!chatNameInput) return;
    try {
      const stored = localStorage.getItem(CHAT_NAME_KEY);
      if (stored) chatNameInput.value = stored;
    } catch (_) { /* localStorage unavailable (private browsing, etc.) - fine, just won't persist */ }
  }

  function bindChatNameInput() {
    if (!chatNameInput) return;
    chatNameInput.addEventListener("change", () => {
      try {
        localStorage.setItem(CHAT_NAME_KEY, chatNameInput.value.trim());
      } catch (_) { /* ignore - see loadChatName */ }
    });
  }

  function bindChatForm() {
    chatForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = chatInput.value.trim();
      if (!text || !wsReady) return;
      const name = chatNameInput ? chatNameInput.value.trim() : "";
      ws.send(`chat:${JSON.stringify({ name, text })}`);
      chatInput.value = "";
    });
  }

  // --- Live library polling (while Settings panel is open) --------------

  const LIBRARY_POLL_MS = 5000; // how often to re-check the roms/ folder
  let libraryPollHandle = null;

  function startLibraryPolling() {
    libraryPollHandle = setInterval(() => {
      if (settingsOpen) refreshLibrary();
    }, LIBRARY_POLL_MS);
  }

  function stopLibraryPolling() {
    if (libraryPollHandle !== null) {
      clearInterval(libraryPollHandle);
      libraryPollHandle = null;
    }
  }

  // --- ROM library --------------------------------------------------------

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  // Cache of the last successful fetch, so typing in the search box can
  // re-render instantly without a network round-trip on every keystroke.
  let lastRomsRes = { roms: [] };
  let lastConfigRes = {};

  function renderRomList() {
    const romsRes = lastRomsRes;
    const configRes = lastConfigRes;
    const query = (romSearchEl && romSearchEl.value.trim().toLowerCase()) || "";
    const filteredRoms = query
      ? romsRes.roms.filter((rom) => rom.filename.toLowerCase().includes(query))
      : romsRes.roms;

    // Preserve the user's current selection across a rebuild (e.g. the 5s
    // library poll, or a new search keystroke) when it's still in the list.
    const previousValue = romSelectEl.value;

    romSelectEl.innerHTML = "";

    if (romsRes.roms.length === 0) {
      const opt = document.createElement("option");
      opt.textContent = "No ROMs uploaded yet.";
      opt.disabled = true;
      romSelectEl.appendChild(opt);
      romSelectEl.disabled = true;
      romPlayBtn.disabled = true;
      romResumeBtn.disabled = true;
      romDeleteBtn.disabled = true;
      return;
    }
    if (filteredRoms.length === 0) {
      const opt = document.createElement("option");
      opt.textContent = "No ROMs match your search.";
      opt.disabled = true;
      romSelectEl.appendChild(opt);
      romSelectEl.disabled = true;
      romPlayBtn.disabled = true;
      romResumeBtn.disabled = true;
      romDeleteBtn.disabled = true;
      return;
    }

    romSelectEl.disabled = false;
    romPlayBtn.disabled = false;
    romDeleteBtn.disabled = false;

    for (const rom of filteredRoms) {
      const opt = document.createElement("option");
      opt.value = rom.filename;
      const isPlaying = rom.filename === configRes.current_rom;
      const meta = `${formatBytes(rom.size_bytes)}${rom.has_save ? " \u00b7 has save" : ""}`;
      opt.textContent = `${isPlaying ? "\u25b6 " : ""}${rom.filename} \u2014 ${meta}`;
      romSelectEl.appendChild(opt);
    }

    // Reselect the previous choice if it survived the filter; otherwise
    // default to the currently-playing ROM, or just the first item.
    const stillPresent = filteredRoms.some((r) => r.filename === previousValue);
    if (stillPresent) {
      romSelectEl.value = previousValue;
    } else if (configRes.current_rom && filteredRoms.some((r) => r.filename === configRes.current_rom)) {
      romSelectEl.value = configRes.current_rom;
    } else {
      romSelectEl.selectedIndex = 0;
    }
    updateResumeButtonState();
    updateEngineSelectState();
  }

  function bindRomSearch() {
    if (!romSearchEl) return;
    romSearchEl.addEventListener("input", renderRomList);
  }

  function bindRomSelectActions() {
    romPlayBtn.addEventListener("click", () => {
      const filename = romSelectEl.value;
      if (filename) playRom(filename, false);
    });
    romResumeBtn.addEventListener("click", () => {
      const filename = romSelectEl.value;
      if (filename) playRom(filename, true);
    });
    romDeleteBtn.addEventListener("click", () => {
      const filename = romSelectEl.value;
      if (filename) deleteRom(filename);
    });
    romSelectEl.addEventListener("change", () => {
      updateResumeButtonState();
      updateEngineSelectState();
    });
  }

  function bindEngineSelect() {
    engineSelect.addEventListener("change", async () => {
      const filename = romSelectEl.value;
      if (!filename) return;
      const engine = engineSelect.value;
      try {
        const res = await fetch(apiPath(`/api/rom/${encodeURIComponent(filename)}/engine`), {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Client-Id": CLIENT_ID },
          body: JSON.stringify({ engine }),
        });
        const data = await res.json();
        if (data.ok) {
          setUploadMsg(`Engine set to ${engine} for ${filename}`, "ok");
          refreshLibrary();
        } else {
          setUploadMsg(data.error || "Could not change engine", "error");
          updateEngineSelectState(); // revert the dropdown to the actual saved value
        }
      } catch (_) {
        setUploadMsg("Could not change engine", "error");
        updateEngineSelectState();
      }
    });
  }

  function updateResumeButtonState() {
    const rom = lastRomsRes.roms.find((r) => r.filename === romSelectEl.value);
    romResumeBtn.disabled = !rom || !rom.has_save;
  }

  function updateEngineSelectState() {
    if (!lastConfigRes.boytacean_available) {
      engineRow.hidden = true;
      return;
    }
    engineRow.hidden = false;
    const rom = lastRomsRes.roms.find((r) => r.filename === romSelectEl.value);
    engineSelect.value = (rom && rom.engine) || "pyboy";
  }

  async function refreshLibrary() {
    const [romsFetch, configFetch] = await Promise.all([
      fetch(apiPath("/api/roms")),
      fetch(apiPath("/api/config")),
    ]);

    // A room that existed when this page loaded but has since expired
    // (the 30-minute empty-room reaper) starts 404ing on these same
    // endpoints - previously this just kept retrying forever, silently,
    // on every poll. Now it's treated the same as a room that was already
    // gone before the page ever loaded.
    if (romsFetch.status === 404 || configFetch.status === 404) {
      showRoomMissingBanner();
      return;
    }

    const [romsRes, configRes] = await Promise.all([romsFetch.json(), configFetch.json()]);

    // Skip the DOM rebuild entirely if nothing actually changed. This
    // matters most for the 5s background poll (startLibraryPolling): on
    // mobile, tapping the ROM <select> opens the browser's native picker
    // sheet, but this poll keeps running underneath it - rebuilding the
    // <select>'s options every cycle (even with identical data) was tearing
    // down and recreating them under the open picker, which reset its
    // scroll position back to the top mid-scroll.
    const changed =
      JSON.stringify(romsRes) !== JSON.stringify(lastRomsRes) ||
      JSON.stringify(configRes) !== JSON.stringify(lastConfigRes);

    lastRomsRes = romsRes;
    lastConfigRes = configRes;

    if (!changed) return;

    romNameEl.textContent = configRes.current_rom || "No ROM loaded";
    storageText.textContent = formatBytes(configRes.library_total_bytes || 0);
    storageDetail.textContent = `${romsRes.roms.length} ROM${romsRes.roms.length === 1 ? "" : "s"} in the library`;

    saveInfo.textContent = configRes.has_save
      ? "A save exists for the current ROM."
      : "No save for the current ROM yet.";
    saveDownload.classList.toggle("busy", !configRes.has_save);
    saveDelete.classList.toggle("busy", !configRes.has_save);

    if (audioBadge) {
      // Only relevant once a ROM is actually running - hidden if nothing's
      // loaded, regardless of audio_available's default value.
      audioBadge.hidden = !configRes.current_rom || configRes.audio_available !== false;
    }

    renderRomList();
  }

  async function playRom(filename, loadSave) {
    setUploadMsg(`${loadSave ? "Resuming" : "Loading"} ${filename}\u2026`, null);
    const res = await fetch(apiPath("/api/play"), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Client-Id": CLIENT_ID },
      body: JSON.stringify({ filename, load_save: loadSave }),
    });
    const data = await res.json();
    if (data.ok) {
      // ROM started fine either way; a "warning" means its save specifically
      // didn't load (started fresh instead) - worth flagging, but not a hard
      // failure, so it still gets the "ok" styling.
      setUploadMsg(data.warning || `Playing ${filename}`, "ok");
      resetAudioSchedule();
      refreshLibrary();
    } else {
      setUploadMsg(data.error || "Failed to load ROM", "error");
    }
  }

  async function deleteRom(filename) {
    if (!confirm(`Delete ${filename}? This also removes its save data.`)) return;
    const res = await fetch(apiPath(`/api/rom/${encodeURIComponent(filename)}`), {
      method: "DELETE",
      headers: { "X-Client-Id": CLIENT_ID },
    });
    const data = await res.json();
    if (data.ok) {
      setUploadMsg(`Deleted ${filename}`, "ok");
      refreshLibrary();
    } else {
      setUploadMsg(data.error || "Delete failed", "error");
    }
  }

  function setUploadMsg(text, kind) {
    uploadMsg.textContent = text || "";
    uploadMsg.classList.toggle("error", kind === "error");
    uploadMsg.classList.toggle("ok", kind === "ok");
  }

  function bindRomUpload() {
    romFileEl.addEventListener("change", async () => {
      const file = romFileEl.files && romFileEl.files[0];
      romFileEl.value = "";
      if (!file) return;

      uploading = true;
      uploadBar.style.width = "0%";
      setUploadMsg("Uploading\u2026", null);

      const formData = new FormData();
      formData.append("rom", file);

      try {
        const res = await fetch(apiPath("/api/upload"), { method: "POST", body: formData });
        const data = await res.json();
        uploadBar.style.width = "100%";
        if (data.ok) {
          setUploadMsg(`Uploaded ${data.filename}`, "ok");
          refreshLibrary();
        } else {
          setUploadMsg(data.error || "Upload failed", "error");
        }
      } catch (_) {
        setUploadMsg("Upload failed", "error");
      } finally {
        uploading = false;
        setTimeout(() => { uploadBar.style.width = "0%"; }, 800);
      }
    });
  }

  // --- Save data controls --------------------------------------------------

  function setSaveMsg(text, kind) {
    saveMsg.textContent = text || "";
    saveMsg.classList.toggle("error", kind === "error");
    saveMsg.classList.toggle("ok", kind === "ok");
  }

  function bindSaveControls() {
    saveNow.addEventListener("click", async () => {
      try {
        const res = await fetch(apiPath("/api/save-now"), {
          method: "POST",
          headers: { "X-Client-Id": CLIENT_ID },
        });
        const data = await res.json();
        if (data.ok) {
          setSaveMsg("Saved", "ok");
          refreshLibrary();
        } else {
          setSaveMsg(data.error || "Save failed", "error");
        }
      } catch (_) {
        setSaveMsg("Save failed", "error");
      }
    });

    saveDownload.addEventListener("click", async () => {
      // Was a raw window.location.href navigation straight to the API
      // route - worked fine when a save existed (the browser recognizes
      // the download response and handles it invisibly), but when there
      // was no save, the server's JSON error response replaced the
      // entire page instead of showing an in-app message, since a plain
      // navigation has no way to inspect the response first. The CSS
      // "busy" class that's supposed to gray this button out when there's
      // no save also only blocks pointer-events (mouse/touch) - it
      // doesn't stop a keyboard Enter/Space on a focused button, or a
      // gamepad's own activateFocusedElementIn() calling .click()
      // directly - so this needed to be safe on its own regardless of
      // that state possibly being stale or bypassed.
      setSaveMsg("Downloading\u2026", null);
      try {
        const res = await fetch(apiPath("/api/save"), {
          headers: { "X-Client-Id": CLIENT_ID },
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          setSaveMsg(data.error || "No save available to download", "error");
          return;
        }
        const blob = await res.blob();
        // Pull the real filename from the response rather than
        // hardcoding one, so it still matches the actual ROM's save
        // name - falls back to something reasonable only if that
        // header is missing for some reason.
        const disposition = res.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="?([^";]+)"?/);
        const filename = match ? match[1] : "save.state";
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        setSaveMsg(`Downloaded ${filename}`, "ok");
      } catch (err) {
        setSaveMsg("Failed to download save", "error");
      }
    });

    saveFileEl.addEventListener("change", async () => {
      const file = saveFileEl.files && saveFileEl.files[0];
      saveFileEl.value = "";
      if (!file) return;
      const formData = new FormData();
      formData.append("save", file);
      setSaveMsg("Uploading save\u2026", null);
      try {
        const res = await fetch(apiPath("/api/save"), {
          method: "POST",
          headers: { "X-Client-Id": CLIENT_ID },
          body: formData,
        });
        const data = await res.json();
        if (data.ok) {
          setSaveMsg("Save applied", "ok");
          resetAudioSchedule();
          // Sync the dropdown to whichever ROM the save was actually
          // applied to - it's the only reliable way to know, since this
          // upload never required the dropdown to already be showing the
          // right ROM in the first place. Without this, "Resume save"
          // afterward checks whatever the dropdown happened to already
          // be pointed at, which silently disables the button if that
          // wasn't the same ROM.
          if (data.rom) romSelectEl.value = data.rom;
          refreshLibrary();
        } else {
          setSaveMsg(data.error || "Upload failed", "error");
        }
      } catch (_) {
        setSaveMsg("Upload failed", "error");
      }
    });

    saveDelete.addEventListener("click", async () => {
      if (!confirm("Delete the save for the current ROM?")) return;
      try {
        const res = await fetch(apiPath("/api/save"), {
          method: "DELETE",
          headers: { "X-Client-Id": CLIENT_ID },
        });
        const data = await res.json();
        if (data.ok) {
          setSaveMsg("Save deleted", "ok");
          refreshLibrary();
        } else {
          setSaveMsg(data.error || "Delete failed", "error");
        }
      } catch (_) {
        setSaveMsg("Delete failed", "error");
      }
    });
  }

  // --- Server controls -------------------------------------------------

  function bindStop() {
    stopBtn.addEventListener("click", async () => {
      const res = await fetch(apiPath("/api/stop"), {
        method: "POST",
        headers: { "X-Client-Id": CLIENT_ID },
      });
      const data = await res.json();
      stopMsg.textContent = data.ok ? "Emulation stopped" : (data.error || "Failed to stop");
      stopMsg.classList.toggle("ok", !!data.ok);
      if (data.ok) {
        clearScreen();
        // Only navigate to the download if a save genuinely exists -
        // /api/save returns a JSON error (not a file) when there's
        // nothing to download, and navigating straight there regardless
        // used to replace the whole page with that raw error response.
        if (data.has_save) {
          window.location.href = apiPath("/api/save");
        }
      }
      refreshLibrary();
    });
  }

  // --- Session controls (private rooms) ---------------------------------

  function bindCreateRoom() {
    const btn = document.getElementById("createRoomBtn");
    const msg = document.getElementById("createRoomMsg");
    if (!btn) return; // only present on the default (non-room) page
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      if (msg) { msg.textContent = "Starting\u2026"; msg.classList.remove("error", "ok"); }
      try {
        const res = await fetch("/api/rooms", { method: "POST" });
        const data = await res.json();
        if (data.ok) {
          window.location.href = `/r/${data.room}`;
        } else {
          if (msg) { msg.textContent = data.error || "Couldn't start a session"; msg.classList.add("error"); }
          btn.disabled = false;
        }
      } catch (_) {
        if (msg) { msg.textContent = "Couldn't start a session"; msg.classList.add("error"); }
        btn.disabled = false;
      }
    });
  }

  function bindSharedDisabledCreateRoom() {
    const btn = document.getElementById("sharedDisabledCreateRoomBtn");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Starting\u2026";
      try {
        const res = await fetch("/api/rooms", { method: "POST" });
        const data = await res.json();
        if (data.ok) {
          window.location.href = `/r/${data.room}`;
        } else {
          alert(data.error || "Couldn't start a session");
          btn.disabled = false;
          btn.textContent = "Create a private room";
        }
      } catch (_) {
        alert("Couldn't start a session");
        btn.disabled = false;
        btn.textContent = "Create a private room";
      }
    });
  }

  function bindCopyLink() {
    const btn = document.getElementById("copyLinkBtn");
    if (!btn) return; // only present when already inside a room
    const originalText = btn.textContent;
    btn.addEventListener("click", async () => {
      const link = window.location.href;
      try {
        await navigator.clipboard.writeText(link);
        btn.textContent = "Copied!";
      } catch (_) {
        // Clipboard API needs a secure context (https) or may be blocked -
        // fall back to a prompt so the link can still be copied by hand.
        window.prompt("Copy this link:", link);
      }
      setTimeout(() => { btn.textContent = originalText; }, 1500);
    });
  }

  // --- Init ------------------------------------------------------------

  clearScreen(); // start on the "powered off" look rather than a plain black canvas

  // A room link that no longer exists (expired/never existed) short-circuits
  // here - nothing to stream, so skip connecting and show the banner instead
  // of a permanently "Connecting..." screen.
  function showRoomMissingBanner() {
    const banner = document.getElementById("roomMissingBanner");
    const shell = document.querySelector(".shell");
    if (banner) banner.hidden = false;
    if (shell) shell.hidden = true;
    stopLibraryPolling();
  }

  function showSharedDisabledBanner() {
    const banner = document.getElementById("sharedDisabledBanner");
    const shell = document.querySelector(".shell");
    if (banner) banner.hidden = false;
    if (shell) shell.hidden = true;
    stopLibraryPolling();
  }

  if (ROOM_MISSING) {
    showRoomMissingBanner();
    return;
  }
  if (SHARED_DISABLED_AT_LOAD) {
    showSharedDisabledBanner();
    // Unlike the room-missing banner above (a plain <a href> link, no JS
    // needed), this banner's button has to actually call the API and
    // navigate once a room's created - its click handler normally gets
    // attached later in the init sequence, which this early return skips
    // entirely. Bound here explicitly so it isn't silently left with no
    // listener at all, which is exactly what was happening before this.
    bindSharedDisabledCreateRoom();
    return;
  }

  fetch(apiPath("/api/config"))
    .then((r) => r.json())
    .then((cfg) => {
      SAMPLE_RATE = cfg.sample_rate || SAMPLE_RATE;
      applyBufferTicks(cfg.audio_batch_ticks || 4, false);
    });
  // Deliberately NOT auto-connecting the WebSocket here anymore - see
  // bindConnectGate below. Requiring an actual click before the
  // connection is even attempted is specifically meant to filter out
  // passive bots (link-preview crawlers rendering the page for a
  // screenshot, simple scanners checking whether something answers) -
  // their whole point is silently loading the page, not simulating a
  // deliberate user action, so they never get past this at all. A real
  // person just sees one extra tap before the game connects.

  bindConnectGate();
  loadHapticSetting();
  loadMuteSetting();
  bindButtons();
  bindKeyboard();
  bindFullscreen();
  bindRomUpload();
  bindBufferSetting();
  bindHapticSetting();
  bindMuteButton();
  bindSaveControls();
  bindStop();
  bindFastForward();
  bindResetButton();
  bindRequestControl();
  loadChatName();
  loadTheme();
  bindThemeSelect();
  loadVideoFilter();
  bindVideoFilterSelect();
  loadSmoothness();
  bindSmoothnessSlider();
  bindChatNameInput();
  bindSettings();
  bindVirtualKeyboard();
  bindGamepad();
  bindRomSearch();
  bindRomSelectActions();
  bindEngineSelect();
  bindCreateRoom();
  bindSharedDisabledCreateRoom();
  bindCopyLink();
  bindChatPanel();
  bindHelpPanel();
  bindChatForm();
  bindCheatPanel();
  startLibraryPolling();
  refreshLibrary();
})();
