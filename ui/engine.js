/* Memory Bank network view.
 *
 * One dependency-free function: createMemoryGraph(root, { fetchJSON, hostEvents })
 * renders a live force-directed graph of the bank into `root` and returns
 * { destroy, refresh, refreshSoon }. The same source is used by the standalone
 * viewer, the Hermes dashboard tab and the Hermes desktop page (ui/build.py
 * inlines it into the latter two), so it never touches globals, never builds
 * markup from strings, and removes everything it adds when destroyed.
 *
 * The graph is laid out in three dimensions and drawn on an ordinary 2D
 * canvas: every node is projected through an orbiting camera and painted as
 * a ready-made picture of a small world, far ones first. Nothing but a canvas
 * is needed, which is what lets one file run unchanged in all three hosts.
 *
 * The picture is a small solar system: a deep background with stars that
 * turn with the view; every node a planet - a matte world with a surface of
 * its own, lit from one side, its far side in shadow - in the colour family
 * of its kind; the topics, which everything hangs off, as golden suns;
 * always-on memories with a ring round them; links drawn as gentle arcs in
 * the colour of what they lead to, and small points of light travelling
 * along them. Every number behind that look is in one block, LOOK, below;
 * the effects are turned down by themselves in a big bank, and all of the
 * motion stops for people who asked their system for less of it.
 */
