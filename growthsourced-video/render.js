// Renders reel.html frame by frame.
//   node render.js stills out_dir 1.0 4.2 ...   -> PNG per time (for checking)
//   node render.js video out.mp4 [fps]          -> H.264 MP4 for Instagram Reels
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require('/opt/node22/lib/node_modules/playwright');

// Separate Reels: one topic each. Scene indices follow the order of the .scene sections in reel.html.
// "end" appends the call-to-action card (reel time 59.0-65.0) over the scene's last frame.
const CLIPS = {
  'ScaleLink-1-herkenbaar': { solo: { scenes: [0, 1], panelA: true, holdA: true }, from: 0, to: 14.6 },
  'ScaleLink-2-van-volger-tot-klant': { solo: { scenes: [2], panelB: true }, from: 14.35, to: 21.1, end: true },
  'ScaleLink-3-targeting': { solo: { scenes: [3], panelB: true }, from: 21.7, to: 28.9, end: true },
  'ScaleLink-4-werkwijze': { solo: { scenes: [4], panelB: true }, from: 29.5, to: 39.9, end: true },
  'ScaleLink-5-geschikt-voor': { solo: { scenes: [5], panelB: true }, from: 40.5, to: 45.5, end: true },
  'ScaleLink-6-resultaten': { solo: { scenes: [6], panelB: true }, from: 46.1, to: 52.9, end: true },
  'ScaleLink-7-kosten': { solo: { scenes: [7], panelB: true }, from: 53.5, to: 59.0, end: true },
};

function clipFrames(c, fps) {
  const out = [], n1 = Math.round((c.to - c.from) * fps);
  for (let k = 0; k < n1; k++) out.push([c.from + k / fps, { solo: c.solo, bgT: k / fps }]);
  if (c.end) {
    const n2 = Math.round(6.0 * fps);
    for (let j = 0; j < n2; j++) out.push([59.0 + j / fps, { solo: c.solo, sceneT: c.to, bgT: (n1 + j) / fps }]);
  }
  return out;
}

// Slow-motion windows: [srcStart, srcEnd, rate] plays that stretch of the animation at `rate` speed.
function timeMap(windows, end) {
  const segs = []; let s = 0, o = 0;
  for (const [a, b, r] of windows.slice().sort((x, y) => x[0] - y[0])) {
    if (a > s) { segs.push({ o0: o, s0: s, s1: a, r: 1 }); o += a - s; }
    segs.push({ o0: o, s0: a, s1: b, r }); o += (b - a) / r; s = b;
  }
  if (end > s) { segs.push({ o0: o, s0: s, s1: end, r: 1 }); o += end - s; }
  return {
    dur: o,
    src(t) { for (const g of segs) if (t < g.o0 + (g.s1 - g.s0) / g.r) return g.s0 + (t - g.o0) * g.r; return end; },
  };
}

