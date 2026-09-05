import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

function findBrowser() {
  const paths = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser'
  ];
  for (const p of paths) {
    if (fs.existsSync(p)) return p;
  }
  return process.platform === 'win32' ? 'chrome.exe' : 'google-chrome';
}

function cleanFilename(rawName, extension) {
  const ext = extension || 'mp4';
  if (!rawName) return `DiskWala_Video.${ext}`;
  let cleaned = rawName.replace(/[*_]+/g, ' ').replace(/\s+/g, ' ').trim();
  cleaned = cleaned.replace(/\.(mp|m|mp4)$/i, '').trim();
  if (!cleaned || cleaned.length < 2) {
    return `DiskWala_Video.${ext}`;
  }
  return `${cleaned}.${ext}`;
}

export async function resolveDiskwalaLink(linkId) {
  const browserPath = findBrowser();
  const port = 9333 + Math.floor(Math.random() * 500);
  const targetUrl = `https://www.diskwala.com/app/${linkId}`;
  const tempProfile = path.join(process.env.TEMP || '/tmp', `cdp_dw_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`);

  let browser = null;
  try {
    browser = spawn(browserPath, [
      '--headless=new',
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${tempProfile}`,
      '--no-sandbox',
      '--disable-gpu',
      '--disable-extensions',
      '--mute-audio',
      targetUrl
    ], { stdio: 'ignore' });
  } catch (err) {
    return {
      success: true,
      surl: linkId,
      full_surl: linkId,
      title: `DiskWala Video (${linkId})`,
      size: "HD Stream",
      size_bytes: 0,
      duration_str: "HD Video",
      thumbnail: null,
      stream_url: null,
      download_url: `https://www.diskwala.com/app/${linkId}`,
      is_hls: false,
      mode: "diskwala",
      playlist: [{
        index: 0,
        title: `DiskWala Video (${linkId})`,
        size: "HD Video",
        thumbnail: null,
        stream_url: null,
        download_url: `https://www.diskwala.com/app/${linkId}`
      }]
    };
  }

  let wsUrl = null;

  for (let i = 0; i < 25; i++) {
    await new Promise(r => setTimeout(r, 200));
    try {
      const data = await new Promise((resolve, reject) => {
        const req = http.get(`http://127.0.0.1:${port}/json`, res => {
          let buf = '';
          res.on('data', d => buf += d);
          res.on('end', () => resolve(buf));
        });
        req.on('error', reject);
      });

      const targets = JSON.parse(data);
      const pageTarget = targets.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
      if (pageTarget) {
        wsUrl = pageTarget.webSocketDebuggerUrl;
        break;
      }
    } catch (e) {}
  }

  if (!wsUrl) {
    if (browser) browser.kill();
    try { fs.rmSync(tempProfile, { recursive: true, force: true }); } catch (e) {}
    throw new Error('Failed to connect to browser CDP');
  }

  return new Promise((resolve) => {
    const ws = new WebSocket(wsUrl);
    let id = 1;
    const pendingReqs = new Map();
    let fileInfo = null;
    let uploader = null;

    const timeoutTimer = setTimeout(() => {
      cleanup();
      buildResponse();
    }, 10000);

    function cleanup() {
      clearTimeout(timeoutTimer);
      try { ws.close(); } catch (e) {}
      if (browser) try { browser.kill(); } catch (e) {}
      setTimeout(() => {
        try { fs.rmSync(tempProfile, { recursive: true, force: true }); } catch (e) {}
      }, 500);
    }

    function buildResponse() {
      const name = fileInfo?.name ? cleanFilename(fileInfo.name) : `DiskWala File (${linkId})`;
      const rawBytes = fileInfo?.size || 0;
      const sizeStr = rawBytes > 0 ? `${(rawBytes / (1024 * 1024)).toFixed(2)} MB` : "HD Video";
      const uploaderName = uploader?.name || "DiskWala Creator";
      const displayTitle = fileInfo?.name ? `${name}` : `DiskWala Video (${linkId})`;

      const result = {
        success: true,
        surl: linkId,
        full_surl: linkId,
        title: displayTitle,
        uploader: uploaderName,
        size: sizeStr,
        size_bytes: rawBytes,
        duration_str: "HD Stream",
        thumbnail: null,
        stream_url: null,
        proxy_stream_url: null,
        download_url: `https://www.diskwala.com/app/${linkId}`,
        is_hls: false,
        mode: "diskwala",
        playlist: [{
          index: 0,
          title: displayTitle,
          size: sizeStr,
          thumbnail: null,
          stream_url: null,
          download_url: `https://www.diskwala.com/app/${linkId}`
        }]
      };

      resolve(result);
    }

    function send(method, params = {}) {
      const msgId = id++;
      return new Promise((res, rej) => {
        pendingReqs.set(msgId, { res, rej });
        ws.send(JSON.stringify({ id: msgId, method, params }));
      });
    }

    ws.onopen = async () => {
      await send('Network.enable');
      await send('Page.enable');
      await send('Runtime.enable');

      // Hook into Webpack in page to instantly extract temp_info
      await new Promise(r => setTimeout(r, 2000));
      try {
        const evalRes = await send('Runtime.evaluate', {
          expression: `(async () => {
            try {
              let api = null;
              if (globalThis.webpackChunkDiskWala) {
                globalThis.webpackChunkDiskWala.push([[888888], {}, (req) => {
                  api = req(46904);
                }]);
              }
              if (api && api.JR) {
                return await new Promise(r => api.JR({ id: "${linkId}" }, res => r(res)));
              }
            } catch(e) {}
            return null;
          })()`,
          awaitPromise: true,
          returnByValue: true
        });

        if (evalRes?.result?.value?.data?.fileInfo) {
          fileInfo = evalRes.result.value.data.fileInfo;
          uploader = evalRes.result.value.data.uploader;
          cleanup();
          buildResponse();
          return;
        }
      } catch (e) {}
    };

    ws.onmessage = async (evt) => {
      const msg = JSON.parse(evt.data);

      if (msg.id && pendingReqs.has(msg.id)) {
        const { res } = pendingReqs.get(msg.id);
        pendingReqs.delete(msg.id);
        res(msg.result);
        return;
      }

      if (msg.method === 'Network.responseReceived') {
        const { requestId, response } = msg.params;
        if (response.url.includes('/file/temp_info')) {
          try {
            const bodyRes = await send('Network.getResponseBody', { requestId });
            if (bodyRes && bodyRes.body) {
              const data = JSON.parse(bodyRes.body);
              if (data?.fileInfo || data?.data?.fileInfo) {
                fileInfo = data.fileInfo || data.data.fileInfo;
                uploader = data.uploader || data.data?.uploader;
                cleanup();
                buildResponse();
              }
            }
          } catch (e) {}
        }
      }
    };

    ws.onerror = () => {
      cleanup();
      buildResponse();
    };
  });
}

if (process.argv[1] && process.argv[1].endsWith('diskwala_engine.js')) {
  const argId = process.argv[2] || "69176413f37dbe35e7b299a5";
  resolveDiskwalaLink(argId).then(res => {
    console.log(JSON.stringify(res));
    process.exit(0);
  }).catch(err => {
    console.log(JSON.stringify({ success: false, error: err.message }));
    process.exit(1);
  });
}