function createMemoryGraph(root, opts) {
  'use strict';
  opts = opts || {};
  var fetchJSON = opts.fetchJSON;
  var doc = root.ownerDocument;
  var win = doc.defaultView;
  var dead = false;
  var stale = false;   // the viewer was restarted: the key in this tab is no longer valid
  var cleanups = [];

  var KIND_LABEL = { memory: 'Memories', entity: 'Entities', skill: 'Skills', session: 'Sessions', topic: 'Topics' };
  var KIND_ONE = { memory: 'Memory', entity: 'Entity', skill: 'Skill', session: 'Session', topic: 'Topic' };
  var PALETTE = {
    dark: { memory: '#f4b84a', entity: '#4cc9f0', skill: '#b794f6', session: '#8493b8', topic: '#6fdc8c' },
    light: { memory: '#c5820a', entity: '#0f86b6', skill: '#7446d8', session: '#5f6d8e', topic: '#23924a' }
  };
  var BASE_R = { memory: 5.4, entity: 4.1, skill: 4.3, session: 3.4, topic: 7.4 };
  var LINK_LEN = { mentions: 36, used: 42, 'in': 32, supersedes: 22, related: 24, continues: 28, from: 30 };
  var REL_TEXT = {
    mentions: 'mentions', used: 'used skill', 'in': 'in topic', supersedes: 'replaces', related: 'related to',
    continues: 'continues', from: 'learned in'
  };
  var VIEW_KEY = 'memorybank-view';      // the one thing kept between visits: '2d' or '3d'
  var FOV_TAN = 0.4663;                  // tan(25 deg): a 50 degree field of view across the canvas' smaller side
  var NEAR = 12;                         // nothing closer to the camera than this is drawn
  var YAW0 = -0.5, PITCH0 = 0.28, PITCH_MAX = 1.45;
  var KINDS = ['memory', 'entity', 'skill', 'session', 'topic'];
  var KIND_IX = { memory: 0, entity: 1, skill: 2, session: 3, topic: 4 };
  var LINK_SHADES = 5;                   // links are stroked in this many depth shades for each kind's colour, one path each
  var LINK_HOT = KINDS.length * LINK_SHADES;           // the bucket of the links of the selected or pointed-at node
  // A body's picture comes in four sizes (a canvas shrinks a picture crudely, and a small one enlarged is a blur).
  // The body fills 1/1.2 of its picture; the margin holds the line of air on its lit edge, or its shadow. The
  // first SMALL sizes are painted when the theme is read, the bigger ones when a body first shows that large.
  var SPRITES = [24, 64, 192, 384], SPRITE_PAD = 1.2, SMALL = 2;
  var SPRITE_UP = [9, 27, 84];           // a body whose radius is this many device pixels or more takes the next size up
  var SPARK_PX = 32, CORONA_PX = 128;    // the point of light on a link and the glow round a sun, one picture per kind
  var SPIN_WAIT = 4000, SPIN_RATE = 0.00011;           // idle turn: ms of quiet before it starts, radians per ms

  // ------------------------------------------------------------------ look
  //
  // Everything that decides how the picture looks, rather than what it shows,
  // is in this one block, so that it can be tuned after seeing it in a real
  // browser without reading the drawing code: change a number, reload, look.
  // The colours themselves are not here - but for the gold of the suns.
  // They come from the host's theme (readTheme: the text colour,
  // --mb-memory ... --mb-topic, --mb-bg, the page's own background); these
  // numbers say how they are mixed, how big and how strong. Sizes are CSS
  // pixels unless they say otherwise.
  var LOOK = {
    // --- Background, painted on the canvas behind everything.
    space: [8, 11, 22],          // dark theme: the base colour, when neither --mb-bg nor the page behind the view gives one
    paper: [244, 246, 250],      // light theme: the same
    spaceCore: [13, 19, 42],     // dark: added to the base in the middle of the view (a touch lighter and bluer)
    spaceRim: 0.6,               // dark: the base is multiplied by this in the corners
    paperCore: 0.75,             // light: how far the middle goes towards white
    paperRim: 0.05,              // light: how far the corners go towards the text colour
    edge: 14,                    // the background fades out over this many pixels at the border, so it meets any page softly
    backdropScale: 0.5,          // the still part of the background is kept at this share of the view's size (it has no detail)
    clouds: [                    // colour clouds: whose colour, centre (x, y) and radius (r) as shares of the view, strength (a)
      { kind: 'skill', x: 0.2, y: 0.26, r: 0.46, a: 0.1 },
      { kind: 'entity', x: 0.8, y: 0.72, r: 0.52, a: 0.085 },
      { kind: 'memory', x: 0.66, y: 0.1, r: 0.3, a: 0.045 }
    ],
    cloudPaper: 0.5,             // light theme: the clouds' strength is multiplied by this

    // --- Stars (dark theme only): points on a sphere far around the scene, turned by the camera's angles.
    stars: 2600,                 // on the whole sphere; about one in twelve is in view at a time
    starSeed: 20261,             // the same sky every time
    starShare: [0.72, 0.22, 0.06],   // how many are faint, middling, bright
    starSize: [0.9, 1.2, 1.6],   // the side of the square each is drawn as (small: on a dense screen a bigger one shows as a square)
    starInk: [0.3, 0.55, 0.9],   // and how strong
    starTint: [150, 180, 255],   // the text colour is mixed with this...
    starTintMix: 0.4,            // ...by this much
    twinkle: 0.1,                // share of the middling and bright ones that twinkle (when motion is allowed)
    twinklePace: [0.5, 1.6],     // radians per second, slowest and fastest
    twinkleDip: 0.6,             // how far a twinkling star dims at its faintest (0 = not at all, 1 = out)

    // --- Planets. Every node but a sun is a small world: a matte ball with a surface, lit from one side, its far
    //     side in shadow. The pictures are painted once per kind, variant, size and theme (paintBody), never
    //     while drawing.
    variants: 5,                 // how many different worlds each kind has; a node takes one by its id, so neighbours differ
    worldSeed: 4177,             // the same worlds every time
    worlds: { memory: 'gas', entity: 'ocean', skill: 'ice', session: 'rock', topic: 'lush' },   // which sort of world a kind is
    lamp: [-0.56, -0.62, 0.55],  // where the light comes from: the left, above, and this side of the picture (one lamp for all)
    night: [0.07, 0.46],         // how much of its colour the dark side keeps (dark, light theme)
    dusk: 0.42,                  // how soft the line between day and night is (0 = a hard edge)
    limb: 0.42,                  // how much the lit side darkens towards its edge
    air: [0.34, 0.14],           // the thin line of lit air along the day side's edge: strength (dark, light theme) ...
    airWidth: 0.05,              // ... and how far it reaches beyond the edge, in radii
    shadow: [0, 0.22],           // light theme: a soft shadow under each world instead of any glow: strength ...
    shadowAt: 0.075,             // ... how far down and to the right it is cast, in radii ...
    shadowBlur: 0.15,            // ... and how soft its edge is
    drift: [0.022, 0.1],         // how far a variant may stray from its kind's colour: hue (in turns) and lightness (share), either way
    paperLift: 1.5,              // light theme: the kinds' colours are darker there, so the worlds are painted this much lighter
    landGrain: 1.4,              // how coarse the ground of a sea-and-land world is (lower: fewer, bigger continents)
    // The sorts of world. Every tone is the kind's own colour changed - [hue turned by this many turns, saturation
    // times this, lightness times this] - so a custom --mb-* colour still gives a planet to match, and the tones
    // are muted (saturation well under 1): natural, not neon. `air` scales the line of air for that sort.
    gas: {                       // banded giants: belts and zones along the latitudes, pushed about by weather, sometimes a storm
      main: [0, 0.64, 0.8], pale: [0.012, 0.52, 1.04], deep: [-0.03, 0.64, 0.5], extra: [-0.065, 0.7, 0.48],   // zone, bright zone, belt, storm
      bands: [1.6, 3.4],         // belts from pole to pole, fewest and most
      swirl: [0.25, 0.75],       // how far the weather pushes them about
      storm: 0.6,                // share of these worlds that have a storm
      air: 0.8
    },
    ocean: {                     // sea worlds: the kind's colour is the sea; land, ice caps and weather over it
      main: [0, 0.64, 0.9], deep: [0.035, 0.7, 0.58], extra: [-0.17, 0.36, 0.54], other: [-0.38, 0.28, 0.66], pale: [0, 0.14, 1.36],   // shallows, deep sea, green land, dry land, ice and cloud
      land: [0.14, 0.34],        // share of the face that is land, least and most
      cap: [0.84, 0.97],         // the ice starts this far towards the poles
      sky: [0.25, 0.5],          // how much cloud
      air: 1
    },
    lush: {                      // land worlds (the topics, when they are not suns): the kind's colour is the land
      extra: [0, 0.62, 0.7], other: [-0.14, 0.45, 0.8], main: [0.17, 0.55, 0.52], deep: [0.2, 0.62, 0.32], pale: [0, 0.1, 1.36],   // green land, dry land, shallows, deep sea, ice and cloud
      land: [0.52, 0.72], cap: [0.84, 0.97], sky: [0.2, 0.45], air: 1
    },
    ice: {                       // ice giants: pale and smooth, a few faint bands, darker poles, sometimes a dark spot
      main: [0, 0.5, 0.84], pale: [-0.015, 0.44, 0.98], deep: [0.03, 0.36, 0.6], extra: [0.02, 0.36, 0.5],    // body, lighter band, poles, spot
      bands: [1, 2.2], swirl: [0.1, 0.3], storm: 0.4, air: 1.4
    },
    rock: {                      // moons: mottled rock, dark plains and craters; no air
      pale: [0, 0.6, 1.14], deep: [0, 0.55, 0.82], extra: [0.02, 0.65, 0.54],                               // highlands (lighter and darker), plains
      craters: [10, 18],         // how many, fewest and most
      craterSize: [0.06, 0.34],  // their radius, smallest and biggest, in the moon's radii (most are small)
      craterDepth: 0.55,         // how strongly their walls catch the light ...
      craterLight: 0.9,          // ... which never makes a wall lighter than this (1 = white): rock is matte, it has no glint
      air: 0
    },
    fade: 0.55,                  // depth: how far the farthest world goes towards the haze (0 = not at all) ...
    fadeSteps: 8,                // ... in this many steps, each a ready-made picture (the world stays solid, it is not seen through)
    haze: [22, 34, 78],          // dark theme: the haze is the background's colour moved towards this (dimmer and a little bluer) ...
    hazeMix: 0.35,               // ... by this much. (Light theme: the haze is the background's colour itself.)

    // --- Suns. The topics - the hubs that everything hangs off - are the suns of the picture: lit from within,
    //     no dark side. Nothing else glows: what matters more is bigger, not brighter. A sun is golden whatever
    //     the theme's topic colour is, and so is everything that goes by the topics' colour while they are suns
    //     (their mark in the legend, their links and the lights on them): these three colours are the gold.
    suns: true,                  // false: topics are drawn as planets like everything else (worlds.topic above), in the topic colour
    sunGold: [250, 190, 48],     // a sun's body, and the colour the topics go by ...
    sunCore: [253, 234, 170],    // ... its middle, a warm white (not a pure one: nothing in the picture is blown out) ...
    sunEdge: [222, 116, 26],     // ... and its edge, a deeper orange
    sunPaper: 0.06,              // light theme: all three are this much deeper, to stand on a pale page
    sunDrift: 0.16,              // how far a variant's body may stray from the gold, towards the edge's orange or the middle's white
    sunFall: [1, 1.5],           // how soon the face turns from the middle's colour to the edge's, least and most (higher: a smaller hot middle)
    sunGrain: 0.26,              // how mottled its face is ...
    sunGrainSize: [4, 7],        // ... and how fine the mottling, coarsest and finest
    sunSoft: 0.1,                // its edge fades out over this share of its radius (a soft disc, not a cut-out)
    corona: 0.28,                // dark theme: the glow round a sun, added to what is behind it: strength at the sun's edge ...
    coronaSize: 2.5,             // ... its radius in sun radii ...
    coronaReach: 56,             // ... but never more than this many pixels beyond the sun (a close-up is not all glow)
    coronaFall: [[0, 1], [0.4, 1], [0.5, 0.52], [0.66, 0.2], [0.84, 0.05], [1, 0]],   // how it falls off: [share of its radius, strength]
    coronaWarm: 0.5,             // its colour: the gold moved this far towards sunEdge

    // --- The ring of an always-on memory: a planetary ring, tilted, passing behind the planet and in front of it.
    ringSize: [1.42, 2.2],       // its inner and outer edge, in planet radii
    ringOpen: 0.3,               // how open it lies: its short axis as a share of its long one
    ringTurn: -0.32,             // how far it is turned on the screen, in radians (negative: its right end up)
    ringPale: [0.5, -0.12],      // its colour: the planet's kind moved this far towards white (dark theme) or black (light theme)
    ringInk: [0.9, 0.88],        // how solid it is at its densest
    ringGap: [0.56, 0.66, 0.75], // a division in it: from and to (shares of its width, inner edge to outer) and how empty
    ringShade: 0.2,              // how much of its light the part in the planet's shadow keeps
    ringPlain: 4,                // a planet smaller than this many pixels in radius gets the same ring as one thin line instead ...
    ringPlainInk: 0.6,           // ... this strong (as a share of ringInk: a line stands out more than a band of dust)

    // --- Links: gentle arcs in the colour of their busier end, laid over the picture (not added to it: the
    //     space between the worlds stays dark). They are quieter than the worlds they join.
    bend: 0.2,                   // how far the middle of a link is pushed aside, as a share of its length (3D: at most)
    bendFlat: 0.13,              // the same in the flat view (where every link bends the full amount)
    linkInk: [0.18, 0.32],       // strength of the nearest links (dark, light theme)
    linkTint: [-0.25, -0.18],    // the link's colour is the kind's moved this far towards white (+) or black (-)
    linkWide: 2,                 // the nearest are never wider than this
    linkCrowd: 1800,             // dark theme: with more links than this in view all are dimmed (by the square root of how many more),
                                 // so that a big bank is not a fog
    linkDeep: 1.4,               // in 3D they are drawn this much stronger (most of them are faded by distance)
    linkFar: 0.72,               // how much the farthest are faded (0 = not at all)
    linkDim: 0.05,               // strength while something is selected, pointed at or searched for
    hotInk: [0.5, 0.6],          // links of the selected or pointed-at node: strength (dark, light theme) ...
    hotWide: 2.4,                // ... never wider than this ...
    hotCrowd: 60,                // ... and with more of them than this, all are dimmed (by the square root of how many more)
    hotGlow: 0.12,               // the soft wider line under them: strength as a share of the link's own ...
    hotGlowWidth: 3,             // ... and width as a multiple of it

    // --- Light travelling along the links (only when motion is allowed): quiet traffic, not sparks.
    lights: 320,                 // most points of light on screen at once
    lightPace: [0.2, 0.34],      // trips per second, slowest and fastest link: one trip takes 3 to 5 seconds
    lightGap: 2.4,               // the pause between two lights on a link, in trips
    lightFar: 0.75,              // how much less often a link in the farthest shade carries light than one in the nearest
    lightSize: 6.5,              // size of a light in layout units (it shrinks with distance) ...
    lightPx: [3, 11],            // ... kept between these pixel sizes
    lightInk: [0.7, 0.8],        // strength of the nearest (dark, light theme)
    lightCore: 0.55,             // dark theme: the middle of a light is its link's colour moved this far towards white
    lightHot: 1.4,               // on the links of the selected or pointed-at node they are this much bigger
    burstPace: 1.5,              // a recall sends light out along the node's links: trips made during the pulse
    burstSize: 1.7,              // and that light is this much bigger

    // --- Marks: the selected node, the pointed-at one, a recall; and everything else while one of them, or a
    //     search, has the view.
    pickInk: 0.95,               // the ring round the selected node, in the accent colour: strength ...
    pickAt: 4.5,                 // ... and how many layout units outside the node it runs (outside its ring, if it wears one)
    hoverInk: 0.5,               // the thinner ring round the pointed-at node: strength ...
    hoverAt: 3.4,                // ... and distance
    pulseInk: 0.6,               // a recall's widening ring: strength at its start ...
    pulseReach: 46,              // ... and how far it travels, in layout units
    dimBody: 0.8,                // a world outside the selection or the search goes this far towards the haze: a dark shape of
                                 // itself, still solid (nothing shows through it), drawn under everything that is lit

    // --- Names.
    labelEdge: 3,                // width of the outline behind the letters ...
    labelEdgeInk: 0.72,          // ... and its strength (it is the background's colour)

    // --- Motion and level of detail.
    frame: 33,                   // ms between frames of everything that moves by itself (light, twinkle, idle turn)
    slowFrame: 12,               // when issuing one frame takes the script longer than this many ms, those run at half the rate
    lodLinks: 2500,              // more links than this on screen: the far ones are drawn ...
    lodBent: 2,                  // ... straight and thinner from about this depth shade on (0 = nearest; the flat view has only that one) ...
    lodThin: 0.8,                // ... by this much; of the nearer ones no more than about lodLinks are curved ...
    lodShort: 36,                // ... and none that is shorter than this on screen.
    lodEase: 0.2                 // None of that comes at a stroke: it sets in over this share of lodLinks more links, and a link
                                 // loses its bend little by little (by depth, by length, by its place in the queue) before it is straight
  };

  // ---------------------------------------------------------------- helpers

  function on(target, type, fn, options) {
    target.addEventListener(type, fn, options);
    cleanups.push(function () { target.removeEventListener(type, fn, options); });
  }
  function el(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function lerp(a, b, t) { return a + (b - a) * t; }
  // The 2D/3D choice is remembered where the host lets a page keep anything;
  // storage can be switched off or out of reach (private mode, a sandboxed
  // frame), and then the view simply starts in 3D every time.
  function storedFlat() {
    try { return win.localStorage.getItem(VIEW_KEY) === '2d'; } catch (err) { return false; }
  }
  function storeFlat(on) {
    try { win.localStorage.setItem(VIEW_KEY, on ? '2d' : '3d'); } catch (err) { /* not kept, nothing else to do */ }
  }
  function num(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
  function fmtDate(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }
  function ago(ts) {
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 45) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    if (s < 86400 * 45) return Math.round(s / 86400) + ' d ago';
    return fmtDate(ts);
  }
  // Listener on a short-lived element: it is collected with the element, so it
  // is deliberately not tracked (tracking would pin every old row in memory).
  function bind(node, type, fn) { node.addEventListener(type, fn); }
  var probe = doc.createElement('canvas');
  probe.width = probe.height = 1;
  var probeCtx = probe.getContext('2d', { willReadFrequently: true });
  // Any CSS colour (hex, rgb, oklch, a theme variable's value...) as [r, g, b],
  // resolved by the browser itself; null when it is not a colour - or, with
  // `solid`, when it is more than half see-through (a tint laid over a page
  // is not the page's colour).
  function rgbOf(cssColor, solid) {
    var value = (cssColor || '').trim();
    if (!value || !probeCtx) return null;
    try {
      probeCtx.fillStyle = '#010203';
      probeCtx.fillStyle = value;
      if (probeCtx.fillStyle === '#010203' && value.toLowerCase() !== '#010203') return null;
      probeCtx.clearRect(0, 0, 1, 1);
      probeCtx.fillRect(0, 0, 1, 1);
      var px = probeCtx.getImageData(0, 0, 1, 1).data;
      if (px[3] === 0 || (solid && px[3] < 128)) return null;
      return [px[0], px[1], px[2]];
    } catch (err) {
      return null;
    }
  }
  function rgba(rgb, a) { return 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',' + a + ')'; }
  // One colour moved part of the way (t) to another, as [r, g, b].
  function blend(from, to, t) {
    return [Math.round(lerp(from[0], to[0], t)), Math.round(lerp(from[1], to[1], t)), Math.round(lerp(from[2], to[2], t))];
  }
  // A colour moved part of the way to white (amount > 0) or to black (< 0).
  function toward(rgb, amount) { return blend(rgb, amount > 0 ? [255, 255, 255] : [0, 0, 0], Math.abs(amount)); }
  function lightness(rgb) { return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255; }
  // Three whole numbers scrambled into one of 32 bits. What a link looks like
  // beyond its ends - which way it bends, when its light sets off - is taken
  // from this, so it is the same on every frame and after every reload.
  function scramble(a, b, c) {
    var h = (Math.imul(a | 0, 73856093) ^ Math.imul(b | 0, 19349663) ^ Math.imul(c | 0, 83492791)) | 0;
    h = Math.imul(h ^ (h >>> 16), 2246822507);
    h = Math.imul(h ^ (h >>> 13), 3266489909);
    return (h ^ (h >>> 16)) >>> 0;
  }
  // A word as a whole number, the same number for the same word every time (something for scramble to work on).
  function wordSeed(word) {
    var text = String(word), h = 0;
    for (var i = 0; i < text.length; i++) h = (Math.imul(h, 31) + text.charCodeAt(i)) | 0;
    return h;
  }
  // A run of numbers between 0 and 1 that is the same every time for the
  // same seed: what a world's surface is rolled from, so that a planet looks
  // the same on every visit.
  function dice(seed) {
    var s = seed >>> 0;
    return function () { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
  }
  // 0 below `lo`, 1 above `hi`, a soft step between (the other way round when lo is the bigger).
  function smooth(lo, hi, v) {
    v = clamp((v - lo) / (hi - lo), 0, 1);
    return v * v * (3 - 2 * v);
  }
  // A colour as [hue, saturation, lightness], each 0..1, and back. The
  // worlds' palettes are a kind's colour turned, muted and darkened, which
  // is done in these terms.
  function toHsl(rgb) {
    var r = rgb[0] / 255, g = rgb[1] / 255, b = rgb[2] / 255, hi = Math.max(r, g, b), lo = Math.min(r, g, b);
    var l = (hi + lo) / 2, d = hi - lo, h = 0;
    if (!d) return [0, 0, l];
    if (hi === r) h = ((g - b) / d + 6) % 6; else if (hi === g) h = (b - r) / d + 2; else h = (r - g) / d + 4;
    return [h / 6, d / (1 - Math.abs(2 * l - 1)), l];
  }
  function fromHsl(h, s, l) {
    function part(n) {
      var k = (n + h * 12) % 12;
      return Math.round(255 * (l - s * Math.min(l, 1 - l) * Math.max(-1, Math.min(k - 3, 9 - k, 1))));
    }
    return [part(0), part(8), part(4)];
  }
  // A colour with its hue turned (in whole turns) and its saturation and lightness multiplied - but never more
  // colourful than it was. (A white with a hint of a tint counts as fully saturated in these terms, and would
  // otherwise come out of a darkening as a strong colour: the cap is the spread between its own channels.)
  function tone(rgb, turn, sat, light) {
    var c = toHsl(rgb), l = clamp(c[2] * light, 0, 0.96), room = 1 - Math.abs(2 * l - 1);
    var most = room > 0 ? c[1] * (1 - Math.abs(2 * c[2] - 1)) / room : 0;
    return fromHsl(((c[0] + turn) % 1 + 1) % 1, Math.min(clamp(c[1] * sat, 0, 1), most), l);
  }

  // ------------------------------------------------------------------- DOM

  var css = [
    '.mb-root{position:absolute;inset:0;overflow:hidden;border-radius:inherit;font:13px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;user-select:none;-webkit-user-select:none}',
    '.mb-root *{box-sizing:border-box}',
    '.mb-canvas{position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none;cursor:grab;outline:none}',
    '.mb-canvas.mb-grabbing{cursor:grabbing}.mb-canvas.mb-point{cursor:pointer}',
    '.mb-glass{background:color-mix(in srgb,currentColor 7%,transparent);border:1px solid color-mix(in srgb,currentColor 13%,transparent);border-radius:10px;backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}',
    '.mb-top{position:absolute;left:12px;top:12px;right:12px;display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap;pointer-events:none}',
    '.mb-top>*{pointer-events:auto}',
    '.mb-brand{padding:7px 12px 8px}',
    '.mb-brand b{display:block;font-size:14px;font-weight:650;letter-spacing:.01em}',
    '.mb-brand span{display:block;opacity:.62;font-size:11.5px;font-variant-numeric:tabular-nums}',
    '.mb-search{position:relative;flex:0 1 280px;min-width:190px}',
    '.mb-search input{width:100%;height:36px;padding:0 12px;border:0;background:transparent;color:inherit;font:inherit;outline:none}',
    '.mb-search input::placeholder{color:inherit;opacity:.45}',
    '.mb-results{position:absolute;left:-1px;right:-1px;top:40px;max-height:320px;overflow:auto;padding:4px;display:none;z-index:3}',
    '.mb-results.mb-on{display:block}',
    '.mb-row{display:flex;gap:8px;align-items:center;padding:5px 8px;border-radius:6px;cursor:pointer}',
    '.mb-row:hover,.mb-row.mb-cur{background:color-mix(in srgb,currentColor 11%,transparent)}',
    '.mb-row span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}',
    '.mb-row em{margin-left:auto;font-style:normal;opacity:.5;font-size:11px;flex:none}',
    '.mb-chips{display:flex;gap:2px;flex-wrap:wrap;padding:5px}',
    '.mb-chip{display:flex;align-items:center;gap:6px;height:26px;padding:0 9px;border-radius:999px;border:0;background:transparent;color:inherit;font:inherit;font-size:12px;cursor:pointer}',
    '.mb-chip:hover{background:color-mix(in srgb,currentColor 10%,transparent)}',
    '.mb-chip.mb-off{opacity:.36}',
    '.mb-chip i{font-style:normal;opacity:.55;font-variant-numeric:tabular-nums}',
    '.mb-dot{width:9px;height:9px;border-radius:50%;flex:none}',
    '.mb-btn{height:26px;padding:0 10px;border-radius:7px;border:1px solid color-mix(in srgb,currentColor 18%,transparent);background:transparent;color:inherit;font:inherit;font-size:12px;cursor:pointer}',
    '.mb-btn:hover{background:color-mix(in srgb,currentColor 10%,transparent)}',
    '.mb-side{position:absolute;top:12px;right:12px;bottom:12px;width:min(370px,calc(100% - 24px));display:none;flex-direction:column;z-index:4;user-select:text;-webkit-user-select:text}',
    '.mb-side.mb-on{display:flex}',
    '.mb-side-head{display:flex;align-items:center;gap:8px;padding:12px 12px 8px 14px}',
    '.mb-side-head span{font-size:11px;letter-spacing:.07em;text-transform:uppercase;opacity:.7}',
    '.mb-x{margin-left:auto;width:26px;height:26px;border-radius:6px;border:0;background:transparent;color:inherit;font-size:17px;line-height:1;cursor:pointer;opacity:.7}',
    '.mb-x:hover{background:color-mix(in srgb,currentColor 11%,transparent);opacity:1}',
    '.mb-side-body{padding:0 14px 14px;overflow:auto;flex:1;min-height:0}',
    '.mb-side h3{margin:0 0 8px;font-size:15px;line-height:1.3;font-weight:650;overflow-wrap:anywhere}',
    '.mb-text{white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 12px;opacity:.92}',
    '.mb-facts{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin:0 0 12px;font-size:12px}',
    '.mb-facts dt{opacity:.55}.mb-facts dd{margin:0;overflow-wrap:anywhere}',
    '.mb-h{margin:12px 0 4px;font-size:11px;letter-spacing:.07em;text-transform:uppercase;opacity:.55}',
    '.mb-quote{margin:0 0 6px;padding:5px 9px;border-left:2px solid color-mix(in srgb,currentColor 25%,transparent);font-size:12.5px;white-space:pre-wrap;overflow-wrap:anywhere}',
    '.mb-acts{display:flex;gap:6px;margin:4px 0 2px}',
    '.mb-bottom{position:absolute;left:12px;right:12px;bottom:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;pointer-events:none}',
    '.mb-bottom>*{pointer-events:auto}',
    '.mb-time{display:flex;align-items:center;gap:8px;padding:5px 12px 5px 6px;flex:0 1 420px;min-width:230px}',
    '.mb-time input{flex:1;min-width:60px;accent-color:currentColor;height:18px;margin:0}',
    '.mb-time span{font-size:11.5px;opacity:.7;font-variant-numeric:tabular-nums;white-space:nowrap;min-width:84px;text-align:right}',
    '.mb-play{width:26px;height:26px;border-radius:50%;border:0;background:transparent;color:inherit;cursor:pointer;font-size:11px;flex:none}',
    '.mb-play:hover{background:color-mix(in srgb,currentColor 11%,transparent)}',
    '.mb-live{padding:6px 11px;font-size:11.5px;display:flex;align-items:center;gap:7px;max-width:100%}',
    '.mb-live span{opacity:.75;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.mb-pip{width:7px;height:7px;border-radius:50%;background:currentColor;opacity:.35;flex:none}',
    '.mb-undo{display:none;padding:4px 6px 4px 11px;font-size:11.5px;align-items:center;gap:9px}',
    '.mb-undo.mb-on{display:flex}.mb-undo span{opacity:.8;white-space:nowrap}',
    '.mb-pip.mb-hot{opacity:1;animation:mb-blink 1.2s ease-out 1}',
    '@keyframes mb-blink{0%{transform:scale(2.2)}100%{transform:scale(1)}}',
    '.mb-tip{position:absolute;pointer-events:none;padding:6px 10px;max-width:300px;font-size:12px;display:none;z-index:5}',
    '.mb-tip b{display:block;font-weight:600;overflow-wrap:anywhere}.mb-tip span{opacity:.62;font-size:11px}',
    '.mb-msg{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);max-width:440px;padding:18px 22px;text-align:center;display:none}',
    '.mb-msg.mb-on{display:block}.mb-msg b{display:block;font-size:15px;margin-bottom:6px}.mb-msg p{margin:0 0 10px;opacity:.72}',
    '.mb-msg code{font:12px ui-monospace,Consolas,monospace;padding:2px 6px;border-radius:5px;background:color-mix(in srgb,currentColor 10%,transparent)}'
  ].join('\n');

  var style = el('style');
  style.textContent = css;
  var wrap = el('div', 'mb-root');
  var canvas = el('canvas', 'mb-canvas');
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', 'Network of memories, entities, skills and sessions');
  canvas.tabIndex = 0;
  var ctx = canvas.getContext('2d');

  var top = el('div', 'mb-top');
  var brand = el('div', 'mb-brand mb-glass');
  var brandTitle = el('b', null, opts.title || 'Memory Bank');
  var brandStats = el('span', null, 'loading…');
  brand.appendChild(brandTitle); brand.appendChild(brandStats);
  var searchBox = el('div', 'mb-search mb-glass');
  var searchInput = el('input');
  searchInput.type = 'search';
  searchInput.placeholder = 'Search memories, people, skills…   ( / )';
  searchInput.setAttribute('aria-label', 'Search the memory bank');
  searchInput.spellcheck = false;
  var results = el('div', 'mb-results mb-glass');
  searchBox.appendChild(searchInput); searchBox.appendChild(results);
  var chips = el('div', 'mb-chips mb-glass');
  top.appendChild(brand); top.appendChild(searchBox); top.appendChild(chips);

  var side = el('div', 'mb-side mb-glass');
  var sideHead = el('div', 'mb-side-head');
  var sideDot = el('div', 'mb-dot');
  var sideKind = el('span');
  var sideClose = el('button', 'mb-x', '×');
  sideClose.type = 'button'; sideClose.title = 'Close (Esc)';
  sideHead.appendChild(sideDot); sideHead.appendChild(sideKind); sideHead.appendChild(sideClose);
  var sideBody = el('div', 'mb-side-body');
  side.appendChild(sideHead); side.appendChild(sideBody);

  var bottom = el('div', 'mb-bottom');
  var timeBox = el('div', 'mb-time mb-glass');
  var playBtn = el('button', 'mb-play', '▶');
  playBtn.type = 'button'; playBtn.title = 'Replay how the bank grew';
  var timeRange = el('input');
  timeRange.type = 'range'; timeRange.min = '0'; timeRange.max = '1000'; timeRange.value = '1000';
  timeRange.setAttribute('aria-label', 'Show the bank as of a date');
  var timeLabel = el('span', null, 'today');
  timeBox.appendChild(playBtn); timeBox.appendChild(timeRange); timeBox.appendChild(timeLabel);
  var live = el('div', 'mb-live mb-glass');
  var livePip = el('div', 'mb-pip');
  var liveText = el('span', null, 'connecting…');
  live.appendChild(livePip); live.appendChild(liveText);
  // Shown for a while after "Forget", so a slip can be taken back on the spot
  // (a retired item is no longer drawn, so it could not be reselected).
  var undo = el('div', 'mb-undo mb-glass');
  var undoText = el('span', null, 'Memory retired');
  var undoBtn = el('button', 'mb-btn', 'Undo');
  undoBtn.type = 'button'; undoBtn.title = 'Make it recallable again';
  undo.appendChild(undoText); undo.appendChild(undoBtn);
  bottom.appendChild(timeBox); bottom.appendChild(live); bottom.appendChild(undo);

  var tip = el('div', 'mb-tip mb-glass');
  var tipTitle = el('b'); var tipMeta = el('span');
  tip.appendChild(tipTitle); tip.appendChild(tipMeta);
  var msg = el('div', 'mb-msg mb-glass');

  wrap.appendChild(style); wrap.appendChild(canvas); wrap.appendChild(top); wrap.appendChild(bottom);
  wrap.appendChild(side); wrap.appendChild(tip); wrap.appendChild(msg);
  root.appendChild(wrap);
  cleanups.push(function () { if (wrap.parentNode) wrap.parentNode.removeChild(wrap); });

  // ----------------------------------------------------------------- state

  var nodes = [];            // every node of the current payload
  var live_nodes = [];       // the ones taking part in layout (kind filter on)
  var edges = [];
  var byId = new Map();
  var kindsOn = { memory: true, entity: true, skill: true, session: true, topic: true };
  var kindCount = {};
  var stats = null;
  // The camera orbits a focus point (tx, ty, tz) at `dist`. The flat view is
  // the same camera looking straight on at a layout whose z is 0, where it
  // behaves exactly like a 2D pan and zoom (scale = focal / dist).
  var flat = storedFlat();
  var cam = { tx: 0, ty: 0, tz: 0, dist: 900, yaw: flat ? 0 : YAW0, pitch: flat ? 0 : PITCH0 };
  var refit = false;         // fit once more when the layout has settled, unless the user moved the camera
  var unsized = false;       // the last fit was made while the view had no size (hidden pane, minimised window)
  var W = 0, H = 0, dpr = 1;
  var alpha = 0, alphaTarget = 0;
  var hover = null, selected = null, focusSet = null, searchSet = null;
  var timeMin = 0, timeMaxAll = 0, timeCut = Infinity, playing = false, playStart = 0;
  var pulses = [];
  var lastEvent = 0, lastEventTs = 0, lastEventText = '';
  // What the theme gives: colours by kind name, and - by kind number
  // (KINDS), which is what the drawing loops go by - the links' colours,
  // what sort of world the kind is, and the pictures: `balls` has one list
  // per kind and variant (kind x LOOK.variants + variant) with the small
  // sizes at every step of depth, `dims` the same small sizes as they show
  // outside a selection or a search, `bigs` the bigger sizes and `rings` the
  // rings as they come to be needed, `coronas` and `sparks` one each.
  // The strings a frame sets as colours are kept ready here too (ink, inks,
  // ring, pick, hover, starInks, labelEdge), so that drawing makes none.
  var theme = {
    dark: true, text: [215, 220, 229], page: LOOK.space, pageNode: null, mark: '', colors: {}, rgb: {},
    links: [], worlds: [], balls: [], dims: [], bigs: [], rings: [], coronas: [], sparks: [], veil: null,
    ink: '', inks: [], ring: '', pick: '', hover: '', starInks: [], labelEdge: ''
  };
  var frame = 0, needsDraw = true, firstFit = true;
  var drag = null, touches = [], pointer = { x: 0, y: 0, inside: false };
  var motionTimer = 0, motionMs = LOOK.frame, drawCost = 0;
  var spinning = false, spinFrom = 0, spinLast = 0, lastInput = 0, reduced = false;
  var bigRoom = 0;           // how many more big pictures this frame may paint (see bigBody)
  var refreshTimer = 0, searchTimer = 0, searchSeq = 0, detailSeq = 0;
  var timers = [];
  var resultItems = [], resultCursor = -1;

  // ----------------------------------------------------------------- theme

  function readTheme() {
    var cs = win.getComputedStyle(wrap);
    var text = rgbOf(cs.color) || [215, 220, 229];
    theme.dark = lightness(text) > 0.5;
    theme.text = text;
    theme.page = pageColor(cs);
    var base = theme.dark ? PALETTE.dark : PALETTE.light, mode = theme.dark ? 0 : 1;
    var haze = theme.dark ? blend(theme.page, LOOK.haze, LOOK.hazeMix) : theme.page;
    theme.bigs = []; theme.rings = [];     // the big pictures and the rings are painted again as they are needed
    KINDS.forEach(function (kind, k) {
      var custom = cs.getPropertyValue('--mb-' + kind).trim();
      var world = kind === 'topic' && LOOK.suns ? 'sun' : LOOK.worlds[kind];
      // The kind's colour: the theme's (or the host's own, --mb-<kind>) - but gold for the suns, whose mark in
      // the legend, links and lights are then gold with them, so that the legend still matches the picture.
      var rgb = world === 'sun' ? sunTone(LOOK.sunGold, mode) : (custom && rgbOf(custom)) || rgbOf(base[kind]);
      theme.rgb[kind] = rgb;
      theme.colors[kind] = rgba(rgb, 1);
      theme.links[k] = toward(rgb, LOOK.linkTint[mode]);
      theme.worlds[k] = world;
      // Every variant of the kind in the small sizes, and the same again at
      // every step of depth: one flat list per variant, a step's sizes side
      // by side. And once more as they show when something else has the view.
      for (var v = 0; v < LOOK.variants; v++) {
        var list = [], dim = [], s, step;
        for (s = 0; s < SMALL; s++) {
          list.push(paintBody(rgb, world, bodySeed(k, v), mode, SPRITES[s]));
          dim.push(hazed(list[s], haze, LOOK.dimBody));
        }
        for (step = 1; step < LOOK.fadeSteps; step++) {
          for (s = 0; s < SMALL; s++) list.push(hazed(list[s], haze, LOOK.fade * step / (LOOK.fadeSteps - 1)));
        }
        theme.balls[k * LOOK.variants + v] = list;
        theme.dims[k * LOOK.variants + v] = dim;
      }
      theme.coronas[k] = world === 'sun' && theme.dark ? makeCorona(rgb) : null;
      theme.sparks[k] = makeSpark(rgb, mode);
    });
    theme.veil = makeVeil(haze);
    var accent = rgbOf(cs.getPropertyValue('--ui-accent').trim());
    theme.accent = accent || (theme.dark ? [255, 255, 255] : [20, 24, 32]);
    theme.ink = rgba(text, 1);
    theme.inks = [];                       // the links' colours are put together again as they are needed
    theme.ring = rgba(toward(theme.rgb.memory, LOOK.ringPale[mode]), LOOK.ringInk[mode]);
    theme.pick = rgba(theme.accent, LOOK.pickInk); theme.hover = rgba(theme.accent, LOOK.hoverInk);
    var star = blend(text, LOOK.starTint, LOOK.starTintMix);
    theme.starInks = LOOK.starInk.map(function (strength) { return rgba(star, strength); });
    // Names are outlined in the background's own colour, a little deeper, so that a link or a world behind them cannot drown them.
    theme.labelEdge = rgba(toward(theme.page, theme.dark ? -0.4 : 0.6), LOOK.labelEdgeInk);
    paintBackdrop();
    Array.from(chips.querySelectorAll('.mb-dot')).forEach(function (dot) {
      dot.style.background = theme.colors[dot.getAttribute('data-kind')];
    });
  }

  // The colour the background starts from. `--mb-bg` sets it outright;
  // otherwise it is what the host page shows behind the view (the nearest
  // ancestor that paints a background), as long as that agrees with the
  // theme the text colour gave - so the view sits in a host's own theme
  // instead of bringing a colour of its own; failing both, a default.
  function pageColor(cs) {
    var own = rgbOf(cs.getPropertyValue('--mb-bg').trim());
    if (own) return own;
    var found = null, from = null;
    try {
      for (var node = root, hops = 0; node && node !== doc && hops < 24 && !found; node = node.parentNode, hops++) {
        found = rgbOf(win.getComputedStyle(node).backgroundColor, true);
        from = node;
      }
    } catch (err) {
      found = null;                        // inside a shadow tree, or not in a page at all: nothing to read
    }
    theme.pageNode = found ? from : null;  // whose background it was: watched for a change of theme (themeMark)
    if (found && (lightness(found) < 0.5) === theme.dark) return found;
    return theme.dark ? LOOK.space : LOOK.paper;
  }

  // Everything the theme is read from, as one string. A host that switches
  // theme restyles the page without any event for it, so this is looked at
  // every few seconds and the theme read again when it has changed.
  function themeMark() {
    var cs = win.getComputedStyle(wrap), mark = cs.color + '|' + cs.getPropertyValue('--mb-bg');
    if (theme.pageNode) mark += '|' + win.getComputedStyle(theme.pageNode).backgroundColor;
    return mark;
  }

  // --------------------------------------------------------------- planets
  //
  // Every node is drawn from a ready-made picture of a small world, so that
  // drawing one is a single scaled drawImage, which is what keeps thousands
  // of them cheap. The pictures are painted here, point by point, once per
  // kind, variant, size and theme: a surface (belts, seas and land, ice,
  // craters) on a ball, then the light - one lamp for all, up and to the
  // left - which leaves the far side in shadow. No shine, no glow: a planet
  // is matte. Only a sun gives light of its own.

  // Smooth noise: a number between 0 and 1 for every point of space, near
  // points getting near numbers. Land, weather, belts and mottled rock are
  // all made of it. (The usual lattice kind: a shuffled table of 256
  // numbers stands at the corners of a grid of unit cubes, and a point takes
  // the blend of the eight corners round it.)
  var NOISE = new Uint8Array(512);
  (function shuffleNoise() {
    var roll = dice(LOOK.worldSeed), i, j, t;
    for (i = 0; i < 256; i++) NOISE[i] = i;
    for (i = 255; i > 0; i--) { j = roll() * (i + 1) | 0; t = NOISE[i]; NOISE[i] = NOISE[j]; NOISE[j] = t; }
    for (i = 0; i < 256; i++) NOISE[256 + i] = NOISE[i];
  })();
  function noise(x, y, z) {
    var xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z), u = x - xi, v = y - yi, w = z - zi;
    u = u * u * (3 - 2 * u); v = v * v * (3 - 2 * v); w = w * w * (3 - 2 * w);
    xi &= 255; yi &= 255; zi &= 255;
    var a = NOISE[xi] + yi, b = NOISE[xi + 1] + yi;
    var aa = NOISE[a] + zi, ab = NOISE[a + 1] + zi, ba = NOISE[b] + zi, bb = NOISE[b + 1] + zi;
    var near = lerp(lerp(NOISE[aa], NOISE[ba], u), lerp(NOISE[ab], NOISE[bb], u), v);
    var far = lerp(lerp(NOISE[aa + 1], NOISE[ba + 1], u), lerp(NOISE[ab + 1], NOISE[bb + 1], u), v);
    return lerp(near, far, w) / 255;
  }
  // Several layers of it, each twice as fine and half as strong as the one
  // before: the ragged look of coasts and clouds. (Still 0..1, mostly near the middle.)
  function cloud(x, y, z, layers) {
    var sum = 0, share = 0.5, all = 0;
    for (var i = 0; i < layers; i++) {
      sum += noise(x, y, z) * share; all += share;
      x *= 2.03; y *= 2.03; z *= 2.03; share *= 0.5;
    }
    return sum / all;
  }

  // The lamp's direction, as a vector of length 1 (x to the right, y down, z towards the viewer).
  var LAMP = (function () {
    var l = LOOK.lamp, len = Math.sqrt(l[0] * l[0] + l[1] * l[1] + l[2] * l[2]) || 1;
    return [l[0] / len, l[1] / len, l[2] / len];
  })();
  // How far a ring's two pictures reach from their middle, in planet radii, for each size they are kept in:
  // the whole ring, and the stretch of it that crosses the planet (which ends a little way off the planet's
  // edge: ringLap, for a planet of r pixels in its picture).
  var RINGS = 3, RING_SPAN = [], RING_FRONT = [];
  function ringLap(r) { return Math.max(0.12, 2 / r); }
  (function () {
    for (var s = 0; s < RINGS; s++) {
      var r = SPRITES[s] / 2 / SPRITE_PAD;
      RING_SPAN[s] = (Math.ceil(r * LOOK.ringSize[1]) + 1) / r;
      RING_FRONT[s] = (Math.ceil(r * (1 + ringLap(r))) + 1) / r;
    }
  })();
  // The cosine and sine of a ring's turn on the screen.
  var RING_COS = Math.cos(LOOK.ringTurn), RING_SIN = Math.sin(LOOK.ringTurn);

  function bodySeed(k, v) { return scramble(k + 1, v + 1, LOOK.worldSeed); }
  // One of the suns' three colours as it is in this theme (`mode`: 0 dark, 1 light - a little deeper on a pale page).
  function sunTone(rgb, mode) { return mode ? toward(rgb, -LOOK.sunPaper) : rgb; }

  // What one world is made of, rolled from its seed: its tones (the kind's
  // colour, changed as its sort of world says, and a little warmer or cooler,
  // lighter or darker than its neighbours), how it stands, and where its
  // weather, its storm or its craters are. `r`, `g`, `b`, `a` are where
  // shadeAt() leaves the colour of the point it was asked for.
  function skinOf(rgb, world, seed, mode) {
    var roll = dice(seed), recipe = LOOK[world] || {}, skin = { world: world, r: 0, g: 0, b: 0, a: 0 };
    var turn = (roll() * 2 - 1) * LOOK.drift[0], light = (1 + (roll() * 2 - 1) * LOOK.drift[1]) * (mode ? LOOK.paperLift : 1);
    function between(range) { return lerp(range[0], range[1], roll()); }
    ['main', 'pale', 'deep', 'extra', 'other'].forEach(function (name) {
      var t = recipe[name];
      skin[name] = t ? tone(rgb, t[0] + turn, t[1], t[2] * light) : rgb;
    });
    // Its own place in the noise, so that no two worlds share their weather.
    skin.ox = roll() * 200; skin.oy = roll() * 200; skin.oz = roll() * 200;
    // How it stands: leaning to one side, and tipped towards the viewer or away. (Rows: the body's own
    // x, y and z - y runs from pole to pole - in terms of the picture's.)
    var lean = (roll() * 2 - 1) * 0.5, tip = (roll() * 2 - 1) * 0.4;
    var cl = Math.cos(lean), sl = Math.sin(lean), ct = Math.cos(tip), st = Math.sin(tip), m = [cl, -sl, 0, sl * ct, cl * ct, -st, sl * st, cl * st, ct];
    skin.m = m;
    skin.air = (recipe.air || 0) * LOOK.air[mode];
    skin.rim = toward(skin.main, mode ? 0.35 : 0.6);          // the colour of lit air: the body's, paler
    skin.shade = toward(rgb, -0.7);                           // and of its shadow on a light page
    if (world === 'sun') {
      // A sun is gold, whatever colour its kind has: a warm white middle, a golden body, a deeper orange edge.
      // One differs from the next a little: more orange or more yellow, a bigger or smaller hot middle, coarser
      // or finer grain.
      var warm = (roll() * 2 - 1) * LOOK.sunDrift;
      skin.pale = sunTone(LOOK.sunCore, mode);
      skin.main = sunTone(blend(LOOK.sunGold, warm > 0 ? LOOK.sunEdge : LOOK.sunCore, Math.abs(warm)), mode);
      skin.deep = sunTone(LOOK.sunEdge, mode);
      skin.fall = between(LOOK.sunFall); skin.grain = between(LOOK.sunGrainSize);
    } else if (world === 'gas' || world === 'ice') {
      skin.bands = between(recipe.bands); skin.swirl = between(recipe.swirl);
      skin.storm = roll() < recipe.storm ? 1 : 0;
      // The storm sits where it can be seen: a spot on the near side, off the equator, turned into the body's own bearings.
      var sx = (roll() * 2 - 1) * 0.45, sy = (roll() < 0.5 ? -1 : 1) * lerp(0.2, 0.5, roll()), sz = Math.sqrt(1 - sx * sx - sy * sy);
      skin.sx = sx * m[0] + sy * m[1] + sz * m[2]; skin.sy = sx * m[3] + sy * m[4] + sz * m[5]; skin.sz = sx * m[6] + sy * m[7] + sz * m[8];
      skin.sr = lerp(0.15, 0.27, roll());
    } else if (world === 'rock') {
      // Craters, scattered over the face that is turned to the viewer (as seen: x, y, z in the picture, then
      // radius and depth); most are small.
      var count = Math.round(between(recipe.craters)), list = new Float32Array(count * 5);
      for (var i = 0; i < count; i++) {
        var out = Math.sqrt(roll()) * 0.95, round = roll() * 6.2832, big = roll();
        list[i * 5] = Math.cos(round) * out; list[i * 5 + 1] = Math.sin(round) * out; list[i * 5 + 2] = Math.sqrt(1 - out * out);
        list[i * 5 + 3] = lerp(recipe.craterSize[0], recipe.craterSize[1], big * big * big);
        list[i * 5 + 4] = recipe.craterDepth * lerp(0.6, 1, roll());
      }
      skin.craters = list;
    } else {
      skin.cap = between(recipe.cap); skin.weather = between(recipe.sky);
      // The sea level is put where the land takes its share of the face
      // that is seen, whatever the noise does just here (the ground's height
      // is tried at a few dozen points spread over the face): a sea world
      // must stay mostly sea, or it would no longer be its kind's colour.
      var share = between(recipe.land), heights = [], grain = LOOK.landGrain;
      for (var p = 0; p < 80; p++) {
        var pr = Math.sqrt((p + 0.5) / 80) * 0.97, px = Math.cos(p * 2.39996) * pr, py = Math.sin(p * 2.39996) * pr, pz = Math.sqrt(1 - pr * pr);
        heights.push(cloud((px * m[0] + py * m[1] + pz * m[2]) * grain + skin.ox, (px * m[3] + py * m[4] + pz * m[5]) * grain + skin.oy,
          (px * m[6] + py * m[7] + pz * m[8]) * grain + skin.oz, 4));
      }
      heights.sort(function (a, b) { return a - b; });
      skin.sea = heights[Math.min(79, Math.round((1 - share) * 80))];
    }
    return skin;
  }
  // The skin's colour set between two tones (t: 0 the first .. 1 the second), or moved part of the way to a third.
  function setTone(skin, a, b, t) {
    skin.r = a[0] + (b[0] - a[0]) * t; skin.g = a[1] + (b[1] - a[1]) * t; skin.b = a[2] + (b[2] - a[2]) * t;
  }
  function wash(skin, c, t) {
    skin.r += (c[0] - skin.r) * t; skin.g += (c[1] - skin.g) * t; skin.b += (c[2] - skin.b) * t;
  }

  // The surface of a banded world at the point (x, y, z) of the body: a
  // tone for every latitude, the latitudes themselves pushed about by the
  // weather, finer streaks running along them, and perhaps a storm. A gas
  // giant goes from dark belts through its main tone to pale zones; an ice
  // giant is nearly even - a faint change of tone - and darkens to its poles.
  function beltsAt(skin, x, y, z, layers) {
    var ox = skin.ox, oy = skin.oy, oz = skin.oz;
    var lat = y * skin.bands * 2 + (cloud(x * 1.8 + ox, y * 1.8 + oy, z * 1.8 + oz, layers) - 0.5) * skin.swirl * 2;
    var belt = smooth(0.28, 0.72, noise(lat + ox, oy, oz)) + (noise(x * 2 + oz, lat * 4 + ox, z * 2 + oy) - 0.5) * 0.4;
    belt = clamp(belt, 0, 1);
    if (skin.world === 'ice') {
      setTone(skin, skin.main, skin.pale, belt);
      wash(skin, skin.deep, smooth(0.4, 1, Math.abs(y)) * 0.6);
    } else if (belt < 0.5) {
      setTone(skin, skin.deep, skin.main, belt * 2);
    } else {
      setTone(skin, skin.main, skin.pale, belt * 2 - 1);
    }
    if (skin.storm) {                                          // an oval, longer along the belts
      var ex = x - skin.sx, ey = (y - skin.sy) * 1.9, ez = z - skin.sz, e = (ex * ex + ey * ey + ez * ez) / (skin.sr * skin.sr);
      if (e < 1) wash(skin, skin.extra, smooth(1, 0.45, e) * 0.85);
    }
  }

  // The surface of a world of sea and land: ground of some height
  // everywhere, sea up to a level (deep, and lighter towards the shore),
  // land above it (green, drier inland), ice towards the poles with a ragged
  // edge, and weather over all of it.
  function landAt(skin, x, y, z, layers) {
    var ox = skin.ox, oy = skin.oy, oz = skin.oz, grain = LOOK.landGrain;
    var high = cloud(x * grain + ox, y * grain + oy, z * grain + oz, layers), shore = smooth(skin.sea, skin.sea + 0.02, high);
    setTone(skin, skin.deep, skin.main, smooth(skin.sea - 0.14, skin.sea, high));
    if (shore > 0) {
      var dry = smooth(0.5, 0.9, noise(x * 2.6 + oz, y * 2.6 + ox, z * 2.6 + oy) + (high - skin.sea) * 2.5), a = skin.extra, b = skin.other;
      skin.r += (a[0] + (b[0] - a[0]) * dry - skin.r) * shore;
      skin.g += (a[1] + (b[1] - a[1]) * dry - skin.g) * shore;
      skin.b += (a[2] + (b[2] - a[2]) * dry - skin.b) * shore;
    }
    var cold = Math.abs(y) + (high - 0.5) * 0.6;
    if (cold > skin.cap) wash(skin, skin.pale, smooth(skin.cap, skin.cap + 0.06, cold));
    wash(skin, skin.pale, smooth(0.5, 0.68, cloud(x * 2.2 + oy, y * 4 + oz, z * 2.2 + ox, layers)) * skin.weather);
  }

  // The surface of a moon: mottled rock, darker plains, and craters. A
  // crater is what the lamp makes of a bowl: its floor a little darker, the
  // wall on the lamp's side in its own shadow, the wall across from it lit,
  // a bright rim. (The craters are placed as seen, so this one works in the
  // picture's bearings: dx, dy and how far the point stands out, nz.)
  function rockAt(skin, x, y, z, dx, dy, nz, layers) {
    var ox = skin.ox, oy = skin.oy, oz = skin.oz, list = skin.craters, lift = 0;
    setTone(skin, skin.deep, skin.pale, smooth(0.3, 0.7, cloud(x * 2.6 + ox, y * 2.6 + oy, z * 2.6 + oz, layers)));
    wash(skin, skin.extra, smooth(0.5, 0.6, cloud(x * 0.9 + oy, y * 0.9 + oz, z * 0.9 + ox, 3)) * 0.7);
    for (var i = 0; i < list.length; i += 5) {
      var ex = dx - list[i], ey = dy - list[i + 1], ez = nz - list[i + 2], e2 = ex * ex + ey * ey + ez * ez, rad = list[i + 3];
      if (e2 >= rad * rad || e2 === 0) continue;
      var q = Math.sqrt(e2) / rad, depth = list[i + 4];
      lift -= depth * 0.22 * smooth(0.9, 0.5, q);
      lift -= depth * smooth(0.25, 0.8, q) * smooth(1, 0.88, q) * (ex * LAMP[0] + ey * LAMP[1] + ez * LAMP[2]) / (q * rad);
      lift += depth * 0.3 * smooth(0.8, 0.93, q) * smooth(1, 0.95, q);
    }
    lift = Math.min(clamp(1 + lift, 0.35, 1.5), LOOK.rock.craterLight * 255 / Math.max(skin.r, skin.g, skin.b, 1));
    skin.r *= lift; skin.g *= lift; skin.b *= lift;
  }

  // One point of a body's picture, (dx, dy) from its middle in radii: its
  // colour is left in skin.r/g/b (0..255) and how solid it is in skin.a
  // (0..1). `r` is the body's radius in pixels (for its edge, one pixel
  // soft), `layers` how fine the surface is worked, `mode` the theme.
  function shadeAt(skin, dx, dy, r, layers, mode) {
    var d2 = dx * dx + dy * dy, d = Math.sqrt(d2), nz = Math.sqrt(d2 < 1 ? 1 - d2 : 0);
    skin.a = 0;
    if (skin.world === 'sun') {
      // A sun has no dark side and no hard edge: a mottled face, warm white
      // in the middle, gold round that and a deeper orange towards the
      // edge, which fades out.
      var soft = smooth(1 + LOOK.sunSoft * 0.4, 1 - LOOK.sunSoft * 0.6, d);
      if (soft <= 0) return;
      var grain = cloud(dx * skin.grain + skin.ox, dy * skin.grain + skin.oy, nz * skin.grain + skin.oz, layers) - 0.5;
      var heat = clamp(Math.pow(nz, skin.fall) + grain * LOOK.sunGrain * 2, 0, 1);
      if (heat < 0.5) setTone(skin, skin.deep, skin.main, heat * 2); else setTone(skin, skin.main, skin.pale, heat * 2 - 1);
      skin.a = soft;
      return;
    }
    // Round the body: in the light theme its shadow on the page, in the
    // dark one the lit air just off its day side.
    var inside = clamp((1 - d) * r + 0.5, 0, 1), under = null, ua = 0;
    if (inside < 1) {
      if (LOOK.shadow[mode]) {
        var sx = dx - LOOK.shadowAt, sy = dy - LOOK.shadowAt;
        ua = LOOK.shadow[mode] * smooth(1 + LOOK.shadowBlur, 1 - LOOK.shadowBlur, Math.sqrt(sx * sx + sy * sy));
        under = skin.shade;
      } else if (skin.air && d > 0 && d < 1 + LOOK.airWidth) {
        var off = 1 - Math.max(0, d - 1) / LOOK.airWidth;
        ua = skin.air * 0.6 * off * off * smooth(-0.15, 0.55, (dx * LAMP[0] + dy * LAMP[1]) / d);
        under = skin.rim;
      }
      if (inside <= 0) {
        if (ua > 0) { skin.r = under[0]; skin.g = under[1]; skin.b = under[2]; skin.a = ua; }
        return;
      }
    }
    // The same point on the body itself, which stands tilted; then what the surface is there.
    var m = skin.m, x = dx * m[0] + dy * m[1] + nz * m[2], y = dx * m[3] + dy * m[4] + nz * m[5], z = dx * m[6] + dy * m[7] + nz * m[8];
    if (skin.world === 'gas' || skin.world === 'ice') beltsAt(skin, x, y, z, layers);
    else if (skin.world === 'rock') rockAt(skin, x, y, z, dx, dy, nz, layers);
    else landAt(skin, x, y, z, layers);
    // Day and night: full light where the surface faces the lamp, falling
    // off towards the line where it turns away, and beyond that only what
    // the night keeps. The lit side also darkens a little towards its edge.
    var lit = dx * LAMP[0] + dy * LAMP[1] + nz * LAMP[2];
    var day = smooth(-LOOK.dusk * 0.3, LOOK.dusk, lit) * (0.7 + 0.3 * clamp(lit, 0, 1)) * (1 - LOOK.limb * (1 - nz) * (1 - nz));
    var k = LOOK.night[mode] + (1 - LOOK.night[mode]) * day;
    skin.r *= k; skin.g *= k; skin.b *= k;
    // The thin line of lit air along the day side's edge.
    if (skin.air) wash(skin, skin.rim, skin.air * smooth(1 - LOOK.airWidth * 3.5, 1, d) * smooth(-0.1, 0.5, lit));
    // On its very edge the body is part see-through, and what is round it shows.
    skin.a = inside + ua * (1 - inside);
    if (ua > 0) {
      skin.r = (skin.r * inside + under[0] * ua * (1 - inside)) / skin.a;
      skin.g = (skin.g * inside + under[1] * ua * (1 - inside)) / skin.a;
      skin.b = (skin.b * inside + under[2] * ua * (1 - inside)) / skin.a;
    }
  }

  // The picture of one world at one size. The smallest size is worked out
  // at four points a pixel (it has few pixels, and each must be a fair
  // average); the surface is worked finer the bigger the picture is.
  // (`mode` picks the theme's value from LOOK: 0 dark, 1 light.)
  function paintBody(rgb, world, seed, mode, size) {
    var c = doc.createElement('canvas');
    c.width = c.height = size;
    var g = c.getContext('2d');
    if (!g || !g.createImageData) return c;
    var skin = skinOf(rgb, world, seed, mode), img = g.createImageData(size, size), data = img.data;
    var h = size / 2, r = h / SPRITE_PAD, fine = size <= SPRITES[0] ? 2 : 1, layers = size <= SPRITES[0] ? 3 : size <= SPRITES[1] ? 4 : 5;
    for (var py = 0, at = 0; py < size; py++) {
      for (var px = 0; px < size; px++, at += 4) {
        var sr = 0, sg = 0, sb = 0, sa = 0;
        for (var j = 0; j < fine; j++) {
          for (var i = 0; i < fine; i++) {
            shadeAt(skin, (px + (i + 0.5) / fine - h) / r, (py + (j + 0.5) / fine - h) / r, r, layers, mode);
            sr += skin.r * skin.a; sg += skin.g * skin.a; sb += skin.b * skin.a; sa += skin.a;
          }
        }
        if (sa > 0) { data[at] = sr / sa; data[at + 1] = sg / sa; data[at + 2] = sb / sa; data[at + 3] = sa / (fine * fine) * 255; }
      }
    }
    g.putImageData(img, 0, 0);
    return c;
  }

  // A body as it looks from farther off: the same picture with the haze's
  // colour laid over the body (and only the body) - dimmer, and in the dark
  // theme a little bluer. Far bodies are drawn from these instead of being
  // made see-through, so a link behind one does not show through.
  function hazed(sprite, haze, amount) {
    var c = doc.createElement('canvas');
    c.width = c.height = sprite.width;
    var g = c.getContext('2d');
    g.drawImage(sprite, 0, 0);
    g.globalCompositeOperation = 'source-atop';
    g.fillStyle = rgba(haze, Math.round(amount * 1000) / 1000);
    g.fillRect(0, 0, c.width, c.height);
    return c;
  }

  // A plain disc of the haze's colour, which all but fills its picture
  // (VEIL_PAD: the picture's size over the disc's; no margin to draw for
  // nothing). The big pictures are not kept at every step of depth (few
  // bodies ever show that large); one of them is hazed by laying this over
  // the body, as strongly as it is far.
  var VEIL_PAD = SPRITES[1] / (SPRITES[1] - 2);
  function makeVeil(haze) {
    var c = doc.createElement('canvas'), size = SPRITES[1];
    c.width = c.height = size;
    var g = c.getContext('2d');
    g.fillStyle = rgba(haze, 1);
    g.beginPath(); g.arc(size / 2, size / 2, size / 2 / VEIL_PAD, 0, 6.2832); g.fill();
    return c;
  }

  // The bigger pictures of a body are painted when it first shows that
  // large - most never do - and no more than one in a frame (bigRoom), so
  // that zooming into a busy part does not stall: until its turn comes a
  // body is drawn from the next size down, and another frame is asked for.
  // `which` is 0 for the first big size, 1 for the second.
  function bigBody(k, v, which) {
    var kv = k * LOOK.variants + v, slot = kv * (SPRITES.length - SMALL) + which, pic = theme.bigs[slot];
    if (pic) return pic;
    if (bigRoom > 0) {
      bigRoom--;
      return (theme.bigs[slot] = paintBody(theme.rgb[KINDS[k]], theme.worlds[k], bodySeed(k, v), theme.dark ? 0 : 1, SPRITES[SMALL + which]));
    }
    needsDraw = true;
    return (which && theme.bigs[slot - 1]) || theme.balls[kv][SMALL - 1];
  }

  // The ring of an always-on memory: a flat ring of dust round the planet,
  // seen at a slant, in a pale version of the planet's colour. It comes as
  // two pictures - the whole ring, drawn before the planet (which covers
  // the middle of it), and the stretch of its near side that crosses the
  // planet, drawn after - so the planet sits in its ring. (Not two halves
  // that meet edge to edge: two see-through pictures never quite add up
  // along such an edge once they are scaled, and the join shows as a
  // hairline across the ring. The second picture fades out just off the
  // planet's edge instead, over the first - and is no bigger than that
  // takes, so that it costs little to draw.)
  // It is lit by the same lamp, and the part of it that lies in the planet's
  // shadow is dark, which is much of what makes it a ring round a ball
  // rather than a hoop on a picture. `size` is the size of the body's
  // picture it goes with; the ring's own is as much bigger as the ring is.
  function paintRing(rgb, mode, size) {
    var r = size / 2 / SPRITE_PAD, half = Math.ceil(r * LOOK.ringSize[1]) + 1, side = half * 2, fine = size <= SPRITES[0] ? 3 : 2;
    var inner = LOOK.ringSize[0], width = LOOK.ringSize[1] - inner, open = LOOK.ringOpen, deep = Math.sqrt(1 - open * open);
    var ct = RING_COS, st = RING_SIN, gap = LOOK.ringGap, dust = toward(rgb, LOOK.ringPale[mode]);
    var lap = ringLap(r), cropped = half - (Math.ceil(r * (1 + lap)) + 1);   // the second picture is this much smaller all round
    var pics = [], ctxs = [], imgs = [], sides = [side, side - cropped * 2], sum = [0, 0], lit = [0, 0], which;
    for (which = 0; which < 2; which++) {
      pics[which] = doc.createElement('canvas');
      pics[which].width = pics[which].height = sides[which];
      ctxs[which] = pics[which].getContext('2d');
      if (!ctxs[which] || !ctxs[which].createImageData) return pics;
      imgs[which] = ctxs[which].createImageData(sides[which], sides[which]);
    }
    for (var py = 0; py < side; py++) {
      for (var px = 0; px < side; px++) {
        sum[0] = sum[1] = lit[0] = lit[1] = 0;
        for (var j = 0; j < fine; j++) {
          for (var i = 0; i < fine; i++) {
            // Where the point is in the ring's own plane: u along its long axis, v along the slanted one
            // (negative: the far side), t from its inner edge (0) to its outer (1).
            var dx = (px + (i + 0.5) / fine - half) / r, dy = (py + (j + 0.5) / fine - half) / r;
            var u = dx * ct + dy * st, v = (dy * ct - dx * st) / open, t = (Math.sqrt(u * u + v * v) - inner) / width;
            if (t <= 0 || t >= 1) continue;
            // How much dust there: soft edges, a division, finer ringlets.
            var a = smooth(0, 0.07, t) * smooth(1, 0.9, t) * (0.7 + 0.3 * noise(t * 11, 0.5, 0.5)) *
              (1 - gap[2] * smooth(gap[0] - 0.03, gap[0], t) * smooth(gap[1] + 0.03, gap[1], t));
            // In space the point stands v * deep towards the viewer. It is in the planet's shadow when
            // the planet is between it and the lamp: beyond the planet along the light, and within one
            // radius of the line through the planet's middle.
            var pz = v * deep, along = dx * LAMP[0] + dy * LAMP[1] + pz * LAMP[2];
            var light = along < 0 ? lerp(LOOK.ringShade, 1, smooth(0.92, 1.1, Math.sqrt(Math.max(0, dx * dx + dy * dy + pz * pz - along * along)))) : 1;
            sum[0] += a; lit[0] += a * light;
            if (v > 0) {                                       // the near side: again, where it crosses the planet
              var front = a * smooth(1 + lap, 1, Math.sqrt(dx * dx + dy * dy));
              sum[1] += front; lit[1] += front * light;
            }
          }
        }
        for (which = 0; which < 2; which++) {
          if (!sum[which]) continue;                           // (nothing of the second picture lies outside its smaller square)
          var data = imgs[which].data, k = lit[which] / sum[which];
          var at = ((py - which * cropped) * sides[which] + px - which * cropped) * 4;
          data[at] = dust[0] * k; data[at + 1] = dust[1] * k; data[at + 2] = dust[2] * k;
          data[at + 3] = sum[which] / (fine * fine) * LOOK.ringInk[mode] * 255;
        }
      }
    }
    ctxs[0].putImageData(imgs[0], 0, 0); ctxs[1].putImageData(imgs[1], 0, 0);
    return pics;
  }
  // The size in which kind k's ring is to be drawn when size s is wanted. Its two pictures are in theme.rings
  // (the whole ring at (k x RINGS + size) x 2, the stretch in front next to it), painted when a ring first
  // shows in that size. A big one takes a while, so it comes out of the same allowance as the big bodies
  // (bigRoom, one a frame): until its turn comes the ring is drawn from the next size down.
  function ringFor(k, s) {
    var slot = (k * RINGS + s) * 2;
    if (theme.rings[slot]) return s;
    if (s) {
      if (bigRoom <= 0) { needsDraw = true; return ringFor(k, s - 1); }
      bigRoom--;
    }
    var pair = paintRing(theme.rgb[KINDS[k]], theme.dark ? 0 : 1, SPRITES[s]);
    theme.rings[slot] = pair[0]; theme.rings[slot + 1] = pair[1];
    return s;
  }

  // The glow round a sun (dark theme only): the one thing in the picture,
  // besides the stars and the lights on the links, that is added to what is
  // behind it. It is strongest at the sun's edge and gone within a couple of
  // radii, so a sun is a quiet light: it does not wash out what is near it.
  // (`rgb`: the suns' gold.)
  function makeCorona(rgb) {
    var c = doc.createElement('canvas');
    c.width = c.height = CORONA_PX;
    var g = c.getContext('2d'), h = CORONA_PX / 2, tint = blend(rgb, LOOK.sunEdge, LOOK.coronaWarm);
    var glow = g.createRadialGradient(h, h, 0, h, h, h);
    LOOK.coronaFall.forEach(function (stop) {
      glow.addColorStop(stop[0], rgba(tint, Math.round(stop[1] * LOOK.corona * 1000) / 1000));
    });
    g.fillStyle = glow;
    g.fillRect(0, 0, CORONA_PX, CORONA_PX);
    return c;
  }

  // The point of light that travels along a link: small and soft, the
  // link's colour made paler in the dark theme (where it is added to what is
  // behind it); a small saturated dot, darker than the link, in the light
  // one (where light on white is nothing).
  function makeSpark(rgb, mode) {
    var c = doc.createElement('canvas');
    c.width = c.height = SPARK_PX;
    var g = c.getContext('2d'), h = SPARK_PX / 2;
    var glow = g.createRadialGradient(h, h, 0, h, h, h);
    if (mode === 0) {
      glow.addColorStop(0, rgba(toward(rgb, LOOK.lightCore), 1));
      glow.addColorStop(0.3, rgba(toward(rgb, LOOK.lightCore * 0.5), 0.6));
      glow.addColorStop(0.6, rgba(rgb, 0.16));
      glow.addColorStop(1, rgba(rgb, 0));
    } else {
      var deep = toward(rgb, -0.22);
      glow.addColorStop(0, rgba(deep, 0.95));
      glow.addColorStop(0.42, rgba(deep, 0.9));
      glow.addColorStop(0.62, rgba(deep, 0.22));
      glow.addColorStop(1, rgba(deep, 0));
    }
    g.fillStyle = glow;
    g.fillRect(0, 0, SPARK_PX, SPARK_PX);
    return c;
  }

  // The still part of the background - a radial gradient, a touch lighter
  // in the middle and darker in the corners, with a few very faint clouds
  // in the kinds' colours over it - is painted once into a small canvas of
  // its own and stretched over the view on every frame (it has no detail to
  // lose). Its border fades out, so the picture has no hard edge against
  // whatever page it sits in. Painted again when the view changes size or
  // the theme changes.
  var backdrop = doc.createElement('canvas'), backdropCtx = backdrop.getContext('2d');
  function paintBackdrop() {
    if (!W || !H || !backdropCtx) return;
    var s = LOOK.backdropScale, w = Math.max(2, Math.round(W * s)), h = Math.max(2, Math.round(H * s)), g = backdropCtx;
    backdrop.width = w; backdrop.height = h;                   // (this also wipes it)
    var page = theme.page, dark = theme.dark, core, rim;
    if (dark) {
      core = [Math.min(255, page[0] + LOOK.spaceCore[0]), Math.min(255, page[1] + LOOK.spaceCore[1]),
        Math.min(255, page[2] + LOOK.spaceCore[2])];
      rim = blend(page, [0, 0, 0], 1 - LOOK.spaceRim);
    } else {
      core = toward(page, LOOK.paperCore);
      rim = blend(page, theme.text, LOOK.paperRim);
    }
    // From the middle out to the corners; the page's own colour sits a bit past half-way.
    var fill = g.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.sqrt(w * w + h * h) / 2);
    fill.addColorStop(0, rgba(core, 1));
    fill.addColorStop(0.58, rgba(page, 1));
    fill.addColorStop(1, rgba(rim, 1));
    g.fillStyle = fill;
    g.fillRect(0, 0, w, h);
    LOOK.clouds.forEach(function (cloud) {
      var tint = theme.rgb[cloud.kind] || theme.text, strength = cloud.a * (dark ? 1 : LOOK.cloudPaper);
      var cx = cloud.x * w, cy = cloud.y * h, r = cloud.r * Math.max(w, h);
      var puff = g.createRadialGradient(cx, cy, 0, cx, cy, r);
      puff.addColorStop(0, rgba(tint, strength));
      puff.addColorStop(0.45, rgba(tint, Math.round(strength * 420) / 1000));
      puff.addColorStop(1, rgba(tint, 0));
      g.fillStyle = puff;
      g.fillRect(cx - r, cy - r, r * 2, r * 2);
    });
    // Rub the border out, one strip along each side (the corners get two, which rounds them).
    var e = Math.max(1, LOOK.edge * s);
    g.globalCompositeOperation = 'destination-out';
    [[0, 0, w, e, 0, 0, 0, e], [0, h - e, w, e, 0, h, 0, h - e], [0, 0, e, h, 0, 0, e, 0], [w - e, 0, e, h, w, 0, w - e, 0]]
      .forEach(function (strip) {
        var wipe = g.createLinearGradient(strip[4], strip[5], strip[6], strip[7]);
        wipe.addColorStop(0, 'rgba(0,0,0,1)');
        wipe.addColorStop(1, 'rgba(0,0,0,0)');
        g.fillStyle = wipe;
        g.fillRect(strip[0], strip[1], strip[2], strip[3]);
      });
    g.globalCompositeOperation = 'source-over';
  }

  // The stars: points scattered evenly over a sphere, faint ones first, then
  // the middling, then the bright (so each class is one run of rectangles in
  // one colour). They are sown from a fixed seed - the same sky on every
  // visit - and a few of the brighter ones are marked to twinkle.
  var starX = new Float32Array(LOOK.stars), starY = new Float32Array(LOOK.stars), starZ = new Float32Array(LOOK.stars);
  var starBlink = new Float32Array(LOOK.stars), starPace = new Float32Array(LOOK.stars), starEnd = [0, 0, 0];
  (function sowStars() {
    var seed = LOOK.starSeed >>> 0, at = 0;
    function next() { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }
    for (var c = 0; c < 3; c++) {
      var end = c === 2 ? LOOK.stars : Math.min(LOOK.stars, at + Math.round(LOOK.stars * LOOK.starShare[c]));
      for (; at < end; at++) {
        var up = next() * 2 - 1, round = next() * 6.2832, across = Math.sqrt(1 - up * up);
        starX[at] = Math.cos(round) * across; starY[at] = up; starZ[at] = Math.sin(round) * across;
        starBlink[at] = c > 0 && next() < LOOK.twinkle ? next() * 6.2832 : -1;      // where in its twinkle it starts; -1: steady
        starPace[at] = lerp(LOOK.twinklePace[0], LOOK.twinklePace[1], next());
      }
      starEnd[c] = at;
    }
  })();

  // The stars that are in view, as small squares. They are turned by the
  // camera's angles and by nothing else: far beyond the scene, they stay put
  // when it is slid or zoomed, and wheel round when it is turned - which is
  // what tells the eye that the scene has depth. (aim() has set the angles.)
  function drawStars(sec, moving) {
    var hw = W / 2, hh = H / 2, pad = LOOK.edge, right = W - pad, low = H - pad, from = 0;
    for (var c = 0; c < 3; c++) {
      var size = LOOK.starSize[c], half = size / 2;
      ctx.fillStyle = theme.starInks[c];
      for (var i = from; i < starEnd[c]; i++) {
        var x = starX[i], y = starY[i], z = starZ[i];
        var z1 = x * sYaw + z * cYaw, depth = y * sPit + z1 * cPit;
        if (depth < 0.2) continue;                             // behind the camera, or too far off to the side
        var k = focal / depth, sx = hw + (x * cYaw - z * sYaw) * k, sy = hh + (y * cPit - z1 * sPit) * k;
        if (sx < pad || sx > right || sy < pad || sy > low) continue;
        if (moving && starBlink[i] >= 0) {
          ctx.globalAlpha = 1 - LOOK.twinkleDip * (0.5 + 0.5 * Math.sin(starBlink[i] + sec * starPace[i]));
          ctx.fillRect(sx - half, sy - half, size, size);
          ctx.globalAlpha = 1;
        } else {
          ctx.fillRect(sx - half, sy - half, size, size);
        }
      }
      from = starEnd[c];
    }
  }

  // ------------------------------------------------------------------ data

  function radiusOf(n) {
    var r = (BASE_R[n.kind] || 4) + Math.min(13, Math.sqrt(n.deg) * 1.15);
    if (n.kind === 'memory') r *= 0.82 + n.imp * 0.5;
    if (n.kind === 'skill') r *= 0.85 + n.imp * 0.4;
    return r;
  }

  function ingest(payload) {
    var kinds = payload.kinds, statuses = payload.statuses, rels = payload.rels;
    var old = byId, fresh = new Map(), list = [];
    // A ball holds far more than a disc of the same width, so it starts smaller.
    var spread = flat ? 26 * Math.sqrt(payload.nodes.length + 1) : 34 * Math.pow(payload.nodes.length + 1, 1 / 3);
    payload.nodes.forEach(function (row) {
      var prev = old.get(row[0]);
      var n = prev || {
        x: 0, y: 0, z: 0, vx: 0, vy: 0, vz: 0, fx: null, fy: null, fz: null, born: 0, seen: false, placed: false
      };
      n.id = row[0]; n.kind = kinds[row[1]]; n.title = row[2]; n.imp = row[3]; n.deg = row[4];
      n.created = row[5]; n.status = statuses[row[6]]; n.inCore = !!row[7]; n.recalls = row[8];
      n.subtype = row[9] || ''; n.lastRecalled = row[10] || 0;
      n.r = radiusOf(n); n.adj = []; n.lower = n.title.toLowerCase();
      n.label = n.title.length > 34 ? n.title.slice(0, 33) + '…' : n.title;
      n.idx = list.length;                 // its slot in the per-frame arrays
      n.kix = KIND_IX[n.kind] == null ? KIND_IX.entity : KIND_IX[n.kind];   // a kind this view does not know is drawn as an entity
      n.look = scramble(n.id, 5, 11) % LOOK.variants;   // which of its kind's worlds it is: always the same one for the same item
      n.flash = 0;                         // when its last recall pulse began (0: none under way)
      fresh.set(n.id, n); list.push(n);
    });
    // (A link's own number is made of its two ends and what it says - the word, not the word's place in this
    // payload's list, which changes as the bank grows.)
    var links = [], relSeeds = rels.map(wordSeed);
    payload.edges.forEach(function (row) {
      var s = fresh.get(row[0]), t = fresh.get(row[1]);
      if (!s || !t) return;
      links.push({ s: s, t: t, rel: rels[row[2]], w: row[3], seed: scramble(row[0], row[1], relSeeds[row[2]]) });
      s.adj.push(t); t.adj.push(s);
    });
    // Newcomers appear next to something they are linked to.
    list.forEach(function (n) {
      if (n.placed) return;
      var anchor = null;
      for (var i = 0; i < n.adj.length; i++) if (n.adj[i].placed) { anchor = n.adj[i]; break; }
      // A random direction: around a circle in the flat view, over a sphere otherwise.
      var angle = Math.random() * Math.PI * 2, up = flat ? 0 : Math.random() * 2 - 1, across = Math.sqrt(1 - up * up);
      var rad = anchor ? 30 : spread * (flat ? Math.sqrt(Math.random()) : Math.pow(Math.random(), 1 / 3));
      n.x = (anchor ? anchor.x : 0) + Math.cos(angle) * across * rad;
      n.y = (anchor ? anchor.y : 0) + Math.sin(angle) * across * rad;
      n.z = flat ? 0 : (anchor ? anchor.z : 0) + up * rad;
      n.placed = true;
    });
    vReserve(list.length, links.length);
    // What each link looks like beyond its two ends, all of it taken from
    // the link's own scrambled number: the axis its bend turns about (a
    // random direction, already scaled by the bend), where its light is in
    // its round, how fast that goes, its place in the queue when only some
    // links may carry light - and the colour, that of its busier end, so
    // that a hub's links fan out in the hub's colour.
    links.forEach(function (e, i) {
      var h = e.seed, h2 = scramble(h, 7, 13);
      var ax = (h & 1023) / 511.5 - 1, ay = ((h >>> 10) & 1023) / 511.5 - 1, az = ((h >>> 20) & 1023) / 511.5 - 1;
      var len = Math.sqrt(ax * ax + ay * ay + az * az);
      if (len < 0.2) { ax = 0; ay = 1; az = 0; len = 1; }
      eAx[i] = ax / len * LOOK.bend; eAy[i] = ay / len * LOOK.bend; eAz[i] = az / len * LOOK.bend;
      ePhase[i] = (h2 & 65535) / 65536 * (1 + LOOK.lightGap);
      ePace[i] = lerp(LOOK.lightPace[0], LOOK.lightPace[1], ((h2 >>> 16) & 255) / 255);
      eRank[i] = (h2 >>> 24) / 256;
      eTint[i] = e.s.deg > e.t.deg ? e.s.kix : e.t.kix;
    });
    // Only a change of shape is worth re-running the layout for.
    var reshaped = list.length !== nodes.length || links.length !== edges.length ||
      list.some(function (n) { return !old.has(n.id); });
    nodes = list; edges = links; byId = fresh; stats = payload.stats;
    kindCount = {};
    var lo = Infinity, hi = 0;
    nodes.forEach(function (n) {
      kindCount[n.kind] = (kindCount[n.kind] || 0) + 1;
      if (n.created > 0) { if (n.created < lo) lo = n.created; if (n.created > hi) hi = n.created; }
    });
    timeMin = lo === Infinity ? 0 : lo;
    timeMaxAll = Math.max(Date.now() / 1000, hi);
    if (selected && !byId.has(selected.id)) select(null);
    if (selected) selected = byId.get(selected.id);
    if (hover && !byId.has(hover.id)) hover = null;
    if (payload.last_event && !lastEvent) lastEvent = payload.last_event;
    applyFilter(reshaped ? 0.35 : 0);
    renderChips();
    renderStats();
    showMessage(nodes.length ? null : 'empty');
    refocus();
  }

  function applyFilter(heat) {
    live_nodes = nodes.filter(function (n) { return kindsOn[n.kind]; });
    nodes.forEach(function (n) { n.on = !!kindsOn[n.kind]; });
    if (heat) reheat(heat); else kick();
  }

  function visible(n) { return n.on && n.created <= timeCut; }

  function renderStats() {
    if (!stats) return;
    // What is on screen, so the line matches the picture.
    brandStats.textContent = num(nodes.length) + ' items · ' + num(edges.length) + ' links · ' +
      num(stats.recalls || 0) + (stats.recalls === 1 ? ' recall' : ' recalls');
    var all = stats.nodes || {};
    var hidden = (all.entity || 0) - (kindCount.entity || 0);
    brand.title = (stats.in_core || 0) + ' memories are always-on (in MEMORY.md / USER.md).' +
      (hidden > 0 ? ' ' + num(hidden) + ' entities mentioned only once are not drawn.' : '');
  }

  function renderChips() {
    // The chips are rebuilt, the one just pressed included; the keyboard focus
    // is handed to its replacement, or the next key would go to the page.
    var held = Array.from(chips.children).indexOf(doc.activeElement);
    clear(chips);
    ['memory', 'entity', 'skill', 'session', 'topic'].forEach(function (kind) {
      if (!kindCount[kind]) return;
      var chip = el('button', 'mb-chip' + (kindsOn[kind] ? '' : ' mb-off'));
      chip.type = 'button';
      chip.title = (kindsOn[kind] ? 'Hide ' : 'Show ') + KIND_LABEL[kind].toLowerCase();
      chip.setAttribute('aria-pressed', kindsOn[kind] ? 'true' : 'false');
      var dot = el('span', 'mb-dot');
      dot.setAttribute('data-kind', kind);
      dot.style.background = theme.colors[kind] || '';
      chip.appendChild(dot);
      chip.appendChild(doc.createTextNode(KIND_LABEL[kind]));
      chip.appendChild(el('i', null, num(kindCount[kind])));
      bind(chip, 'click', function () {
        kindsOn[kind] = !kindsOn[kind];
        applyFilter(0.6); renderChips(); refocus();
      });
      chips.appendChild(chip);
    });
    var fit = el('button', 'mb-chip', 'Fit');
    fit.type = 'button'; fit.title = 'Fit everything in view (F)';
    bind(fit, 'click', function () { fitView(true); });
    chips.appendChild(fit);
    // The button names the view that is showing; the title says what a click does.
    var mode = el('button', 'mb-chip', flat ? '2D' : '3D');
    mode.type = 'button';
    mode.title = flat ? 'Showing the flat 2D view - switch to 3D' : 'Showing the 3D view - switch to flat 2D';
    mode.setAttribute('aria-label', mode.title);
    bind(mode, 'click', function () { setFlat(!flat); });
    chips.appendChild(mode);
    if (held >= 0 && chips.children[held]) chips.children[held].focus();
  }

  function showMessage(kind, detail) {
    clear(msg);
    if (!kind) { msg.className = 'mb-msg mb-glass'; return; }
    msg.className = 'mb-msg mb-glass mb-on';
    if (kind === 'key') {
      msg.appendChild(el('b', null, 'This viewer was restarted'));
      msg.appendChild(el('p', null, 'The key in this tab is out of date. Open the new link that the command printed when it started.'));
      msg.appendChild(el('code', null, 'hermes memorybank ui'));
    } else if (kind === 'empty') {
      msg.appendChild(el('b', null, 'The bank is empty so far'));
      msg.appendChild(el('p', null, 'It fills as Hermes works. To index your existing skills, memory files and past sessions now, run:'));
      msg.appendChild(el('code', null, 'hermes memorybank import'));
    } else {
      msg.appendChild(el('b', null, 'Could not load the memory bank'));
      msg.appendChild(el('p', null, detail || 'The backend did not answer.'));
      var retry = el('button', 'mb-btn', 'Try again');
      retry.type = 'button';
      bind(retry, 'click', function () { refresh(); });
      msg.appendChild(retry);
    }
  }

  // ------------------------------------------------------------ simulation

  // Barnes-Hut over an octree. In the flat view every body has z = 0, so only
  // four of the eight children are ever used and it works as a quadtree.
  var qCap = 0, qChild, qMass, qCx, qCy, qCz, qBody, qN = 0, qX0 = 0, qY0 = 0, qZ0 = 0, qSize = 1;
  var stackNode = new Int32Array(4096), stackSize = new Float32Array(4096);

  function qReserve(count) {
    var need = count * 6 + 64;
    if (need <= qCap) return;
    qCap = need;
    qChild = new Int32Array(qCap * 8); qMass = new Float32Array(qCap);
    qCx = new Float32Array(qCap); qCy = new Float32Array(qCap); qCz = new Float32Array(qCap);
    qBody = new Int32Array(qCap);
  }
  function qNew() {
    var i = qN++, c = i * 8;
    qChild[c] = qChild[c + 1] = qChild[c + 2] = qChild[c + 3] = -1;
    qChild[c + 4] = qChild[c + 5] = qChild[c + 6] = qChild[c + 7] = -1;
    qMass[i] = 0; qCx[i] = 0; qCy[i] = 0; qCz[i] = 0; qBody[i] = -1;
    return i;
  }
  function qInsert(b, x, y, z, m) {
    var node = 0, x0 = qX0, y0 = qY0, z0 = qZ0, size = qSize, depth = 0;
    for (;;) {
      if (qMass[node] === 0) {
        qBody[node] = b; qMass[node] = m; qCx[node] = x; qCy[node] = y; qCz[node] = z;
        return;
      }
      if (qBody[node] !== -1) {
        if (depth > 22 || qN + 2 >= qCap) {           // coincident points: merge
          var tm = qMass[node] + m;
          qCx[node] = (qCx[node] * qMass[node] + x * m) / tm;
          qCy[node] = (qCy[node] * qMass[node] + y * m) / tm;
          qCz[node] = (qCz[node] * qMass[node] + z * m) / tm;
          qMass[node] = tm;
          return;
        }
        var half0 = size / 2, ox = qCx[node], oy = qCy[node], oz = qCz[node];
        var oq = (ox >= x0 + half0 ? 1 : 0) + (oy >= y0 + half0 ? 2 : 0) + (oz >= z0 + half0 ? 4 : 0);
        var moved = qNew();
        qBody[moved] = qBody[node]; qMass[moved] = qMass[node];
        qCx[moved] = ox; qCy[moved] = oy; qCz[moved] = oz;
        qChild[node * 8 + oq] = moved;
        qBody[node] = -1;
      }
      var total = qMass[node] + m;
      qCx[node] = (qCx[node] * qMass[node] + x * m) / total;
      qCy[node] = (qCy[node] * qMass[node] + y * m) / total;
      qCz[node] = (qCz[node] * qMass[node] + z * m) / total;
      qMass[node] = total;
      var half = size / 2;
      var q = (x >= x0 + half ? 1 : 0) + (y >= y0 + half ? 2 : 0) + (z >= z0 + half ? 4 : 0);
      if (q & 1) x0 += half;
      if (q & 2) y0 += half;
      if (q & 4) z0 += half;
      size = half; depth++;
      var child = qChild[node * 8 + q];
      if (child === -1) {
        if (qN >= qCap) return;
        child = qNew();
        qChild[node * 8 + q] = child;
        qBody[child] = b; qMass[child] = m; qCx[child] = x; qCy[child] = y; qCz[child] = z;
        return;
      }
      node = child;
    }
  }

  function tick() {
    var list = live_nodes, n = list.length, i, a;
    alpha += (alphaTarget - alpha) * 0.022;        // cools even with nothing to lay out, or the loop would never rest
    if (!n) return;

    var minX = Infinity, minY = Infinity, minZ = Infinity, maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    for (i = 0; i < n; i++) {
      a = list[i];
      if (a.x < minX) minX = a.x; if (a.x > maxX) maxX = a.x;
      if (a.y < minY) minY = a.y; if (a.y > maxY) maxY = a.y;
      if (a.z < minZ) minZ = a.z; if (a.z > maxZ) maxZ = a.z;
    }
    qReserve(n);
    qN = 0; qX0 = minX - 1; qY0 = minY - 1; qZ0 = minZ - 1;
    qSize = Math.max(maxX - minX, maxY - minY, maxZ - minZ) + 2;
    qNew();
    for (i = 0; i < n; i++) { a = list[i]; a.i = i; qInsert(i, a.x, a.y, a.z, 1 + a.r * 0.22); }

    // Repulsion (Barnes-Hut).
    var theta2 = 0.81, strength = 30 * alpha, maxD2 = 420 * 420;
    for (i = 0; i < n; i++) {
      a = list[i];
      var ax = a.x, ay = a.y, az = a.z, fx = 0, fy = 0, fz = 0, sp = 1;
      stackNode[0] = 0; stackSize[0] = qSize;
      while (sp > 0) {
        sp--;
        var node = stackNode[sp], size = stackSize[sp], mass = qMass[node];
        if (mass === 0) continue;
        var dx = qCx[node] - ax, dy = qCy[node] - ay, dz = qCz[node] - az, d2 = dx * dx + dy * dy + dz * dz;
        var leaf = qBody[node] !== -1;
        if (leaf || size * size < theta2 * d2) {
          if (leaf && qBody[node] === i) continue;
          if (d2 > maxD2) continue;
          if (d2 < 4) {
            dx = (Math.random() - 0.5) * 2; dy = (Math.random() - 0.5) * 2;
            dz = flat ? 0 : (Math.random() - 0.5) * 2; d2 = 4;
          }
          var f = strength * mass / d2;
          fx -= dx * f; fy -= dy * f; fz -= dz * f;
        } else if (sp + 8 < 4096) {
          var c = node * 8, hs = size / 2, kid;
          if ((kid = qChild[c]) !== -1) { stackNode[sp] = kid; stackSize[sp++] = hs; }
          if ((kid = qChild[c + 1]) !== -1) { stackNode[sp] = kid; stackSize[sp++] = hs; }
          if ((kid = qChild[c + 2]) !== -1) { stackNode[sp] = kid; stackSize[sp++] = hs; }
          if ((kid = qChild[c + 3]) !== -1) { stackNode[sp] = kid; stackSize[sp++] = hs; }
          if ((kid = qChild[c + 4]) !== -1) { stackNode[sp] = kid; stackSize[sp++] = hs; }
          if ((kid = qChild[c + 5]) !== -1) { stackNode[sp] = kid; stackSize[sp++] = hs; }
          if ((kid = qChild[c + 6]) !== -1) { stackNode[sp] = kid; stackSize[sp++] = hs; }
          if ((kid = qChild[c + 7]) !== -1) { stackNode[sp] = kid; stackSize[sp++] = hs; }
        }
      }
      a.vx += fx; a.vy += fy; a.vz += fz;
    }

    // Springs.
    for (i = 0; i < edges.length; i++) {
      var e = edges[i], s = e.s, t = e.t;
      if (!s.on || !t.on) continue;
      var lx = t.x + t.vx - s.x - s.vx, ly = t.y + t.vy - s.y - s.vy, lz = t.z + t.vz - s.z - s.vz;
      var len = Math.sqrt(lx * lx + ly * ly + lz * lz) || 0.01;
      var want = (LINK_LEN[e.rel] || 44) + (s.r + t.r) * 0.9;
      var ds = s.adj.length, dt = t.adj.length;
      var k = (len - want) / len * alpha * (0.9 / Math.min(ds, dt));
      lx *= k; ly *= k; lz *= k;
      var bias = ds / (ds + dt);
      t.vx -= lx * bias; t.vy -= ly * bias; t.vz -= lz * bias;
      s.vx += lx * (1 - bias); s.vy += ly * (1 - bias); s.vz += lz * (1 - bias);
    }

    // Pull to the middle (harder for loose items, so they ring the core
    // instead of drifting off), then move.
    var g = 0.07 * alpha;
    for (i = 0; i < n; i++) {
      a = list[i];
      if (a.fx != null) {
        a.x = a.fx; a.y = a.fy; a.z = flat ? 0 : a.fz; a.vx = 0; a.vy = 0; a.vz = 0;
        continue;
      }
      var pull = a.adj.length ? g : g * 2.4;
      a.vx = (a.vx - a.x * pull) * 0.58;
      a.vy = (a.vy - a.y * pull) * 0.58;
      a.x += a.vx; a.y += a.vy;
      if (flat) {
        // The flat view has no depth: whatever is left of it sinks away
        // over a few ticks (a switch from 3D is seen folding flat).
        a.vz = 0; a.z = Math.abs(a.z) < 0.5 ? 0 : a.z * 0.8;
      } else {
        a.vz = (a.vz - a.z * pull) * 0.58;
        a.z += a.vz;
      }
    }
  }

  function reheat(amount) {
    alpha = Math.max(alpha, amount);
    kick();
  }

  // ---------------------------------------------------------------- camera

  var cYaw = 1, sYaw = 0, cPit = 1, sPit = 0, focal = 800;   // set by aim()
  var dNear = 0, dFar = 0;                                   // nearest and farthest node depth of the last projection
  var fly = null;
  // Per-frame scratch, sized once per payload so that drawing allocates nothing.
  var vCap = 0, eCap = 0, pX, pY, pK, pD, pR, pF, pB, order, labelIdx, sortOut, sortStep, sortHigh, eShade;
  var pA, pL, pS;                                            // how strongly a node is drawn, is it lit, its step of depth
  var eOrder, eCX, eCY;                                      // the links in view sorted by bucket; where each one's curve bends to
  var eAx, eAy, eAz, ePhase, ePace, eRank, eTint;            // per link, set when the data arrives (see ingest)
  var cutX = 0, cutY = 0;                                    // set by cut()
  var SORT_STEPS = 1024, sortAt = new Int32Array(SORT_STEPS + 1);
  var linkCount = new Int32Array(LINK_HOT + 1), linkAt = new Int32Array(LINK_HOT + 1), grid = new Uint8Array(1);
  var sparkRoom = 0, carrying = 0;                           // how many more lights this frame may draw; how many links carried light in the last one

  function vReserve(count, links) {
    if (count > vCap) {
      vCap = count + 256;
      pX = new Float32Array(vCap); pY = new Float32Array(vCap); pK = new Float32Array(vCap);
      pD = new Float32Array(vCap); pR = new Float32Array(vCap); pF = new Uint8Array(vCap); pB = new Uint8Array(vCap);
      pA = new Float32Array(vCap); pL = new Uint8Array(vCap); pS = new Uint8Array(vCap);
      order = new Int32Array(vCap); labelIdx = new Int32Array(vCap);
      sortOut = new Int32Array(vCap); sortStep = new Int32Array(vCap); sortHigh = new Uint16Array(vCap);
    }
    if (links > eCap) {
      eCap = links + 512;
      eShade = new Uint8Array(eCap); eOrder = new Int32Array(eCap); eCX = new Float32Array(eCap); eCY = new Float32Array(eCap);
      eAx = new Float32Array(eCap); eAy = new Float32Array(eCap); eAz = new Float32Array(eCap);
      ePhase = new Float32Array(eCap); ePace = new Float32Array(eCap); eRank = new Float32Array(eCap); eTint = new Uint8Array(eCap);
    }
  }
  vReserve(1, 1);

  // The camera's axes for the current angles. Its "right" in world terms is
  // (cYaw, 0, -sYaw), "down" (-sYaw*sPit, cPit, -cYaw*sPit) and "forward"
  // (sYaw*cPit, sPit, cYaw*cPit); the focal length follows the canvas, so the
  // field of view stays the same when the window changes size.
  function aim() {
    if (flat && !fly) { cam.yaw = 0; cam.pitch = 0; cam.tz = 0; }   // the flat view only ever looks straight on
    cYaw = Math.cos(cam.yaw); sYaw = Math.sin(cam.yaw);
    cPit = Math.cos(cam.pitch); sPit = Math.sin(cam.pitch);
    focal = Math.max(160, Math.min(W, H)) * 0.5 / FOV_TAN;
  }

  // Where every node lands on the canvas: pX/pY in CSS pixels, pK the pixels
  // per world unit at its depth, pD its distance in front of the camera. pF is
  // 0 for a node that is hidden or behind the camera, 1 when it is off screen
  // (a link to it may still cross the picture) and 2 when it is on screen -
  // it, or what is drawn round it: its ring, its glow.
  // pB marks the ones behind the camera: for those pX/pY hold the place
  // across the view before the division by depth, which is what cut() needs.
  function projectAll() {
    aim();
    var tx = cam.tx, ty = cam.ty, tz = cam.tz, dist = cam.dist, hw = W / 2, hh = H / 2;
    var lo = Infinity, hi = -Infinity, coronas = theme.coronas, ringed = RING_SPAN[0], glow = LOOK.coronaSize, most = LOOK.coronaReach;
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      pB[i] = 0;
      if (!n.on || n.created > timeCut) { pF[i] = 0; continue; }
      var dx = n.x - tx, dy = n.y - ty, dz = n.z - tz;
      var z1 = dx * sYaw + dz * cYaw;
      var depth = dy * sPit + z1 * cPit + dist;
      if (depth < NEAR) {
        pF[i] = 0; pB[i] = 1; pX[i] = dx * cYaw - dz * sYaw; pY[i] = dy * cPit - z1 * sPit; pD[i] = depth;
        continue;
      }
      var k = focal / depth;
      var sx = hw + (dx * cYaw - dz * sYaw) * k, sy = hh + (dy * cPit - z1 * sPit) * k;
      // How far from its middle it is drawn: its picture, or its ring's, or its glow, and a little for the marks round it.
      var body = n.r * k, reach = body * SPRITE_PAD;
      if (n.inCore) reach = body * ringed;
      if (coronas[n.kix]) reach = Math.min(body * glow, body + most);
      reach += 3 * k + 2;
      pX[i] = sx; pY[i] = sy; pK[i] = k; pD[i] = depth;
      pF[i] = sx + reach < 0 || sx - reach > W || sy + reach < 0 || sy - reach > H ? 1 : 2;
      if (depth < lo) lo = depth;
      if (depth > hi) hi = depth;
    }
    dNear = lo; dFar = hi;
  }

  // Where the link from a node in front of the camera to one behind it leaves
  // the view (into cutX, cutY): the point at which it meets the near plane.
  // Without this a planet in plain sight would lose its links to whatever the
  // camera has moved past.
  function cut(back, front) {
    var kf = pK[front], u = pX[back], v = pY[back], k = focal / NEAR;
    var t = (NEAR - pD[back]) / (pD[front] - pD[back]);         // the front end is at NEAR or beyond: never a division by 0
    cutX = W / 2 + (u + ((pX[front] - W / 2) / kf - u) * t) * k;
    cutY = H / 2 + (v + ((pY[front] - H / 2) / kf - v) * t) * k;
  }

  // The world point that shows at canvas position (px, py) at a given depth.
  function unproject(px, py, depth) {
    aim();
    var a = (px - W / 2) * depth / focal, b = (py - H / 2) * depth / focal, c = depth - cam.dist;
    return {
      x: cam.tx + cYaw * a - sYaw * sPit * b + sYaw * cPit * c,
      y: cam.ty + cPit * b + sPit * c,
      z: cam.tz - sYaw * a - cYaw * sPit * b + cYaw * cPit * c
    };
  }

  // The user took the camera: no automatic move may pull it back.
  function steer() { fly = null; firstFit = false; refit = false; }

  function orbitBy(dx, dy) {
    steer();
    cam.yaw += dx * 0.0065;
    cam.pitch = clamp(cam.pitch + dy * 0.0065, -PITCH_MAX, PITCH_MAX);   // never over the top: up stays up
  }
  // Slide the picture by (dx, dy) pixels: the focus point moves the other way.
  function panBy(dx, dy) {
    steer(); aim();
    var s = cam.dist / focal;
    cam.tx -= (cYaw * dx - sYaw * sPit * dy) * s;
    cam.ty -= cPit * dy * s;
    cam.tz -= (-sYaw * dx - cYaw * sPit * dy) * s;
  }
  // Come closer or back off, keeping what is under (px, py) under it.
  function zoomAt(px, py, dist) {
    steer(); aim();
    dist = clamp(dist, Math.max(focal / 6, NEAR * 5), focal / 0.05);   // never so close that the focus fades out
    var a = px - W / 2, b = py - H / 2, s = (cam.dist - dist) / focal;
    cam.tx += (cYaw * a - sYaw * sPit * b) * s;
    cam.ty += cPit * b * s;
    cam.tz += (-sYaw * a - cYaw * sPit * b) * s;
    cam.dist = dist;
  }

  function resize() {
    var rect = wrap.getBoundingClientRect();
    var w = Math.max(1, Math.round(rect.width)), h = Math.max(1, Math.round(rect.height));
    var ratio = Math.min(2, win.devicePixelRatio || 1);
    if (w === W && h === H && ratio === dpr) return;
    W = w; H = h; dpr = ratio;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    paintBackdrop();
    // A view that was fitted while it had no size shows a speck once it gets
    // one: fit it again. (Otherwise a resize leaves the camera where it is.)
    if ((firstFit || unsized) && nodes.length) fitView(false);
    kick();
  }

  function fitView(animate, yaw, pitch) {
    var list = nodes.filter(visible);
    if (!list.length || !W) return;
    unsized = W < 40 || H < 40;
    var minX = Infinity, minY = Infinity, minZ = Infinity, maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    list.forEach(function (n) {
      if (n.x - n.r < minX) minX = n.x - n.r; if (n.x + n.r > maxX) maxX = n.x + n.r;
      if (n.y - n.r < minY) minY = n.y - n.r; if (n.y + n.r > maxY) maxY = n.y + n.r;
      if (n.z - n.r < minZ) minZ = n.z - n.r; if (n.z + n.r > maxZ) maxZ = n.z + n.r;
    });
    aim();
    var pad = 70, cx = (minX + maxX) / 2, cy = (minY + maxY) / 2, cz = flat ? 0 : (minZ + maxZ) / 2, dist;
    if (flat) {
      dist = focal / clamp(Math.min((W - pad * 2) / Math.max(40, maxX - minX),
        (H - pad * 2 - 40) / Math.max(40, maxY - minY)), 0.06, 2.2);
    } else {
      // Fit the ball around everything rather than its outline from here, so
      // nothing leaves the picture when the view is turned.
      var reach = 20;
      list.forEach(function (n) {
        var dx = n.x - cx, dy = n.y - cy, dz = n.z - cz, d = Math.sqrt(dx * dx + dy * dy + dz * dz) + n.r;
        if (d > reach) reach = d;
      });
      // (A ball leaves the corners of its box empty, so less margin will do.)
      var slope = Math.max(60, Math.min(W - 80, H - 120)) / 2 / focal;
      dist = clamp(reach * Math.sqrt(1 + slope * slope) / slope, focal / 2.2, focal / 0.06);
    }
    refit = false;
    flyTo(cx, cy, cz, dist, animate, yaw, pitch);
  }

  function flyTo(tx, ty, tz, dist, animate, yaw, pitch) {
    var to = {
      tx: tx, ty: ty, tz: tz, dist: dist,
      yaw: yaw == null ? cam.yaw : yaw, pitch: pitch == null ? cam.pitch : pitch
    };
    fly = {
      a: { tx: cam.tx, ty: cam.ty, tz: cam.tz, dist: cam.dist, yaw: cam.yaw, pitch: cam.pitch },
      b: to, t0: performance.now(), ms: animate ? 420 : 0
    };
    if (!animate) flyStep(fly.t0);
    kick();
  }

  // Where the camera is on its way from fly.a to fly.b at time `now`.
  function flyStep(now) {
    var u = fly.ms ? clamp((now - fly.t0) / fly.ms, 0, 1) : 1, ez = u < 0.5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2;
    cam.tx = lerp(fly.a.tx, fly.b.tx, ez); cam.ty = lerp(fly.a.ty, fly.b.ty, ez); cam.tz = lerp(fly.a.tz, fly.b.tz, ez);
    cam.yaw = lerp(fly.a.yaw, fly.b.yaw, ez); cam.pitch = lerp(fly.a.pitch, fly.b.pitch, ez);
    cam.dist = 1 / lerp(1 / fly.a.dist, 1 / fly.b.dist, ez);          // even steps of scale, as a 2D zoom makes
    if (u >= 1) fly = null;
  }

  // Centre on a node: the focus point moves to it and the camera comes closer.
  function centerOn(n) {
    aim();
    var dist = clamp(Math.min(cam.dist, focal / 1.15), focal / 2.6, focal / 0.3);
    // With the side panel open, aim a little to the right of the node so that it sits clear of the panel.
    var shift = (side.classList.contains('mb-on') && W > 760 ? 185 : 0) * dist / focal;
    refit = false;
    flyTo(n.x + cYaw * shift, n.y, flat ? 0 : n.z - sYaw * shift, dist, true);
  }

  function refocus() {
    var set = null;
    var anchor = selected || hover;
    if (anchor && visible(anchor)) {
      set = new Set([anchor.id]);
      anchor.adj.forEach(function (m) { set.add(m.id); });
    }
    focusSet = set;
    kick();
  }

  // Switch between the 3D view and the flat one. Going flat, the layout folds
  // onto z = 0 and the camera swings round to face it; going 3D, every node
  // gets some depth to start from and the layout puffs out again.
  function setFlat(on) {
    on = !!on;
    if (on === flat) return;
    flat = on;
    storeFlat(on);
    releaseDrag();
    cam.yaw = Math.atan2(Math.sin(cam.yaw), Math.cos(cam.yaw));     // turn back the short way round
    var depth = 34 * Math.pow(nodes.length + 1, 1 / 3);
    nodes.forEach(function (n) {
      n.vz = 0;
      if (!flat) n.z = (Math.random() * 2 - 1) * depth;
      else if (!n.on) n.z = 0;             // not in the layout, so nothing would fold it
    });
    reheat(0.7);
    if (!nodes.length) { cam.yaw = flat ? 0 : YAW0; cam.pitch = flat ? 0 : PITCH0; }
    fitView(true, flat ? 0 : YAW0, flat ? 0 : PITCH0);
    refit = true;                          // and once more when the new layout has found its size
    renderChips();
    motionSoon();
  }

  // Everything that moves by itself - the light along the links, the
  // twinkling stars and the slow turn of an idle view - is paced by this one
  // timer, one frame about 30 times a second, instead of the animation loop,
  // so a page that is only being looked at is not kept busy at full rate. It is not armed at all for people who asked for
  // reduced motion or in a hidden tab: there the picture stands still and
  // the loop rests once the layout has settled. (Re)arming it also puts the
  // turn off for the usual wait.
  function motionSoon() {
    win.clearTimeout(motionTimer);
    motionTimer = 0; spinning = false;
    if (dead || reduced || doc.hidden) return;
    lastInput = Math.max(lastInput, performance.now());
    motionTimer = win.setTimeout(motionStep, motionMs);
  }
  function motionStep() {
    motionTimer = 0;
    if (dead || reduced || doc.hidden) { spinning = false; return; }
    // Nothing on show (an empty bank, every kind hidden, a stale key): look again in a second.
    if (stale || !live_nodes.length) {
      spinning = false;
      motionTimer = win.setTimeout(motionStep, 1000);
      return;
    }
    var now = performance.now();
    // The turn only when nothing else is going on: in the 3D view, after a
    // few quiet seconds, with the layout settled and nothing being dragged,
    // flown to, replayed, pointed at or read in the side panel.
    if (flat || now - lastInput < SPIN_WAIT || alpha > 0.004 || alphaTarget > 0 || drag || fly || playing || selected || hover) {
      spinning = false;
    } else {
      if (!spinning) { spinning = true; spinFrom = now; spinLast = now; }
      // Ease in over a second and a half, so the start is not a jolt.
      cam.yaw += SPIN_RATE * Math.min(1, (now - spinFrom) / 1500) * Math.min(100, now - spinLast);
      if (cam.yaw > Math.PI) cam.yaw -= 2 * Math.PI;
      spinLast = now;
    }
    // A new frame only when there is something new to show: the view is
    // turning, or the last frame had links that carry light. (Otherwise -
    // a node without links selected, a search that leaves no link lit - the
    // picture stands still and the loop rests; whatever changes that draws a
    // frame of its own, which is seen here. The stars twinkle in the frames
    // that there are; they are not a reason for one.)
    if (spinning || carrying) kick();
    motionTimer = win.setTimeout(motionStep, motionMs);
  }
  // Any key, wheel or pointer activity in the view: the turn stops at once.
  function noteInput() {
    lastInput = performance.now();
    spinning = false;
  }

  // --------------------------------------------------------------- drawing

  // Order the first `count` entries of an index array by key, biggest first:
  // a counting sort in two rounds of 1,024 steps (the fine half of the key's
  // range lo..hi, then the coarse half), so about a million steps in all.
  // One round was too coarse for a big bank: bodies that shared a step swapped
  // places as the view turned. It is four passes whatever the order was and
  // allocates nothing; entries with the same key - every node of the flat
  // view - keep the order they came in.
  function sortDown(idx, count, key, lo, hi) {
    var last = SORT_STEPS - 1, top = SORT_STEPS * SORT_STEPS - 1, scale = hi > lo ? top / (hi - lo) : 0, i, b, q;
    for (b = 0; b <= SORT_STEPS; b++) sortAt[b] = 0;
    for (i = 0; i < count; i++) {
      q = top - clamp((key[idx[i]] - lo) * scale | 0, 0, top);
      sortStep[i] = q; sortAt[(q & last) + 1]++;
    }
    for (b = 0; b < SORT_STEPS; b++) sortAt[b + 1] += sortAt[b];
    for (i = 0; i < count; i++) { q = sortStep[i]; b = sortAt[q & last]++; sortOut[b] = idx[i]; sortHigh[b] = q >> 10; }
    for (b = 0; b <= SORT_STEPS; b++) sortAt[b] = 0;
    for (i = 0; i < count; i++) sortAt[sortHigh[i] + 1]++;
    for (b = 0; b < SORT_STEPS; b++) sortAt[b + 1] += sortAt[b];
    for (i = 0; i < count; i++) idx[sortAt[sortHigh[i]]++] = sortOut[i];
  }

  // Both ends off the same side of the canvas: nothing of the line can show.
  function aside(x0, y0, x1, y1) {
    return (x0 < 0 && x1 < 0) || (x0 > W && x1 > W) || (y0 < 0 && y1 < 0) || (y0 > H && y1 > H);
  }

  // Add every link of one bucket (one kind's colour at one depth, or the
  // highlighted ones) to the current path. Each is a gentle arc: the middle
  // of the link is pushed sideways in the layout's own space - across the
  // link and the link's own fixed axis, by a share of its length - and that
  // point, projected like a node, is where the curve bends to. Being part
  // of the scene, the arc turns with the view; it cannot flip or jitter
  // between frames. In the flat view the push is in the plane, to one side
  // or the other. A big bank cannot have every link curved (see draw):
  // `crowd` says how far that has set in (0: not at all, every link takes
  // its whole bend; up to 1). With it a link bends less the farther off it
  // is (not at all from depth shade LOOK.lodBent on, less and less over the
  // shade before that), the shorter it is on screen, and the later its
  // place in the queue (`keep`: the share of the near links that may
  // bend); `most` scales them all. Each of those takes the bend away
  // little by little, so that a link is all but straight before it is
  // drawn as a line: none snaps. The same goes for a bend that would come
  // close to the camera, which is eased out before it could pass it. One
  // that runs on behind the camera is drawn straight, as far as the near
  // plane. Where each curve bends to is kept (eCX, eCY) for the light that
  // travels along it.
  function traceLinks(bucket, crowd, keep, most) {
    var tx = cam.tx, ty = cam.ty, tz = cam.tz, dist = cam.dist, hw = W / 2, hh = H / 2;
    var least = LOOK.lodShort * LOOK.lodShort, ease = LOOK.lodEase, fog = dFar - dNear > 1 ? 1 / (dFar - dNear) : 0;
    var farFrom = LOOK.lodBent / LINK_SHADES;                  // from this depth on (0 nearest .. 1 farthest) a link of a crowded view is straight
    for (var q = linkAt[bucket], end = q + linkCount[bucket]; q < end; q++) {
      var i = eOrder[q], s = edges[i].s, t = edges[i].t, a = s.idx, b = t.idx;
      if (pB[a]) { cut(a, b); ctx.moveTo(cutX, cutY); ctx.lineTo(pX[b], pY[b]); continue; }
      if (pB[b]) { cut(b, a); ctx.moveTo(pX[a], pY[a]); ctx.lineTo(cutX, cutY); continue; }
      // Where the link's own middle shows (nearer its far end than half-way between the two, as things are
      // in perspective): where a link without a bend "bends to", so that its light runs the same whether it
      // is drawn as a line or as a curve with nothing left of its bend.
      var ax = pX[a], ay = pY[a], bx = pX[b], by = pY[b], da = pD[a], db = pD[b];
      var cx = (ax * da + bx * db) / (da + db), cy = (ay * da + by * db) / (da + db);
      // What its place in the queue, its depth and its length on screen each leave of its bend, 0 to 1, and
      // with that how much of its bend it takes. (Worked out for every link, crowded view or not, and without
      // the helpers: plain sums, so that the frame of a big bank leaves no garbage behind.)
      var middle = (da + db) / 2, byRank = (keep - eRank[i]) / ease, byDepth = (farFrom - (middle - dNear) * fog) * LINK_SHADES;
      var byLength = ((bx - ax) * (bx - ax) + (by - ay) * (by - ay)) / least - 1;
      byRank = byRank < 0 ? 0 : byRank > 1 ? 1 : byRank;
      byDepth = byDepth < 0 ? 0 : byDepth > 1 ? 1 : byDepth * byDepth * (3 - 2 * byDepth);
      byLength = byLength < 0 ? 0 : byLength > 1 ? 1 : byLength;
      var bend = most * (1 - crowd * (1 - byRank * byDepth * byLength));
      ctx.moveTo(ax, ay);
      if (bend > 0) {
        var dx = t.x - s.x, dy = t.y - s.y, dz = t.z - s.z, ox, oy, oz;
        if (flat) {
          var side = eAz[i] < 0 ? -LOOK.bendFlat : LOOK.bendFlat;
          ox = dy * side; oy = -dx * side; oz = 0;
        } else {
          ox = dy * eAz[i] - dz * eAy[i]; oy = dz * eAx[i] - dx * eAz[i]; oz = dx * eAy[i] - dy * eAx[i];
        }
        // How deep the bend would be: nearer than a few times the near plane, it is eased out (which leaves
        // it between there and the link's own middle, never past the camera).
        var depth = middle + (oy * sPit + (ox * sYaw + oz * cYaw) * cPit) * bend;
        if (depth < NEAR * 4) {
          var room = depth > NEAR ? (depth - NEAR) / (NEAR * 3) : 0;
          bend *= room * room * (3 - 2 * room);
        }
        var mx = (s.x + t.x) / 2 + ox * bend - tx, my = (s.y + t.y) / 2 + oy * bend - ty, mz = (s.z + t.z) / 2 + oz * bend - tz;
        var z1 = mx * sYaw + mz * cYaw, k = focal / (my * sPit + z1 * cPit + dist);
        cx = hw + (mx * cYaw - mz * sYaw) * k; cy = hh + (my * cPit - z1 * sPit) * k;
        ctx.quadraticCurveTo(cx, cy, bx, by);
      } else {
        ctx.lineTo(bx, by);
      }
      eCX[i] = cx; eCY[i] = cy;
    }
  }

  // The colour a bucket of links is stroked in. There is a small fixed set
  // of them (kind x depth, dimmed or not, flat or not, and the highlighted
  // ones), so each is put together once per theme, not on every frame.
  function linkInk(slot, kind, strength) {
    return theme.inks[slot] || (theme.inks[slot] = rgba(theme.links[kind], Math.round(strength * 1000) / 1000));
  }

  // Points of light on the links of one bucket: each link has its own start
  // and its own pace, and its light runs from the link's first end to its
  // second, along the curve, then waits a while. `keep` thins them out (a
  // link carries light when its place in the queue is below it); the frame's
  // allowance (sparkRoom) is the hard limit. All of a bucket's lights are
  // one run of the same picture at the same strength; a light grows as it
  // comes out of one body and shrinks as it sinks into the other. The links
  // that carry light are counted (carrying): with none, nothing moves, and
  // no further frame is asked for on the light's account.
  function sparkle(bucket, sprite, sec, keep, size, strength) {
    var round = 1 + LOOK.lightGap, half = size / 2;
    ctx.globalAlpha = strength;
    for (var q = linkAt[bucket], end = q + linkCount[bucket]; q < end && sparkRoom > 0; q++) {
      var i = eOrder[q];
      if (eRank[i] >= keep) continue;
      carrying++;
      var u = (ePhase[i] + sec * ePace[i]) % round;
      if (u >= 1) continue;                                    // between two lights
      var a = edges[i].s.idx, b = edges[i].t.idx;
      if (pB[a] || pB[b]) continue;                            // cut off at the camera: nothing to run along
      var v = 1 - u, x = v * v * pX[a] + 2 * u * v * eCX[i] + u * u * pX[b], y = v * v * pY[a] + 2 * u * v * eCY[i] + u * u * pY[b];
      if (x < -half || x > W + half || y < -half || y > H + half) continue;
      var near = (u < v ? u : v) * 7, d = near < 1 ? half * (0.35 + 0.65 * near) : half;
      ctx.drawImage(sprite, x - d, y - d, d * 2, d * 2);
      sparkRoom--;
    }
  }

  // A recall sends light out along the recalled node's links, away from
  // it, while its pulse lasts. Those lights all set off at once, and so
  // close together they would add up to a white blot round the node: each
  // is as strong as its share of the circle they have reached allows -
  // faint while they are packed round the node, full once they have
  // spread out. Like the other lights they come from under the bodies and
  // out of the frame's allowance - of which they take no more than half,
  // so that the rest of the traffic does not stop for them.
  function burst(now, mode) {
    for (var i = 0, least = sparkRoom / 2; i < edges.length && sparkRoom > least; i++) {
      if (eShade[i] === 255) continue;
      var e = edges[i], a = e.s.idx, b = e.t.idx, from = e.s.flash ? e.s : e.t.flash ? e.t : null;
      if (!from || pB[a] || pB[b]) continue;                   // (a link cut off at the camera has no curve to run along)
      var out = (now - from.flash) / 2200 * LOOK.burstPace;
      if (out <= 0 || out >= 1) continue;
      var u = from === e.s ? out : 1 - out, v = 1 - u, f = from.idx;
      var x = v * v * pX[a] + 2 * u * v * eCX[i] + u * u * pX[b], y = v * v * pY[a] + 2 * u * v * eCY[i] + u * u * pY[b];
      var size = clamp(LOOK.lightSize * pK[f], LOOK.lightPx[0], LOOK.lightPx[1]) * LOOK.burstSize;
      var gone = Math.sqrt((x - pX[f]) * (x - pX[f]) + (y - pY[f]) * (y - pY[f]));   // how far from the node it has come
      ctx.globalAlpha = LOOK.lightInk[mode] * (1 - out) * Math.min(1, 6.2832 * gone / (from.adj.length * size));
      ctx.drawImage(theme.sparks[from.kix], x - size / 2, y - size / 2, size, size);
      sparkRoom--;
    }
  }

  // How far out the mark round a node runs - the ring round the selected
  // one, or the pointed-at one: `gap` layout units outside it, or outside
  // its ring if it wears one (the mark is not to cut through that).
  function markAt(n, gap) {
    var k = pK[n.idx], ringed = n.inCore && n.r * k >= LOOK.ringPlain;
    return (n.r * (ringed ? LOOK.ringSize[1] : 1) + gap) * k;
  }

  function draw(now) {
    var i, j, k, n, e, a, b, count = 0, labN = 0, rLo = Infinity, rHi = 0;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    projectAll();
    var zoom = focal / cam.dist;                               // pixels per world unit at the focus point
    var fog = dFar - dNear > 1 ? 1 / (dFar - dNear) : 0;       // 0 in the flat view: there is no depth to cue
    var dark = theme.dark, mode = dark ? 0 : 1, text = theme.text;
    var dimming = focusSet || searchSet;
    var anchor = selected || hover;
    var moving = !reduced, sec = now / 1000;                   // light and twinkle: only when motion is allowed

    // The background: the still picture, then (in the dark theme) the stars.
    ctx.drawImage(backdrop, 0, 0, W, H);
    if (dark) drawStars(sec, moving);

    // Links, sorted into buckets - the colour of one kind at one of a few
    // depths, far ones thinner and fainter - and stroked one path per bucket,
    // so thousands of them cost a few dozen strokes. Which links are in view
    // is settled first, then they are put in bucket order (a counting sort).
    var linksOn = 0;
    for (j = 0; j <= LINK_HOT; j++) linkCount[j] = 0;
    for (i = 0; i < edges.length; i++) {
      e = edges[i]; a = e.s.idx; b = e.t.idx;
      var fa = pF[a], fb = pF[b], mid;
      eShade[i] = 255;                                         // until shown otherwise: hidden, behind the camera or off to one side
      if (fa && fb) {
        if (fa === 1 && fb === 1 && aside(pX[a], pY[a], pX[b], pY[b])) continue;
        mid = (pD[a] + pD[b]) * 0.5;
      } else {
        // One end behind the camera: what counts is the piece in front of it.
        var back = pB[a] ? a : b, front = back === a ? b : a;
        if (!pB[back] || !pF[front]) continue;
        cut(back, front);
        if (pF[front] === 1 && aside(cutX, cutY, pX[front], pY[front])) continue;
        mid = (NEAR + pD[front]) * 0.5;
      }
      j = anchor && (e.s === anchor || e.t === anchor) ? LINK_HOT
        : eTint[i] * LINK_SHADES + clamp((mid - dNear) * fog * LINK_SHADES | 0, 0, LINK_SHADES - 1);
      eShade[i] = j; linkCount[j]++; linksOn++;
    }
    for (j = 0, k = 0; j <= LINK_HOT; j++) { linkAt[j] = k; k += linkCount[j]; }
    for (i = 0; i < edges.length; i++) { j = eShade[i]; if (j !== 255) eOrder[linkAt[j]++] = i; }
    for (j = 0; j <= LINK_HOT; j++) linkAt[j] -= linkCount[j];
    // The links are thin lines laid over the background, so that the space
    // between the worlds stays dark. (In 3D they are what gives the cloud
    // its shape, and most of them are faded by distance, so the near ones
    // are drawn a little stronger.) A big bank gets its far links straight
    // and thin, and of its near ones only so many curved (and only where
    // they are long enough for the curve to show): that many curves would
    // cost more than they show. It comes on by degrees (crowd: 0 with
    // LOOK.lodLinks links in view, 1 with a fifth more), and each link gives
    // up its bend by degrees (see traceLinks), so that nothing changes at a
    // stroke as the view turns or closes in.
    var crowd = clamp((linksOn / LOOK.lodLinks - 1) / LOOK.lodEase, 0, 1), nearLinks = 1;
    for (j = 0; j < LINK_HOT; j++) if (j % LINK_SHADES < LOOK.lodBent) nearLinks += linkCount[j];
    var keepBent = LOOK.lodLinks / nearLinks;
    var linkAlpha = (dimming ? LOOK.linkDim : LOOK.linkInk[mode]) * (fog ? LOOK.linkDeep : 1);
    var slot0 = (dimming ? 1 : 0) + (fog ? 2 : 0), far, shadeDepth, width, bucket;
    if (dark && linksOn > LOOK.linkCrowd) ctx.globalAlpha = Math.sqrt(LOOK.linkCrowd / linksOn);
    for (j = LINK_SHADES - 1; j >= 0; j--) {
      far = j / (LINK_SHADES - 1); shadeDepth = fog ? dNear + (j + 0.5) / LINK_SHADES / fog : cam.dist;
      width = clamp(0.5 * focal / shadeDepth, 0.75 - 0.3 * far, LOOK.linkWide);
      if (dimming || j >= LOOK.lodBent) width *= 1 - (1 - LOOK.lodThin) * crowd;
      for (k = 0; k < KINDS.length; k++) {
        bucket = k * LINK_SHADES + j;
        if (!linkCount[bucket]) continue;
        ctx.beginPath();
        traceLinks(bucket, crowd, keepBent, dimming ? 1 - crowd : 1);
        ctx.lineWidth = width;
        ctx.strokeStyle = linkInk(slot0 * LINK_HOT + bucket, k, linkAlpha * (1 - LOOK.linkFar * far));
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
    // The links of the selected or pointed-at node: clear, over a wider, fainter line of the same colour -
    // but not so wide or, where there are very many, so strong that they bury what they lead to.
    if (linkCount[LINK_HOT]) {
      width = clamp(0.8 * zoom, 1.5, LOOK.hotWide);
      if (linkCount[LINK_HOT] > LOOK.hotCrowd) ctx.globalAlpha = Math.sqrt(LOOK.hotCrowd / linkCount[LINK_HOT]);
      ctx.beginPath();
      traceLinks(LINK_HOT, 0, 0, 1);
      ctx.lineWidth = width * LOOK.hotGlowWidth;
      ctx.strokeStyle = linkInk(4 * LINK_HOT + anchor.kix, anchor.kix, LOOK.hotInk[mode] * LOOK.hotGlow);
      ctx.stroke();
      ctx.lineWidth = width;
      ctx.strokeStyle = linkInk(4 * LINK_HOT + KINDS.length + anchor.kix, anchor.kix, LOOK.hotInk[mode]);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // Light travelling along the links: on every link of the selected or
    // pointed-at node and, after a recall, of the recalled one; otherwise
    // on as many as the allowance lets through, near links before far ones
    // (and none on links that are dimmed). In the dark theme it is light:
    // added to what is behind it.
    carrying = 0;
    if (moving && linksOn) {
      if (dark) ctx.globalCompositeOperation = 'lighter';
      sparkRoom = LOOK.lights;
      var sparkSize, lightK = LOOK.lightSize * focal;
      if (linkCount[LINK_HOT]) {
        sparkSize = clamp(lightK / cam.dist, LOOK.lightPx[0], LOOK.lightPx[1]) * LOOK.lightHot;
        sparkle(LINK_HOT, theme.sparks[anchor.kix], sec, 2, sparkSize, LOOK.lightInk[mode]);
      }
      if (pulses.length) burst(now, mode);
      if (!dimming) {
        // About this share of the links in view would be showing a light at any moment if all of them carried one.
        var showing = (linksOn - linkCount[LINK_HOT]) * (1 - LOOK.lightFar / 2) / (1 + LOOK.lightGap);
        var keep = showing > sparkRoom ? sparkRoom / showing : 1;
        for (j = 0; j < LINK_SHADES; j++) {
          far = j / (LINK_SHADES - 1); shadeDepth = fog ? dNear + (j + 0.5) / LINK_SHADES / fog : cam.dist;
          sparkSize = clamp(lightK / shadeDepth, LOOK.lightPx[0], LOOK.lightPx[1]);
          for (k = 0; k < KINDS.length; k++) {
            bucket = k * LINK_SHADES + j;
            if (linkCount[bucket]) {
              sparkle(bucket, theme.sparks[k], sec, keep * (1 - LOOK.lightFar * far), sparkSize, LOOK.lightInk[mode] * (1 - LOOK.linkFar * far));
            }
          }
        }
      }
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = 'source-over';
    }

    // Nodes, far ones first so that near ones cover them. Farther also means
    // dimmer (a hazed picture of the world, see hazed()) - less so for what
    // is selected, pointed at or found, and not at all for the selected or
    // pointed-at node itself: the focus is not to look faded for being at
    // the back. A first pass settles how each one is drawn: its size, how
    // strongly, how hazed.
    for (i = 0; i < nodes.length; i++) {
      n = nodes[i];
      if (!n.on || n.created > timeCut) { n.seen = false; continue; }
      if (!n.seen) { n.seen = true; n.born = now; }
      if (pF[i] === 2) order[count++] = i;
    }
    sortDown(order, count, pD, dNear, dFar);
    var steps = LOOK.fadeSteps - 1, suns = 0, r, al, depth;
    for (j = 0; j < count; j++) {
      i = order[j]; n = nodes[i];
      var grow = Math.min(1, (now - n.born) / 320);
      if (grow < 1) needsDraw = true;
      k = pK[i]; depth = pD[i]; r = n.r * (0.25 + 0.75 * grow) * k;
      var lit = !dimming || (focusSet && focusSet.has(n.id)) || (!focusSet && searchSet && searchSet.has(n.id));
      var away = (depth - dNear) * fog;                        // 0 nearest .. 1 farthest (0 throughout the flat view)
      al = n.status === 'superseded' ? 0.4 : n.status === 'evicted' ? 0.72 : 1;
      if (depth < NEAR * 5) al *= (depth - NEAR) / (NEAR * 4);   // about to pass the camera: fade out, do not fill the view
      pR[i] = r; pA[i] = al; pL[i] = lit ? 1 : 0;
      pS[i] = n === anchor ? 0 : clamp(away * LOOK.fadeSteps * (dimming ? 0.5 : 1) | 0, 0, steps);
      if (lit && theme.coronas[n.kix]) suns++;
    }
    // The suns' glow, under every body: the one light of a body that is
    // added to what is behind it (dark theme only). It reaches a couple of
    // radii and never far in pixels, so a sun does not wash out what is near
    // it and a close-up is not all glow; a far sun's is fainter.
    if (suns) {
      ctx.globalCompositeOperation = 'lighter';
      for (j = 0; j < count; j++) {
        i = order[j];
        var corona = theme.coronas[nodes[i].kix];
        if (!corona || !pL[i]) continue;
        r = Math.min(pR[i] * LOOK.coronaSize, pR[i] + LOOK.coronaReach);
        ctx.globalAlpha = pA[i] * (1 - LOOK.fade * pS[i] / steps);
        ctx.drawImage(corona, pX[i] - r, pY[i] - r, r * 2, r * 2);
      }
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = 'source-over';
    }
    // What is outside the selection or the search goes first, under all the
    // rest, each as a dark shape of itself: solid, so that nothing shows
    // through it, yet never in front of anything that is lit. (One small
    // picture does for every size: it has little left to show.)
    if (dimming) {
      for (j = 0; j < count; j++) {
        i = order[j];
        if (pL[i]) continue;
        r = pR[i] * SPRITE_PAD; n = nodes[i];
        ctx.globalAlpha = pA[i];
        ctx.drawImage(theme.dims[n.kix * LOOK.variants + n.look][pR[i] * dpr < SPRITE_UP[0] ? 0 : 1], pX[i] - r, pY[i] - r, r * 2, r * 2);
      }
      ctx.globalAlpha = 1;
    }

    // The bodies, each one picture: the size that suits how big it shows,
    // at its step of depth. An always-on memory wears a ring, which goes
    // with its planet - the ring before it, the stretch in front after - so
    // that the planet sits in its ring and a nearer body covers both; on a
    // planet too small for that to show it is the same ring as one thin
    // line, likewise round the back first and across the front after.
    ctx.lineWidth = Math.max(1, 0.5 * zoom);
    ctx.strokeStyle = theme.ring;
    bigRoom = 1;
    var thin = (LOOK.ringSize[0] + LOOK.ringSize[1]) / 2;      // where the thin line runs: down the middle of the ring, in planet radii
    for (j = 0; j < count; j++) {
      i = order[j];
      if (!pL[i]) continue;                                    // (drawn already, as a dark shape)
      n = nodes[i]; r = pR[i]; al = pA[i];
      var x = pX[i], y = pY[i], sr = r * SPRITE_PAD, px = r * dpr, ring = -1, rr = 0;     // (ring: -1 none, -2 the thin line, else its pictures)
      if (n.inCore) {
        if (r < LOOK.ringPlain) {
          ring = -2; rr = r * thin;
          ctx.globalAlpha = al * LOOK.ringPlainInk;
          ctx.beginPath(); ctx.ellipse(x, y, rr, rr * LOOK.ringOpen, LOOK.ringTurn, 0, 6.2832); ctx.stroke();
        } else {
          k = ringFor(n.kix, px < SPRITE_UP[0] ? 0 : px < SPRITE_UP[1] ? 1 : 2);
          ring = (n.kix * RINGS + k) * 2; rr = r * RING_SPAN[k];
          ctx.globalAlpha = al * (1 - LOOK.fade * pS[i] / steps);
          ctx.drawImage(theme.rings[ring], x - rr, y - rr, rr * 2, rr * 2);
          rr = r * RING_FRONT[k];
        }
      }
      ctx.globalAlpha = al;
      if (px < SPRITE_UP[1]) {
        ctx.drawImage(theme.balls[n.kix * LOOK.variants + n.look][pS[i] * SMALL + (px < SPRITE_UP[0] ? 0 : 1)], x - sr, y - sr, sr * 2, sr * 2);
      } else {
        ctx.drawImage(bigBody(n.kix, n.look, px < SPRITE_UP[2] ? 0 : 1), x - sr, y - sr, sr * 2, sr * 2);
        if (pS[i]) {                                           // a big picture is hazed by laying the haze's colour over the body
          ctx.globalAlpha = al * LOOK.fade * pS[i] / steps;
          sr = r * VEIL_PAD;
          ctx.drawImage(theme.veil, x - sr, y - sr, sr * 2, sr * 2);
        }
      }
      if (ring >= 0) {
        ctx.globalAlpha = al * (1 - LOOK.fade * pS[i] / steps);
        ctx.drawImage(theme.rings[ring + 1], x - rr, y - rr, rr * 2, rr * 2);
      } else if (ring === -2) {
        ctx.globalAlpha = al * LOOK.ringPlainInk;
        ctx.beginPath(); ctx.ellipse(x, y, rr, rr * LOOK.ringOpen, LOOK.ringTurn, 0, 3.1416); ctx.stroke();
      }
      if (pD[i] < NEAR * 5 && n !== anchor) continue;          // fading out: its name would be left hanging
      labelIdx[labN++] = i;
      if (r < rLo) rLo = r;
      if (r > rHi) rHi = r;
    }
    ctx.globalAlpha = 1;

    // On top of everything: a crisp ring in the accent colour round the
    // selected node, a thinner and fainter one round the pointed-at node,
    // and recall pulses - widening rings of light. (The light a recall sends
    // out along the links is drawn with the other lights: see burst.)
    if (selected && pF[selected.idx]) {
      i = selected.idx;
      ctx.lineWidth = Math.max(1.8, 0.9 * zoom); ctx.strokeStyle = theme.pick;
      ctx.beginPath(); ctx.arc(pX[i], pY[i], markAt(selected, LOOK.pickAt), 0, 6.2832); ctx.stroke();
    }
    if (hover && hover !== selected && pF[hover.idx]) {
      i = hover.idx;
      ctx.lineWidth = Math.max(1.2, 0.5 * zoom); ctx.strokeStyle = theme.hover;
      ctx.beginPath(); ctx.arc(pX[i], pY[i], markAt(hover, LOOK.hoverAt), 0, 6.2832); ctx.stroke();
    }
    if (dark) ctx.globalCompositeOperation = 'lighter';
    for (i = pulses.length - 1; i >= 0; i--) {
      var p = pulses[i], age = (now - p.t0) / 2200;
      if (age >= 1 || byId.get(p.node.id) !== p.node) {        // over, or its node has left
        if (p.node.flash === p.t0) p.node.flash = 0;
        pulses.splice(i, 1);
        continue;
      }
      j = p.node.idx;
      if (age < 0 || !pF[j]) { needsDraw = true; continue; }
      var swell = 1 - Math.pow(1 - age, 3);
      ctx.lineWidth = Math.max(1.8, 0.8 * pK[j]) * (1 - age);
      ctx.strokeStyle = rgba(theme.rgb[p.node.kind] || text, Math.round(LOOK.pulseInk * (1 - age) * 1000) / 1000);
      ctx.beginPath(); ctx.arc(pX[j], pY[j], (p.node.r + 4 + swell * LOOK.pulseReach) * pK[j], 0, 6.2832); ctx.stroke();
      p.node.flash = p.t0;
      needsDraw = true;
    }
    ctx.globalCompositeOperation = 'source-over';

    // Labels: biggest on screen first (in 3D that is the near ones), never
    // overlapping, always for the focused set. A coarse grid of taken cells
    // decides what still fits. In the flat view the grid is counted from the
    // layout's origin, not the canvas corner, so it slides with the picture
    // and the names that fit stay the same ones while the view is panned.
    // Each name is outlined in the background's colour first, so that it
    // reads over a planet, a sun or a link as well as over empty space.
    sortDown(labelIdx, labN, pR, rLo, rHi);
    var cols = (W / 24 | 0) + 3, rows = (H / 15 | 0) + 4, shown = 0;
    var gx = flat ? ((W / 2 - cam.tx * zoom) % 24 + 24) % 24 : 0, gy = flat ? ((H / 2 - cam.ty * zoom) % 15 + 15) % 15 : 0;
    if (grid.length < cols * rows) grid = new Uint8Array(cols * rows); else grid.fill(0);
    ctx.font = '11.5px ui-sans-serif, system-ui, "Segoe UI", sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    ctx.fillStyle = theme.ink;
    ctx.lineJoin = 'round'; ctx.lineWidth = LOOK.labelEdge; ctx.strokeStyle = theme.labelEdge;
    for (j = 0; j < labN && shown < 150; j++) {
      i = labelIdx[j]; n = nodes[i];
      var screenR = pR[i], must = n === anchor;
      // Always name the biggest hubs so there is something to navigate by;
      // beyond those, only what is big enough and in the nearer part of the
      // cloud, or the names of the far side would pile up behind the near one.
      if (!must && j >= 14 && (screenR < (dimming ? 3.2 : 6.4) || (!dimming && (pD[i] - dNear) * fog > 0.4))) continue;
      var lx = pX[i], ly = pY[i] + screenR + 4;
      var halfW = Math.min(110, n.label.length * 3.3) + 6;
      var row = Math.round((ly - gy) / 15) + 2, c0 = Math.floor((lx - halfW - gx) / 24) + 1, c1 = Math.floor((lx + halfW - gx) / 24) + 1;
      if (row < 1 || row >= rows - 1 || c1 < 0 || c0 >= cols) continue;   // it would be off the canvas anyway
      if (c0 < 0) c0 = 0;
      if (c1 >= cols) c1 = cols - 1;
      var free = true, c, at = row * cols;
      for (c = c0; c <= c1; c++) if (grid[at + c] || grid[at - cols + c] || grid[at + cols + c]) { free = false; break; }
      if (!free && !must) continue;
      for (c = c0; c <= c1; c++) grid[at + c] = 1;
      ctx.globalAlpha = must ? 1 : 0.82 * (1 - 0.45 * (pD[i] - dNear) * fog);
      ctx.strokeText(n.label, lx, ly);
      ctx.fillText(n.label, lx, ly);
      shown++;
    }
    ctx.globalAlpha = 1;
  }

  function loop(now) {
    frame = 0;
    if (dead) return;
    if (fly) { flyStep(now); needsDraw = true; }
    if (playing) {
      var pu = clamp((now - playStart) / 14000, 0, 1);
      setTimeCut(pu, true);
      if (pu >= 1) stopPlay();
      needsDraw = true;
    }
    if (alpha > 0.004 || alphaTarget > 0) { tick(); needsDraw = true; }
    // (Not while the timeline or the kind chips show only part of the bank:
    // the fit would close in on that part and leave the rest outside later.)
    if (refit && alpha < 0.08 && !fly && !drag && !playing && timeCut === Infinity && live_nodes.length === nodes.length) {
      refit = false; fitView(true);
    }
    if (needsDraw) {
      needsDraw = false;
      var began = performance.now();
      draw(now);
      // How long the script took over the frame, smoothed. Where that is a
      // lot (a big bank on a slow machine), what moves by itself is paced at
      // half the rate, so that the view stays free for the hand that uses it.
      drawCost += (performance.now() - began - drawCost) * 0.1;
      motionMs = drawCost > LOOK.slowFrame ? LOOK.frame * 2 : LOOK.frame;
    }
    if (fly || playing || alpha > 0.004 || alphaTarget > 0 || needsDraw) kick();
  }
  function kick() {
    needsDraw = true;
    if (!frame && !dead) frame = win.requestAnimationFrame(loop);
  }

  // ----------------------------------------------------------- interaction

  // The front-most node under the pointer. In the flat view, where all are
  // equally near, it is the one the pointer is deepest inside. The ring of
  // an always-on memory belongs to its planet: a click on the ring is a
  // click on the planet (as it was on the circle that used to mark one).
  function pick(px, py) {
    projectAll();
    var best = null, bestDepth = Infinity, bestD = Infinity;
    for (var i = nodes.length - 1; i >= 0; i--) {
      if (pF[i] !== 2 || pD[i] < NEAR * 2) continue;          // off screen, or so close that it has all but faded out
      var dx = pX[i] - px, dy = pY[i] - py, r = nodes[i].r * pK[i], d = Math.sqrt(dx * dx + dy * dy) - r - 5;
      if (d >= 0 && nodes[i].inCore && r >= LOOK.ringPlain) {
        // Where the pointer is in the ring's own plane, as the ring lies on the screen (see paintRing): on the
        // ring when it is between the ring's two edges, give or take the same few pixels.
        var u = dx * RING_COS + dy * RING_SIN, v = (dy * RING_COS - dx * RING_SIN) / LOOK.ringOpen, out = Math.sqrt(u * u + v * v);
        if (out > LOOK.ringSize[0] * r - 5 && out < LOOK.ringSize[1] * r + 5) d = -0.01;   // (the planet itself comes first)
      }
      if (d >= 0) continue;
      if (pD[i] < bestDepth - 0.01 || (pD[i] < bestDepth + 0.01 && d < bestD)) {
        best = nodes[i]; bestDepth = pD[i]; bestD = d;
      }
    }
    return best;
  }
  function local(ev) {
    var rect = canvas.getBoundingClientRect();
    return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
  }

  function showTip(n, px, py) {
    if (!n) { tip.style.display = 'none'; return; }
    tipTitle.textContent = n.title || '(untitled)';
    var bits = [KIND_ONE[n.kind] + (n.subtype && n.kind !== 'entity' ? ' · ' + n.subtype : '')];
    if (n.kind === 'memory') {
      if (n.inCore) bits.push('always-on'); else if (n.status !== 'active') bits.push(n.status);
      if (n.recalls) bits.push('recalled ' + n.recalls + '×');
    }
    if (n.deg) bits.push(n.deg + (n.deg === 1 ? ' link' : ' links'));
    if (n.created) bits.push(fmtDate(n.created));
    tipMeta.textContent = bits.join(' · ');
    tip.style.display = 'block';
    var tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = clamp(px + 14, 6, W - tw - 6) + 'px';
    tip.style.top = clamp(py + 16, 6, H - th - 6) + 'px';
  }

  function touchAt(id) {
    for (var i = 0; i < touches.length; i++) if (touches[i].id === id) return i;
    return -1;
  }
  function releaseDrag() {
    if (!drag) return;
    if (drag.node) { drag.node.fx = null; drag.node.fy = null; drag.node.fz = null; alphaTarget = 0; }
    drag = null;
    canvas.classList.remove('mb-grabbing');
  }

  // Any activity stops the idle turn, wherever in the view it happens.
  on(wrap, 'pointerdown', noteInput, true);
  on(wrap, 'pointermove', noteInput, true);
  on(wrap, 'keydown', noteInput, true);
  on(wrap, 'wheel', noteInput, { capture: true, passive: true });

  on(canvas, 'pointerdown', function (ev) {
    if (ev.button > 2) return;
    var p = local(ev);
    if (ev.pointerType === 'mouse') touches.length = 0;        // one mouse, however a button-up was missed
    if (touchAt(ev.pointerId) < 0) touches.push({ id: ev.pointerId, x: p.x, y: p.y });
    canvas.setPointerCapture(ev.pointerId);
    fly = null;
    showTip(null);
    if (ev.button === 1) ev.preventDefault();                  // no auto-scroll cursor: the middle button pans
    if (touches.length > 1) { releaseDrag(); return; }         // a second finger: the pair zooms and pans instead
    var hit = ev.button === 0 ? pick(p.x, p.y) : null;
    var node = hit && !ev.shiftKey ? hit : null;
    // A node is moved; empty space turns the view, or slides it (always in
    // the flat view, and with Shift or the right or middle button in 3D).
    drag = {
      id: ev.pointerId, button: ev.button, hit: hit, node: node,
      turn: !node && !flat && ev.button === 0 && !ev.shiftKey,
      depth: node ? pD[node.idx] : 0, x0: p.x, y0: p.y, px: p.x, py: p.y, moved: false
    };
    if (node) { node.fx = node.x; node.fy = node.y; node.fz = node.z; alphaTarget = 0.22; kick(); }
    canvas.classList.add('mb-grabbing');
  });
  on(canvas, 'pointermove', function (ev) {
    var p = local(ev), at = touchAt(ev.pointerId);
    pointer.x = p.x; pointer.y = p.y; pointer.inside = true;
    if (touches.length > 1) {
      if (at < 0 || at > 1) return;
      // Two fingers: spreading them zooms about their middle, moving them together slides the picture.
      var mine = touches[at], other = touches[1 - at];
      var span0 = Math.sqrt(Math.pow(mine.x - other.x, 2) + Math.pow(mine.y - other.y, 2));
      var mx0 = (mine.x + other.x) / 2, my0 = (mine.y + other.y) / 2;
      mine.x = p.x; mine.y = p.y;
      var span1 = Math.sqrt(Math.pow(mine.x - other.x, 2) + Math.pow(mine.y - other.y, 2));
      if (span0 > 8 && span1 > 8) zoomAt(mx0, my0, cam.dist * span0 / span1);
      panBy((mine.x + other.x) / 2 - mx0, (mine.y + other.y) / 2 - my0);
      kick();
      return;
    }
    if (at >= 0) { touches[at].x = p.x; touches[at].y = p.y; }
    if (drag && drag.id === ev.pointerId) {
      var dx = p.x - drag.x0, dy = p.y - drag.y0;
      if (!drag.moved && dx * dx + dy * dy > 16) drag.moved = true;
      if (drag.node) {
        // In the plane that faces the camera, at the depth the node was picked up.
        var w = unproject(p.x, p.y, drag.depth);
        drag.node.fx = w.x; drag.node.fy = w.y; drag.node.fz = flat ? 0 : w.z;
      } else if (drag.moved) {
        if (drag.turn) orbitBy(p.x - drag.px, p.y - drag.py); else panBy(p.x - drag.px, p.y - drag.py);
        drag.px = p.x; drag.py = p.y;
      }
      kick();
      return;
    }
    var n = pick(p.x, p.y);
    if (n !== hover) {
      hover = n;
      canvas.classList.toggle('mb-point', !!n);
      if (!selected) refocus(); else kick();
    }
    showTip(n, p.x, p.y);
  });
  function endDrag(ev) {
    var at = touchAt(ev.pointerId);
    if (at >= 0) touches.splice(at, 1);
    if (!drag || drag.id !== ev.pointerId) return;
    var d = drag;
    releaseDrag();
    if (!d.moved && d.button === 0) select(d.hit || null, false);
    kick();
  }
  on(canvas, 'pointerup', endDrag);
  on(canvas, 'pointercancel', endDrag);
  on(canvas, 'pointerleave', function () {
    pointer.inside = false;
    if (hover && !drag) { hover = null; canvas.classList.remove('mb-point'); if (!selected) refocus(); }
    showTip(null);
  });
  // The right button slides the view, so its menu would only get in the way.
  on(canvas, 'contextmenu', function (ev) { ev.preventDefault(); });
  on(canvas, 'dblclick', function (ev) {
    var p = local(ev), n = pick(p.x, p.y);
    if (n) centerOn(n); else fitView(true);
  });
  on(canvas, 'wheel', function (ev) {
    ev.preventDefault();
    var p = local(ev);
    var delta = ev.deltaMode === 1 ? ev.deltaY * 30 : ev.deltaY;
    zoomAt(p.x, p.y, cam.dist * Math.exp(delta * 0.0016));
    kick();
  }, { passive: false });

  var ARROWS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  on(wrap, 'keydown', function (ev) {
    var typing = ev.target === searchInput;
    var plain = !typing && !ev.ctrlKey && !ev.metaKey && !ev.altKey;     // Ctrl with + or - is the browser's own zoom
    if (ev.key === 'Escape') {
      if (results.classList.contains('mb-on') || searchInput.value) { setSearch(''); searchInput.blur(); }
      else select(null);
      ev.stopPropagation();
    } else if (!typing && ev.key === '/') {
      ev.preventDefault(); searchInput.focus(); searchInput.select();
    } else if (!typing && (ev.key === 'f' || ev.key === 'F')) {
      fitView(true);
    } else if (plain && (ev.key === '+' || ev.key === '=')) {
      zoomAt(W / 2, H / 2, cam.dist / 1.25); kick();
    } else if (plain && (ev.key === '-' || ev.key === '_')) {
      zoomAt(W / 2, H / 2, cam.dist * 1.25); kick();
    } else if (plain && ARROWS[ev.key] && ev.target !== timeRange && !side.contains(ev.target)) {
      // The arrows turn the view; with Shift, and always in the flat view,
      // they slide it. (The timeline slider keeps its own arrow keys, and
      // in the detail panel they scroll what is being read.)
      var step = ARROWS[ev.key];
      ev.preventDefault();
      if (flat || ev.shiftKey) panBy(-step[0] * 60, -step[1] * 60); else orbitBy(step[0] * 20, step[1] * 20);
      kick();
    }
  });

  // ---------------------------------------------------------------- search

  function setSearch(text) {
    searchInput.value = text;
    runSearch();
  }
  function runSearch() {
    var q = searchInput.value.trim().toLowerCase();
    win.clearTimeout(searchTimer);
    if (!q) {
      searchSet = null; resultItems = []; resultCursor = -1;
      results.classList.remove('mb-on'); clear(results);
      kick();
      return;
    }
    var words = q.split(/\s+/);
    var local_hits = nodes.filter(function (n) {
      if (!n.on) return false;
      for (var i = 0; i < words.length; i++) if (n.lower.indexOf(words[i]) < 0) return false;
      return true;
    }).sort(function (a, b) { return b.r - a.r; });
    showResults(local_hits.slice(0, 40));
    var seq = ++searchSeq;
    searchTimer = win.setTimeout(function () {
      if (dead || !fetchJSON) return;
      fetchJSON('/search?limit=30&q=' + encodeURIComponent(searchInput.value.trim())).then(function (res) {
        if (dead || seq !== searchSeq) return;
        var merged = local_hits.slice(0, 40), have = new Set(merged.map(function (n) { return n.id; }));
        (res.results || []).forEach(function (r) {
          var n = byId.get(r.id);
          if (n && n.on && !have.has(n.id)) { have.add(n.id); merged.push(n); }
        });
        showResults(merged);
      }).catch(function () {});
    }, 220);
  }
  function showResults(list) {
    searchSet = new Set(list.map(function (n) { return n.id; }));
    resultItems = list.slice(0, 12); resultCursor = -1;
    clear(results);
    if (!list.length) {
      results.appendChild(el('div', 'mb-row', 'No matches'));
    }
    resultItems.forEach(function (n, index) {
      var row = el('div', 'mb-row');
      var dot = el('div', 'mb-dot'); dot.style.background = theme.colors[n.kind];
      row.appendChild(dot);
      row.appendChild(el('span', null, n.title || '(untitled)'));
      row.appendChild(el('em', null, KIND_ONE[n.kind]));
      bind(row, 'mousedown', function (ev) { ev.preventDefault(); chooseResult(index); });
      results.appendChild(row);
    });
    if (list.length > resultItems.length) {
      results.appendChild(el('div', 'mb-row', '+ ' + (list.length - resultItems.length) + ' more highlighted in the graph'));
    }
    results.classList.add('mb-on');
    kick();
  }
  function chooseResult(index) {
    var n = resultItems[index];
    if (!n) return;
    results.classList.remove('mb-on');
    select(n, true);
  }
  on(searchInput, 'input', runSearch);
  on(searchInput, 'focus', function () { if (searchInput.value.trim()) results.classList.add('mb-on'); });
  on(searchInput, 'blur', function () { results.classList.remove('mb-on'); });
  on(searchInput, 'keydown', function (ev) {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      if (!resultItems.length) return;
      resultCursor = (resultCursor + (ev.key === 'ArrowDown' ? 1 : -1) + resultItems.length) % resultItems.length;
      Array.from(results.children).forEach(function (row, i) {
        row.classList.toggle('mb-cur', i === resultCursor);
      });
    } else if (ev.key === 'Enter') {
      chooseResult(resultCursor >= 0 ? resultCursor : 0);
    }
  });

  // -------------------------------------------------------------- timeline

  function setTimeCut(fraction, fromPlay) {
    fraction = clamp(fraction, 0, 1);
    if (fraction >= 0.999) {
      timeCut = Infinity; timeLabel.textContent = 'today';
    } else {
      timeCut = timeMin + (timeMaxAll - timeMin) * fraction;
      var shown = 0;
      for (var i = 0; i < nodes.length; i++) if (visible(nodes[i])) shown++;
      timeLabel.textContent = fmtDate(timeCut) + ' · ' + num(shown);
    }
    if (fromPlay) timeRange.value = String(Math.round(fraction * 1000));
    refocus();
  }
  function stopPlay() { playing = false; playBtn.textContent = '▶'; }
  on(timeRange, 'input', function () { stopPlay(); setTimeCut(+timeRange.value / 1000, false); });
  on(playBtn, 'click', function () {
    if (playing) { stopPlay(); return; }
    select(null);
    playing = true; playStart = performance.now(); playBtn.textContent = '❚❚';
    nodes.forEach(function (n) { n.seen = false; });
    setTimeCut(0, true);
    kick();
  });

  // ---------------------------------------------------------------- detail

  function select(n, center) {
    n = n && byId.get(n.id);               // a list may still offer an item that a refresh has since dropped
    selected = n || null;
    if (!selected) {
      side.classList.remove('mb-on'); clear(sideBody);
      refocus();
      return;
    }
    refocus();
    side.classList.add('mb-on');
    sideDot.style.background = theme.colors[n.kind];
    sideKind.textContent = KIND_ONE[n.kind] + (n.subtype && n.kind !== 'entity' ? ' · ' + n.subtype : '');
    clear(sideBody);
    sideBody.appendChild(el('h3', null, n.title || '(untitled)'));
    sideBody.appendChild(el('p', 'mb-text', 'Loading…'));
    // Keep the chosen node in sight: the panel must not sit on top of it, and
    // one chosen from a list may be behind the camera.
    projectAll();
    if (center || (visible(n) && !pF[n.idx]) || (W > 520 && pF[n.idx] && pX[n.idx] > W - side.offsetWidth - 30)) centerOn(n);
    if (!fetchJSON) return;
    var seq = ++detailSeq;
    fetchJSON('/node/' + n.id).then(function (d) {
      if (dead || seq !== detailSeq || !selected || selected.id !== n.id) return;
      renderDetail(n, d);
    }).catch(function (err) {
      if (dead || seq !== detailSeq) return;
      clear(sideBody);
      sideBody.appendChild(el('h3', null, n.title));
      sideBody.appendChild(el('p', 'mb-text', 'Could not load the details (' + (err && err.message || 'error') + ').'));
    });
  }

  function fact(dl, name, value) {
    if (value == null || value === '') return;
    dl.appendChild(el('dt', null, name)); dl.appendChild(el('dd', null, String(value)));
  }

  function renderDetail(n, d) {
    clear(sideBody);
    var meta = d.meta || {};
    sideBody.appendChild(el('h3', null, d.kind === 'memory' ? (d.title || '') : (d.title || '(untitled)')));
    if (d.kind === 'memory' || d.kind === 'skill') {
      if (d.content && d.content !== d.title) sideBody.appendChild(el('p', 'mb-text', d.content));
    }
    if (d.kind === 'session') {
      (meta.prompts || []).forEach(function (text, i) {
        if (i === 0) sideBody.appendChild(el('div', 'mb-h', 'You asked'));
        sideBody.appendChild(el('p', 'mb-quote', text));
      });
      if (meta.outcome) {
        sideBody.appendChild(el('div', 'mb-h', 'Where it ended'));
        sideBody.appendChild(el('p', 'mb-quote', meta.outcome));
      }
    }
    var dl = el('dl', 'mb-facts');
    if (d.kind === 'memory') {
      fact(dl, 'Status', d.in_core ? 'always-on (in ' + (meta.core === 'user' ? 'USER.md' : 'MEMORY.md') + ')'
        : d.status === 'evicted' ? 'kept after leaving ' + (meta.core === 'user' ? 'USER.md' : 'MEMORY.md')
        : d.status === 'superseded' ? 'replaced by a newer wording' : d.status);
      fact(dl, 'Recalled', d.recall_count ? d.recall_count + '× · last ' + ago(d.last_recalled) : 'not yet');
      fact(dl, 'Importance', Math.round(d.importance * 100) + '%');
      fact(dl, 'Source', ({ tool: 'saved by Hermes', 'core:memory': 'MEMORY.md', 'core:user': 'USER.md' })[d.source] || d.source);
    }
    if (d.kind === 'skill') {
      fact(dl, 'Category', meta.category);
      fact(dl, 'Used', meta.use_count != null ? meta.use_count + '×' : null);
      fact(dl, 'Last used', meta.last_used_at ? fmtDate(Date.parse(meta.last_used_at) / 1000) : null);
      fact(dl, 'Written by', meta.created_by);
    }
    if (d.kind === 'session') {
      fact(dl, 'Messages', meta.messages);
      fact(dl, 'Model', meta.model);
    }
    if (d.kind === 'entity') fact(dl, 'Type', d.subtype);
    fact(dl, d.kind === 'session' ? 'Started' : 'Added', fmtDate(d.created_at));
    if (d.updated_at - d.created_at > 86400) fact(dl, 'Updated', fmtDate(d.updated_at));
    sideBody.appendChild(dl);

    if (d.kind === 'memory' && !d.in_core) {
      var acts = el('div', 'mb-acts');
      var archived = d.status === 'archived';
      var btn = el('button', 'mb-btn', archived ? 'Restore' : 'Forget');
      btn.type = 'button';
      btn.title = archived ? 'Make this memory recallable again' : 'Stop recalling this memory (it is kept in history)';
      bind(btn, 'click', function () {
        btn.disabled = true;
        fetchJSON('/node/' + d.id + '/' + (archived ? 'restore' : 'forget'), { method: 'POST' })
          .then(function () { if (!archived) { select(null); offerUndo(d.id); } refresh(); })
          .catch(function (err) {
            btn.disabled = false;
            if (err && err.status === 401) return keyExpired();
            var why = err && err.status === 409 ? 'this item cannot be changed here.'
              : err && err.status >= 500 ? 'the bank is busy, try again in a moment.' : (err && err.message) || 'error';
            sideBody.appendChild(el('p', 'mb-text', 'Could not ' + (archived ? 'restore' : 'forget') + ' it: ' + why));
          });
      });
      acts.appendChild(btn);
      sideBody.appendChild(acts);
    }

    var groups = {};
    (d.links || []).forEach(function (l) { (groups[l.kind] = groups[l.kind] || []).push(l); });
    ['memory', 'entity', 'skill', 'session', 'topic'].forEach(function (kind) {
      var list = groups[kind];
      if (!list) return;
      sideBody.appendChild(el('div', 'mb-h', KIND_LABEL[kind] + ' · ' + list.length));
      list.slice(0, 40).forEach(function (l) {
        var row = el('div', 'mb-row');
        var dot = el('div', 'mb-dot'); dot.style.background = theme.colors[l.kind];
        row.appendChild(dot);
        row.appendChild(el('span', null, l.title || '(untitled)'));
        var relText = REL_TEXT[l.rel] || l.rel;
        if (l.rel === 'supersedes' && l.dir === 'in') relText = 'replaced by';
        row.appendChild(el('em', null, relText));
        bind(row, 'click', function () {
          var target = byId.get(l.id);
          if (target) {
            if (!target.on) { kindsOn[target.kind] = true; applyFilter(0.6); renderChips(); }
            select(target, true);
          }
        });
        sideBody.appendChild(row);
      });
      if (list.length > 40) sideBody.appendChild(el('div', 'mb-row', '+ ' + (list.length - 40) + ' more'));
    });
    sideBody.scrollTop = 0;
  }
  on(sideClose, 'click', function () { select(null); });

  // ------------------------------------------------------------- live data

  function setLive(text, hot) {
    liveText.textContent = text;
    if (hot) {
      livePip.classList.remove('mb-hot');
      void livePip.offsetWidth;
      livePip.classList.add('mb-hot');
    }
  }

  function describe(ev) {
    var count = (ev.nodes || []).length;
    switch (ev.type) {
      case 'recall': return 'Recalled ' + count + (count === 1 ? ' item' : ' items');
      case 'remember': return 'New memory saved';
      case 'update': return 'Memory updated';
      case 'forget': return 'Memory retired';
      case 'restore': return 'Memory restored';
      case 'core': return 'MEMORY.md / USER.md mirrored';
      case 'skills': return 'Skills re-indexed';
      case 'sessions': return 'Sessions indexed';
      case 'session': return 'New session indexed';
      default: return 'Updated';
    }
  }

  function applyEvents(list, quiet) {
    var changed = false;
    list.forEach(function (ev) {
      if (ev.id <= lastEvent) return;
      lastEvent = ev.id;
      lastEventTs = ev.ts; lastEventText = describe(ev);
      if (ev.type === 'recall') {
        if (!quiet) (ev.nodes || []).forEach(function (id, i) {
          var n = byId.get(id);
          if (n) { n.recalls += 1; pulses.push({ node: n, t0: performance.now() + i * 140 }); }
        });
      } else {
        changed = true;
      }
    });
    if (lastEventTs) setLive(lastEventText + ' · ' + ago(lastEventTs), !quiet && list.length > 0);
    else setLive('Live · waiting for activity', false);
    if (pulses.length) kick();
    if (changed && !quiet) refreshSoon();
  }

  function keyExpired() {
    stale = true;
    showMessage('key');
    setLive('Key expired', false);
  }

  function poll() {
    if (dead || stale || !fetchJSON || doc.hidden) return;
    fetchJSON('/events?after=' + lastEvent).then(function (res) {
      if (dead) return;
      applyEvents(res.events || [], false);
    }).catch(function (err) {
      if (dead) return;
      if (err && err.status === 401) return keyExpired();
      setLive('Offline · retrying', false);
    });
  }

  function refresh() {
    if (dead || stale || !fetchJSON) return Promise.resolve();
    return fetchJSON('/graph?limit=' + (opts.limit || 4000)).then(function (payload) {
      if (dead) return;
      var first = !nodes.length;
      ingest(payload);
      if (first) {
        // Settle most of the layout before the first paint.
        alpha = 1;
        var t0 = performance.now();
        for (var i = 0; i < 260 && performance.now() - t0 < 380; i++) tick();
        firstFit = true; resize(); fitView(false); firstFit = false;
        refit = alpha > 0.08;              // a big bank is still spreading: fit again when it has its size
        return fetchJSON('/events').then(function (res) {
          if (dead) return;
          var list = res.events || [];
          lastEvent = 0;
          applyEvents(list, true);
          lastEvent = Math.max(lastEvent, res.last || 0, payload.last_event || 0);
        }).catch(function () { setLive('Live', false); });
      }
    }).catch(function (err) {
      if (dead) return;
      if (err && err.status === 401) return keyExpired();
      if (!nodes.length) showMessage('error', err && err.message);
      setLive('Offline · retrying', false);
    });
  }

  function refreshSoon() {
    win.clearTimeout(refreshTimer);
    refreshTimer = win.setTimeout(refresh, 700);
  }

  var undoId = 0, undoTimer = 0;
  function hideUndo() {
    win.clearTimeout(undoTimer);
    undo.classList.remove('mb-on');
    undoId = 0;
  }
  function offerUndo(id) {
    hideUndo();
    undoId = id;
    undo.classList.add('mb-on');
    undoTimer = win.setTimeout(hideUndo, 20000);
  }
  bind(undoBtn, 'click', function () {
    var id = undoId;
    if (!id || !fetchJSON) return;
    hideUndo();
    fetchJSON('/node/' + id + '/restore', { method: 'POST' }).then(refresh).catch(function (err) {
      if (err && err.status === 401) return keyExpired();
      offerUndo(id);
    });
  });

  // ------------------------------------------------------------- lifecycle

  var observer = null;
  if (win.ResizeObserver) {
    observer = new win.ResizeObserver(resize);
    observer.observe(wrap);
    cleanups.push(function () { observer.disconnect(); });
  } else {
    on(win, 'resize', resize);
  }
  on(doc, 'visibilitychange', function () {
    if (!doc.hidden) { poll(); kick(); }
    motionSoon();                          // nothing moves by itself in a hidden tab; in a shown one it may again (the turn after the usual wait)
  });
  var scheme = win.matchMedia ? win.matchMedia('(prefers-color-scheme: dark)') : null;
  function rethemed() { readTheme(); theme.mark = themeMark(); kick(); }
  if (scheme && scheme.addEventListener) {
    scheme.addEventListener('change', rethemed);
    cleanups.push(function () { scheme.removeEventListener('change', rethemed); });
  }
  // People who asked their system for less motion get a view that only moves when they move it.
  var calm = win.matchMedia ? win.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function recalmed() { reduced = !!calm.matches; motionSoon(); kick(); }
  reduced = !!(calm && calm.matches);
  if (calm && calm.addEventListener) {
    calm.addEventListener('change', recalmed);
    cleanups.push(function () { calm.removeEventListener('change', recalmed); });
  }
  timers.push(win.setInterval(poll, 5000));
  timers.push(win.setInterval(function () {      // host theme switches restyle the page, not the media query
    if (themeMark() !== theme.mark) rethemed();
    if (lastEventTs) liveText.textContent = lastEventText + ' · ' + ago(lastEventTs);
  }, 2500));

  readTheme();
  theme.mark = themeMark();
  resize();
  refresh();
  motionSoon();

  function destroy() {
    if (dead) return;
    dead = true;
    if (frame) win.cancelAnimationFrame(frame);
    win.clearTimeout(refreshTimer); win.clearTimeout(searchTimer); win.clearTimeout(undoTimer);
    win.clearTimeout(motionTimer);
    timers.forEach(function (t) { win.clearInterval(t); });
    cleanups.forEach(function (fn) { try { fn(); } catch (e) { /* already gone */ } });
    cleanups = [];
  }

  // `sync` asks for whatever happened since the last look (the host calls it
  // when Hermes pushes a change notice, so updates do not wait for the poll).
  return { destroy: destroy, refresh: refresh, refreshSoon: refreshSoon, sync: poll };
}