(async () => {
  const [mode, out, ...rest] = process.argv.slice(2);
  const browser = await chromium.launch();
  // HQ=1: every frame is rendered at 2x and scaled down (smoother text and edges), then encoded at a higher quality.
  const HQ = !!process.env.HQ;
  const page = await browser.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: HQ ? 2 : 1 });
  await page.goto('file://' + path.join(__dirname, process.env.PAGE || 'reel.html'));
  if (process.env.STORY) {
    // STORY=offset: a 4:5 ad laid out on a full 1080x1920 Stories canvas, its content moved down by `offset` px.
    // The CSS `translate` property composes with the transforms render() sets, so the animation is untouched.
    const off = +process.env.STORY;
    await page.addStyleTag({ content: `#stage{ height:1920px !important; }
      #stage > *:not(.blob):not(#cta){ translate:0 ${off}px; } #cta > *:not(.grid){ translate:0 ${off}px; }` });
  }
  // HIDE_SCENE=1: only the background and the call to action, so the CTA's circle opens over a clean canvas.
  if (process.env.HIDE_SCENE) await page.addStyleTag({ content: '#stage > *:not(.blob):not(#cta){ visibility:hidden !important; }' });
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => document.fonts.load('800 40px PJS'));
  const stage = await page.$('#stage');

  if (mode === 'count') {
    const warp = await page.evaluate(() => ({ w: window.WARP || [], end: window.SRC_END || window.DURATION }));
    console.log(Math.round(timeMap(warp.w, warp.end).dur * 30));
  } else if (mode === 'stills') {
    fs.mkdirSync(out, { recursive: true });
    for (const t of rest) {
      await page.evaluate((x) => window.render(x), +t);
      await stage.screenshot({ path: path.join(out, `t${(+t).toFixed(2).padStart(6, '0')}.png`) });
    }
  } else if (mode === 'clip') {
    // clip NAME out.mp4: one separate Reel from CLIPS
    const fps = 30, frames = clipFrames(CLIPS[out], fps);
    const ff = spawn('ffmpeg', [
      '-y', '-loglevel', 'error',
      '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'png', '-i', '-',
      '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000', '-map', '0:v', '-map', '1:a', '-shortest',
      '-c:v', 'libx264', '-profile:v', 'high', '-level', '4.2', '-preset', 'slow', '-crf', '17',
      '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p', '-r', String(fps), '-g', String(fps * 2),
      '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
      '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', rest[0],
    ], { stdio: ['pipe', 'inherit', 'inherit'] });
    for (const [t, o] of frames) {
      await page.evaluate(([x, opts]) => window.render(x, opts), [t, o]);
      const buf = await stage.screenshot({ type: 'png' });
      if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once('drain', r));
    }
    ff.stdin.end();
    await new Promise((r) => ff.on('close', r));
  } else if (mode === 'clipstills') {
    // clipstills NAME out_dir sec sec ...: PNGs at moments within one clip (seconds from the clip's start)
    fs.mkdirSync(rest[0], { recursive: true });
    const frames = clipFrames(CLIPS[out], 30);
    for (const sec of rest.slice(1)) {
      const [t, o] = frames[Math.min(frames.length - 1, Math.round(+sec * 30))];
      await page.evaluate(([x, opts]) => window.render(x, opts), [t, o]);
      await stage.screenshot({ path: path.join(rest[0], `${out}-${sec}.png`) });
    }
  } else {
    // video out.mp4 [fps] [firstFrame lastFrame]: with a frame range, writes a video-only segment
    const fps = +(rest[0] || 30);
    const warp = await page.evaluate(() => ({ w: window.WARP || [], end: window.SRC_END || window.DURATION }));
    const map = timeMap(warp.w, warp.end);
    const segment = rest.length >= 3;
    const first = segment ? +rest[1] : 0;
    const frames = segment ? +rest[2] : Math.round(map.dur * fps);
    const audio = segment ? [] : ['-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000',
      '-map', '0:v', '-map', '1:a', '-shortest'];
    const audioCodec = segment ? ['-an'] : ['-c:a', 'aac', '-b:a', '128k'];
    const ff = spawn('ffmpeg', [
      '-y', '-loglevel', 'error',
      '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'png', '-i', '-',
      ...audio,
      '-c:v', 'libx264', '-profile:v', 'high', '-level', '4.2', '-preset', 'slow', '-crf', HQ ? '12' : '17',
      ...(HQ ? ['-tune', 'animation', '-x264-params', 'aq-mode=3'] : []),
      '-vf', HQ ? 'scale=w=iw/2:h=ih/2:flags=lanczos+accurate_rnd+full_chroma_int:out_color_matrix=bt709:out_range=tv:sws_dither=ed,format=yuv420p'
                : 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p', '-r', String(fps), '-g', String(fps * 2),
      '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
      ...audioCodec, '-movflags', '+faststart', out,
    ], { stdio: ['pipe', 'inherit', 'inherit'] });
    const started = Date.now();
    for (let i = first; i < frames; i++) {
      await page.evaluate((x) => window.render(x), map.src(i / fps));
      const buf = await stage.screenshot({ type: 'png' });
      if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once('drain', r));
      if (i % 150 === 0) console.log(`frame ${i}/${frames}  ${((Date.now() - started) / 1000).toFixed(0)}s`);
    }
    ff.stdin.end();
    await new Promise((r) => ff.on('close', r));
  }
  await browser.close();
})();
