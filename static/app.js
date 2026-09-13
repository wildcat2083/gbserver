(() => {
  "use strict";

  const WIDTH = 160, HEIGHT = 144;
  const MSG_VIDEO = 1;
  const MSG_AUDIO = 2;

  const ROOM = document.body.dataset.room || null;
  const ROOM_MISSING = document.body.dataset.roomMissing === "true";
  const SHARED_DISABLED_AT_LOAD = document.body.dataset.sharedDisabled === "true";

  function apiPath(path) {
    return ROOM ? `/r/${ROOM}${path}` : path;
  }

  function generateClientId() {
    try {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    } catch (_) {   }
    return "cid-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
  }
  const CLIENT_ID = generateClientId();
  const KICK_CLOSE_CODE = 4001;
  const SHARED_DISABLED_CLOSE_CODE = 4002;

  let isController = false;

  const canvas = document.getElementById("screen");
  const ctx = canvas.getContext("2d", { alpha: false });
  const canvasGL = document.getElementById("screenGL");
  const gameStage = document.getElementById("gameStage");
  const toggleControlsBtn = document.getElementById("toggleControlsBtn");
  const imageData = ctx.createImageData(WIDTH, HEIGHT);

  const FILTER_KEY = "gbserver.videoFilter";
  let currentFilter = "off";
  const SMOOTHNESS_KEY = "gbserver.smartSmoothness";
  let smartSmoothness = 2.0;
  const HQX_KEY = "gbserver.hqxStrength";
  let hqxStrength = 1.0;
  const THEME_KEY = "gbserver.theme";
  const VALID_THEMES = ["dmg", "pocket", "grape", "light-yellow", "dark", "clearshell"];
  let glState = null;

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

      vec3 preTL  = texture2D(uTexture, base - texel).rgb;
      vec3 postBR = texture2D(uTexture, base + texel * 2.0).rgb;
      vec3 preTR  = texture2D(uTexture, base + vec2(texel.x * 2.0, -texel.y)).rgb;
      vec3 postBL = texture2D(uTexture, base + vec2(-texel.x, texel.y * 2.0)).rgb;

      float w00 = (1.0 - frac.x) * (1.0 - frac.y);
      float w10 = frac.x * (1.0 - frac.y);
      float w01 = (1.0 - frac.x) * frac.y;
      float w11 = frac.x * frac.y;

      float diagTLBR = length(c00 - c11) + 0.5 * (length(preTL - c00) + length(c11 - postBR));
      float diagTRBL = length(c10 - c01) + 0.5 * (length(preTR - c10) + length(c01 - postBL));
      float diagDiff = diagTRBL - diagTLBR;
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

  const GL_FRAGMENT_HQX_SRC = `
    #ifdef GL_FRAGMENT_PRECISION_HIGH
    precision highp float;
    #else
    precision mediump float;
    #endif
    varying vec2 vTexCoord;
    uniform sampler2D uTexture;
    uniform vec2 uTextureSize;
    uniform float uStrength;

    bool looksDifferent(vec3 a, vec3 b) {
      vec3 delta = a - b;
      float luma   = dot(delta, vec3( 0.299,  0.587,  0.114));
      float chromaU = dot(delta, vec3(-0.169, -0.331,  0.500));
      float chromaV = dot(delta, vec3( 0.500, -0.419, -0.081));
      return abs(luma) > 0.188 || abs(chromaU) > 0.027 || abs(chromaV) > 0.031;
    }

    void main() {
      vec2 texel = 1.0 / uTextureSize;
      vec2 texelPos = vTexCoord * uTextureSize;
      vec2 pixelCentre = (floor(texelPos) + 0.5) * texel;
      vec2 withinPixel = fract(texelPos);
      vec2 quadrant = step(0.5, withinPixel);
      vec2 cornerDistance = abs(withinPixel - 0.5) * 2.0;

      vec3 centre = texture2D(uTexture, pixelCentre).rgb;
      vec3 above  = texture2D(uTexture, pixelCentre + vec2(0.0, -texel.y)).rgb;
      vec3 below  = texture2D(uTexture, pixelCentre + vec2(0.0,  texel.y)).rgb;
      vec3 left   = texture2D(uTexture, pixelCentre + vec2(-texel.x, 0.0)).rgb;
      vec3 right  = texture2D(uTexture, pixelCentre + vec2( texel.x, 0.0)).rgb;

      vec3 vertNeighbour  = (quadrant.y < 0.5) ? above : below;
      vec3 vertOpposite   = (quadrant.y < 0.5) ? below : above;
      vec3 horizNeighbour = (quadrant.x < 0.5) ? left  : right;
      vec3 horizOpposite  = (quadrant.x < 0.5) ? right : left;

      bool onDiagonalStep =
        !looksDifferent(vertNeighbour, horizNeighbour) &&
        looksDifferent(vertNeighbour, horizOpposite) &&
        looksDifferent(horizNeighbour, vertOpposite);

      vec3 colour = centre;
      if (onDiagonalStep) {
        float cutCoverage = step(0.6, cornerDistance.x + cornerDistance.y);
        vec3 cornerColour = 0.5 * (vertNeighbour + horizNeighbour);
        colour = mix(centre, cornerColour, uStrength * cutCoverage);
      }
      gl_FragColor = vec4(colour, 1.0);
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
    const hqxProgram = buildProgram(gl, GL_FRAGMENT_HQX_SRC);
    if (!smoothProgram || !smartProgram || !hqxProgram) return null;

    const smoothLocs = {
      pos: gl.getAttribLocation(smoothProgram, "aPosition"),
    };
    const smartLocs = {
      pos: gl.getAttribLocation(smartProgram, "aPosition"),
      size: gl.getUniformLocation(smartProgram, "uTextureSize"),
      smoothness: gl.getUniformLocation(smartProgram, "uSmoothness"),
    };
    const hqxLocs = {
      pos: gl.getAttribLocation(hqxProgram, "aPosition"),
      size: gl.getUniformLocation(hqxProgram, "uTextureSize"),
      strength: gl.getUniformLocation(hqxProgram, "uStrength"),
    };

    const quadBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);

    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    glState = {
      gl, smoothProgram, smartProgram, hqxProgram,
      smoothLocs, smartLocs, hqxLocs, quadBuffer, texture,
      lastTexFilterMode: null,
    };
    return glState;
  }

  function renderFilteredFrame(pixelBytes) {
    const state = ensureGL();
    if (!state) {
      currentFilter = "off";
      return false;
    }
    const {
      gl, smoothProgram, smartProgram, hqxProgram,
      smoothLocs, smartLocs, hqxLocs, quadBuffer, texture,
    } = state;
    const isSmart = currentFilter === "smart";
    const isHqx = currentFilter === "hq2x" || currentFilter === "hq4x";
    const program = isHqx ? hqxProgram : (isSmart ? smartProgram : smoothProgram);
    const locs = isHqx ? hqxLocs : (isSmart ? smartLocs : smoothLocs);

    gl.viewport(0, 0, canvasGL.width, canvasGL.height);
    gl.useProgram(program);

    gl.bindTexture(gl.TEXTURE_2D, texture);
    const filterMode = (isSmart || isHqx) ? gl.NEAREST : gl.LINEAR;
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
    } else if (isHqx) {
      gl.uniform2f(locs.size, WIDTH, HEIGHT);
      gl.uniform1f(locs.strength, hqxStrength);
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

    const scale = currentFilter === "hq4x" ? 4 : (currentFilter === "hq2x" ? 2 : 1);
    if (canvasGL.width !== WIDTH * scale) {
      canvasGL.width = WIDTH * scale;
      canvasGL.height = HEIGHT * scale;
    }

    const smoothnessRow = document.getElementById("smoothnessRow");
    if (smoothnessRow) smoothnessRow.hidden = currentFilter !== "smart";
    const hqxRow = document.getElementById("hqxRow");
    if (hqxRow) hqxRow.hidden = currentFilter !== "hq2x" && currentFilter !== "hq4x";
  }

  function applyTheme(themeId) {
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
    } catch (_) {   }
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
      } catch (_) {   }
    });
  }

  function loadVideoFilter() {
    try {
      const stored = localStorage.getItem(FILTER_KEY);
      if (stored === "off" || stored === "smooth" || stored === "smart"
          || stored === "hq2x" || stored === "hq4x") {
        currentFilter = stored;
      }
    } catch (_) {   }
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
      } catch (_) {   }
      applyFilterVisibility();
    });
  }

  function loadSmoothness() {
    try {
      const stored = parseFloat(localStorage.getItem(SMOOTHNESS_KEY));
      if (!isNaN(stored)) smartSmoothness = stored;
    } catch (_) {   }
    const slider = document.getElementById("smoothnessRange");
    const value = document.getElementById("smoothnessValue");
    if (slider) slider.value = smartSmoothness;
    if (value) value.textContent = smartSmoothness.toFixed(1);
  }

  function loadHqxStrength() {
    try {
      const stored = parseFloat(localStorage.getItem(HQX_KEY));
      if (!isNaN(stored)) hqxStrength = stored;
    } catch (_) {   }
    const slider = document.getElementById("hqxRange");
    const value = document.getElementById("hqxValue");
    if (slider) slider.value = hqxStrength;
    if (value) value.textContent = hqxStrength.toFixed(2);
  }

  function bindHqxSlider() {
    const slider = document.getElementById("hqxRange");
    const value = document.getElementById("hqxValue");
    if (!slider) return;
    slider.addEventListener("input", () => {
      hqxStrength = parseFloat(slider.value);
      if (value) value.textContent = hqxStrength.toFixed(2);
      try {
        localStorage.setItem(HQX_KEY, String(hqxStrength));
      } catch (_) {   }
    });
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
      } catch (_) {   }
    });
  }

  function toggleFullscreen() {
    const fsElement =
      document.fullscreenElement || document.webkitFullscreenElement;
    if (fsElement) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      return;
    }
    const request = gameStage.requestFullscreen || gameStage.webkitRequestFullscreen;
    if (request) {
      request.call(gameStage).catch(() => {
      });
    }
  }

  let controlsHidden = false;

  function setControlsHidden(hiddenState) {
    controlsHidden = hiddenState;
    gameStage.classList.toggle("controls-hidden", hiddenState);
    toggleControlsBtn.textContent = hiddenState ? "Show controls" : "Hide controls";
    toggleControlsBtn.setAttribute("aria-pressed", hiddenState ? "true" : "false");
  }

  function bindFullscreen() {
    canvas.addEventListener("dblclick", toggleFullscreen);
    canvasGL.addEventListener("dblclick", toggleFullscreen);

    const updateToggleVisibility = () => {
      const isFullscreen = !!(document.fullscreenElement || document.webkitFullscreenElement);
      toggleControlsBtn.hidden = !isFullscreen;
      if (!isFullscreen && controlsHidden) setControlsHidden(false);
    };
    document.addEventListener("fullscreenchange", updateToggleVisibility);
    document.addEventListener("webkitfullscreenchange", updateToggleVisibility);

    toggleControlsBtn.addEventListener("click", () => setControlsHidden(!controlsHidden));
  }

  function clearScreen() {
    ctx.fillStyle = "#000000";
    ctx.fillRect(0, 0, WIDTH, HEIGHT);

    if (glState) {
      const { gl } = glState;
      gl.viewport(0, 0, canvasGL.width, canvasGL.height);
      gl.clearColor(0, 0, 0, 1.0);
      gl.clear(gl.COLOR_BUFFER_BIT);
    }
  }

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
  const savDownload = document.getElementById("savDownload");
  const saveDelete = document.getElementById("saveDelete");
  const saveFileEl = document.getElementById("saveFile");
  const savConvertFileEl = document.getElementById("savConvertFile");
  const stopBtn = document.getElementById("stopBtn");
  const fastForwardBtn = document.getElementById("fastForwardBtn");
  const requestControlBtn = document.getElementById("requestControlBtn");
  const grantControlBtn = document.getElementById("grantControlBtn");
  const stopMsg = document.getElementById("stopMsg");

  let SAMPLE_RATE = 24000;
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

  function setStatus(text, live) {
    statusEl.textContent = text;
    liveDot.classList.toggle("live", !!live);
  }

  let audioCtx = null;
  let nextStartTime = 0;
  let audioEnabled = false;
  let audioMuted = false;

  const TARGET_LATENCY = 0.10;

  const MAX_SCHEDULE_AHEAD = 0.7;

  const MAX_REBUILD_PULL = 0.01;

  const SMOOTH_ALPHA = 0.35;
  let smoothPrevL = 0;
  let smoothPrevR = 0;
  const DENORMAL_FLOOR = 1e-6;

  function ensureAudioContext() {
    if (audioCtx) return;
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: SAMPLE_RATE });
    nextStartTime = audioCtx.currentTime + TARGET_LATENCY;
    audioEnabled = true;
  }

  function resetAudioSchedule() {
    if (audioCtx) nextStartTime = audioCtx.currentTime + TARGET_LATENCY;
    smoothPrevL = 0;
    smoothPrevR = 0;
  }

  function playAudioChunk(int8Bytes) {
    if (!audioEnabled || !audioCtx || audioMuted) return;
    const nSamples = int8Bytes.length / 2;
    if (nSamples < 1) return;

    if (captureArmed) {
      if (captureBytes < captureLimitBytes) {
        captureChunks.push(int8Bytes.slice());
        captureBytes += int8Bytes.length;
      } else {
        captureArmed = false;
        console.log(`[audio-capture] ${(captureBytes / (SAMPLE_RATE * 2)).toFixed(1)}s captured - call __downloadAudioCapture() to save it.`);
      }
    }

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

    const source = audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(audioCtx.destination);

    const now = audioCtx.currentTime;
    if (diag) recordDiag(now, nSamples);
    if (nextStartTime < now) {
      nextStartTime = now;
    } else if (nextStartTime > now + MAX_SCHEDULE_AHEAD) {
      nextStartTime = now + TARGET_LATENCY;
    }

    const cushion = nextStartTime - now;
    let rate = 1;
    if (cushion < TARGET_LATENCY) {
      rate = 1 - MAX_REBUILD_PULL * Math.min(1, (TARGET_LATENCY - cushion) / TARGET_LATENCY);
    }
    source.playbackRate.value = rate;

    source.start(nextStartTime);
    nextStartTime += buffer.duration / rate;
  }

  let captureArmed = false;
  let captureChunks = [];
  let captureBytes = 0;
  let captureLimitBytes = 0;

  window.__startAudioCapture = function (seconds = 15) {
    captureChunks = [];
    captureBytes = 0;
    captureLimitBytes = Math.round(SAMPLE_RATE * 2 * seconds);
    captureArmed = true;
    console.log(`[audio-capture] armed for ${seconds}s - play something, then call __downloadAudioCapture().`);
  };

  let diag = null;

  function recordDiag(now, nSamples) {
    diag.chunkCount++;
    if (diag.lastArrival !== null) {
      const gapMs = (now - diag.lastArrival) * 1000;
      if (gapMs < diag.minGapMs) diag.minGapMs = gapMs;
      if (gapMs > diag.maxGapMs) diag.maxGapMs = gapMs;
    }
    diag.lastArrival = now;
    const cushionMs = (nextStartTime - now) * 1000;
    if (cushionMs < diag.minCushionMs) diag.minCushionMs = cushionMs;
    if (nextStartTime < now) {
      diag.underrunCount++;
      console.warn(`[audio-diag] UNDERRAN by ${(-cushionMs).toFixed(1)}ms (lost that much audio; no gap inserted) - chunk #${diag.chunkCount}`);
    } else if (cushionMs < TARGET_LATENCY * 1000 * 0.5) {
      diag.stretchCount++;
    }
    if (nextStartTime > now + MAX_SCHEDULE_AHEAD) {
      diag.resetCount++;
      console.warn(`[audio-diag] SCHEDULE RESET - was ${cushionMs.toFixed(1)}ms ahead - chunk #${diag.chunkCount}`);
    }
    if (now - diag.lastSummary > 2) {
      console.log(diagSummary(nSamples));
      diag.lastSummary = now;
      diag.minGapMs = Infinity;
      diag.maxGapMs = 0;
      diag.minCushionMs = Infinity;
    }
  }

  function diagSummary(nSamples) {
    return `[audio-diag] ${diag.chunkCount} chunks, ${diag.underrunCount} underruns, `
      + `${diag.resetCount} resets, ${diag.stretchCount} stretched, `
      + `cushion min ${diag.minCushionMs.toFixed(1)}ms, `
      + `gap range ${diag.minGapMs.toFixed(1)}-${diag.maxGapMs.toFixed(1)}ms`
      + (nSamples ? `, this chunk had ${nSamples} samples` : "");
  }

  window.__audioDiagnostics = function (on = true) {
    if (!on) {
      if (diag) console.log("[audio-diag] stopped. " + diagSummary(0));
      diag = null;
      return;
    }
    diag = {
      underrunCount: 0, resetCount: 0, chunkCount: 0, stretchCount: 0,
      lastArrival: null, minGapMs: Infinity, maxGapMs: 0,
      minCushionMs: Infinity,
      lastSummary: audioCtx ? audioCtx.currentTime : 0,
    };
    console.log("[audio-diag] logging scheduling behaviour - __audioDiagnostics(false) to stop.");
  };

  function _buildWavFromInt8Chunks(chunks, sampleRate) {
    const totalBytes = chunks.reduce((n, c) => n + c.length, 0);
    const numChannels = 2;
    const bitsPerSample = 8;
    const byteRate = sampleRate * numChannels * (bitsPerSample / 8);
    const blockAlign = numChannels * (bitsPerSample / 8);
    const buffer = new ArrayBuffer(44 + totalBytes);
    const view = new DataView(buffer);
    function writeString(offset, str) {
      for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
    }
    writeString(0, "RIFF");
    view.setUint32(4, 36 + totalBytes, true);
    writeString(8, "WAVE");
    writeString(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, numChannels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, byteRate, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, bitsPerSample, true);
    writeString(36, "data");
    view.setUint32(40, totalBytes, true);

    let offset = 44;
    for (const chunk of chunks) {
      for (let i = 0; i < chunk.length; i++) {
        view.setUint8(offset++, (chunk[i] + 128) & 0xff);
      }
    }
    return buffer;
  }

  window.__downloadAudioCapture = function () {
    const chunks = captureChunks;
    if (chunks.length === 0) {
      console.warn("No audio captured - call __startAudioCapture() first, then play for a few seconds.");
      return;
    }
    const wavBuffer = _buildWavFromInt8Chunks(chunks, SAMPLE_RATE);
    const blob = new Blob([wavBuffer], { type: "audio/wav" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "raw_audio_capture.wav";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    console.log(`Downloaded raw_audio_capture.wav - ${chunks.length} chunks, ${(captureBytes / (SAMPLE_RATE * 2)).toFixed(1)}s captured.`);
  };

  function bindConnectGate() {
    const gate = document.getElementById("connectGate");
    const btn = document.getElementById("connectBtn");
    if (!gate || !btn) return;
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
          clearScreen();
        } else if (event.data === "controlrequested:1") {
          if (isController && grantControlBtn) grantControlBtn.hidden = false;
        } else if (event.data.startsWith("redirect:")) {
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
    if (!isController) return;
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
    document.body.classList.toggle("viewer-mode", !isController);

    if (requestControlBtn) requestControlBtn.hidden = isController;
    if (!isController && grantControlBtn) grantControlBtn.hidden = true;
  }

  function updateViewerCountUI(count) {
    const badge = document.getElementById("viewerCountBadge");
    if (!badge) return;
    if (count <= 0) {
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
    } catch (_) {
    }
  }

  function bindFastForward() {
    if (!fastForwardBtn) return;
    fastForwardBtn.addEventListener("click", toggleFastForward);
  }

  async function triggerReset() {
    if (!isController) return;
    try {
      const res = await fetch(apiPath("/api/reset"), {
        method: "POST",
        headers: { "X-Client-Id": CLIENT_ID },
      });
      if (!res.ok) {
        const data = await res.json();
        console.error("Reset failed:", data.error || res.status);
      }
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
        grantControlBtn.hidden = true;
      });
    }
  }

  function hapticTap() {
    if (!hapticsEnabled || !navigator.vibrate) return;
    try { navigator.vibrate(HAPTIC_MS); } catch (_) {   }
  }

  function bindButtons() {
    document.addEventListener("contextmenu", (e) => e.preventDefault());

    const activePointers = new Map();

    function buttonAt(x, y) {
      const el = document.elementFromPoint(x, y);
      return (el && el.closest(".btn[data-btn]")) || null;
    }

    function pressButton(btn) {
      btn.classList.add("pressed");
      hapticTap();
      sendInput("press", btn.dataset.btn);
      feedKonamiBuffer(btn.dataset.btn);
    }

    function releaseButton(btn) {
      btn.classList.remove("pressed");
      sendInput("release", btn.dataset.btn);
    }

    function handlePointerDown(e) {
      const btn = buttonAt(e.clientX, e.clientY);
      if (!btn) return;
      e.preventDefault();
      pressButton(btn);
      activePointers.set(e.pointerId, btn);
      startAudioAndHideHint();
    }

    function handlePointerMove(e) {
      if (!activePointers.has(e.pointerId)) return;
      const current = activePointers.get(e.pointerId);
      const btn = buttonAt(e.clientX, e.clientY);
      if (btn === current) return;
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
        e.preventDefault();
        if (!heldKeys.has(e.code)) {
          heldKeys.add(e.code);
          toggleFastForward();
        }
        return;
      }
      if (e.key === "*") {
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

  const GAMEPAD_BUTTON_MAP = {
    0: "a",
    1: "b",
    8: "select",
    9: "start",
    12: "up",
    13: "down",
    14: "left",
    15: "right",
  };
  const STICK_DEADZONE = 0.5;
  const FAST_FORWARD_GAMEPAD_BUTTON = 5;
  const RESET_GAMEPAD_BUTTON = 4;
  const SETTINGS_GAMEPAD_BUTTON = 3;
  const TURBO_A_GAMEPAD_BUTTON = 2;
  const TURBO_INTERVAL_MS = 100;

  let gamepadIndex = null;
  const gamepadHeld = new Set();
  let ffGamepadWasPressed = false;
  let resetGamepadWasPressed = false;
  let settingsGamepadWasPressed = false;
  let turboAPhaseOn = false;
  let turboALastToggleTime = 0;
  const menuNavWasPressed = { up: false, down: false, left: false, right: false, a: false, b: false };
  const konamiGamepadWasPressed = { up: false, down: false, left: false, right: false, a: false, b: false };
  const vkeyGamepadWasPressed = { up: false, down: false, left: false, right: false, a: false, b: false };
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
        const settingsBtnState = pad.buttons[SETTINGS_GAMEPAD_BUTTON];
        const settingsIsDown = !!settingsBtnState && settingsBtnState.pressed;
        if (settingsIsDown && !settingsGamepadWasPressed) setSettingsOpen(!settingsOpen);
        settingsGamepadWasPressed = settingsIsDown;

        const konamiButtons = { up: 12, down: 13, left: 14, right: 15, a: 0, b: 1 };
        for (const [action, idx] of Object.entries(konamiButtons)) {
          const btn = pad.buttons[idx];
          const isDown = !!btn && btn.pressed;
          if (isDown && !konamiGamepadWasPressed[action]) feedKonamiBuffer(action);
          konamiGamepadWasPressed[action] = isDown;
        }

        if (isVkeyboardOpen()) {
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
          for (const [idx, name] of Object.entries(GAMEPAD_BUTTON_MAP)) {
            const btn = pad.buttons[idx];
            const isDown = !!btn && btn.pressed;
            if (isDown) pressLogical(name);
            else releaseLogical(name);
          }

          const x = pad.axes[0] || 0;
          const y = pad.axes[1] || 0;
          if (y < -STICK_DEADZONE) pressLogical("up"); else if (!pad.buttons[12] || !pad.buttons[12].pressed) releaseLogical("up");
          if (y > STICK_DEADZONE) pressLogical("down"); else if (!pad.buttons[13] || !pad.buttons[13].pressed) releaseLogical("down");
          if (x < -STICK_DEADZONE) pressLogical("left"); else if (!pad.buttons[14] || !pad.buttons[14].pressed) releaseLogical("left");
          if (x > STICK_DEADZONE) pressLogical("right"); else if (!pad.buttons[15] || !pad.buttons[15].pressed) releaseLogical("right");

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
            turboAPhaseOn = false;
            releaseLogical("a");
          }
        }

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

  function startAudioAndHideHint() {
    ensureAudioContext();
    if (audioCtx.state === "suspended") audioCtx.resume();
  }
  document.body.addEventListener("pointerdown", startAudioAndHideHint, { once: true });

  function loadHapticSetting() {
    try {
      const stored = localStorage.getItem(HAPTIC_KEY);
      if (stored !== null) hapticsEnabled = stored === "1";
    } catch (_) {   }
    if (hapticToggle) hapticToggle.checked = hapticsEnabled;
  }

  function bindHapticSetting() {
    hapticToggle.addEventListener("change", () => {
      hapticsEnabled = hapticToggle.checked;
      try { localStorage.setItem(HAPTIC_KEY, hapticsEnabled ? "1" : "0"); } catch (_) {   }
    });
  }

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
    } catch (_) {   }
    updateMuteButtonUI();
  }

  function bindMuteButton() {
    const btn = document.getElementById("muteBtn");
    if (!btn) return;
    btn.addEventListener("click", () => {
      audioMuted = !audioMuted;
      try { localStorage.setItem(MUTE_KEY, audioMuted ? "1" : "0"); } catch (_) {   }
      updateMuteButtonUI();
    });
  }

  async function loadBufferSetting() {
    try {
      const res = await fetch(apiPath("/api/audio-batch"));
      const data = await res.json();
      applyBufferTicks(data.ticks, false);
    } catch (_) {   }
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

  function setSettingsOpen(open) {
    settingsOpen = open;
    settingsPanel.hidden = !open;
    settingsBackdrop.hidden = !open;
    settingsBtn.setAttribute("aria-expanded", open ? "true" : "false");
    document.body.style.overflow = open ? "hidden" : "";
    if (open && chatOpen) setChatOpen(false);
    if (open && helpOpen) setHelpOpen(false);
    if (open && cheatPanelOpen) setCheatPanelOpen(false);
    if (open) {
      const elements = getFocusableSettingsElements();
      if (elements.length > 0) elements[0].focus();

      if (turboAPhaseOn) {
        turboAPhaseOn = false;
        releaseLogical("a");
      }
    } else if (isVkeyboardOpen()) {
      closeVirtualKeyboard();
    }
  }

  function getFocusableElementsIn(container) {
    if (!container) return [];
    return Array.from(container.querySelectorAll('button, select, input:not([type="file"]), textarea, [tabindex]'))
      .filter((el) => !el.disabled && el.offsetParent !== null);
  }

  function moveFocusIn(container, direction) {
    const elements = getFocusableElementsIn(container);
    if (elements.length === 0) return;
    const currentIndex = elements.indexOf(document.activeElement);
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
  }

  function getFocusableSettingsElements() {
    return getFocusableElementsIn(settingsPanel);
  }
  function moveSettingsFocus(direction) {
    moveFocusIn(settingsPanel, direction);
  }
  function adjustFocusedSettingsElement(direction) {
    adjustFocusedElementIn(settingsPanel, direction);
  }

  let vkeyTargetInput = null;
  let vkeyRow = 0;
  let vkeyCol = 0;

  function getVkeyRows() {
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
      el.click();
    } else if (el.tagName === "INPUT" && el.type === "checkbox") {
      el.checked = !el.checked;
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else if ((el.tagName === "INPUT" && el.type === "text") || el.tagName === "TEXTAREA") {
      openVirtualKeyboard(el);
    } else if (el.tagName === "SELECT") {
      adjustFocusedElementIn(container, 1);
    }
  }

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

  function setChatOpen(open) {
    chatOpen = open;
    chatPanel.hidden = !open;
    chatBackdrop.hidden = !open;
    chatBtn.setAttribute("aria-expanded", open ? "true" : "false");
    document.body.style.overflow = open ? "hidden" : "";
    if (open && settingsOpen) setSettingsOpen(false);
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
    if (open && settingsOpen) setSettingsOpen(false);
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

  const KONAMI_SEQUENCE = [
    "up", "up", "down", "down",
    "left", "right", "left", "right",
    "b", "a",
  ];
  let konamiBuffer = [];
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
      feedKonamiBuffer(KEY_MAP[e.code]);
    });
  }

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
      if (settingsOpen) setSettingsOpen(false);
      if (chatOpen) setChatOpen(false);
      if (helpOpen) setHelpOpen(false);
      cheatFocusRow = 0;
      cheatFocusCol = 0;
      cheatCodesInput.focus();

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
    textSpan.textContent = entry.text;

    line.appendChild(roleSpan);
    line.appendChild(textSpan);
    chatMessages.appendChild(line);

    const nearBottom = chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight < 60;
    if (nearBottom || chatOpen) chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  const CHAT_NAME_KEY = "gbserver.chatName";

  function loadChatName() {
    if (!chatNameInput) return;
    try {
      const stored = localStorage.getItem(CHAT_NAME_KEY);
      if (stored) chatNameInput.value = stored;
    } catch (_) {   }
  }

  function bindChatNameInput() {
    if (!chatNameInput) return;
    chatNameInput.addEventListener("change", () => {
      try {
        localStorage.setItem(CHAT_NAME_KEY, chatNameInput.value.trim());
      } catch (_) {   }
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

  const LIBRARY_POLL_MS = 5000;
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

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  let lastRomsRes = { roms: [] };
  let lastConfigRes = {};

  function renderRomList() {
    const romsRes = lastRomsRes;
    const configRes = lastConfigRes;
    const query = (romSearchEl && romSearchEl.value.trim().toLowerCase()) || "";
    const filteredRoms = query
      ? romsRes.roms.filter((rom) => rom.filename.toLowerCase().includes(query))
      : romsRes.roms;

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
          updateEngineSelectState();
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

    if (romsFetch.status === 404 || configFetch.status === 404) {
      showRoomMissingBanner();
      return;
    }

    const [romsRes, configRes] = await Promise.all([romsFetch.json(), configFetch.json()]);

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

    if (audioBadge) {
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

    savDownload.addEventListener("click", async () => {
      setSaveMsg("Extracting .sav\u2026", null);
      try {
        const res = await fetch(apiPath("/api/sav"), {
          headers: { "X-Client-Id": CLIENT_ID },
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          setSaveMsg(data.error || "Could not extract a .sav for this session", "error");
          return;
        }
        const blob = await res.blob();
        const disposition = res.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="?([^";]+)"?/);
        const filename = match ? match[1] : "save.sav";
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
        setSaveMsg("Failed to download .sav", "error");
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
          if (data.rom) romSelectEl.value = data.rom;
          refreshLibrary();
        } else {
          setSaveMsg(data.error || "Upload failed", "error");
        }
      } catch (_) {
        setSaveMsg("Upload failed", "error");
      }
    });

    savConvertFileEl.addEventListener("change", async () => {
      const file = savConvertFileEl.files && savConvertFileEl.files[0];
      savConvertFileEl.value = "";
      if (!file) return;
      const formData = new FormData();
      formData.append("sav", file);
      setSaveMsg("Converting save\u2026", null);
      try {
        const res = await fetch(apiPath("/api/convert-save"), {
          method: "POST",
          headers: { "X-Client-Id": CLIENT_ID },
          body: formData,
        });
        const data = await res.json();
        if (data.ok) {
          setSaveMsg("Save converted \u2014 play to your save point, then use \u201cSave now\u201d to capture it", "ok");
          resetAudioSchedule();
          if (data.rom) romSelectEl.value = data.rom;
          refreshLibrary();
        } else {
          setSaveMsg(data.error || "Conversion failed", "error");
        }
      } catch (_) {
        setSaveMsg("Conversion failed", "error");
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
        if (data.has_save) {
          window.location.href = apiPath("/api/save");
        }
      }
      refreshLibrary();
    });
  }

  function bindCreateRoom() {
    const btn = document.getElementById("createRoomBtn");
    const msg = document.getElementById("createRoomMsg");
    if (!btn) return;
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
    if (!btn) return;
    const originalText = btn.textContent;
    btn.addEventListener("click", async () => {
      const link = window.location.href;
      try {
        await navigator.clipboard.writeText(link);
        btn.textContent = "Copied!";
      } catch (_) {
        window.prompt("Copy this link:", link);
      }
      setTimeout(() => { btn.textContent = originalText; }, 1500);
    });
  }

  clearScreen();

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
    bindSharedDisabledCreateRoom();
    return;
  }

  fetch(apiPath("/api/config"))
    .then((r) => r.json())
    .then((cfg) => {
      SAMPLE_RATE = cfg.sample_rate || SAMPLE_RATE;
      applyBufferTicks(cfg.audio_batch_ticks || 4, false);
    });

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
  loadHqxStrength();
  bindHqxSlider();
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
