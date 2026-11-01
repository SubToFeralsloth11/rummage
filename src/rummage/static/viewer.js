// Rummage 3D viewer: glTF/GLB, FBX, OBJ(+MTL), STL, PLY, Collada, Blockbench .bbmodel.
// Animations, texture picker for textures stored elsewhere, wireframe, auto-rotate.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

const D2R = Math.PI / 180;
const ICON = {
  play: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M7 5v14l12-7z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>',
  rotate: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/></svg>',
  wire: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 3v18M4 7.5l16 9M20 7.5l-16 9"/></svg>',
  grid: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/></svg>',
  reset: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/></svg>',
  texture: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h6V3M9 15h6V9M15 21v-6h6"/></svg>',
  full: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>',
};

export async function mountViewer(container, opts) {
  const { url, ext, fileId, onStatus = () => {} } = opts;
  container.innerHTML = "";
  container.classList.add("v3d");
  const canvasWrap = document.createElement("div");
  canvasWrap.className = "v3d-canvas";
  container.appendChild(canvasWrap);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  canvasWrap.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x10131b);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x2a2f3d, 0.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(3, 6, 4);
  scene.add(sun);

  const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.autoRotateSpeed = 1.5;

  const missing = new Set();
  const manager = new THREE.LoadingManager();
  manager.onError = (u) => missing.add(decodeURIComponent(u.split("/").pop()));
  manager.onProgress = (_u, loaded, total) => onStatus("textures", `Loading textures ${loaded}/${total}`);

  let model, flipY = true, animations = [];
  try {
    onStatus("model", "Building the 3D model");
    ({ model, animations, flipY } = await loadModel(ext, url, manager));
  } catch (e) {
    dispose();
    throw e;
  }
  scene.add(model);

  // Fit the camera to the model.
  const box = new THREE.Box3().setFromObject(model);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  const grid = new THREE.GridHelper(maxDim * 4, 20, 0x3a4260, 0x232838);
  grid.position.y = box.min.y;
  scene.add(grid);
  function resetView() {
    const dist = maxDim / (2 * Math.tan((camera.fov * D2R) / 2)) * 1.7;
    camera.position.copy(center).add(new THREE.Vector3(dist * 0.6, dist * 0.45, dist * 0.8));
    camera.near = dist / 200;
    camera.far = dist * 50;
    camera.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();
  }
  resetView();

  // Stats line.
  let tris = 0, meshes = 0;
  model.traverse((o) => {
    if (o.isMesh && o.geometry) {
      meshes++;
      const g = o.geometry;
      tris += (g.index ? g.index.count : g.attributes.position.count) / 3;
    }
  });

  // ---------- animation ----------
  const mixer = animations.length ? new THREE.AnimationMixer(model) : null;
  let action = null, playing = true, speed = 1;
  function playClip(i) {
    if (!mixer) return;
    mixer.stopAllAction();
    action = mixer.clipAction(animations[i]);
    action.reset().play();
    playing = true;
    updateAnimUI();
  }

  // ---------- toolbar ----------
  const bar = document.createElement("div");
  bar.className = "v3d-bar";
  bar.innerHTML = `
    <button data-a="rotate" title="Spin">${ICON.rotate}</button>
    <button data-a="wire" title="Wireframe">${ICON.wire}</button>
    <button data-a="grid" title="Grid" class="on">${ICON.grid}</button>
    <button data-a="reset" title="Reset view">${ICON.reset}</button>
    <button data-a="texture" title="Apply a texture from this project">${ICON.texture}</button>
    <button data-a="full" title="Fullscreen">${ICON.full}</button>
    <span class="v3d-info">${meshes} mesh${meshes === 1 ? "" : "es"} · ${Math.round(tris).toLocaleString()} triangles${animations.length ? ` · ${animations.length} animation${animations.length === 1 ? "" : "s"}` : ""}</span>`;
  container.appendChild(bar);

  const anim = document.createElement("div");
  anim.className = "v3d-anim";
  if (mixer) {
    anim.innerHTML = `
      <button data-a="play" title="Play / pause"></button>
      <select data-a="clip">${animations.map((c, i) => `<option value="${i}">${escapeHtml(c.name || "Animation " + (i + 1))}</option>`).join("")}</select>
      <input data-a="scrub" type="range" min="0" max="1000" value="0">
      <span data-a="time">0.00s</span>
      <select data-a="speed"><option value="0.25">0.25×</option><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option></select>`;
    container.appendChild(anim);
  }
  const note = document.createElement("div");
  note.className = "v3d-note";
  container.appendChild(note);

  function updateAnimUI() {
    if (!mixer) return;
    anim.querySelector('[data-a="play"]').innerHTML = playing ? ICON.pause : ICON.play;
  }
  if (mixer) {
    anim.querySelector('[data-a="clip"]').onchange = (e) => playClip(+e.target.value);
    anim.querySelector('[data-a="play"]').onclick = () => { playing = !playing; updateAnimUI(); };
    anim.querySelector('[data-a="speed"]').onchange = (e) => (speed = +e.target.value);
    const scrub = anim.querySelector('[data-a="scrub"]');
    scrub.oninput = () => {
      if (!action) return;
      playing = false;
      updateAnimUI();
      action.time = (scrub.value / 1000) * action.getClip().duration;
      mixer.update(0);
    };
    playClip(0);
  }

  let wire = false;
  const originalMaps = new Map();
  bar.onclick = async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const a = b.dataset.a;
    if (a === "rotate") { controls.autoRotate = !controls.autoRotate; b.classList.toggle("on", controls.autoRotate); }
    if (a === "wire") {
      wire = !wire;
      b.classList.toggle("on", wire);
      eachMaterial(model, (m) => { m.wireframe = wire; });
    }
    if (a === "grid") { grid.visible = !grid.visible; b.classList.toggle("on", grid.visible); }
    if (a === "reset") resetView();
    if (a === "full") container.requestFullscreen?.();
    if (a === "texture") togglePicker();
  };

  // ---------- texture picker ----------
  let picker = null;
  async function togglePicker() {
    if (picker) { picker.remove(); picker = null; return; }
    picker = document.createElement("div");
    picker.className = "v3d-picker";
    picker.innerHTML = `<div class="v3d-picker-head">Textures in this project<button data-x>Reset</button></div><div class="v3d-picker-grid">Looking…</div>`;
    container.appendChild(picker);
    picker.querySelector("[data-x]").onclick = () => {
      eachMaterial(model, (m) => {
        if (originalMaps.has(m)) { m.map = originalMaps.get(m); m.needsUpdate = true; }
      });
    };
    const res = await fetch(`/api/nearby-images?id=${fileId}`).then((r) => r.json()).catch(() => ({ images: [] }));
    const gridEl = picker?.querySelector(".v3d-picker-grid");
    if (!gridEl) return;
    if (!res.images?.length) { gridEl.textContent = "No images found near this model."; return; }
    gridEl.innerHTML = res.images.map((im, i) =>
      `<button data-i="${i}" title="${escapeHtml(im.dir + "/" + im.name)}"><img loading="lazy" src="${im.url}"><span>${escapeHtml(im.name)}</span></button>`).join("");
    gridEl.onclick = (e) => {
      const b = e.target.closest("button[data-i]");
      if (!b) return;
      const im = res.images[+b.dataset.i];
      onStatus("textures", "Applying " + im.name);
      new THREE.TextureLoader().load(im.url, (tex) => {
        tex.colorSpace = THREE.SRGBColorSpace;
        tex.flipY = flipY;
        if (ext === "bbmodel" || tex.image.width <= 128) tex.magFilter = THREE.NearestFilter;
        eachMaterial(model, (m) => {
          if (!originalMaps.has(m)) originalMaps.set(m, m.map || null);
          m.map = tex;
          if (m.color) m.color.set(0xffffff);
          m.needsUpdate = true;
        });
        onStatus(null);
        gridEl.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      });
    };
  }

  // ---------- render loop ----------
  const clock = new THREE.Clock();
  let raf = 0;
  function frame() {
    raf = requestAnimationFrame(frame);
    const dt = clock.getDelta();
    if (mixer && playing) {
      mixer.update(dt * speed);
      if (action) {
        const d = action.getClip().duration || 1;
        const t = action.time % d;
        const scrub = anim.querySelector('[data-a="scrub"]');
        if (document.activeElement !== scrub) scrub.value = Math.round((t / d) * 1000);
        anim.querySelector('[data-a="time"]').textContent = t.toFixed(2) + "s";
      }
    }
    controls.update();
    renderer.render(scene, camera);
  }
  const ro = new ResizeObserver(() => {
    const w = canvasWrap.clientWidth, h = canvasWrap.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  });
  ro.observe(canvasWrap);
  frame();

  // Report textures that couldn't be found (after loaders settle).
  setTimeout(() => {
    if (missing.size) {
      note.innerHTML = `${missing.size} texture${missing.size === 1 ? "" : "s"} not found: ${[...missing].slice(0, 4).map(escapeHtml).join(", ")}. Use the texture button to pick one.`;
      note.classList.add("show");
    }
  }, 1500);
  onStatus(null);

  function dispose() {
    cancelAnimationFrame(raf);
    ro.disconnect();
    controls.dispose();
    scene.traverse((o) => {
      o.geometry?.dispose();
      const mats = Array.isArray(o.material) ? o.material : o.material ? [o.material] : [];
      mats.forEach((m) => { Object.values(m).forEach((v) => v?.isTexture && v.dispose()); m.dispose(); });
    });
    pmrem.dispose();
    renderer.dispose();
    renderer.forceContextLoss();
    container.innerHTML = "";
  }
  return { dispose };
}

function eachMaterial(root, fn) {
  root.traverse((o) => {
    if (!o.isMesh && !o.isPoints) return;
    (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => m && fn(m));
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const plainMaterial = (vertexColors = false) =>
  new THREE.MeshStandardMaterial({ color: vertexColors ? 0xffffff : 0xb8c0d8, metalness: 0.1, roughness: 0.6, vertexColors, side: THREE.DoubleSide });

async function loadModel(ext, url, manager) {
  if (ext === "glb" || ext === "gltf") {
    const { GLTFLoader } = await import("three/addons/loaders/GLTFLoader.js");
    const g = await new GLTFLoader(manager).loadAsync(url);
    return { model: g.scene, animations: g.animations || [], flipY: false };
  }
  if (ext === "fbx") {
    const { FBXLoader } = await import("three/addons/loaders/FBXLoader.js");
    const o = await new FBXLoader(manager).loadAsync(url);
    return { model: o, animations: o.animations || [], flipY: true };
  }
  if (ext === "obj") {
    const { OBJLoader } = await import("three/addons/loaders/OBJLoader.js");
    const { MTLLoader } = await import("three/addons/loaders/MTLLoader.js");
    const loader = new OBJLoader(manager);
    const text = await fetch(url).then((r) => r.text());
    const mtlName = (text.match(/^mtllib\s+(.+)$/m) || [])[1]?.trim() || url.split("/").pop().replace(/\.obj$/i, ".mtl");
    const base = url.slice(0, url.lastIndexOf("/") + 1);
    let hasMtl = false;
    // Fetch the .mtl ourselves so a missing one isn't reported as a missing texture.
    const mtlText = await fetch(base + encodeURIComponent(mtlName)).then((r) => (r.ok ? r.text() : null)).catch(() => null);
    if (mtlText) {
      const mtl = new MTLLoader(manager).setResourcePath(base).parse(mtlText, base);
      mtl.preload();
      loader.setMaterials(mtl);
      hasMtl = true;
    }
    const o = loader.parse(text);
    if (!hasMtl) o.traverse((m) => { if (m.isMesh) m.material = plainMaterial(); });
    return { model: o, animations: [], flipY: true };
  }
  if (ext === "stl") {
    const { STLLoader } = await import("three/addons/loaders/STLLoader.js");
    const geo = await new STLLoader(manager).loadAsync(url);
    geo.computeVertexNormals();
    const mesh = new THREE.Mesh(geo, plainMaterial(!!geo.attributes.color));
    mesh.rotation.x = -Math.PI / 2; // STL is usually Z-up
    return { model: wrap(mesh), animations: [], flipY: true };
  }
  if (ext === "ply") {
    const { PLYLoader } = await import("three/addons/loaders/PLYLoader.js");
    const geo = await new PLYLoader(manager).loadAsync(url);
    geo.computeVertexNormals();
    const obj = geo.index || geo.attributes.normal
      ? new THREE.Mesh(geo, plainMaterial(!!geo.attributes.color))
      : new THREE.Points(geo, new THREE.PointsMaterial({ size: 0.01, vertexColors: !!geo.attributes.color }));
    return { model: wrap(obj), animations: [], flipY: true };
  }
  if (ext === "dae") {
    const { ColladaLoader } = await import("three/addons/loaders/ColladaLoader.js");
    const c = await new ColladaLoader(manager).loadAsync(url);
    return { model: c.scene, animations: c.scene.animations || c.animations || [], flipY: true };
  }
  if (ext === "bbmodel") {
    const raw = await fetch(url).then((r) => r.text());
    return buildBBModel(JSON.parse(raw.startsWith("<lz>") ? lzutf8Decode(raw.slice(4)) : raw));
  }
  throw new Error(`No 3D preview for .${ext} files`);
}

// Blockbench compresses files as "<lz>" + LZUTF8 in "StorageBinaryString" encoding (15 bits per char).
function lzutf8Decode(s) {
  const bytes = [];
  let remainder = 0, state = 0;
  for (let i = 0; i < s.length; i++) {
    const value = s.charCodeAt(i);
    if (value >= 32768) { if (value === 32769) bytes.pop(); state = 0; continue; }
    if (state === 0) remainder = value;
    else {
      const v = (remainder << state) | (value >>> (15 - state));
      bytes.push((v >>> 8) & 255, v & 255);
      remainder = value & ((1 << (15 - state)) - 1);
    }
    state = state === 15 ? 0 : state + 1;
  }
  const out = [];
  for (let i = 0; i < bytes.length;) {
    const b = bytes[i];
    if (b >>> 6 !== 3 || i === bytes.length - 1 || bytes[i + 1] >>> 7 === 1) { out.push(b); i++; continue; }
    const len = b & 31;
    let dist;
    if (b >>> 5 === 6) { dist = bytes[i + 1]; i += 2; } else { dist = (bytes[i + 1] << 8) | bytes[i + 2]; i += 3; }
    const start = out.length - dist;
    for (let k = 0; k < len; k++) out.push(out[start + k]);
  }
  return new TextDecoder().decode(new Uint8Array(out));
}

function wrap(o) {
  const g = new THREE.Group();
  g.add(o);
  return g;
}

// ---------- Blockbench ----------
function num(v) {
  const n = typeof v === "number" ? v : parseFloat(v);
  return Number.isFinite(n) ? n : 0;
}

function buildBBModel(json) {
  const res = json.resolution || { width: 16, height: 16 };
  const loader = new THREE.TextureLoader();
  const hidden = new THREE.MeshBasicMaterial({ visible: false });
  const fallback = new THREE.MeshLambertMaterial({ color: 0x9aa3b5, side: THREE.DoubleSide });
  const textures = (json.textures || []).map((t) => {
    let map = null;
    if (typeof t.source === "string" && t.source.startsWith("data:")) {
      map = loader.load(t.source);
      map.magFilter = THREE.NearestFilter;
      map.minFilter = THREE.NearestFilter;
      map.colorSpace = THREE.SRGBColorSpace;
    }
    return {
      w: t.uv_width || res.width, h: t.uv_height || res.height,
      mat: map ? new THREE.MeshLambertMaterial({ map, transparent: true, alphaTest: 0.05, side: THREE.DoubleSide }) : fallback,
    };
  });
  const mats = [hidden, fallback, ...textures.map((t) => t.mat)];
  const texOf = (i) => (i === null || i === undefined || i === false ? null : textures[i] ? i : null);
  const elements = Object.fromEntries((json.elements || []).map((e) => [e.uuid, e]));
  // Blockbench 5 keeps group name/pivot/rotation in json.groups; the outliner only holds uuid + children.
  const groupDefs = Object.fromEntries((json.groups || []).map((g) => [g.uuid, g]));
  const groups = {};
  const root = new THREE.Group();

  // BoxGeometry face order: +x east, -x west, +y up, -y down, +z south, -z north.
  const FACE = ["east", "west", "up", "down", "south", "north"];

  function cube(el) {
    const from = el.from || [0, 0, 0], to = el.to || [1, 1, 1], inf = num(el.inflate);
    const sx = Math.abs(to[0] - from[0]) + inf * 2, sy = Math.abs(to[1] - from[1]) + inf * 2, sz = Math.abs(to[2] - from[2]) + inf * 2;
    const geo = new THREE.BoxGeometry(Math.max(sx, 0.001), Math.max(sy, 0.001), Math.max(sz, 0.001));
    const uv = geo.attributes.uv;
    geo.clearGroups();
    FACE.forEach((name, fi) => {
      const f = el.faces?.[name];
      const ti = texOf(f?.texture);
      geo.addGroup(fi * 6, 6, f ? (ti === null ? (f.texture === null ? 0 : 1) : ti + 2) : 0);
      if (!f || ti === null || !f.uv) return;
      const { w, h } = textures[ti];
      const [x1, y1, x2, y2] = f.uv.map(num);
      // corners clockwise from top-left, rotated by the face's rotation
      let c = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]];
      const steps = ((num(f.rotation) / 90) % 4 + 4) % 4;
      for (let s = 0; s < steps; s++) c = [c[3], c[0], c[1], c[2]];
      const order = [c[0], c[1], c[3], c[2]]; // BoxGeometry vertex order: TL, TR, BL, BR
      order.forEach(([u, v], k) => uv.setXY(fi * 4 + k, u / w, 1 - v / h));
    });
    uv.needsUpdate = true;
    const mesh = new THREE.Mesh(geo, mats);
    mesh.position.set((from[0] + to[0]) / 2, (from[1] + to[1]) / 2, (from[2] + to[2]) / 2);
    return mesh;
  }

  function polyMesh(el) {
    const verts = el.vertices || {};
    const pos = [], uvs = [], idx = [];
    const geo = new THREE.BufferGeometry();
    let gi = 0;
    for (const f of Object.values(el.faces || {})) {
      const ti = texOf(f.texture);
      const vk = f.vertices || [];
      if (vk.length < 3) continue;
      const tri = vk.length === 4 ? [[0, 1, 2], [0, 2, 3]] : [[0, 1, 2]];
      const start = idx.length;
      for (const t of tri) {
        for (const k of t) {
          const v = verts[vk[k]] || [0, 0, 0];
          const u = f.uv?.[vk[k]] || [0, 0];
          pos.push(num(v[0]), num(v[1]), num(v[2]));
          const tx = ti === null ? { w: 16, h: 16 } : textures[ti];
          uvs.push(num(u[0]) / tx.w, 1 - num(u[1]) / tx.h);
          idx.push(gi++);
        }
      }
      geo.addGroup(start, idx.length - start, ti === null ? 1 : ti + 2);
    }
    geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    geo.setAttribute("uv", new THREE.Float32BufferAttribute(uvs, 2));
    geo.setIndex(idx);
    geo.computeVertexNormals();
    return new THREE.Mesh(geo, mats);
  }

  function element(el, parentOrigin) {
    if (el.visibility === false || (el.type && el.type !== "cube" && el.type !== "mesh")) return null;
    const o = el.origin || [0, 0, 0];
    const pivot = new THREE.Group();
    pivot.position.set(o[0] - parentOrigin[0], o[1] - parentOrigin[1], o[2] - parentOrigin[2]);
    const r = el.rotation || [0, 0, 0];
    pivot.rotation.set(num(r[0]) * D2R, num(r[1]) * D2R, num(r[2]) * D2R, "ZYX");
    const mesh = el.type === "mesh" ? polyMesh(el) : cube(el);
    if (el.type !== "mesh") mesh.position.sub(new THREE.Vector3(...o));
    pivot.add(mesh);
    return pivot;
  }

  function node(n, parent, parentOrigin) {
    if (typeof n === "string") {
      const el = elements[n];
      const obj = el && element(el, parentOrigin);
      if (obj) parent.add(obj);
      return;
    }
    n = { ...(groupDefs[n?.uuid] || {}), ...n };
    if (n.visibility === false) return;
    const o = n.origin || [0, 0, 0];
    const g = new THREE.Group();
    g.name = n.name || "";
    g.position.set(o[0] - parentOrigin[0], o[1] - parentOrigin[1], o[2] - parentOrigin[2]);
    const r = n.rotation || [0, 0, 0];
    g.rotation.set(num(r[0]) * D2R, num(r[1]) * D2R, num(r[2]) * D2R, "ZYX");
    g.userData.base = { pos: g.position.clone(), rot: g.rotation.clone(), scale: g.scale.clone() };
    groups[n.uuid] = g;
    parent.add(g);
    for (const c of n.children || []) node(c, g, o);
  }
  for (const n of json.outliner || Object.keys(elements)) node(n, root, [0, 0, 0]);

  // Blockbench units are pixels (16 per block); scale so the model is a sensible size.
  root.scale.setScalar(1 / 16);
  const animations = (json.animations || []).map((a) => bbAnimation(a, groups)).filter(Boolean);
  return { model: wrap(root), animations, flipY: true };
}

function bbAnimation(a, groups) {
  const tracks = [];
  let maxT = num(a.length);
  for (const [uuid, animator] of Object.entries(a.animators || {})) {
    const g = groups[uuid];
    if (!g || !g.userData.base) continue;
    const base = g.userData.base;
    const byChannel = {};
    for (const kf of animator.keyframes || []) (byChannel[kf.channel] ||= []).push(kf);
    for (const [ch, kfs] of Object.entries(byChannel)) {
      kfs.sort((x, y) => num(x.time) - num(y.time));
      const times = kfs.map((k) => num(k.time));
      maxT = Math.max(maxT, ...times);
      const pt = (k) => k.data_points?.[0] || {};
      if (ch === "rotation") {
        const q = new THREE.Quaternion(), e = new THREE.Euler();
        const vals = kfs.flatMap((k) => {
          const p = pt(k);
          e.set(base.rot.x - num(p.x) * D2R, base.rot.y - num(p.y) * D2R, base.rot.z + num(p.z) * D2R, "ZYX");
          return q.setFromEuler(e).toArray();
        });
        tracks.push(new THREE.QuaternionKeyframeTrack(`${g.uuid}.quaternion`, times, vals));
      } else if (ch === "position") {
        const vals = kfs.flatMap((k) => {
          const p = pt(k);
          return [base.pos.x - num(p.x), base.pos.y + num(p.y), base.pos.z + num(p.z)];
        });
        tracks.push(new THREE.VectorKeyframeTrack(`${g.uuid}.position`, times, vals));
      } else if (ch === "scale") {
        const vals = kfs.flatMap((k) => {
          const p = pt(k);
          const s = (v) => (v === undefined || v === "" ? 1 : num(v));
          return [s(p.x), s(p.y), s(p.z)];
        });
        tracks.push(new THREE.VectorKeyframeTrack(`${g.uuid}.scale`, times, vals));
      }
    }
  }
  if (!tracks.length) return null;
  return new THREE.AnimationClip(a.name || "animation", maxT || -1, tracks);
}
